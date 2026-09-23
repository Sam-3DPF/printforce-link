"""Manage the printer fleet: connect all printers and produce the aggregated
state report the bridge POSTs to 3DPF."""

import logging
import socket
import threading
import time
from typing import List, Dict, Optional

from concurrent.futures import Future

from .config import PrinterConfig
from .discover import DiscoveredPrinter, discover
from .printer import _DEFAULT_STALE_AFTER_SECONDS, BambuPrinter
from .printer_worker import PrinterWorker

logger = logging.getLogger(__name__)


# How often, at most, to run an SSDP re-discovery scan while a printer is offline (U1).
# A whole farm can be legitimately offline (bridge just started, a power blip), and a
# scan every poll would add latency for nothing.
_DEFAULT_REDISCOVER_INTERVAL_SECONDS = 60.0
# A short listen is enough: a printer that just changed IP is actively broadcasting SSDP.
_DEFAULT_DISCOVER_TIMEOUT_SECONDS = 5.0
# A removed/re-added serial may need one current worker while one stale generation unwinds.
# Bound those stale lifetimes so repeated config churn cannot grow threads without limit.
_MAX_RECONNECT_WORKERS_PER_SERIAL = 2
# A stalled connect must not pin a serial: two hung workers used to block a
# reserved IP from ever being tried again.
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 12.0
# The backstop is slower than the session watchdog. It only looks at clients
# that already had a handshake and have been quiet for five minutes.
_RECOVERY_INTERVAL_SECONDS = 60.0
_RECOVERY_SILENCE_SECONDS = 300.0
_RECOVERY_COOLDOWN_SECONDS = 300.0
# An SSDP flap must not open a new MQTT session every scan. The TCP probe of
# the stored address is cheap and is not counted here.
_ADDRESS_PROOF_COOLDOWN_SECONDS = 300.0
_ADDRESS_SOURCES = ("ssdp", "net_info", "pin")


def _default_tcp_probe(ip: str) -> bool:
    """True when port 8883 accepts a TCP connection.

    One second: a closed port must not sit on the caller. The fleet runs this
    off the report loop.
    """
    sock = socket.create_connection((ip, 8883), timeout=1.0)
    try:
        return True
    finally:
        sock.close()


def _default_discover(timeout: float, probe_ips=None) -> List[DiscoveredPrinter]:
    return discover(timeout=timeout, probe_ips=probe_ips)


def _printer_had_session(printer) -> bool:
    value = getattr(printer, "had_session", False)
    if callable(value):
        value = value()
    return bool(value)


def _note_address(printer, kind, **fields) -> None:
    """Record an address decision. A fake with no log still finishes the gate."""
    log = getattr(printer, "log", None)
    record = getattr(log, "record_event", None) if log is not None else None
    if not callable(record):
        return
    try:
        record(kind, **fields)
    except Exception:
        logger.debug(
            "printer %s: could not record %s",
            getattr(printer, "bambu_id", "?"), kind,
        )


def _printer_address_candidates(printer) -> List[str]:
    fn = getattr(printer, "address_candidates", None)
    if not callable(fn):
        return []
    try:
        found = fn()
    except Exception:
        logger.debug(
            "printer %s: address candidates were not read",
            getattr(printer, "bambu_id", "?"),
        )
        return []
    if not isinstance(found, (list, tuple)):
        return []
    out: List[str] = []
    for ip in found:
        if isinstance(ip, str) and ip and ip not in out:
            out.append(ip)
    return out


def _call_proves_serial(printer, ip: str) -> bool:
    """False when this printer cannot show it lives at ``ip``.

    A missing method is not proof. Swapping on SSDP alone is how a neighbor
    printer steals the serial.
    """
    proof = getattr(printer, "proves_serial_at", None)
    if not callable(proof):
        return False
    try:
        return bool(proof(ip))
    except Exception as exc:
        logger.warning(
            "printer %s: address proof at %s failed (%s)",
            getattr(printer, "bambu_id", "?"), ip, type(exc).__name__,
        )
        return False


def _note_backstop(printer) -> None:
    """The session cannot see this rebuild. Fakes have no log."""
    log = getattr(printer, "log", None)
    record = getattr(log, "record_event", None)
    if not callable(record):
        return
    try:
        record("backstop")
    except Exception:
        logger.debug(
            "printer %s: could not record the backstop",
            getattr(printer, "bambu_id", "?"),
        )


def _printer_silent_for(printer, now):
    fn = getattr(printer, "silent_for", None)
    if not callable(fn):
        return None
    return fn(now)


def _call_discover(discover_fn, timeout: float, probe_ips) -> List[DiscoveredPrinter]:
    """Older test injectors only take `timeout`. Production always gets probe_ips."""
    try:
        return discover_fn(timeout, probe_ips)
    except TypeError:
        return discover_fn(timeout)


class Fleet:
    def __init__(self, printer_configs: List[PrinterConfig],
                 stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
                 *,
                 printer_factory=BambuPrinter,
                 discover_fn=None,
                 rediscover_interval_seconds: float = _DEFAULT_REDISCOVER_INTERVAL_SECONDS,
                 discover_timeout_seconds: float = _DEFAULT_DISCOVER_TIMEOUT_SECONDS,
                 monotonic=time.monotonic,
                 on_address=None,
                 connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
                 tcp_probe=None):
        # `stale_after_seconds` is how long a printer may say nothing new before it is
        # presumed gone (Config.stale_after_seconds). It is per-fleet because it is
        # derived from the poll interval — see BambuPrinter.snapshot.
        self._stale_after_seconds = stale_after_seconds
        # `printer_factory` and `discover_fn` are injectable so the fleet's connection
        # logic (U1/U2) is testable without the library or real SSDP.
        self._lock = threading.RLock()
        self._make_printer = printer_factory
        self._workers = {}
        self._printers = []
        for cfg in printer_configs:
            printer = printer_factory(cfg, stale_after_seconds=stale_after_seconds)
            self._printers.append(printer)
            # One worker per serial, kept when an IP change swaps the printer object.
            self._workers[cfg.bambu_id] = PrinterWorker(cfg.bambu_id)
        for printer in self._printers:
            self._wire_defer(printer)
        self._configs = {c.bambu_id: c for c in printer_configs}
        # Every add/remove changes the serial's generation. Slow I/O may finish later,
        # but it can only commit while the generation it started under is still current.
        self._membership_generations = {c.bambu_id: 0 for c in printer_configs}
        self._adds_in_flight = {}
        self._discover = discover_fn if discover_fn is not None else _default_discover
        self._rediscover_interval = rediscover_interval_seconds
        self._discover_timeout = discover_timeout_seconds
        self._monotonic = monotonic
        self._last_discovery_monotonic = None
        # One daemon worker per active serial. There is no shared queue or shared worker
        # capacity: a stuck printer consumes only its own worker and cannot starve another.
        self._reconnects_in_flight = {}
        # Ownership above follows active membership generations and is deliberately
        # cleared on remove. This count follows actual thread lifetimes across generations.
        self._reconnect_worker_counts = {}
        self._on_address = on_address
        self._connect_timeout = connect_timeout_seconds
        self._tcp_probe = tcp_probe if tcp_probe is not None else _default_tcp_probe
        self._last_recovery_monotonic = None
        self._recovery_tried_at = {}
        self._recovery_inflight = {}
        # (serial, candidate) -> monotonic time of the last MQTT proof.
        self._address_proof_at = {}
        self._pin_candidates = {}

    def connect_all(self) -> None:
        """Connect the fleet without letting one printer hold up startup.

        The macOS updater keeps a new build only if this process reaches 3DPF
        within two minutes. ``connect()`` publishes a full status dump and waits
        for the broker to ack; a half-open socket never does. Reconnects already
        abandon that wait after ``_connect_timeout``. Startup spends that same
        budget once, across the whole fleet, then moves on. A printer that did
        not answer is reported OFFLINE and retried later.
        """
        with self._lock:
            printers = list(self._printers)
        if not printers:
            return
        pending = []
        for printer in printers:
            finished = threading.Event()
            error: List[BaseException] = []

            def _connect(printer=printer, finished=finished, error=error):
                try:
                    printer.connect()
                except BaseException as exc:
                    error.append(exc)
                finally:
                    finished.set()

            threading.Thread(
                target=_connect,
                name=f"printer-connect-{printer.bambu_id}",
                daemon=True,
            ).start()
            pending.append((printer, finished, error))

        deadline = time.monotonic() + self._connect_timeout
        for printer, finished, error in pending:
            remaining = deadline - time.monotonic()
            if remaining < 0 or not finished.wait(remaining):
                logger.warning(
                    "could not connect to %s: startup connect timed out",
                    printer.bambu_id,
                )
                continue
            if error:
                logger.warning(
                    "could not connect to %s: %s",
                    printer.bambu_id, type(error[0]).__name__,
                )

    def by_id(self, bambu_id: str):
        """The BambuPrinter with this serial, or None. The dispatcher (U9) needs the
        live connection object (not just a snapshot) to FTPS-upload + MQTT-start."""
        with self._lock:
            return next((p for p in self._printers if p.bambu_id == bambu_id), None)

    def submit(self, bambu_id: str, fn, *args, **kwargs) -> Optional[Future]:
        """Queue ``fn`` on this serial's worker. None when the serial is not a member.

        The future is failed with ``WorkerBusy`` when that printer's queue is full.
        The caller is not blocked, and the fleet lock is not held across the work.
        """
        with self._lock:
            if not any(p.bambu_id == bambu_id for p in self._printers):
                return None
            worker = self._workers.get(bambu_id)
        if worker is None:
            return None
        return worker.submit(fn, *args, **kwargs)

    def worker_busy(self, bambu_id: str) -> bool:
        """True when this serial has a job queued or running."""
        with self._lock:
            worker = self._workers.get(bambu_id)
        if worker is None:
            return False
        return bool(worker.busy)

    def apply_control(self, bambu_id: str, action: str) -> bool:
        """Publish pause/resume/stop, or queue refresh on the printer worker.

        The membership lock is not held across the publish. Refresh sleeps inside
        the pushall window, so it is queued and this returns True once it is queued.
        """
        printer = self.by_id(bambu_id)
        if printer is None:
            logger.warning("control %s requested for unknown printer %s",
                           action, bambu_id)
            return False
        if action == "pause":
            return printer.pause_print()
        if action == "resume":
            if hasattr(printer, "resume_from_stage"):
                return printer.resume_from_stage()
            return printer.resume_print()
        if action == "stop":
            return printer.stop_print()
        if action == "refresh":
            return self._enqueue_refresh(bambu_id)
        logger.warning("unknown control %s requested for printer %s", action, bambu_id)
        return False

    def _enqueue_refresh(self, bambu_id: str) -> bool:
        """True when ``request_full_status`` was queued, not when the printer answered."""
        with self._lock:
            worker = self._workers.get(bambu_id)
        if worker is None or self.by_id(bambu_id) is None:
            return False

        def _refresh():
            current = self.by_id(bambu_id)
            if current is None:
                return False
            return current.request_full_status()

        future = worker.submit(_refresh)
        if future.cancelled():
            return False
        if future.done():
            return future.exception() is None
        return True

    def dispatch(self, bambu_id: str, file_path: str, ams_mapping, plate_number: int = 1,
                 remote_name: Optional[str] = None) -> bool:
        """Upload + start `file_path` on the named printer. False if that printer isn't in
        the fleet; otherwise the printer's start result. Raises on a transport error so
        the router re-queues rather than dropping the job.

        `remote_name` is what the printer shows as the current job. Omit it to use the
        local basename (the cloud-send spool is `{batch_id}.3mf` and must not leak).
        """
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            cancel = self._worker_cancel_locked(bambu_id)
        if printer is None:
            logger.error("dispatch requested for unknown printer %s", bambu_id)
            return False
        return printer.upload_and_start(
            file_path, ams_mapping, plate_number, remote_name=remote_name,
            cancel=cancel,
        )

    def upload(self, bambu_id: str, file_path: str,
               remote_name: Optional[str] = None) -> Optional[str]:
        """FTPS-upload only. None if that printer is not in the fleet.

        The worker's cancel event is passed through so removal stops the transfer
        between blocks. The membership lock is not held across the socket.
        """
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            cancel = self._worker_cancel_locked(bambu_id)
        if printer is None:
            logger.error("upload requested for unknown printer %s", bambu_id)
            return None
        return printer.upload_file(file_path, remote_name=remote_name, cancel=cancel)

    def start_print(self, bambu_id: str, remote_name: str, ams_mapping,
                    plate_number: int = 1) -> bool:
        """MQTT-start a file already on the printer."""
        printer = self.by_id(bambu_id)
        if printer is None:
            logger.error("start_print requested for unknown printer %s", bambu_id)
            return False
        return printer.start_print(remote_name, ams_mapping, plate_number)

    def snapshot(self) -> List[Dict]:
        """One state report per printer — the bridge's wire contract with 3DPF:

            [{
                "bambu_id": str,
                "status": IDLE | PRINTING | PAUSED | NEEDS_CLEARING | ERROR | OFFLINE,
                # Empty slots included. **None means "no AMS information"** — the
                # printer has reported no unit list yet — and is NOT the same claim as
                # [], which says the AMS is empty and makes the cloud delete the slot
                # rows. See `ams.parse_ams`.
                "slots": [{slot_number, color_hex, filament_type}] | None,

                # telemetry, FLAT on the report (this is what the cloud's
                # ingest_printer_state reads — not a nested object):
                "progress_percent", "layer_num", "total_layer_num", "remaining_seconds",
                "nozzle_temper", "nozzle_target_temper", "bed_temper",
                "bed_target_temper", "chamber_temper", "gcode_file", "subtask_name",
                "nozzle_diameter", "stage", "tray_exist_bits",
                "hms_severity", "hms_code", "hms_count", "hms_title", "hms_detail",
                "print_error",
                "gcode_state", "hms_present", "hms_empty",
                "has_active_file", "has_active_task", "has_active_project",
                "local_ip",
                "stage_queue_empty", "print_type", "historical_failed_ready",

                "print_duration_seconds": int | None,
                "print_duration_source": "bridge" | "printer" | None,

                # Separate from status. live = socket up and a report on this
                # session is inside the staleness window. stale = last telemetry
                # and last slots, status still OFFLINE. offline = null telemetry
                # and slots None (not []).
                "connection": "live" | "stale" | "offline",
                "last_message_age_seconds": float | None,
                "connect_error": str | None,          # session down_reason; None when live
                "session_started_at": str | None,     # ISO-8601 UTC of this CONNACK
            }, ...]

        `bambu_id` / `status` keep the shipped mapping, so an older ingest keeps
        working. `connection` and the stamps beside it are additive; unknown keys
        are ignored on the far side. `slots: []` still means the printer reported
        an AMS with no units. `slots: None` means no AMS information — including
        every `connection: offline` report, which must not be read as an empty AMS.
        A stale report keeps the last `slots` list. A printer that cannot be read
        reports OFFLINE with null telemetry rather than being omitted — a missing
        printer and an unreachable one are different facts. See `BambuPrinter.snapshot`.
        """
        # Membership only. Each snapshot runs outside the lock so one printer's
        # network I/O cannot stall the report for the rest of the fleet.
        with self._lock:
            printers = list(self._printers)
        return [printer.snapshot() for printer in printers]

    def add_printer(self, cfg: PrinterConfig) -> None:
        """Add a printer to a running fleet without a restart (U2) — the precondition for
        the web wizard to make a printer appear live. Idempotent by serial. A connect
        failure does not stop the add: the printer joins OFFLINE and self-heals via
        reconcile_connections()."""
        bambu_id = cfg.bambu_id
        with self._lock:
            if (any(p.bambu_id == bambu_id for p in self._printers)
                    or bambu_id in self._adds_in_flight):
                return
            generation = self._membership_generations.get(bambu_id, 0) + 1
            self._membership_generations[bambu_id] = generation
            self._adds_in_flight[bambu_id] = generation
        printer = self._make_printer(cfg, stale_after_seconds=self._stale_after_seconds)
        try:
            printer.connect()
        except Exception as e:
            logger.warning("could not connect to newly added %s: %s",
                           cfg.bambu_id, type(e).__name__)
        with self._lock:
            owns_add = self._adds_in_flight.get(bambu_id) == generation
            if owns_add:
                self._adds_in_flight.pop(bambu_id, None)
            cancelled = (
                not owns_add
                or self._membership_generations.get(bambu_id) != generation
                or any(p.bambu_id == bambu_id for p in self._printers)
            )
            if not cancelled:
                self._printers.append(printer)
                self._configs[bambu_id] = cfg
                if bambu_id not in self._workers:
                    self._workers[bambu_id] = PrinterWorker(bambu_id)
        if cancelled:
            printer.disconnect()
            return
        self._wire_defer(printer)
        logger.info("added printer %s (%s) to the fleet", cfg.bambu_id, cfg.name)

    def remove_printer(self, bambu_id: str) -> None:
        """Remove a printer from a running fleet (U2), closing its connection. No-op if
        the serial isn't in the fleet."""
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            add_pending = bambu_id in self._adds_in_flight
            if printer is None and not add_pending:
                return
            self._membership_generations[bambu_id] = (
                self._membership_generations.get(bambu_id, 0) + 1
            )
            self._adds_in_flight.pop(bambu_id, None)
            self._reconnects_in_flight.pop(bambu_id, None)
            self._configs.pop(bambu_id, None)
            self._pin_candidates.pop(bambu_id, None)
            worker = self._workers.pop(bambu_id, None)
            if printer is not None:
                # Remove membership before network cleanup. Any late worker completion
                # sees a different generation and may only close its replacement.
                self._printers = [p for p in self._printers if p is not printer]
        if worker is not None:
            # Stop outside the lock: the in-flight job may need by_id, and cancel
            # must be visible without waiting for a long transfer to finish.
            worker.stop()
        if printer is not None:
            printer.disconnect()
            logger.info("removed printer %s from the fleet", bambu_id)

    def known_ips(self) -> List[str]:
        """LAN addresses the fleet is currently dialing — used to unicast SSDP."""
        with self._lock:
            return [p.current_ip for p in self._printers if p.current_ip]

    def reconcile_connections(self) -> None:
        """Propose a new address when an offline printer is seen somewhere else.

        SSDP and the last ``print.net.info`` block are candidates. The stored
        IP stays until it fails a TCP probe and the candidate proves this
        serial. That proof runs on the reconnect worker, so this method
        returns without waiting on it.

        Same-IP silence is not a new printer. The session watchdog resets a quiet
        client, and ``recover_dead_sessions`` probes port 8883 for one that has
        been down for minutes. Swapping the printer object here would drop the
        stopwatch and the cancel latch.

        Scans only when at least one printer is offline AND `rediscover_interval` has
        elapsed since the last scan — a healthy farm pays nothing, and a whole farm that
        is briefly down is not hammered. Each serial owns at most one daemon reconnect
        worker, so a blocked connect cannot block the reporter or another printer.
        A newly learned IP is persisted only after the proof.
        """
        with self._lock:
            offline = [p for p in self._printers if p.is_offline]
        if not offline:
            return
        now = self._monotonic()
        with self._lock:
            if (self._last_discovery_monotonic is not None
                    and now - self._last_discovery_monotonic < self._rediscover_interval):
                return
            self._last_discovery_monotonic = now
        try:
            found = {
                d.serial: d for d in _call_discover(
                    self._discover, self._discover_timeout, self.known_ips(),
                )
            }
        except Exception as e:
            logger.warning("re-discovery scan failed (%s); will retry next interval",
                           type(e).__name__)
            return
        for printer in offline:
            seen = set()
            hit = found.get(printer.bambu_id)
            if hit is not None and hit.ip:
                seen.add(hit.ip)
                self.propose_address(printer.bambu_id, hit.ip, "ssdp")
            for ip in _printer_address_candidates(printer):
                if ip in seen:
                    continue
                seen.add(ip)
                self.propose_address(printer.bambu_id, ip, "net_info")
            with self._lock:
                pin = self._pin_candidates.get(printer.bambu_id)
            if pin and pin not in seen:
                self.propose_address(printer.bambu_id, pin, "pin")

    def propose_address(self, bambu_id: str, ip: str, source: str) -> None:
        """Ask the off-loop gate to consider ``ip`` for this serial.

        Returns immediately. ``source`` is ``ssdp``, ``net_info``, or ``pin``.
        The address already being dialed is not a candidate. A pair proved in
        the last five minutes is skipped so a flapping answer cannot open
        another MQTT session.
        """
        if source not in _ADDRESS_SOURCES or not ip or not bambu_id:
            return
        with self._lock:
            if source == "pin":
                # 3DPF sends a changed pin once. It stays a candidate for
                # later offline passes, in case the printer moves after the
                # stored address was kept.
                self._pin_candidates[bambu_id] = ip
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            if printer is None or ip == printer.current_ip:
                return
            if self._address_proof_throttled(bambu_id, ip):
                return
        self._schedule_reconnect(printer, ip, source)

    def recover_dead_sessions(self) -> None:
        """Rebuild a client that already connected, then went silent, if 8883 answers.

        At most once a minute for the fleet, and once per five minutes per printer.
        The TCP probe runs on a daemon thread so the report loop does not wait on it.
        A printer that has never completed a handshake is left to paho.
        """
        now = self._monotonic()
        with self._lock:
            if (self._last_recovery_monotonic is not None
                    and now - self._last_recovery_monotonic < _RECOVERY_INTERVAL_SECONDS):
                return
            self._last_recovery_monotonic = now
            due = []
            for printer in self._printers:
                if not _printer_had_session(printer):
                    continue
                silent = _printer_silent_for(printer, now)
                if silent is None or silent <= _RECOVERY_SILENCE_SECONDS:
                    continue
                tried = self._recovery_tried_at.get(printer.bambu_id)
                if tried is not None and now - tried < _RECOVERY_COOLDOWN_SECONDS:
                    continue
                if printer.bambu_id in self._recovery_inflight:
                    continue
                token = object()
                self._recovery_inflight[printer.bambu_id] = token
                self._recovery_tried_at[printer.bambu_id] = now
                due.append((printer, token))
        for printer, token in due:
            worker = threading.Thread(
                target=self._probe_and_rebuild,
                args=(printer, token),
                name=f"link-recover-{printer.bambu_id}",
                daemon=True,
            )
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    if self._recovery_inflight.get(printer.bambu_id) is token:
                        self._recovery_inflight.pop(printer.bambu_id, None)
                logger.warning(
                    "printer %s recovery worker could not start; will retry",
                    printer.bambu_id,
                )

    def _probe_and_rebuild(self, printer, token) -> None:
        """One TCP probe, then an in-place client reset if the port accepted it."""
        bambu_id = printer.bambu_id
        try:
            ip = printer.current_ip
            try:
                answered = bool(self._tcp_probe(ip))
            except Exception:
                answered = False
            if not answered:
                return
            with self._lock:
                if printer not in self._printers:
                    return
            logger.info(
                "printer %s silent and port 8883 accepts; rebuilding the session",
                bambu_id,
            )
            printer.rebuild_session()
            _note_backstop(printer)
        except Exception as exc:
            logger.warning(
                "printer %s session rebuild failed (%s)",
                bambu_id, type(exc).__name__,
            )
        finally:
            with self._lock:
                if self._recovery_inflight.get(bambu_id) is token:
                    self._recovery_inflight.pop(bambu_id, None)

    def _candidate_replaces_current(self, printer, candidate: str, source: str,
                                    generation: int, token) -> bool:
        """True when the stored port is closed and ``candidate`` proves this serial.

        Runs on the reconnect worker. An answering stored address is kept and
        does not consume the proof cooldown.
        """
        current = printer.current_ip
        if not candidate or candidate == current:
            return False
        try:
            answered = bool(self._tcp_probe(current))
        except Exception:
            answered = False
        if answered:
            _note_address(
                printer, "address_kept",
                ip=current, candidate=candidate, source=source,
            )
            return False
        bambu_id = printer.bambu_id
        with self._lock:
            if (
                printer not in self._printers
                or self._membership_generations.get(bambu_id) != generation
                or self._reconnects_in_flight.get(bambu_id) != (generation, token)
            ):
                return False
            self._address_proof_at[(bambu_id, candidate)] = self._monotonic()
        if _call_proves_serial(printer, candidate):
            return True
        _note_address(
            printer, "address_rejected",
            ip=current, candidate=candidate, source=source,
        )
        return False

    def _address_proof_throttled(self, bambu_id: str, ip: str) -> bool:
        """True when this pair was proved inside the cooldown. Caller holds ``self._lock``."""
        tried = self._address_proof_at.get((bambu_id, ip))
        if tried is None:
            return False
        return self._monotonic() - tried < _ADDRESS_PROOF_COOLDOWN_SECONDS

    def _schedule_reconnect(self, printer, new_ip: str, source: str) -> None:
        """Start at most one daemon reconnect worker for this fleet member/serial."""
        bambu_id = printer.bambu_id
        with self._lock:
            current = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            cfg = self._configs.get(bambu_id)
            live_workers = self._reconnect_worker_counts.get(bambu_id, 0)
            if (
                current is not printer
                or cfg is None
                or bambu_id in self._reconnects_in_flight
                or live_workers >= _MAX_RECONNECT_WORKERS_PER_SERIAL
            ):
                return
            generation = self._membership_generations.get(bambu_id, 0)
            token = object()
            self._reconnects_in_flight[bambu_id] = (generation, token)
            self._reconnect_worker_counts[bambu_id] = live_workers + 1
            replacement_cfg = PrinterConfig(
                bambu_id=cfg.bambu_id,
                ip=new_ip,
                access_code=cfg.access_code,
                name=cfg.name,
            )
        worker = threading.Thread(
            target=self._run_reconnect,
            args=(printer, replacement_cfg, generation, token, source),
            name=f"printer-reconnect-{bambu_id}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            with self._lock:
                if self._reconnects_in_flight.get(bambu_id) == (generation, token):
                    self._reconnects_in_flight.pop(bambu_id, None)
                self._release_reconnect_worker_slot(bambu_id)
            logger.warning("printer %s reconnect worker could not start; will retry", bambu_id)

    def _run_reconnect(self, printer, replacement_cfg: PrinterConfig,
                       generation: int, token, source: str) -> None:
        """Prove the candidate off-loop, then swap only if membership is unchanged.

        The stored address is kept when port 8883 still accepts. Otherwise the
        candidate must complete an MQTT session for this serial. The swap carries
        the membership generation, so a connect that finishes after remove cannot
        put the serial back.
        """
        bambu_id = printer.bambu_id
        replacement = None
        swapped = False
        previous_ip = printer.current_ip
        candidate = replacement_cfg.ip
        try:
            with self._lock:
                if (
                    printer not in self._printers
                    or self._membership_generations.get(bambu_id) != generation
                    or self._reconnects_in_flight.get(bambu_id) != (generation, token)
                ):
                    return
            if not self._candidate_replaces_current(
                printer, candidate, source, generation, token,
            ):
                return
            logger.info("printer %s reconnecting at %s (was %s)",
                        bambu_id, candidate, previous_ip)
            replacement = self._make_printer(
                replacement_cfg,
                stale_after_seconds=self._stale_after_seconds,
            )
            # Same worker: an IP swap must not strand queued commands on the old object.
            self._wire_defer(replacement)
            if not self._connect_replacement(replacement, replacement_cfg.ip, bambu_id):
                return
            with self._lock:
                if (
                    self._membership_generations.get(bambu_id) == generation
                    and self._reconnects_in_flight.get(bambu_id) == (generation, token)
                ):
                    for index, current in enumerate(self._printers):
                        if current is printer:
                            self._printers[index] = replacement
                            self._configs[bambu_id] = replacement_cfg
                            swapped = True
                            break
        except Exception as e:
            logger.warning("printer %s reconnect to %s failed (%s); will retry",
                           bambu_id, replacement_cfg.ip, type(e).__name__)
        finally:
            with self._lock:
                if self._reconnects_in_flight.get(bambu_id) == (generation, token):
                    self._reconnects_in_flight.pop(bambu_id, None)
                self._release_reconnect_worker_slot(bambu_id)
            if replacement is not None and not swapped:
                replacement.disconnect()
            if swapped:
                _note_address(
                    replacement, "address_switched",
                    ip=candidate, previous=previous_ip, source=source,
                )
                printer.disconnect()
                if (
                    self._on_address is not None
                    and replacement_cfg.ip
                    and replacement_cfg.ip != previous_ip
                ):
                    try:
                        self._on_address(bambu_id, replacement_cfg.ip)
                    except Exception as e:
                        logger.warning(
                            "printer %s: could not persist new address %s (%s)",
                            bambu_id, replacement_cfg.ip, type(e).__name__,
                        )

    def _connect_replacement(self, replacement, ip: str, bambu_id: str) -> bool:
        """Run connect() with a timeout so a hung MQTT handshake cannot pin the serial."""
        finished = threading.Event()
        error: List[BaseException] = []

        def _connect():
            try:
                replacement.connect()
            except BaseException as e:
                error.append(e)
            finally:
                finished.set()

        worker = threading.Thread(
            target=_connect,
            name=f"printer-connect-{bambu_id}",
            daemon=True,
        )
        worker.start()
        if not finished.wait(self._connect_timeout):
            logger.warning("printer %s reconnect to %s timed out; will retry",
                           bambu_id, ip)
            return False
        if error:
            logger.warning("printer %s reconnect to %s failed (%s); will retry",
                           bambu_id, ip, type(error[0]).__name__)
            return False
        return True

    def _worker_cancel_locked(self, bambu_id: str):
        """Caller holds ``self._lock``. The event ``stop`` sets during removal."""
        worker = self._workers.get(bambu_id)
        if worker is None:
            return None
        return worker.cancel_event

    def _wire_defer(self, printer) -> None:
        """Point snapshot's AMS refresh at this serial's worker, when the printer can."""
        hook = getattr(printer, "set_defer", None)
        if not callable(hook):
            return
        serial = printer.bambu_id

        def defer(fn, *args, **kwargs):
            return self.submit(serial, fn, *args, **kwargs)

        hook(defer)

    def _release_reconnect_worker_slot(self, bambu_id: str) -> None:
        """Release one live-worker slot. Caller must hold ``self._lock``."""
        remaining = self._reconnect_worker_counts.get(bambu_id, 0) - 1
        if remaining > 0:
            self._reconnect_worker_counts[bambu_id] = remaining
        else:
            self._reconnect_worker_counts.pop(bambu_id, None)
