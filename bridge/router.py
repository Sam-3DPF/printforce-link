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
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .ams import normalize_hex
from .printer import is_cancel_failed

logger = logging.getLogger(__name__)

# Watchdog fields stored beside an assignment. Other keys stay untouched.
_SEND_ATTEMPT_FIELDS = frozenset({
    "submission_id", "attempts", "phase", "phase_started_at",
    "last_failure", "uploaded",
})


def is_cancel_failed_snapshot(snap: Optional[Dict]) -> bool:
    """True when a drain snapshot is a user-cancel, not a real fail."""
    if not isinstance(snap, dict):
        return False
    if snap.get("user_cancelled") is True:
        return True
    return is_cancel_failed(
        print_error=snap.get("print_error"),
        hms_code=snap.get("hms_code"),
        hms=snap.get("hms"),
    )

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

# A start can succeed before the printer publishes its first state for the new job.
# During this bounded window, a terminal snapshot may still describe the old print.
ASSIGNMENT_STARTUP_GRACE_SECONDS = 60.0

# A restart close needs the same live state twice, at least this far apart.
# One report, or a gap that is still inside the window, is not steady.
RESTART_RECONCILE_STEADY_SECONDS = 30.0

_RESTART_ACTIVE_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
_RESTART_KNOWN_STATES = _RESTART_ACTIVE_STATES | frozenset({"FINISH", "IDLE", "FAILED"})


def _normalize_submission_id(value) -> Optional[str]:
    """String form of a Link submission id. Zero and blank are absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value == 0:
            return None
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


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
        # {bambu_id: {"batch_id", "plate_number", "terminal",
        #             "started_at", "observed_active", "submission_id",
        #             "observed_running", "recovered"}}
        self.assignments: dict = self._load_assignments()
        # Optional. Set by the app so a recorded submission id is registered
        # on the printer before the next report. None in unit tests.
        self._submission_registrar = None

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

    def set_submission_registrar(self, registrar) -> None:
        """Called with ``(bambu_id, submission_id)`` after a submission is recorded.

        The printer matches ``subtask_id`` against ids it has been given.
        Recording the assignment without telling the printer would make the
        finish look external.
        """
        self._submission_registrar = registrar

    def record_assignment(self, bambu_id: str, batch_id: str,
                          plate_number: Optional[int] = None,
                          started_at: Optional[float] = None,
                          submission_id: Optional[str] = None) -> None:
        """Remember that `batch_id` is now printing on `bambu_id`, so its completion can be
        reported after the job has left the queue. Recorded at physical start; persisted so
        a restart mid-print can still report the finish.

        ``submission_id`` is the id Link sent on the start, when it sent one.
        None keeps today's status-based completion. A present id makes
        completion follow lifecycle events for that id only.
        """
        sid = _normalize_submission_id(submission_id)
        with self._lock:
            self.assignments[bambu_id] = {
                "batch_id": batch_id, "plate_number": plate_number, "terminal": None,
                "started_at": time.time() if started_at is None else float(started_at),
                "observed_active": False,
                "submission_id": sid,
                "observed_running": False,
            }
            self._persist_assignments()
            registrar = self._submission_registrar
        if sid and registrar is not None:
            registrar(bambu_id, sid)

    def update_send_attempt(self, bambu_id: str, **fields) -> None:
        """Record the send watchdog beside this printer's assignment.

        The attempt count, phase, and last failure live on the same object as
        the batch assignment, so a restart continues the same send instead of
        uploading the file again. Only those attempt fields are written.
        """
        fields = {
            key: value for key, value in fields.items() if key in _SEND_ATTEMPT_FIELDS
        }
        if not fields:
            return
        with self._lock:
            assignment = self.assignments.get(bambu_id)
            if assignment is None:
                return
            assignment.update(fields)
            self._persist_assignments()

    def mark_assignment_active(self, bambu_id: str) -> None:
        """Persist proof that this assignment reached PRINTING/PAUSED at least once."""
        with self._lock:
            assignment = self.assignments.get(bambu_id)
            if assignment is not None and not assignment.get("observed_active"):
                assignment["observed_active"] = True
                self._persist_assignments()

    def mark_assignment_recovered(self, bambu_id: str) -> None:
        """The first live report of this session still shows this submission.

        ``recovered`` is how a later reader tells a restart-keep from a start
        that has not been seen yet. ``observed_running`` is set too: the print
        is on the machine, so the next terminal edge belongs to this submission.
        """
        with self._lock:
            assignment = self.assignments.get(bambu_id)
            if assignment is None:
                return
            changed = False
            if assignment.get("recovered") is not True:
                assignment["recovered"] = True
                changed = True
            if assignment.get("observed_running") is not True:
                assignment["observed_running"] = True
                changed = True
            if changed:
                self._persist_assignments()

    def mark_assignment_running(self, bambu_id: str) -> None:
        """Persist proof that a ``print_started`` event matched this submission.

        Separate from ``observed_active``, which is the status-based proof
        used when the assignment has no submission id.
        """
        with self._lock:
            assignment = self.assignments.get(bambu_id)
            if assignment is not None and assignment.get("observed_running") is not True:
                assignment["observed_running"] = True
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

    def annotate_reports(self, reports: List[Dict]) -> List[Dict]:
        """Copy reports and attach persisted pre-report assignment proof.

        The report-state POST happens before this pass's snapshots are drained, so this
        value means the assignment had already reached PRINTING/PAUSED on an earlier pass.
        Missing assignments are explicit False, keeping a newer backend safe during a
        Link-first rolling release.
        """
        assignments = self.assignments_snapshot()
        annotated = []
        for report in reports if isinstance(reports, list) else []:
            if not isinstance(report, dict):
                continue
            item = dict(report)
            assignment = assignments.get(report.get("bambu_id"))
            item["assignment_observed_active"] = (
                isinstance(assignment, dict)
                and assignment.get("observed_active") is True
            )
            annotated.append(item)
        return annotated

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
        if not isinstance(raw, dict):
            return {}
        changed = False
        for bambu_id, assignment in list(raw.items()):
            if not isinstance(bambu_id, str) or not bambu_id or not isinstance(assignment, dict):
                del raw[bambu_id]
                changed = True
                continue
            # Pre-grace assignments were also written only after a physical start.
            # Preserve that proof when upgrading their persisted three-field shape.
            if "started_at" not in assignment and "observed_active" not in assignment:
                assignment["started_at"] = 0.0
                assignment["observed_active"] = True
                changed = True
            else:
                if not isinstance(assignment.get("observed_active"), bool):
                    assignment["observed_active"] = False
                    changed = True
                try:
                    started_at = float(assignment.get("started_at"))
                except (TypeError, ValueError):
                    started_at = float("nan")
                if not math.isfinite(started_at) or started_at < 0:
                    assignment["started_at"] = time.time()
                    changed = True
            # Old files have observed_active and no submission id. Copy the
            # active bit so a later reader sees the same proof under the new
            # name, and keep reading the old key for status-based completion.
            if "observed_running" not in assignment:
                assignment["observed_running"] = assignment.get("observed_active") is True
                changed = True
            elif not isinstance(assignment.get("observed_running"), bool):
                assignment["observed_running"] = False
                changed = True
            if "submission_id" not in assignment:
                assignment["submission_id"] = None
                changed = True
            else:
                normalized = _normalize_submission_id(assignment.get("submission_id"))
                if normalized != assignment.get("submission_id"):
                    assignment["submission_id"] = normalized
                    changed = True
            if "recovered" in assignment and not isinstance(assignment.get("recovered"), bool):
                assignment["recovered"] = False
                changed = True
        if changed:
            self._atomic_write(self.assignments_path, raw)
        return raw

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


def _gcode_state_token(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def _session_token(snap: Dict):
    """Identity of the MQTT session this report belongs to.

    ``session_seq`` wins when it is present, including 0 (no CONNACK yet).
    A report with neither token cannot be reconciled once: there is nothing
    to remember the decision against.
    """
    seq = snap.get("session_seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return ("seq", seq)
    started = snap.get("session_started_at")
    if isinstance(started, str) and started.strip():
        return ("at", started.strip())
    return None


def _stop_serials(desired) -> set:
    return {
        row.get("bambu_id")
        for row in (desired or [])
        if isinstance(row, dict)
        and isinstance(row.get("control"), dict)
        and row["control"].get("action") == "stop"
        and row.get("bambu_id")
    }


class RestartReconciler:
    """Close a persisted assignment the printer's first steady report contradicts.

    Runs once per session, and only on a live report whose session has carried
    ``gcode_state``. A CONNACK, a stale or offline report, and a live delta
    that never included ``gcode_state`` make no decision and do not consume
    that once. A close also waits until the same state and the same matched
    submission id have been live at least ``RESTART_RECONCILE_STEADY_SECONDS``
    apart, and until the assignment is older than the startup grace: a
    just-sent file still reads IDLE while it downloads.

    An assignment Link submitted follows its submission id. A legacy assignment
    (no id) is closed here only when it was already confirmed and the steady
    state is IDLE or FAILED. Every other legacy case stays on the status path.
    """

    def __init__(self, router: Router, fleet, dpf, now_fn=time.time, monotonic=None):
        self._router = router
        self._fleet = fleet
        self._dpf = dpf
        self._now = now_fn
        self._monotonic = monotonic or time.monotonic
        # bambu_id -> session token already decided.
        self._reconciled: Dict = {}
        # bambu_id -> (steady key, monotonic time of the first live sample).
        self._steady: Dict = {}

    def reconcile(self, snapshots: List[Dict], desired: Optional[List[Dict]] = None) -> None:
        stops = _stop_serials(desired)
        snaps = {
            snap.get("bambu_id"): snap
            for snap in snapshots
            if isinstance(snap, dict) and snap.get("bambu_id")
        }
        for bambu_id, assignment in self._router.assignments_snapshot().items():
            if not isinstance(assignment, dict):
                continue
            try:
                self._consider(bambu_id, assignment, snaps.get(bambu_id), bambu_id in stops)
            except Exception:
                logger.exception(
                    "restart reconcile for printer %s failed; will retry", bambu_id,
                )

    def _consider(self, bambu_id: str, assignment: Dict, snap, stop_in_flight: bool) -> None:
        if stop_in_flight or not isinstance(snap, dict):
            return
        if snap.get("connection") != "live":
            return
        state = _gcode_state_token(snap.get("gcode_state"))
        if not state or snap.get("session_gcode_seen") is not True:
            return
        session = _session_token(snap)
        if session is None or self._reconciled.get(bambu_id) == session:
            return
        if assignment.get("terminal") is not None:
            self._reconciled[bambu_id] = session
            self._steady.pop(bambu_id, None)
            return

        matched = _normalize_submission_id(snap.get("print_submission_id"))
        action = self._action(assignment, state, matched)
        if action is None:
            return
        if action == "keep":
            self._router.mark_assignment_recovered(bambu_id)
            logger.info(
                "printer %s: submission %s still on the machine after reconnect; assignment kept",
                bambu_id, _normalize_submission_id(assignment.get("submission_id")),
            )
            self._finish_decision(bambu_id, session)
            return
        if action == "leave":
            self._finish_decision(bambu_id, session)
            return
        if not self._is_steady(bambu_id, session, state, matched):
            return
        if self._assignment_age(assignment) < ASSIGNMENT_STARTUP_GRACE_SECONDS:
            return
        if action == "finish":
            if not self._emit_finish(bambu_id, assignment):
                return
        else:
            self._router.set_assignment_terminal(bambu_id, "ended_unobserved")
        self._finish_decision(bambu_id, session)

    def _finish_decision(self, bambu_id: str, session) -> None:
        self._reconciled[bambu_id] = session
        self._steady.pop(bambu_id, None)

    def _emit_finish(self, bambu_id: str, assignment: Dict) -> bool:
        """Queue the unobserved finish. False leaves the session unreconciled.

        A fleet that cannot take the event must not consume the once-per-session
        decision: the next live report still has to be able to queue it.
        """
        submission_id = _normalize_submission_id(assignment.get("submission_id"))
        emit = getattr(self._fleet, "emit_recovered_event", None)
        if not callable(emit) or not submission_id:
            return False
        logger.info(
            "printer %s: submission %s already finished after reconnect; "
            "queueing unobserved finish",
            bambu_id, submission_id,
        )
        emit(bambu_id, "print_finished", submission_id)
        return True

    def _action(self, assignment: Dict, state: str, matched: Optional[str]) -> Optional[str]:
        """``keep``, ``finish``, ``unobserved``, ``leave``, or None (not yet known).

        Only a confirmed assignment — seen running, or recovered — can end
        unobserved. An unconfirmed one is a start the send path is still
        confirming; a reset during that watchdog is a new session too, and
        closing it here would race the retry.
        """
        sid = _normalize_submission_id(assignment.get("submission_id"))
        confirmed = (
            assignment.get("observed_active") is True
            or assignment.get("observed_running") is True
            or assignment.get("recovered") is True
        )
        if sid:
            if state in _RESTART_ACTIVE_STATES and matched == sid:
                return "keep"
            if state == "FINISH" and matched == sid:
                return "finish"
            if state in {"IDLE", "FAILED"} or (
                state in _RESTART_KNOWN_STATES and matched != sid
            ):
                return "unobserved" if confirmed else "leave"
            return None
        if confirmed and state in {"IDLE", "FAILED"}:
            return "unobserved"
        if state in _RESTART_KNOWN_STATES:
            return "leave"
        return None

    def _is_steady(self, bambu_id: str, session, state: str, matched: Optional[str]) -> bool:
        now = float(self._monotonic())
        key = (session, state, matched)
        previous = self._steady.get(bambu_id)
        if previous is None or previous[0] != key:
            self._steady[bambu_id] = (key, now)
            return False
        return now - previous[1] >= RESTART_RECONCILE_STEADY_SECONDS

    def _assignment_age(self, assignment: Dict) -> float:
        try:
            started = float(assignment.get("started_at"))
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(started):
            return 0.0
        return max(0.0, self._now() - started)


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

    def __init__(self, router: Router, fleet, dpf, now_fn=time.time, monotonic=None):
        self._router = router
        self._fleet = fleet
        self._dpf = dpf
        self._now = now_fn
        self._reconciler = RestartReconciler(
            router, fleet, dpf, now_fn=now_fn, monotonic=monotonic,
        )
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

        # A persisted assignment the first steady report of this session contradicts
        # is latched here, before the completion pass reports that latch.
        self._reconciler.reconcile(snapshots, desired or [])

        # Then detect finished/failed prints and report them (U11) — also independent of
        # idle printers, and durable: a latched completion is retried until 3DPF acks.
        self._report_completions(snapshots, desired or [])

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
        # Local color-only auto-dispatch is off once the cloud Sliced Queue
        # exists (KTD6). Drain only retries owed DISPATCHED reports and
        # completion reports. Start is a cloud send command.
        return

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

    def _report_completions(self, snapshots: List[Dict],
                            desired: Optional[List[Dict]] = None) -> None:
        """Edge-detect each assigned printer finishing/failing from THIS pass's fresh
        snapshot, then report it (retrying until acked). A finished print maps to
        NEEDS_CLEARING and a failed one to ERROR (printer.py's status map); anything else
        (still PRINTING, OFFLINE) is not yet terminal and waits.

        A user-cancel or a live stop is not a fail and not a finish. Drop the
        assignment so a later wire FINISH cannot report_complete a send that
        return-to-queue already released.
        """
        stop_serials = _stop_serials(desired)
        snap_by_id = {
            s.get("bambu_id"): s
            for s in snapshots
            if isinstance(s, dict) and s.get("bambu_id")
        }
        for bambu_id, assignment in self._router.assignments_snapshot().items():
            try:
                self._detect_and_report_completion(
                    bambu_id, assignment, snap_by_id.get(bambu_id),
                    stop_in_flight=bambu_id in stop_serials,
                )
            except Exception:
                logger.exception("completion handling for printer %s failed; will retry", bambu_id)

    def _detect_and_report_completion(self, bambu_id: str, assignment: Dict,
                                      snap: Optional[Dict],
                                      stop_in_flight: bool = False) -> None:
        # A submission id means Link started this print and can see its edges.
        # Status alone would close the batch on someone else's FINISH. Assignments
        # with no id are today's sends: they still complete from status.
        if _normalize_submission_id(assignment.get("submission_id")):
            self._detect_submission_completion(
                bambu_id, assignment, snap, stop_in_flight=stop_in_flight,
            )
            return
        terminal = assignment.get("terminal")
        if terminal is None:
            status = snap.get("status") if isinstance(snap, dict) else None
            observed_active = assignment.get("observed_active") is True
            if status in ("PRINTING", "PAUSED"):
                if not observed_active:
                    self._router.mark_assignment_active(bambu_id)
                return
            started_at = assignment.get("started_at")
            try:
                startup_age = max(0.0, self._now() - float(started_at))
            except (TypeError, ValueError):
                startup_age = 0.0
            # Cancel codes can linger in Bambu's merged MQTT payload after a new
            # print starts. They are terminal evidence only while the normalized
            # snapshot is IDLE/ERROR; never drop a live PRINTING assignment.
            cancel_terminal = (
                status in ("IDLE", "ERROR") and is_cancel_failed_snapshot(snap)
            )
            startup_grace_elapsed = (
                startup_age >= ASSIGNMENT_STARTUP_GRACE_SECONDS
            )
            if stop_in_flight:
                self._router.clear_assignment(bambu_id)
                logger.info(
                    "printer %s: explicit cloud stop; not reporting failed or complete",
                    bambu_id,
                )
                return
            if cancel_terminal:
                if observed_active:
                    self._router.clear_assignment(bambu_id)
                    logger.info(
                        "printer %s: active print cancelled; not reporting failed or complete",
                        bambu_id,
                    )
                    return
                if not startup_grace_elapsed:
                    return
                terminal = "failed"
            elif status in ("NEEDS_CLEARING", "FINISH"):
                if observed_active:
                    terminal = "complete"
                elif startup_grace_elapsed:
                    terminal = "failed"
                else:
                    return
            elif status == "ERROR" and (observed_active or startup_grace_elapsed):
                terminal = "failed"
            else:
                return  # pre-start terminal, idle, or unreadable — not terminal yet
            self._router.set_assignment_terminal(bambu_id, terminal)

        self._ack_terminal(bambu_id, assignment, terminal)

    def _detect_submission_completion(self, bambu_id: str, assignment: Dict,
                                      snap: Optional[Dict],
                                      stop_in_flight: bool = False) -> None:
        """Complete, fail, or clear from this pass's lifecycle events only.

        A ``print_finished`` / ``print_failed`` / ``print_cancelled`` counts
        only when its ``submission_id`` is this assignment's and its origin
        is ``link``. An external finish, or a finish for another submission,
        leaves the assignment alone even if the printer's status is terminal.
        A standing terminal status with no matching event is not a completion:
        the first report after a restart often is.
        """
        terminal = assignment.get("terminal")
        submission_id = _normalize_submission_id(assignment.get("submission_id"))
        if terminal is None:
            if stop_in_flight:
                self._router.clear_assignment(bambu_id)
                logger.info(
                    "printer %s: explicit cloud stop; not reporting failed or complete",
                    bambu_id,
                )
                return
            terminal = self._terminal_from_events(bambu_id, submission_id, snap)
            if terminal is None:
                return
            self._router.set_assignment_terminal(bambu_id, terminal)

        self._ack_terminal(bambu_id, assignment, terminal)

    def _ack_terminal(self, bambu_id: str, assignment: Dict, terminal: str) -> None:
        """POST a latched outcome and drop the assignment once 3DPF acks it.

        ``ended_unobserved`` is a failure reason, not a second endpoint. It is
        retried on later passes until the ack, the same way ``failed`` is.
        """
        batch_id = assignment.get("batch_id")
        if not batch_id:
            self._router.clear_assignment(bambu_id)
            return
        plate = assignment.get("plate_number")
        if terminal == "complete":
            acked = self._dpf.report_complete(batch_id, plate)
        elif terminal == "ended_unobserved":
            acked = self._dpf.report_failed(
                batch_id, plate, reason="ended_unobserved",
            )
        else:
            acked = self._dpf.report_failed(batch_id, plate)
        if isinstance(acked, dict) and acked.get("batch_id"):
            self._router.clear_assignment(bambu_id)
            logger.info("reported %s of batch %s on printer %s", terminal, batch_id, bambu_id)

    def _terminal_from_events(self, bambu_id: str, submission_id: str,
                              snap: Optional[Dict]) -> Optional[str]:
        """``complete``, ``failed``, or None. A matching cancel clears and returns None.

        Events are in queue order. A cancel later in the same pass drops the
        assignment instead of reporting a finish that the cancel superseded.
        """
        events = snap.get("events") if isinstance(snap, dict) else None
        if not isinstance(events, list):
            return None
        terminal = None
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("origin") != "link":
                continue
            if _normalize_submission_id(event.get("submission_id")) != submission_id:
                continue
            kind = event.get("type")
            if kind == "print_started":
                self._router.mark_assignment_running(bambu_id)
            elif kind == "print_cancelled":
                self._router.clear_assignment(bambu_id)
                logger.info(
                    "printer %s: submission %s cancelled; not reporting failed or complete",
                    bambu_id, submission_id,
                )
                return None
            elif kind == "print_finished":
                terminal = "complete"
            elif kind == "print_failed":
                terminal = "failed"
        return terminal

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
