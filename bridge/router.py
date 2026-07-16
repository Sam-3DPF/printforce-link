"""The bridge's job queue.

A sliced file arrives from OrcaSlicer at the print-host endpoint (U7), is stored
on disk, and is enqueued here as a Job. In U7 the queue only *accumulates* jobs;
U9 adds the matching + dispatch that drains it onto idle, color-satisfying
printers.

The queue is PERSISTED to disk (a small JSON file) after every mutation, and
reloaded on startup, so a file uploaded seconds before the bridge restarts is not
lost — it is exactly the window U9's dispatch would otherwise drop. The stored
sliced file itself lives on disk under `stored_path`; the queue only holds the
pointer + routing key.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .ams import normalize_hex

logger = logging.getLogger(__name__)

# A job we could derive a batch key for and can hand to routing (U9).
QUEUED = "queued"
# A job we stored but could not derive any batch correlation for. Held and
# surfaced for manual triage — never silently dropped (U7 requirement).
UNRESOLVED = "unresolved"
# Physically started on a printer, but 3DPF has NOT yet acked the dispatch report.
# The print is running, so the job must never be re-matched to a printer — but the
# report is still OWED, so the job stays in the durable queue and each drain pass
# re-POSTs `dispatched` (idempotent cloud-side) until it acks, then removes it. Deleting
# the job on physical start instead would strand the batch NEW-in-cloud forever if that
# one POST failed (no filament deducted, order stuck, completion later rejected).
DISPATCHED = "dispatched"


@dataclass
class Job:
    id: str
    stored_path: str          # server-generated path to the sliced .3mf on disk
    correlation_key: Optional[str]  # batch_name / correlation id, or None if unresolved
    print_flag: bool          # OrcaSlicer asked to print immediately (?print=true)
    status: str               # QUEUED | UNRESOLVED
    enqueued_at: float        # epoch seconds, for stable ordering + surfacing
    # Cloud resolution (U8), cached on the job the first time it resolves so a job that
    # waits in the queue for a matching printer doesn't re-hit the resolve endpoint every
    # drain pass. Both default None (a fresh or pre-U9 queued job), and — being trailing
    # defaults — an older queue.json without these keys still loads via Job(**item).
    batch_id: Optional[str] = None
    required_colors: Optional[List[str]] = None
    # The printer a DISPATCHED job was started on, so its owed report can be re-sent from
    # the persisted job alone (survives a restart). None until dispatched.
    dispatched_to: Optional[str] = None

    @staticmethod
    def new(stored_path: str, correlation_key: Optional[str], print_flag: bool,
            status: str, now: Optional[float] = None) -> "Job":
        return Job(
            id=uuid.uuid4().hex,
            stored_path=stored_path,
            correlation_key=correlation_key,
            print_flag=bool(print_flag),
            status=status,
            enqueued_at=now if now is not None else time.time(),
        )


class Router:
    """Durable FIFO of undispatched jobs, persisted to `queue_path`."""

    def __init__(self, queue_path: str):
        self.queue_path = queue_path
        # Printer->batch assignments live in a sibling file (U11): once a dispatched job is
        # acked and removed from the queue, the bridge still needs to know which batch is
        # printing on which printer to report its completion. Kept separate so the shipped
        # queue.json format is untouched.
        self.assignments_path = queue_path + ".assignments"
        # The print-host serves on threads, so enqueue() runs concurrently with
        # itself and (in U9) with the main-thread drain. Guard the in-memory list
        # AND the file write together: without it two enqueues can interleave
        # their whole-queue _persist() writes and lose a job (the atomic rename
        # prevents torn files, not racing writers).
        self._lock = threading.Lock()
        self.jobs: List[Job] = self._load()
        # {bambu_id: {"batch_id", "plate_number", "terminal": None|"complete"|"failed"}}
        self.assignments: dict = self._load_assignments()

    def enqueue(self, job: Job) -> Job:
        with self._lock:
            self.jobs.append(job)
            self._persist()
        logger.info("enqueued job %s status=%s key=%s", job.id, job.status,
                    job.correlation_key)
        return job

    def pending(self) -> List[Job]:
        """Jobs still awaiting dispatch. U9 removes a job on successful dispatch, so
        `pending()` is exactly the undispatched set (QUEUED + held UNRESOLVED)."""
        with self._lock:
            return list(self.jobs)

    def mark_resolved(self, job_id: str, batch_id: str, required_colors: List[str]) -> None:
        """Cache a job's cloud resolution (U8) so later drain passes skip the resolve
        call. Persisted so the resolution also survives a restart."""
        with self._lock:
            for job in self.jobs:
                if job.id == job_id:
                    job.batch_id = batch_id
                    job.required_colors = list(required_colors)
                    self._persist()
                    return

    def mark_dispatched(self, job_id: str, bambu_id: str) -> None:
        """Move a job to DISPATCHED (physically started on `bambu_id`) and persist. The
        job stays in the queue — never re-matched to a printer — until its `dispatched`
        report acks and `remove` drops it. The in-memory status flips even if the persist
        raises (disk full), so within this process the job is never re-dispatched; only a
        crash before the persist reopens that window."""
        with self._lock:
            for job in self.jobs:
                if job.id == job_id:
                    job.status = DISPATCHED
                    job.dispatched_to = bambu_id
                    self._persist()
                    return

    def remove(self, job_id: str) -> None:
        """Drop a job from the queue (dispatched AND reported — 3DPF owns it now) and
        persist, so a restart doesn't re-report a print that is already accounted for."""
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.id != job_id]
            if len(self.jobs) != before:
                self._persist()

    # --- printer->batch assignments (U11 completion tracking) ----------------

    def record_assignment(self, bambu_id: str, batch_id: str,
                          plate_number: Optional[int] = None) -> None:
        """Remember that `batch_id` is now printing on `bambu_id`, so its completion can be
        reported after the job has left the queue. Recorded at physical start; persisted so
        a restart mid-print can still report the finish."""
        with self._lock:
            self.assignments[bambu_id] = {
                "batch_id": batch_id, "plate_number": plate_number, "terminal": None,
            }
            self._persist_assignments()

    def set_assignment_terminal(self, bambu_id: str, kind: str) -> None:
        """Latch a printer's assignment as finished (`complete`) or failed (`failed`). Once
        latched the completion pass reports it — retrying until acked — and the latch keeps
        it from being re-detected. No-op if there's no assignment or it's already latched."""
        with self._lock:
            a = self.assignments.get(bambu_id)
            if a is not None and a.get("terminal") is None:
                a["terminal"] = kind
                self._persist_assignments()

    def clear_assignment(self, bambu_id: str) -> None:
        """Drop a printer's assignment once its completion/failure has been acked."""
        with self._lock:
            if bambu_id in self.assignments:
                del self.assignments[bambu_id]
                self._persist_assignments()

    def assignments_snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.assignments.items()}

    # --- persistence ---------------------------------------------------------

    def _load(self) -> List[Job]:
        raw = self._read_json(self.queue_path, "job queue")
        jobs = []
        for item in raw if isinstance(raw, list) else []:
            try:
                jobs.append(Job(**item))
            except TypeError:
                logger.warning("skipping malformed queued job: %r", item)
        return jobs

    def _load_assignments(self) -> dict:
        raw = self._read_json(self.assignments_path, "printer assignments")
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _read_json(path: str, label: str):
        """Read a JSON file, tolerating absence and corruption — the bridge is unsupervised,
        so a corrupt state file must log loudly and start empty rather than wedge startup."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("could not read %s at %s (%s); starting empty", label, path, e)
            return None

    def _persist(self) -> None:
        self._atomic_write(self.queue_path, [asdict(j) for j in self.jobs])

    def _persist_assignments(self) -> None:
        self._atomic_write(self.assignments_path, self.assignments)

    @staticmethod
    def _atomic_write(path: str, data) -> None:
        """Write `data` as JSON atomically (temp file + fsync + rename) so a crash mid-write
        can't leave a truncated file that loses the whole state on next boot."""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())  # the restart-safe claim needs the bytes on disk,
                                       # not just in the OS page cache, before the rename
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


class Dispatcher:
    """Drains the Router's queue onto idle, color-satisfying printers (U9).

    One `drain(snapshots)` pass per bridge loop:
      * resolve each QUEUED job's required-color set from the cloud (U8), cached on the
        job so a job that waits doesn't re-resolve every pass;
      * match it to an IDLE printer whose live AMS holds every required color, through the
        SAME canonical hex normalizer the cloud routes/reports with (`normalize_hex`);
      * FTPS-upload + MQTT-start it with an explicit AMS mapping computed from that
        printer's live slots (R11), then tell 3DPF it's PRINTING (U10).

    Matching is on the FRESH snapshot passed in each pass — that IS the KTD3
    dispatch-time re-validation: a printer whose color changed since the file was
    uploaded simply isn't a match now, and a NEEDS_CLEARING (finished-but-uncleared)
    printer is not IDLE so it is skipped until U13's mark-clear flips it. One job per
    printer per pass; a job with no idle match stays queued and retries when a printer
    next reports IDLE.
    """

    def __init__(self, router: Router, fleet, dpf):
        self._router = router
        self._fleet = fleet
        self._dpf = dpf
        # Job ids we've already logged as "waiting for a color", so a job that waits hours
        # for a filament swap logs once, not an identical line every ~15s pass.
        self._waiting_logged: set = set()
        # Same one-shot throttle for a job whose batch won't resolve — surfaced once as an
        # anomaly (3DPF builds the batch before the operator ever uploads, so a resolve
        # miss is a real problem, not a normal race) instead of silently polling forever.
        self._resolve_failed_logged: set = set()

    def drain(self, snapshots: List[Dict], desired: Optional[List[Dict]] = None) -> None:
        # First, flush any owed dispatch reports (jobs already printing whose `dispatched`
        # POST hasn't acked yet) — this needs no idle printer, so it runs every pass.
        for job in self._router.pending():
            if job.status == DISPATCHED:
                try:
                    self._send_report(job.id, job.batch_id, job.dispatched_to)
                except Exception:
                    logger.exception("re-report of dispatched job %s failed; will retry", job.id)

        # Then detect finished/failed prints and report them (U11) — also independent of
        # idle printers, and durable: a latched completion is retried until 3DPF acks.
        self._report_completions(snapshots)

        # Printers the operator has marked cleared (U13): 3DPF returns desired_status IDLE
        # for a finished printer whose plate was cleared. Its gcode_state is still FINISH
        # (so its snapshot reads NEEDS_CLEARING), but the operator's clear frees it to take
        # the next job — so treat it as dispatchable, using the colors its snapshot reports.
        cleared = {
            d.get("bambu_id")
            for d in (desired or [])
            if isinstance(d, dict) and d.get("desired_status") == "IDLE" and d.get("bambu_id")
        }
        # A printer that still OWES a completion report (its terminal is latched but 3DPF
        # hasn't acked) must not take a new job: dispatching would overwrite its single
        # assignment slot and lose the owed completion, stranding that batch. Hold it back
        # until _report_completions above acks and clears it (next pass, or this one).
        owed = {
            bid for bid, a in self._router.assignments_snapshot().items()
            if a.get("terminal") is not None
        }
        idle = {
            s.get("bambu_id"): s
            for s in snapshots
            if isinstance(s, dict) and s.get("bambu_id")
            and s.get("bambu_id") not in owed
            and (s.get("status") == "IDLE" or s.get("bambu_id") in cleared)
        }
        if not idle:
            return  # nothing to dispatch onto; leave the queue untouched
        # Normalize each idle printer's color set ONCE per pass — not once per queued job
        # inside _match, which repeats the same work N times over the same M printers.
        idle_colors = {bid: self._color_set(snap) for bid, snap in idle.items()}
        claimed: set = set()  # a printer takes at most one job per pass
        for job in self._router.pending():
            if job.status != QUEUED:
                continue  # UNRESOLVED held for triage; DISPATCHED handled above
            try:
                self._try_dispatch(job, idle, idle_colors, claimed)
            except Exception:
                # One bad job must not stop the rest of the queue draining, nor kill the
                # bridge loop. Log with a traceback and leave the job queued.
                logger.exception("dispatch of job %s failed; leaving it queued", job.id)

    def _try_dispatch(self, job: Job, idle: Dict[str, Dict],
                      idle_colors: Dict[str, set], claimed: set) -> None:
        resolved = self._resolve(job)
        if resolved is None:
            return  # not resolvable yet — stay queued, retry next pass
        batch_id, required = resolved
        bambu_id = self._match(required, idle_colors, claimed)
        if bambu_id is None:
            if job.id not in self._waiting_logged:
                self._waiting_logged.add(job.id)  # surface once, not every pass
                logger.info("job %s waiting for an idle printer with colors %s",
                            job.id, required)
            return
        snap = idle[bambu_id]
        ams_mapping = self._ams_mapping(required, snap)

        # The file is about to physically print. Past a successful start, success means
        # REMOVE from the queue (so a restart can't re-dispatch a running print) —
        # regardless of whether the cloud report lands.
        started = self._fleet.dispatch(bambu_id, job.stored_path, ams_mapping)
        if not started:
            logger.warning("printer %s did not accept job %s; leaving it queued", bambu_id, job.id)
            return
        claimed.add(bambu_id)
        self._waiting_logged.discard(job.id)
        # Durably record "printing on bambu_id, report owed" BEFORE reporting: the physical
        # print has started, so the job must never be re-matched to a printer again. Then
        # try to report — if that fails, the job stays DISPATCHED and a later pass retries.
        self._router.mark_dispatched(job.id, bambu_id)
        # Also record the printer->batch assignment so the finish can be reported after the
        # job leaves the queue (U11). Recorded at physical start, so a lost dispatch report
        # or a restart mid-print doesn't lose track of what's on the machine.
        self._router.record_assignment(bambu_id, batch_id)
        logger.info("dispatched job %s -> printer %s (batch %s)", job.id, bambu_id, batch_id)
        self._send_report(job.id, batch_id, bambu_id)

    def _report_completions(self, snapshots: List[Dict]) -> None:
        """Edge-detect each assigned printer finishing/failing from THIS pass's fresh
        snapshot, then report it (retrying until acked). A finished print maps to
        NEEDS_CLEARING and a failed one to ERROR (printer.py's status map); anything else
        (still PRINTING, OFFLINE) is not yet terminal and waits."""
        snap_by_id = {
            s.get("bambu_id"): s
            for s in snapshots
            if isinstance(s, dict) and s.get("bambu_id")
        }
        for bambu_id, assignment in self._router.assignments_snapshot().items():
            try:
                self._detect_and_report_completion(bambu_id, assignment, snap_by_id.get(bambu_id))
            except Exception:
                logger.exception("completion handling for printer %s failed; will retry", bambu_id)

    def _detect_and_report_completion(self, bambu_id: str, assignment: Dict,
                                      snap: Optional[Dict]) -> None:
        terminal = assignment.get("terminal")
        if terminal is None:
            status = snap.get("status") if isinstance(snap, dict) else None
            if status == "NEEDS_CLEARING":
                terminal = "complete"
            elif status == "ERROR":
                terminal = "failed"
            else:
                return  # still printing, or the printer is unreadable — not terminal yet
            self._router.set_assignment_terminal(bambu_id, terminal)

        batch_id = assignment.get("batch_id")
        if not batch_id:
            self._router.clear_assignment(bambu_id)  # nothing to report against
            return
        plate = assignment.get("plate_number")
        if terminal == "complete":
            acked = self._dpf.report_complete(batch_id, plate)
        else:
            acked = self._dpf.report_failed(batch_id, plate)
        if isinstance(acked, dict) and acked.get("batch_id"):
            self._router.clear_assignment(bambu_id)  # 3DPF has it — the report is no longer owed
            logger.info("reported %s of batch %s on printer %s", terminal, batch_id, bambu_id)

    def _send_report(self, job_id: str, batch_id: Optional[str], bambu_id: Optional[str]) -> None:
        """POST the `dispatched` report; on a 3DPF ack, drop the job from the queue. A
        failed report ({} — network/5xx exhausted, or a 4xx) leaves the job DISPATCHED so
        the next drain pass retries. The cloud endpoint is idempotent, so re-reporting an
        already-recorded dispatch is a safe no-op."""
        if not batch_id or not bambu_id:
            return  # nothing to report against; leave as-is
        acked = self._dpf.report_dispatched(batch_id, bambu_id)
        if isinstance(acked, dict) and acked.get("batch_id"):
            self._router.remove(job_id)  # 3DPF has it now — the report is no longer owed

    def _resolve(self, job: Job) -> Optional[Tuple[str, List[str]]]:
        """(batch_id, required_colors) for a job, from the cache or a fresh cloud resolve;
        None when the cloud can't resolve it yet (404 / empty)."""
        if job.batch_id is not None and job.required_colors is not None:
            return job.batch_id, job.required_colors
        if not job.correlation_key:
            return None  # an UNRESOLVED job has no key; never resolve on nothing
        resolved = self._dpf.resolve_batch(job.correlation_key)
        batch_id = resolved.get("batch_id") if isinstance(resolved, dict) else None
        if not batch_id:
            # 3DPF has no batch for this key. Surface it once (it should have resolved on
            # the first try — the batch exists before the operator uploads), then keep
            # retrying quietly in case it's a transient cloud blip.
            if job.id not in self._resolve_failed_logged:
                self._resolve_failed_logged.add(job.id)
                logger.warning("job %s: 3DPF could not resolve batch for key %r — held, "
                               "retrying (check the upload correlated to a real batch)",
                               job.id, job.correlation_key)
            return None
        required = [c for c in (resolved.get("required_colors") or []) if c]
        self._router.mark_resolved(job.id, batch_id, required)
        self._resolve_failed_logged.discard(job.id)  # recovered — clear the anomaly latch
        return batch_id, required

    @staticmethod
    def _color_set(snap: Dict) -> set:
        """A printer's normalized AMS color set (empty slots and unparseable hexes
        dropped), computed once per pass and reused across every queued job in it."""
        have = {normalize_hex(s.get("color_hex")) for s in snap.get("slots") or []}
        have.discard(None)
        return have

    @staticmethod
    def _match(required: List[str], idle_colors: Dict[str, set], claimed: set):
        """First idle, unclaimed printer whose live AMS color set ⊇ `required`, or None.

        Both sides pass through `normalize_hex`, so a Bambu `FF6A13FF` tray and a required
        `#FF6A13` compare equal (R-C). An empty required set matches any idle printer."""
        req = {normalize_hex(c) for c in required}
        req.discard(None)
        for bambu_id, have in idle_colors.items():
            if bambu_id in claimed:
                continue
            if req <= have:
                return bambu_id
        return None

    @staticmethod
    def _ams_mapping(required: List[str], snap: Dict) -> List[int]:
        """Explicit filament→tray mapping (R11): `required[i]`'s color → the 0-based
        global AMS tray holding it (first tray of a color wins). Only reached after
        `_match` proved every required color is present, so every lookup resolves.

        Global tray index is `slot_number - 1` (slot_number is 1-based, unit*4+tray+1)."""
        color_to_tray: Dict[str, int] = {}
        for s in snap.get("slots") or []:
            nh = normalize_hex(s.get("color_hex"))
            if nh is None or nh in color_to_tray:
                continue
            slot_number = s.get("slot_number")
            if isinstance(slot_number, int):
                color_to_tray[nh] = slot_number - 1
        mapping = []
        for c in required:
            tray = color_to_tray.get(normalize_hex(c))
            if tray is not None:
                mapping.append(tray)
        return mapping
