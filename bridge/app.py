"""Bridge entrypoint.

Connects the fleet, then loops: read each printer's state, report it to 3DPF,
and act on the desired-state the response carries. Heartbeats on a slower
interval. Run with: `python -m bridge.app config.toml`.
"""

from collections import OrderedDict
import faulthandler
import inspect
import logging
import os
import re
import sys
import threading
import time
from typing import List, Dict, Optional

from . import __version__
from .ams import normalize_hex
from .bambu.commands import live_slot_number_allowed, live_slot_to_tray, tray_index_allowed
from .config import Config, PrinterConfig, load_config
from .discovery_reporter import DiscoveryReporter
from .dpf_client import DpfClient
from .fleet import Fleet
from .printer import BambuPrinter
from .pairing import ensure_paired, maybe_repair
from .reconciler import ConfigReconciler
from .router import ASSIGNMENT_STARTUP_GRACE_SECONDS, Dispatcher, Router
from .send_pipeline import (
    MAX_ATTEMPTS,
    decide,
    discard_attempt,
    failure_latched,
    failure_reason,
    latch_failure,
    load_attempt,
    mark_uploaded,
    printer_is_held,
    ready_for_upload,
    release_settled_attempts,
    save_attempt,
    snapshot_commands_rejected,
    uploaded_already,
)
from .store import PrinterStore
from .updater import SelfUpdater, default_state_path

logger = logging.getLogger(__name__)
AMS_MAPPING_FORMAT = "filament-id-v1"
MAX_LOGICAL_FILAMENT_ID = 256
# Above the report interval. A stuck pass dumps its stacks and the process stays up.
_REPORT_LOOP_DUMP_SECONDS = 30.0
_FILAMENT_FAMILIES = ("PETG", "PLA", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS")
# A legacy marker was written immediately before a physical start. Do not reinterpret it
# as stale residue during the same startup uncertainty window used by the assignment
# tracker, and do not let report + heartbeat in one loop count as two observations.
LEGACY_MARKER_MIN_AGE_SECONDS = ASSIGNMENT_STARTUP_GRACE_SECONDS
LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS = 5.0
LEGACY_READY_OBSERVATION_LIMIT = 256
# MQTT start_print True is not an ack. Callers may still pass
# confirm_wait_seconds; the watchdog decides when the send is confirmed.
CLOUD_SEND_CONFIRM_WAIT_SECONDS = 8.0
STARTED_MARKER_COMMANDED = "commanded"
STARTED_MARKER_CONFIRMED = "confirmed"


class _LegacyMarkerReadiness:
    """Bound, resettable evidence for clearing one ambiguous pre-plate marker."""

    def __init__(
        self,
        monotonic=time.monotonic,
        min_gap_seconds=LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS,
        max_entries=LEGACY_READY_OBSERVATION_LIMIT,
    ):
        self._monotonic = monotonic
        self._min_gap = max(0.0, float(min_gap_seconds))
        self._max_entries = max(1, int(max_entries))
        self._observations = OrderedDict()

    def __len__(self):
        return len(self._observations)

    def reset(self, key) -> None:
        self._observations.pop(key, None)

    def retain(self, keys) -> None:
        keep = set(keys)
        for key in list(self._observations):
            if key not in keep:
                self.reset(key)

    def observe(self, key, marker_identity, ready: bool) -> bool:
        """Return true only on a second ready observation of the same old marker."""
        if not ready:
            self.reset(key)
            return False
        now = float(self._monotonic())
        previous = self._observations.get(key)
        if previous is not None:
            previous_identity, first_ready_at = previous
            if marker_identity == previous_identity and now >= first_ready_at:
                if now - first_ready_at >= self._min_gap:
                    self.reset(key)
                    return True
                self._observations.move_to_end(key)
                return False
        self._observations[key] = (marker_identity, now)
        self._observations.move_to_end(key)
        while len(self._observations) > self._max_entries:
            self._observations.popitem(last=False)
        return False


def _confirm_startup_health(dpf, updater) -> bool:
    """Write the update-healthy marker once this process has reached 3DPF.

    Returns True when the cloud answered. A miss is not fatal: the report loop
    confirms again after the first successful state post.
    """
    try:
        reached = bool(dpf.heartbeat(link=updater.metadata()))
    except Exception:
        logger.info("startup health check could not reach 3DPF yet")
        return False
    if reached:
        updater.confirm_running()
    return reached


def _filament_family(value) -> Optional[str]:
    raw = value.strip().upper() if isinstance(value, str) else ""
    for family in _FILAMENT_FAMILIES:
        if family in raw:
            return family
    return None


def _store_path_for(config_path: str) -> str:
    """Keep the local printer store next to config.toml."""
    directory = os.path.dirname(os.path.abspath(config_path)) or "."
    return os.path.join(directory, "printers.json")


def _merge_printer_configs(from_config: List[PrinterConfig],
                           from_store: List[PrinterConfig]) -> List[PrinterConfig]:
    """Merge the hand-authored config.toml printers with the couriered local store (U4).

    config.toml wins on a serial collision — migration safety, so a stale store never
    overrides a printer the operator still lists by hand — and the store contributes
    every serial config.toml doesn't already have."""
    seen = {c.bambu_id for c in from_config}
    return list(from_config) + [c for c in from_store if c.bambu_id not in seen]


def _start_printhost(cfg: Config, dpf: Optional["DpfClient"] = None) -> Optional[Router]:
    """Start the OctoPrint print-host in a daemon thread if it's configured.

    Returns the shared Router (so the dispatch loop can drain it in U9), or None
    when the bridge runs observability-only. Imported lazily so a bridge without
    a [printhost] block never touches the print-host module.
    """
    if not cfg.printhost:
        return None
    from .printhost import PrintHostService, build_server

    ph = cfg.printhost
    router = Router(ph.queue_path)
    def _forward(file_bytes: bytes, filename: str):
        if dpf is None:
            return None
        row = dpf.enqueue_sliced_file(file_bytes, filename)
        if row:
            return row
        dpf.enqueue_failed_stub(filename, "PrintForce Link could not park this file in 3DPF.")
        return None

    service = PrintHostService(
        upload_key=ph.upload_key,
        spool_dir=ph.spool_dir,
        router=router,
        max_bytes=ph.max_upload_bytes,
        cloud_forward=_forward if dpf is not None else None,
    )
    # Bind + load the cert HERE, in the main thread: a bad cert path or a port
    # already in use raises now and crashes startup loudly, instead of dying
    # silently inside the daemon thread while the bridge keeps heartbeating
    # healthy and every OrcaSlicer upload gets connection-refused.
    httpd = build_server(service, ph.host, ph.port, ph.cert_file, ph.key_file)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="printhost",
        daemon=True,
    )
    thread.start()
    logger.info("print-host listening on https://%s:%s", ph.host, ph.port)
    return router


_OFFLINE_DIAGNOSTIC_AFTER_SECONDS = 300.0


class _OfflineDiagnosticTrigger:
    """One automatic diagnostic per offline spell.

    The clock starts the first time a serial is reported OFFLINE. More than
    five minutes later, one run is queued. Further OFFLINE reports in that
    spell do not queue another. Any other status clears the spell, so the
    next long outage can run again. A worker that is already busy is left
    for this pass: the check waits on the printer, and it is not marked done
    until a worker actually accepts it.
    """

    def __init__(self, monotonic=time.monotonic,
                 offline_after_seconds=_OFFLINE_DIAGNOSTIC_AFTER_SECONDS):
        self._monotonic = monotonic
        self._offline_after = float(offline_after_seconds)
        self._since = {}
        self._ran = set()

    def consider(self, reports, fleet, dpf) -> None:
        now = float(self._monotonic())
        for report in reports or []:
            if not isinstance(report, dict):
                continue
            serial = report.get("bambu_id")
            if not serial:
                continue
            serial = str(serial)
            if report.get("status") != "OFFLINE":
                self._since.pop(serial, None)
                self._ran.discard(serial)
                continue
            since = self._since.get(serial)
            if since is None:
                self._since[serial] = now
                continue
            if serial in self._ran:
                continue
            if now - since <= self._offline_after:
                continue
            busy = getattr(fleet, "worker_busy", None)
            if callable(busy) and busy(serial):
                continue
            by_id = getattr(fleet, "by_id", None)
            printer = by_id(serial) if callable(by_id) else None
            if printer is None:
                continue
            if _queue_diagnose(
                fleet, printer, dpf, serial, None, trigger="auto_offline",
            ):
                self._ran.add(serial)


def _ack_reported_events(fleet, response, reports) -> None:
    """Drop lifecycle events only after ``report_state`` accepts the POST.

    A failed POST returns an empty dict. The same event ids go out on the
    next pass. Acking before the accept would lose the edge.
    """
    if not isinstance(response, dict) or not response:
        return
    ack = getattr(fleet, "ack_events", None)
    if not callable(ack):
        return
    by_printer = {}
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        bambu_id = report.get("bambu_id")
        events = report.get("events")
        if not bambu_id or not isinstance(events, list):
            continue
        ids = [
            event.get("id")
            for event in events
            if isinstance(event, dict) and isinstance(event.get("id"), str) and event.get("id")
        ]
        if ids:
            by_printer[bambu_id] = ids
    if by_printer:
        ack(by_printer)


def _register_persisted_submissions(fleet, router) -> None:
    """Register submission ids loaded from the assignment file.

    Has to happen before the printer's first report of this process. A
    finish seen before the id is registered is classified external, and an
    external finish does not close the batch.
    """
    if router is None or fleet is None:
        return
    snapshot = getattr(router, "assignments_snapshot", None)
    register = getattr(fleet, "register_submission", None)
    if not callable(snapshot) or not callable(register):
        return
    try:
        assignments = snapshot()
    except Exception:
        logger.exception("could not read assignments to register submission ids")
        return
    if not isinstance(assignments, dict):
        return
    for bambu_id, assignment in assignments.items():
        if not isinstance(assignment, dict):
            continue
        submission_id = assignment.get("submission_id")
        if submission_id:
            register(bambu_id, submission_id)


def main(config_path: str = "config.toml") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(config_path)
    logger.info("Loaded %s", cfg)  # __repr__ redacts secrets
    update_restart_lock = threading.Lock()
    updater = SelfUpdater(
        __version__,
        state_path=default_state_path(config_path),
        restart_lock=update_restart_lock,
    )

    store = PrinterStore(_store_path_for(config_path))

    # Cloud credential: config.toml (legacy/hand-authored) OR pairing (U6). On first run
    # the installer passes a one-time pair token in BRIDGE_PAIR_TOKEN; the bridge exchanges
    # it for a durable token and stores it, so nothing is ever pasted into a file.
    pair_token = os.environ.get("BRIDGE_PAIR_TOKEN")
    cloud_token = cfg.cloud_token or ensure_paired(store, cfg.dpf_base_url, pair_token)
    if not cloud_token:
        logger.error(
            "no cloud credential: config.toml has none and pairing did not complete. "
            "Re-issue a pair token in 3DPF (Integrations -> Bambu Bridge) and re-run the "
            "install command, or set cloud_token in config.toml.")
        return

    # Printers come from config.toml (legacy/hand-authored) AND the couriered local
    # store (U4) — the store is how the onboarding wizard's printers reach the bridge
    # without a file edit. On restart the store re-connects everything already onboarded.
    printer_configs = _merge_printer_configs(cfg.printers, store.configs())
    config_dir = os.path.dirname(os.path.abspath(config_path)) or "."
    ams_cache_path = os.path.join(config_dir, "ams-cache.json")
    log_dir = os.path.join(config_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    def make_printer(printer_cfg, stale_after_seconds=None):
        kwargs = {
            "ams_cache_path": ams_cache_path,
            "log_path": _printer_log_path(log_dir, printer_cfg.bambu_id),
        }
        if stale_after_seconds is not None:
            kwargs["stale_after_seconds"] = stale_after_seconds
        return BambuPrinter(printer_cfg, **kwargs)

    fleet = Fleet(
        printer_configs,
        stale_after_seconds=cfg.stale_after_seconds,
        printer_factory=make_printer,
        on_address=store.update_ip,
    )
    dpf = DpfClient(cfg.dpf_base_url, cloud_token)
    # The macOS swap watchdog deletes this build unless update-healthy appears
    # within two minutes. connect_all can sit on a half-open printer socket for
    # that whole window, so reach 3DPF before connecting printers.
    _confirm_startup_health(dpf, updater)
    # Print-host accepts OrcaSlicer uploads and forwards them into the cloud
    # Sliced Queue. Local auto-dispatch is off; start is a cloud send command.
    # The router is loaded before connect_all so a persisted submission id is
    # registered before the printer's first report. A finish that arrives
    # first would otherwise be classified external.
    router = _start_printhost(cfg, dpf)
    dispatcher = None
    if router is not None:
        dispatcher = Dispatcher(router, fleet, dpf)
        router.set_submission_registrar(fleet.register_submission)
        _register_persisted_submissions(fleet, router)
        logger.info("print-host enabled; %d job(s) restored from the queue",
                    len(router.pending()))
    fleet.connect_all()
    reconciler = ConfigReconciler(dpf, fleet, store)
    def _remember_models(found) -> None:
        for printer in found:
            serial = getattr(printer, "serial", None)
            model = getattr(printer, "model", None)
            if serial and model:
                store.set_model(serial, model)
                fleet.set_model(serial, model)

    discovery_reporter = DiscoveryReporter(dpf, on_found=_remember_models)
    logger.info("%d printer(s) at startup (%d from config.toml, %d from the local store)",
                len(printer_configs), len(cfg.printers), len(store.configs()))

    last_heartbeat = 0.0
    last_repair_attempt = None
    started_sends = set()
    applied_controls = set()
    offline_diagnostics = _OfflineDiagnosticTrigger()
    # One readiness object per serial. retain() drops keys this call did not see,
    # so a shared object would forget another printer's legacy-marker observations.
    legacy_marker_readiness = {}
    cloud_send_jobs = {}
    spool_dir = cfg.printhost.spool_dir if cfg.printhost else "/tmp/printforce-spool"
    os.makedirs(spool_dir, exist_ok=True)
    logger.info("Reporting every %ss; heartbeat every %ss; a printer that says nothing "
                "new for %ss is reported OFFLINE",
                cfg.state_interval_seconds, cfg.heartbeat_interval_seconds,
                cfg.stale_after_seconds)
    while True:
        arm_report_loop_dump()
        # The updater downloads concurrently, but its final swap/restart must wait until
        # this iteration has finished every irreversible printer action and durable marker.
        update_restart_lock.acquire()
        try:
            if router is not None:
                _register_persisted_submissions(fleet, router)
            reports = fleet.snapshot()
            wire_reports = (
                router.annotate_reports(reports)
                if router is not None
                else [
                    {**report, "assignment_observed_active": False}
                    for report in reports
                    if isinstance(report, dict)
                ]
            )
            response = dpf.report_state(wire_reports, link=updater.metadata())
            _ack_reported_events(fleet, response, wire_reports)
            last_repair_attempt = maybe_repair(
                dpf,
                store,
                cfg.dpf_base_url,
                pair_token,
                cfg.cloud_token,
                time.monotonic(),
                last_repair_attempt,
            )
            if response:
                updater.confirm_running()
            force_update = updater.apply_cloud_command(
                response.get("update") if isinstance(response, dict) else None
            )
            desired = response.get("printers") if isinstance(response, dict) else None
            # scan_requested (U7): true for a short TTL after the operator's "Add Printer"
            # click (U8) POSTs /api/bridge/scan. Drives discovery_reporter.tick() below —
            # the bridge scans once at startup, then goes quiet, then reopens exactly one
            # bounded burst per request instead of scanning forever.
            scan_requested = bool(response.get("scan_requested")) if isinstance(response, dict) else False
            _apply_desired(
                desired or [], fleet, dpf, spool_dir, started_sends, applied_controls,
                router=router, legacy_marker_readiness=legacy_marker_readiness,
                cloud_send_jobs=cloud_send_jobs,
            )
            # After sends are queued, so a worker already uploading is skipped
            # this pass. One run per offline spell, not one per loop.
            offline_diagnostics.consider(reports, fleet, dpf)
            # After sends are queued: a worker that is mid-upload is busy even
            # while the snapshot still says IDLE, and a restart must not kill it.
            printers_busy = _printers_busy(reports, fleet)
            updater.tick_async(force=force_update, printers_busy=printers_busy)

            # Drain queued uploads onto idle, color-matched printers, matching on THIS
            # pass's fresh reports (the KTD3 dispatch-time re-validation). U9.
            #
            # This runs inline AFTER report_state, so the current pass's state already
            # reached 3DPF before any upload blocks. A dispatch's FTPS upload + MQTT start
            # is synchronous, so a very large upload delays only the NEXT snapshot; typical
            # sliced files are well under the staleness window. If uploads ever grow large
            # enough to risk flapping other printers OFFLINE, move drain() to a worker
            # thread (the Router lock already makes its queue thread-safe). drain() also
            # re-sends any owed dispatch report (a job printing but not yet acked by 3DPF)
            # every pass until it lands, so a blip at report time can't strand the batch.
            # `desired` carries the clear-plate signal (U13): a printer the operator marked
            # cleared comes back with desired_status IDLE, and drain resumes dispatch to it.
            if dispatcher is not None:
                dispatcher.drain(reports, desired or [])

            # Pull any newly-couriered printer config (a printer added in the web wizard),
            # store it, and add it to the running fleet without a restart (U4). Throttled.
            reconciler.tick()

            # Report the printers seen on the LAN so the onboarding wizard can list them
            # (U11). Scans once at startup then goes quiet; scan_requested reopens one
            # bounded on-demand burst (U7). Throttled; code-free.
            discovery_reporter.tick(
                scan_requested=scan_requested, probe_ips=fleet.known_ips(),
            )

            # Self-heal any printer that dropped off the network — re-discover it by
            # serial and reconnect at its new IP if DHCP moved it (U1). Throttled and only
            # when something is actually offline, so a healthy farm pays nothing.
            # A client that already had a session and has been silent for minutes is
            # rebuilt in place when its port still accepts TCP. That does not wait
            # for SSDP and does not replace the printer object.
            fleet.reconcile_connections()
            fleet.recover_dead_sessions()

            now = time.monotonic()
            if now - last_heartbeat >= cfg.heartbeat_interval_seconds:
                heartbeat = dpf.heartbeat(link=updater.metadata())
                if heartbeat:
                    updater.confirm_running()
                force_update = updater.apply_cloud_command(
                    heartbeat.get("update") if isinstance(heartbeat, dict) else None
                )
                heartbeat_desired = (
                    heartbeat.get("printers") if isinstance(heartbeat, dict) else None
                )
                _apply_desired(
                    heartbeat_desired or [], fleet, dpf, spool_dir, started_sends,
                    applied_controls, router=router,
                    legacy_marker_readiness=legacy_marker_readiness,
                    cloud_send_jobs=cloud_send_jobs,
                )
                printers_busy = _printers_busy(reports, fleet)
                updater.tick_async(force=force_update, printers_busy=printers_busy)
                last_heartbeat = now
        except Exception:
            # Never let one bad iteration kill the long-running reporter — nothing
            # supervises/restarts it. Log and keep polling.
            logger.exception("bridge loop iteration failed; continuing")
        finally:
            update_restart_lock.release()

        time.sleep(cfg.state_interval_seconds)


_CONTROL_ACTIONS = frozenset({
    "pause", "resume", "stop", "refresh", "collect_log", "diagnose",
    "gcode_line", "bed_temperature", "nozzle_temperature", "chamber_temperature",
    "print_speed", "fan_speed", "airduct", "home", "move",
    "motors_off", "motors_on", "skip_objects", "select_extruder", "timelapse",
    "calibration", "chamber_light", "drying", "filament_load", "filament_unload",
    "ams_control", "filament_setting", "filament_setting_reset", "extrusion_cali_sel",
    "ignore", "idle_ignore", "clean_print_error",
    "check_assistant", "jump_to_liveview", "cancle",
})


def arm_report_loop_dump(timeout=_REPORT_LOOP_DUMP_SECONDS) -> None:
    """Re-arm the report-loop stack dump. The previous arm is cancelled first.

    The timeout stays above the report interval. ``exit`` is false: a stall
    writes stacks and the process keeps running. Tests call this directly.
    """
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(timeout, exit=False)


def _printers_busy(reports, fleet) -> bool:
    """True when a self-update restart could cut a print or an in-flight send.

    PRINTING and PAUSED are the steady signal. A cloud upload occupies the
    printer worker before the status changes, so a busy worker counts too.
    """
    if any(
        isinstance(report, dict) and report.get("status") in ("PRINTING", "PAUSED")
        for report in reports
    ):
        return True
    busy = getattr(fleet, "worker_busy", None)
    if not callable(busy):
        return False
    for report in reports:
        if not isinstance(report, dict):
            continue
        serial = report.get("bambu_id")
        if serial and busy(serial):
            return True
    return False


# started_sends is one set shared by every printer worker.
_STARTED_SENDS_LOCK = threading.Lock()


def _send_known(started_sends, key) -> bool:
    with _STARTED_SENDS_LOCK:
        return key in started_sends


def _send_mark(started_sends, key) -> None:
    with _STARTED_SENDS_LOCK:
        started_sends.add(key)


def _send_drop(started_sends, key) -> None:
    with _STARTED_SENDS_LOCK:
        started_sends.discard(key)


def _send_keys(started_sends):
    with _STARTED_SENDS_LOCK:
        return list(started_sends)


def _apply_desired(desired: List[Dict], fleet, dpf, spool_dir: str,
                   started_sends, applied_controls, router=None,
                   legacy_marker_readiness=None, cloud_send_jobs=None) -> None:
    """Apply control on this thread, then cloud sends on each printer's worker.

    Controls stay here: publish does not block, and refresh is queued inside
    ``Fleet.apply_control``. Cloud sends do network I/O, so when the fleet has
    ``submit`` each serial runs on its own worker. A serial whose send is still
    queued or running is left for the next pass instead of being queued twice.
    Fakes without ``submit`` keep the single inline call.
    """
    _handle_desired(desired, fleet, applied_controls, spool_dir, router=router, dpf=dpf)
    submit = getattr(fleet, "submit", None)
    if not callable(submit):
        _handle_cloud_sends(
            desired, fleet, dpf, spool_dir, started_sends, router=router,
            legacy_marker_readiness=legacy_marker_readiness,
        )
        return
    if cloud_send_jobs is None:
        cloud_send_jobs = {}
    if not isinstance(legacy_marker_readiness, dict):
        legacy_marker_readiness = {}
    grouped = {}
    order = []
    for row in desired or []:
        if not isinstance(row, dict) or not row.get("bambu_id"):
            continue
        serial = str(row["bambu_id"])
        if serial not in grouped:
            order.append(serial)
            grouped[serial] = []
        grouped[serial].append(row)
    for serial in order:
        inflight = cloud_send_jobs.get(serial)
        if inflight is not None and not inflight.done():
            continue
        readiness = legacy_marker_readiness.get(serial)
        if not isinstance(readiness, _LegacyMarkerReadiness):
            readiness = _LegacyMarkerReadiness()
            legacy_marker_readiness[serial] = readiness
        future = submit(
            serial,
            _handle_cloud_sends,
            grouped[serial],
            fleet,
            dpf,
            spool_dir,
            started_sends,
            router=router,
            legacy_marker_readiness=readiness,
            release_failures=False,
        )
        if future is not None:
            cloud_send_jobs[serial] = future
    # Each worker sees only its own serial. Releasing here, with every send
    # still in desired, is what keeps one printer from clearing another's latch.
    release_settled_attempts(spool_dir, _desired_cloud_send_keys(desired))


def _control_from_row(row: dict):
    control = row.get("control")
    if not isinstance(control, dict):
        return None
    action = control.get("action")
    control_id = control.get("id")
    if action not in _CONTROL_ACTIONS or not control_id:
        return None
    control_out = {"id": str(control_id), "action": action}
    for key, value in control.items():
        if key in ("id", "action"):
            continue
        control_out[key] = value
    return control_out


def _row_has_stop(row: dict) -> bool:
    control = _control_from_row(row)
    return control is not None and control["action"] == "stop"


def _desired_allows_send(desired: List[Dict], bambu_id: str, batch_id: str,
                         plate_index: int = 1, item_id=None) -> bool:
    """True only when a fresh desired-state still authorizes this exact send."""
    return _authorized_send(
        desired, bambu_id, batch_id, plate_index=plate_index, item_id=item_id,
    ) is not None


def _authorized_send(desired: List[Dict], bambu_id: str, batch_id: str,
                     plate_index: int = 1, item_id=None) -> Optional[dict]:
    """Return the fresh exact send command, or None when authorization changed."""
    for row in desired:
        if not isinstance(row, dict) or str(row.get("bambu_id") or "") != str(bambu_id):
            continue
        if _row_has_stop(row) or str(row.get("desired_status") or "IDLE") != "IDLE":
            return None
        send = row.get("send")
        fresh_item_id = send.get("item_id") if isinstance(send, dict) else None
        if (
            isinstance(send, dict)
            and str(send.get("batch_id") or "") == str(batch_id)
            and int(send.get("plate_index") or 1) == int(plate_index)
            and (not item_id or not fresh_item_id or str(fresh_item_id) == str(item_id))
        ):
            return send
        return None
    return None


def _handle_desired(desired: List[Dict], fleet=None, applied_controls=None,
                    spool_dir: Optional[str] = None, router=None, dpf=None) -> None:
    """Act on the authoritative desired-state 3DPF returns.

    `control` is one-shot: the same id is published once, then remembered like
    `started_sends`. Unknown keys are ignored so an old agent does not crash.
    """
    if applied_controls is None:
        applied_controls = set()
    for row in desired:
        if not isinstance(row, dict):
            continue
        logger.debug("desired-state: %s -> %s", row.get("bambu_id"), row.get("desired_status"))
        if fleet is None:
            continue
        control = _control_from_row(row)
        bambu_id = row.get("bambu_id")
        if control is None or not bambu_id:
            continue
        _apply_control(
            fleet, str(bambu_id), control, applied_controls, spool_dir, router, dpf=dpf,
        )


def _printer_log_path(log_dir: str, serial: str) -> str:
    """One jsonl file per serial. The serial is a path segment, so it is sanitized."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(serial))[:128] or "printer"
    return os.path.join(log_dir, f"printer-{safe}.jsonl")


def _log_upload_accepted(result) -> bool:
    return isinstance(result, dict) and bool(result)


def _queue_collect_log(fleet, printer, dpf, bambu_id: str, control_id: str) -> bool:
    """Upload off the report loop.

    A queued POST counts as applied, the same way refresh does: this pass must
    not wait on 3DPF. A printer with no ``collect_log`` is a no-op. No uploader,
    or an empty response, leaves the id unmarked so the next pass retries.
    """
    collect = getattr(printer, "collect_log", None)
    if not callable(collect):
        logger.warning("printer %s: collect_log is not available", bambu_id)
        return True
    try:
        payload = collect()
    except Exception:
        logger.exception(
            "printer %s: collect_log failed; will retry this control.id", bambu_id,
        )
        return False
    upload = getattr(dpf, "upload_printer_log", None) if dpf is not None else None
    if not callable(upload):
        logger.warning(
            "printer %s: collect_log has nowhere to upload; will retry", bambu_id,
        )
        return False

    def _send():
        return upload(bambu_id, payload, control_id=control_id)

    submit = getattr(fleet, "submit", None)
    if callable(submit):
        future = submit(bambu_id, _send)
        if future is None or future.cancelled():
            return False
        if future.done():
            if future.exception() is not None:
                return False
            return _log_upload_accepted(future.result())
        return True
    try:
        result = _send()
    except Exception:
        logger.exception(
            "printer %s: collect_log upload failed; will retry this control.id",
            bambu_id,
        )
        return False
    return _log_upload_accepted(result)


def _diagnostic_posted(result) -> bool:
    return isinstance(result, dict) and bool(result)


def _queue_diagnose(fleet, printer, dpf, bambu_id: str, control_id, *, trigger) -> bool:
    """Run the check off the report loop, then POST it.

    The MQTT windows alone can take 30 seconds. Doing that here would stall
    every other printer. A queued run counts as applied, the same way
    collect_log's upload does: this pass must not wait on the printer or on
    3DPF. No reporter, or an empty response from an inline call, leaves the
    id unmarked so the next pass retries.
    """
    report = getattr(dpf, "report_diagnostic", None) if dpf is not None else None
    if not callable(report):
        logger.warning(
            "printer %s: diagnose has nowhere to report; will retry", bambu_id,
        )
        return False
    diagnose = getattr(printer, "diagnose", None)
    if not callable(diagnose):
        logger.warning("printer %s: diagnose is not available", bambu_id)
        return True

    def _run():
        return report(bambu_id, diagnose(trigger=trigger), control_id=control_id)

    submit = getattr(fleet, "submit", None)
    if callable(submit):
        future = submit(bambu_id, _run)
        if future is None or future.cancelled():
            return False
        if future.done():
            if future.exception() is not None:
                return False
            return _diagnostic_posted(future.result())
        return True
    try:
        result = _run()
    except Exception:
        logger.exception(
            "printer %s: diagnose failed; will retry this control.id", bambu_id,
        )
        return False
    return _diagnostic_posted(result)


def _apply_control(fleet, bambu_id: str, control: dict, applied_controls,
                   spool_dir: Optional[str], router, dpf=None) -> None:
    control_id = control["id"]
    marker = (
        os.path.join(spool_dir, f"control-{control_id}.applied")
        if spool_dir else None
    )
    if control_id in applied_controls or (marker and os.path.exists(marker)):
        applied_controls.add(control_id)
        return
    printer = fleet.by_id(bambu_id) if hasattr(fleet, "by_id") else None
    if printer is None:
        logger.warning("control %s for unknown printer %s", control["action"], bambu_id)
        return
    action = control["action"]
    if action == "collect_log":
        if not _queue_collect_log(fleet, printer, dpf, bambu_id, control_id):
            return
    elif action == "diagnose":
        if not _queue_diagnose(
            fleet, printer, dpf, bambu_id, control_id, trigger="operator",
        ):
            return
    else:
        result = None
        params = {
            key: value for key, value in control.items() if key not in ("id", "action")
        }
        try:
            if callable(getattr(fleet, "apply_control", None)):
                result = fleet.apply_control(bambu_id, action, params)
            elif action == "pause":
                result = printer.pause_print()
            elif action == "resume":
                result = (
                    printer.resume_from_stage()
                    if hasattr(printer, "resume_from_stage")
                    else printer.resume_print()
                )
            elif action == "stop":
                result = printer.stop_print()
            elif action == "refresh":
                result = (
                    printer.request_full_status()
                    if hasattr(printer, "request_full_status")
                    else False
                )
        except Exception:
            logger.exception("printer %s: %s failed; will retry this control.id",
                             bambu_id, action)
            return
        if result is False:
            logger.warning("printer %s: %s was not published; will retry this control.id",
                           bambu_id, action)
            return
        if action == "stop" and router is not None and hasattr(router, "clear_assignment"):
            router.clear_assignment(bambu_id)
    applied_controls.add(control_id)
    if marker:
        try:
            with open(marker, "w"):
                pass
        except OSError:
            pass


def _desired_cloud_send_keys(desired) -> set:
    """Send keys the cloud is still asking for, on every printer."""
    live = set()
    for row in desired or []:
        if not isinstance(row, dict):
            continue
        send = row.get("send")
        if not isinstance(send, dict):
            continue
        bambu_id = row.get("bambu_id")
        batch_id = send.get("batch_id")
        if not bambu_id or not batch_id:
            continue
        live.add((str(batch_id), str(bambu_id), int(send.get("plate_index") or 1)))
    return live


def _handle_cloud_sends(desired: List[Dict], fleet, dpf, spool_dir: str,
                       started_sends=None, router=None,
                       legacy_marker_readiness=None,
                       wall_time=time.time,
                       confirm_wait_seconds=CLOUD_SEND_CONFIRM_WAIT_SECONDS,
                       sleep_fn=time.sleep,
                       release_failures: bool = True) -> None:
    """Start a print only when the cloud Sliced Queue says so.

    MQTT publish True is not a physical start. DISPATCHED is reported only after
    the printer snapshot shows PRINTING or PAUSED. A commanded send stays in the
    watchdog until an active snapshot confirms it or the attempt budget fails it.
    """
    import os
    if started_sends is None:
        started_sends = set()
    if legacy_marker_readiness is None:
        legacy_marker_readiness = _LegacyMarkerReadiness()
    live = set()
    pending_legacy_markers = set()
    seen_serials = {
        str(row.get("bambu_id"))
        for row in desired
        if isinstance(row, dict) and row.get("bambu_id")
    }
    for row in desired:
        if not isinstance(row, dict):
            continue
        send = row.get("send")
        if not isinstance(send, dict):
            continue
        bambu_id = row.get("bambu_id")
        batch_id = send.get("batch_id")
        file_url = send.get("file_url")
        plate_index = int(send.get("plate_index") or 1)
        if not bambu_id or not batch_id:
            continue
        key = (str(batch_id), str(bambu_id), plate_index)
        live.add(key)
        if failure_latched(_cloud_send_started_path(spool_dir, key)):
            continue
        dest = os.path.join(spool_dir, f"{batch_id}.3mf")
        started_path = _cloud_send_started_path(spool_dir, key)
        legacy_started_path = dest + ".started"
        assignment_matches = _router_assignment_matches(router, key)
        if os.path.exists(legacy_started_path):
            migration_key = _router_assignment_key_for_batch(router, str(batch_id))
            try:
                if migration_key is not None:
                    migration_path = _cloud_send_started_path(spool_dir, migration_key)
                    if os.path.exists(migration_path):
                        os.unlink(legacy_started_path)
                    else:
                        os.replace(legacy_started_path, migration_path)
            except OSError:
                pass
            if os.path.exists(legacy_started_path) and not os.path.exists(started_path):
                pending_legacy_markers.add(key)
                live_snapshot = (
                    _live_snapshot(fleet, str(bambu_id))
                    if migration_key is None
                    else None
                )
                try:
                    marker_stat = os.stat(legacy_started_path)
                    marker_age = float(wall_time()) - marker_stat.st_mtime
                    marker_identity = (
                        getattr(marker_stat, "st_mtime_ns", marker_stat.st_mtime),
                        getattr(marker_stat, "st_ctime_ns", marker_stat.st_ctime),
                    )
                except (OSError, TypeError, ValueError):
                    marker_age = -1.0
                    marker_identity = None
                ready_observation = (
                    migration_key is None
                    and marker_identity is not None
                    and marker_age >= LEGACY_MARKER_MIN_AGE_SECONDS
                    and _legacy_marker_snapshot_allows_start(live_snapshot)
                )
                if legacy_marker_readiness.observe(
                    key, marker_identity, ready_observation,
                ):
                    try:
                        os.unlink(legacy_started_path)
                    except OSError:
                        logger.warning(
                            "cloud send %s: could not clear safe legacy start marker; "
                            "not starting",
                            batch_id,
                        )
                        continue
            else:
                legacy_marker_readiness.reset(key)
            if os.path.exists(legacy_started_path):
                logger.warning(
                    "cloud send %s: legacy start marker is ambiguous and live "
                    "snapshot does not prove the printer is physically idle; "
                    "not starting",
                    batch_id,
                )
                continue
        if _send_known(started_sends, key) or os.path.exists(started_path) or assignment_matches:
            _send_mark(started_sends, key)
            if _row_has_stop(row):
                logger.info("cloud send %s: stop during confirmation; abandoning", batch_id)
                _cancel_cloud_send(spool_dir, key, started_sends, router)
                continue
            if router is not None and not assignment_matches:
                router.record_assignment(
                    str(bambu_id), str(batch_id), plate_index,
                    started_at=_cloud_send_started_at(started_path, wall_time),
                )
            _advance_cloud_send(
                key, send, fleet, dpf, spool_dir, started_sends, router, wall_time,
            )
            continue
        if _row_has_stop(row):
            logger.info("cloud send %s: live stop; not starting", batch_id)
            continue
        if str(row.get("desired_status") or "IDLE") != "IDLE":
            continue
        snapshot = _live_snapshot(fleet, str(bambu_id))
        if not ready_for_upload(snapshot):
            logger.warning(
                "cloud send %s: printer %s is not idle and live; not uploading",
                batch_id, bambu_id,
            )
            if snapshot_commands_rejected(snapshot):
                _fail_cloud_send(
                    key, dpf, spool_dir, started_sends, router, "commands_rejected",
                )
            continue
        if printer_is_held(_send_keys(started_sends), bambu_id, key, live):
            logger.info(
                "cloud send %s: printer %s already has a send in progress",
                batch_id, bambu_id,
            )
            continue
        ams_mapping = _resolve_cloud_ams_mapping(send, fleet, bambu_id)
        if ams_mapping is None:
            logger.warning(
                "cloud send %s: invalid or incomplete AMS mapping; not starting",
                batch_id,
            )
            continue
        if not os.path.exists(dest):
            tmp = dest + ".part"
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                if not dpf.download_url(file_url, tmp):
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    logger.warning("could not download send file for batch %s", batch_id)
                    continue
                os.replace(tmp, dest)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                logger.warning("could not download send file for batch %s", batch_id)
                continue
        remote_name = _cloud_remote_name(send)
        uploaded = None
        if hasattr(fleet, "upload") and hasattr(fleet, "start_print"):
            if uploaded_already(started_path):
                uploaded = remote_name or os.path.basename(dest)
            else:
                uploaded = fleet.upload(bambu_id, dest, remote_name=remote_name)
                mark_uploaded(started_path)
            latest = dpf.heartbeat() if hasattr(dpf, "heartbeat") else {}
            latest_rows = latest.get("printers") if isinstance(latest, dict) else None
            fresh_send = _authorized_send(
                latest_rows or [],
                bambu_id,
                batch_id,
                plate_index=plate_index,
                item_id=send.get("item_id"),
            )
            if fresh_send is None:
                logger.warning(
                    "cloud send %s: fresh desired-state no longer authorizes start; "
                    "leaving the uploaded file idle",
                    batch_id,
                )
                continue
            rematch = _resolve_cloud_ams_mapping(fresh_send, fleet, bambu_id)
            if rematch is not None:
                ams_mapping = rematch
            if ams_mapping is None:
                logger.warning(
                    "cloud send %s: live AMS changed after upload; "
                    "leaving the uploaded file idle",
                    batch_id,
                )
                continue
            started = _mqtt_start_print(
                fleet, bambu_id, uploaded or remote_name or os.path.basename(dest),
                ams_mapping, plate_index,
            )
        else:
            started = fleet.dispatch(
                bambu_id, dest, ams_mapping, plate_index,
                remote_name=remote_name,
            )
        if started:
            _send_mark(started_sends, key)
            try:
                _write_cloud_send_marker(started_path, STARTED_MARKER_COMMANDED)
            except OSError:
                pass
            submission_id = _submission_on_printer(fleet, bambu_id)
            if router is not None:
                _record_cloud_assignment(
                    router, str(bambu_id), str(batch_id), plate_index,
                    started_at=float(wall_time()), submission_id=submission_id,
                )
            live_snap = _live_snapshot(fleet, str(bambu_id))
            _remember_attempt(
                started_path, router, bambu_id, wall_time,
                submission_id=submission_id, uploaded=True,
                gcode_file=live_snap.get("gcode_file") if isinstance(live_snap, dict) else None,
            )
            if _snapshot_shows_active(live_snap):
                _report_confirmed_dispatch(key, dpf, spool_dir, router)
        else:
            logger.warning("printer %s did not start batch %s", bambu_id, batch_id)
    for key in _send_keys(started_sends):
        _batch_id, bambu_id, _plate_index = key
        if bambu_id in seen_serials and key not in live:
            _send_drop(started_sends, key)
            leftover = _cloud_send_started_path(spool_dir, key)
            try:
                os.unlink(leftover)
            except OSError:
                pass
            discard_attempt(leftover)
    _cleanup_orphaned_cloud_send_markers(spool_dir, live, seen_serials)
    if release_failures:
        release_settled_attempts(spool_dir, live)
    legacy_marker_readiness.retain(pending_legacy_markers)


def _cloud_send_started_path(spool_dir: str, key) -> str:
    batch_id, bambu_id, plate_index = key
    safe_batch = re.sub(r"[^A-Za-z0-9._-]", "_", str(batch_id))[:128] or "batch"
    safe_printer = re.sub(r"[^A-Za-z0-9._-]", "_", str(bambu_id))[:128] or "printer"
    return os.path.join(
        spool_dir,
        f"cloud-send-{safe_batch}-{safe_printer}-plate-{int(plate_index)}.started",
    )


def _snapshot_shows_active(snapshot) -> bool:
    return isinstance(snapshot, dict) and snapshot.get("status") in ("PRINTING", "PAUSED")


def _write_cloud_send_marker(path: str, state: str) -> None:
    with open(path, "w") as handle:
        handle.write(state)


def _cloud_send_marker_state(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _cloud_send_started_at(started_path: str, wall_time) -> float:
    try:
        return float(os.stat(started_path).st_mtime)
    except (OSError, TypeError, ValueError):
        return float(wall_time())


def _assignment_observed_active(router, bambu_id: str) -> bool:
    if router is None or not callable(getattr(router, "assignments_snapshot", None)):
        return False
    assignment = router.assignments_snapshot().get(str(bambu_id))
    return isinstance(assignment, dict) and assignment.get("observed_active") is True


def _cloud_send_already_confirmed(started_path: str, router, bambu_id: str) -> bool:
    return (
        _cloud_send_marker_state(started_path) == STARTED_MARKER_CONFIRMED
        or _assignment_observed_active(router, bambu_id)
    )


def _report_confirmed_dispatch(key, dpf, spool_dir: str, router) -> None:
    batch_id, bambu_id, _plate_index = key
    if router is not None and hasattr(router, "mark_assignment_active"):
        router.mark_assignment_active(str(bambu_id))
    try:
        _write_cloud_send_marker(
            _cloud_send_started_path(spool_dir, key), STARTED_MARKER_CONFIRMED,
        )
    except OSError:
        pass
    dpf.report_dispatched(batch_id, bambu_id)


def _clear_pending_cloud_send(spool_dir: str, key, started_sends, router) -> None:
    _batch_id, bambu_id, _plate_index = key
    _send_drop(started_sends, key)
    leftover = _cloud_send_started_path(spool_dir, key)
    try:
        os.unlink(leftover)
    except OSError:
        pass
    discard_attempt(leftover)
    if router is not None and _router_assignment_matches(router, key):
        clearer = getattr(router, "clear_assignment", None)
        if callable(clearer):
            clearer(str(bambu_id))


def _cancel_cloud_send(spool_dir: str, key, started_sends, router) -> None:
    """Drop a send the operator stopped or removed. No failure is reported."""
    _clear_pending_cloud_send(spool_dir, key, started_sends, router)


def _fail_cloud_send(key, dpf, spool_dir, started_sends, router, reason: str) -> None:
    batch_id, _bambu_id, plate_index = key
    report_failed = getattr(dpf, "report_failed", None)
    if callable(report_failed):
        acked = report_failed(batch_id, plate_index, reason=reason)
        if not isinstance(acked, dict) or not acked:
            return
    _clear_pending_cloud_send(spool_dir, key, started_sends, router)
    latch_failure(_cloud_send_started_path(spool_dir, key), key, reason)


def _submission_on_printer(fleet, bambu_id: str):
    by_id = getattr(fleet, "by_id", None)
    printer = by_id(bambu_id) if callable(by_id) else None
    if printer is None:
        return None
    return getattr(printer, "last_submission_id", None)


def _record_cloud_assignment(router, bambu_id, batch_id, plate_index, *,
                             started_at, submission_id) -> None:
    kwargs = {
        "started_at": started_at,
    }
    try:
        params = inspect.signature(router.record_assignment).parameters
    except (TypeError, ValueError):
        params = None
    accepts_submission = params is not None and (
        "submission_id" in params
        or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())
    )
    if params is None:
        try:
            router.record_assignment(
                bambu_id, batch_id, plate_index, submission_id=submission_id, **kwargs,
            )
        except TypeError:
            router.record_assignment(bambu_id, batch_id, plate_index, **kwargs)
        return
    if accepts_submission:
        router.record_assignment(
            bambu_id, batch_id, plate_index, submission_id=submission_id, **kwargs,
        )
        return
    router.record_assignment(bambu_id, batch_id, plate_index, **kwargs)


def _remember_attempt(started_path, router, bambu_id, wall_time, *,
                      submission_id, uploaded, gcode_file=None) -> None:
    """Store this send, including the file name on the printer before it starts.

    A later republish writes the attempt without calling this, so the pre-send
    name is what phase A compares.
    """
    now = float(wall_time())
    record = load_attempt(started_path, router, str(bambu_id), now)
    record["phase"] = "A"
    record["phase_started_at"] = now
    record["attempts"] = int(record.get("attempts") or 1)
    record["uploaded"] = True if uploaded else record.get("uploaded")
    if submission_id is not None:
        record["submission_id"] = submission_id
    record.setdefault("last_failure", None)
    record["gcode_file"] = gcode_file
    save_attempt(started_path, router, str(bambu_id), record)


def _hard_reset_printer(fleet, bambu_id: str) -> None:
    by_id = getattr(fleet, "by_id", None)
    printer = by_id(bambu_id) if callable(by_id) else None
    session = getattr(printer, "_session", None) if printer is not None else None
    for candidate in (
        getattr(session, "hard_reset", None),
        getattr(printer, "hard_reset", None),
        getattr(printer, "rebuild_session", None),
        getattr(fleet, "hard_reset", None),
    ):
        if callable(candidate):
            try:
                candidate()
            except Exception:
                logger.exception("printer %s: hard reset failed", bambu_id)
            return


def _cloud_send_session_connected(fleet, bambu_id: str) -> bool:
    """True when a republish may publish.

    No printer, or a printer with no session, counts as connected so a fake
    publishes on the republish pass. A real session waits until it is connected.
    """
    by_id = getattr(fleet, "by_id", None)
    printer = by_id(bambu_id) if callable(by_id) else None
    if printer is None:
        return True
    session = getattr(printer, "_session", None)
    if session is None:
        return True
    connected = getattr(session, "connected", False)
    if callable(connected):
        connected = connected()
    return bool(connected)


def _republish_start(send, fleet, bambu_id: str, dest: str, plate_index: int) -> bool:
    if not hasattr(fleet, "start_print"):
        return False
    ams_mapping = _resolve_cloud_ams_mapping(send, fleet, bambu_id)
    if ams_mapping is None:
        return False
    remote_name = _cloud_remote_name(send)
    return bool(_mqtt_start_print(
        fleet, bambu_id, remote_name or os.path.basename(dest),
        ams_mapping, plate_index,
    ))


def _advance_cloud_send(key, send, fleet, dpf, spool_dir, started_sends, router,
                        wall_time) -> None:
    """Move one in-flight send through the watchdog. Does not upload again."""
    batch_id, bambu_id, plate_index = key
    started_path = _cloud_send_started_path(spool_dir, key)
    if _cloud_send_already_confirmed(started_path, router, bambu_id):
        dpf.report_dispatched(batch_id, bambu_id)
        return
    now = float(wall_time())
    snapshot = _live_snapshot(fleet, str(bambu_id))
    record = load_attempt(started_path, router, str(bambu_id), now)
    action = decide(record, snapshot, now)
    if action == "confirm":
        _report_confirmed_dispatch(key, dpf, spool_dir, router)
        return
    if action == "enter_b":
        record["phase"] = "B"
        record["phase_started_at"] = now
        record["last_failure"] = None
        save_attempt(started_path, router, str(bambu_id), record)
        return
    if action == "reset_retry":
        if record.get("pending_republish"):
            record["attempts"] = int(record.get("attempts") or 1) + 1
            record["last_failure"] = "no_echo"
            if int(record["attempts"]) >= MAX_ATTEMPTS:
                save_attempt(started_path, router, str(bambu_id), record)
                _fail_cloud_send(
                    key, dpf, spool_dir, started_sends, router,
                    failure_reason(record, snapshot),
                )
                return
        _hard_reset_printer(fleet, bambu_id)
        record["pending_republish"] = True
        record["last_failure"] = "no_echo"
        record["phase"] = "A"
        record["phase_started_at"] = now
        save_attempt(started_path, router, str(bambu_id), record)
        return
    if action == "republish":
        if not _cloud_send_session_connected(fleet, bambu_id):
            return
        dest = os.path.join(spool_dir, f"{batch_id}.3mf")
        if not _republish_start(send, fleet, bambu_id, dest, plate_index):
            return
        record["pending_republish"] = False
        record["attempts"] = int(record.get("attempts") or 1) + 1
        record["phase"] = "A"
        record["phase_started_at"] = now
        fresh = _submission_on_printer(fleet, bambu_id)
        if fresh is not None:
            record["submission_id"] = fresh
        record["uploaded"] = True
        save_attempt(started_path, router, str(bambu_id), record)
        return
    if action == "retry":
        record["last_failure"] = "no_active"
        dest = os.path.join(spool_dir, f"{batch_id}.3mf")
        if not _republish_start(send, fleet, bambu_id, dest, plate_index):
            _fail_cloud_send(
                key, dpf, spool_dir, started_sends, router,
                failure_reason(record, snapshot),
            )
            return
        record["attempts"] = int(record.get("attempts") or 1) + 1
        record["phase"] = "A"
        record["phase_started_at"] = now
        fresh = _submission_on_printer(fleet, bambu_id)
        if fresh is not None:
            record["submission_id"] = fresh
        record["uploaded"] = True
        save_attempt(started_path, router, str(bambu_id), record)
        return
    if action == "fail":
        logger.warning(
            "cloud send %s: printer %s did not start after %s attempts",
            batch_id, bambu_id, record.get("attempts"),
        )
        _fail_cloud_send(
            key, dpf, spool_dir, started_sends, router,
            failure_reason(record, snapshot),
        )


def _router_assignment_matches(router, key) -> bool:
    if router is None or not callable(getattr(router, "assignments_snapshot", None)):
        return False
    batch_id, bambu_id, plate_index = key
    assignment = router.assignments_snapshot().get(bambu_id)
    if not isinstance(assignment, dict):
        return False
    try:
        assigned_plate = int(assignment.get("plate_number") or 1)
    except (TypeError, ValueError):
        return False
    return (
        str(assignment.get("batch_id") or "") == batch_id
        and assigned_plate == plate_index
    )


def _router_assignment_key_for_batch(router, batch_id: str):
    if router is None or not callable(getattr(router, "assignments_snapshot", None)):
        return None
    matches = []
    for bambu_id, assignment in router.assignments_snapshot().items():
        if (
            not isinstance(assignment, dict)
            or str(assignment.get("batch_id") or "") != batch_id
        ):
            continue
        try:
            plate_index = int(assignment.get("plate_number") or 1)
        except (TypeError, ValueError):
            continue
        matches.append((batch_id, str(bambu_id), plate_index))
    return matches[0] if len(matches) == 1 else None


def _cleanup_orphaned_cloud_send_markers(spool_dir: str, live, seen_serials) -> None:
    """Remove durable markers only for printers covered by this desired-state."""
    if not seen_serials:
        return
    live_paths = {
        os.path.abspath(_cloud_send_started_path(spool_dir, key))
        for key in live
    }
    printer_suffixes = {
        f"-{re.sub(r'[^A-Za-z0-9._-]', '_', serial)[:128] or 'printer'}-plate-"
        for serial in seen_serials
    }
    try:
        entries = list(os.scandir(spool_dir))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if (
            not name.startswith("cloud-send-")
            or not name.endswith(".started")
            or os.path.abspath(entry.path) in live_paths
            or not any(
                re.search(re.escape(suffix) + r"\d+\.started\Z", name)
                for suffix in printer_suffixes
            )
        ):
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            pass


def _cloud_remote_name(send: dict):
    raw = send.get("filename")
    if not isinstance(raw, str):
        return None
    from .printhost import sanitize_upload_filename
    return sanitize_upload_filename(raw)


def _required_filaments(send: dict):
    if send.get("ams_mapping_format") != AMS_MAPPING_FORMAT:
        return None
    raw = send.get("required_filaments")
    if not isinstance(raw, list) or not raw:
        return None
    required = []
    seen_ids = set()
    for filament in raw:
        if not isinstance(filament, dict):
            return None
        filament_id = filament.get("filament_id")
        color = normalize_hex(filament.get("hex"))
        family = _filament_family(filament.get("family"))
        if (
            isinstance(filament_id, bool)
            or not isinstance(filament_id, int)
            or filament_id < 1
            or filament_id > MAX_LOGICAL_FILAMENT_ID
            or filament_id in seen_ids
            or color is None
            or family is None
        ):
            return None
        seen_ids.add(filament_id)
        required.append((filament_id, color, family))
    return required


def _validate_sparse_ams_mapping(value, required):
    expected_length = max((filament_id for filament_id, _color, _family in required), default=0)
    if not isinstance(value, list) or len(value) != expected_length:
        return None
    required_positions = {filament_id - 1 for filament_id, _color, _family in required}
    mapping = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not tray_index_allowed(item):
            return None
        if index in required_positions:
            if item < 0:
                return None
        elif item != -1:
            return None
        mapping.append(item)
    return mapping


def _mapping_from_live_slots(required, live):
    slots = live.get("slots") if isinstance(live, dict) else None
    if not isinstance(slots, list):
        return None
    required_colors = {color for _filament_id, color, _family in required}
    candidates = {}
    for slot in slots:
        if not isinstance(slot, dict):
            return None
        color = normalize_hex(slot.get("color_hex"))
        if color not in required_colors:
            continue
        family = _filament_family(slot.get("filament_type"))
        slot_number = slot.get("slot_number")
        if family is None or not live_slot_number_allowed(slot_number):
            return None
        candidates.setdefault((color, family), []).append(live_slot_to_tray(slot_number))
    mapping = [-1] * max(
        (filament_id for filament_id, _color, _family in required),
        default=0,
    )
    for filament_id, color, family in required:
        trays = candidates.get((color, family)) or []
        if len(trays) != 1:
            return None
        mapping[filament_id - 1] = trays[0]
    return _validate_sparse_ams_mapping(mapping, required)


def _live_snapshot(fleet, bambu_id: str):
    by_id = getattr(fleet, "by_id", None)
    if not callable(by_id):
        return None
    try:
        printer = by_id(bambu_id)
    except Exception:
        return {}
    if printer is None:
        return None
    if not hasattr(printer, "snapshot"):
        return {}
    try:
        snap = printer.snapshot()
    except Exception:
        return {}
    return snap if isinstance(snap, dict) else {}


def _strict_zero(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value == 0
    )


def _legacy_marker_snapshot_allows_start(snapshot) -> bool:
    """True only for fresh live proof that no physical print can be in progress."""
    if not isinstance(snapshot, dict):
        return False
    status = snapshot.get("status")
    if status in ("PRINTING", "PAUSED", "NEEDS_CLEARING", "OFFLINE"):
        return False
    if status != "IDLE" and snapshot.get("historical_failed_ready") is not True:
        return False
    return (
        snapshot.get("has_active_file") is False
        and snapshot.get("has_active_task") is False
        and snapshot.get("has_active_project") is False
        and _strict_zero(snapshot.get("progress_percent"))
        and _strict_zero(snapshot.get("nozzle_target_temper"))
        and _strict_zero(snapshot.get("bed_target_temper"))
        and snapshot.get("hms_empty") is True
    )


def _mqtt_start_print(fleet, bambu_id, remote_name, ams_mapping, plate_index):
    """Publish the start. IDLE, FINISH, and FAILED need no stop first."""
    return fleet.start_print(bambu_id, remote_name, ams_mapping, plate_index)


def _resolve_cloud_ams_mapping(send: dict, fleet, bambu_id: str) -> Optional[list]:
    """Validate the sparse contract. Remap from live slots when a unit list exists.

    `slots is None` is Link's "no AMS unit list this cycle". That is not a tray
    disagreement. Use the already-validated cloud mapping so upload-then-start
    still fires. A live list that uniquely remaps wins. If that list cannot
    uniquely bind, use the cloud mapping. A malformed `slots` value and a
    broken snapshot with no `slots` key still fail closed.
    """
    logical_required = _required_filaments(send)
    if logical_required is None:
        return None
    validated = _validate_sparse_ams_mapping(send.get("ams_mapping"), logical_required)
    if validated is None:
        return None
    live = _live_snapshot(fleet, bambu_id)
    if live is None:
        return validated
    if not isinstance(live, dict) or "slots" not in live:
        return None
    slots = live.get("slots")
    if slots is None:
        return validated
    if not isinstance(slots, list):
        return None
    live_mapping = _mapping_from_live_slots(logical_required, live)
    if live_mapping is not None:
        return live_mapping
    return validated


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
