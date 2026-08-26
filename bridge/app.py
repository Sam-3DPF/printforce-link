"""Bridge entrypoint.

Connects the fleet, then loops: read each printer's state, report it to 3DPF,
and act on the desired-state the response carries. Heartbeats on a slower
interval. Run with: `python -m bridge.app config.toml`.
"""

import logging
import os
import sys
import threading
import time
from typing import List, Dict, Optional

from . import __version__
from .ams import normalize_hex
from .config import Config, PrinterConfig, load_config
from .discovery_reporter import DiscoveryReporter
from .dpf_client import DpfClient
from .fleet import Fleet
from .pairing import ensure_paired
from .reconciler import ConfigReconciler
from .router import Dispatcher, Router
from .store import PrinterStore
from .updater import SelfUpdater, default_state_path

logger = logging.getLogger(__name__)
AMS_MAPPING_FORMAT = "filament-id-v1"
MAX_LOGICAL_FILAMENT_ID = 256
MAX_BAMBU_AMS_TRAY_INDEX = 15
_FILAMENT_FAMILIES = ("PETG", "PLA", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS")


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
    fleet = Fleet(printer_configs, stale_after_seconds=cfg.stale_after_seconds)
    fleet.connect_all()
    dpf = DpfClient(cfg.dpf_base_url, cloud_token)
    reconciler = ConfigReconciler(dpf, fleet, store)
    discovery_reporter = DiscoveryReporter(dpf)
    logger.info("%d printer(s) at startup (%d from config.toml, %d from the local store)",
                len(printer_configs), len(cfg.printers), len(store.configs()))

    # Print-host accepts OrcaSlicer uploads and forwards them into the cloud
    # Sliced Queue. Local auto-dispatch is off; start is a cloud send command.
    router = _start_printhost(cfg, dpf)
    dispatcher = None
    if router is not None:
        dispatcher = Dispatcher(router, fleet, dpf)
        logger.info("print-host enabled; %d job(s) restored from the queue",
                    len(router.pending()))

    last_heartbeat = 0.0
    started_sends = set()
    applied_controls = set()
    spool_dir = cfg.printhost.spool_dir if cfg.printhost else "/tmp/printforce-spool"
    os.makedirs(spool_dir, exist_ok=True)
    logger.info("Reporting every %ss; heartbeat every %ss; a printer that says nothing "
                "new for %ss is reported OFFLINE",
                cfg.state_interval_seconds, cfg.heartbeat_interval_seconds,
                cfg.stale_after_seconds)
    while True:
        # The updater downloads concurrently, but its final swap/restart must wait until
        # this iteration has finished every irreversible printer action and durable marker.
        update_restart_lock.acquire()
        try:
            reports = fleet.snapshot()
            response = dpf.report_state(reports, link=updater.metadata())
            if response:
                updater.confirm_running()
            force_update = updater.apply_cloud_command(
                response.get("update") if isinstance(response, dict) else None
            )
            updater.tick_async(force=force_update)
            desired = response.get("printers") if isinstance(response, dict) else None
            # scan_requested (U7): true for a short TTL after the operator's "Add Printer"
            # click (U8) POSTs /api/bridge/scan. Drives discovery_reporter.tick() below —
            # the bridge scans once at startup, then goes quiet, then reopens exactly one
            # bounded burst per request instead of scanning forever.
            scan_requested = bool(response.get("scan_requested")) if isinstance(response, dict) else False
            _apply_desired(
                desired or [], fleet, dpf, spool_dir, started_sends, applied_controls,
                router=router,
            )

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
            discovery_reporter.tick(scan_requested=scan_requested)

            # Self-heal any printer that dropped off the network — re-discover it by
            # serial and reconnect at its new IP if DHCP moved it (U1). Throttled and only
            # when something is actually offline, so a healthy farm pays nothing.
            fleet.reconcile_connections()

            now = time.monotonic()
            if now - last_heartbeat >= cfg.heartbeat_interval_seconds:
                heartbeat = dpf.heartbeat(link=updater.metadata())
                if heartbeat:
                    updater.confirm_running()
                force_update = updater.apply_cloud_command(
                    heartbeat.get("update") if isinstance(heartbeat, dict) else None
                )
                updater.tick_async(force=force_update)
                heartbeat_desired = (
                    heartbeat.get("printers") if isinstance(heartbeat, dict) else None
                )
                _apply_desired(
                    heartbeat_desired or [], fleet, dpf, spool_dir, started_sends,
                    applied_controls, router=router,
                )
                last_heartbeat = now
        except Exception:
            # Never let one bad iteration kill the long-running reporter — nothing
            # supervises/restarts it. Log and keep polling.
            logger.exception("bridge loop iteration failed; continuing")
        finally:
            update_restart_lock.release()

        time.sleep(cfg.state_interval_seconds)


_CONTROL_ACTIONS = frozenset({"pause", "resume", "stop"})


def _apply_desired(desired: List[Dict], fleet, dpf, spool_dir: str,
                   started_sends, applied_controls, router=None) -> None:
    """Apply control then cloud sends from one desired-state payload."""
    _handle_desired(desired, fleet, applied_controls, spool_dir, router=router)
    _handle_cloud_sends(
        desired, fleet, dpf, spool_dir, started_sends, router=router,
    )


def _control_from_row(row: dict):
    control = row.get("control")
    if not isinstance(control, dict):
        return None
    action = control.get("action")
    control_id = control.get("id")
    if action not in _CONTROL_ACTIONS or not control_id:
        return None
    return {"id": str(control_id), "action": action}


def _row_has_stop(row: dict) -> bool:
    control = _control_from_row(row)
    return control is not None and control["action"] == "stop"


def _desired_allows_send(desired: List[Dict], bambu_id: str, batch_id: str) -> bool:
    """True only when a fresh desired-state still authorizes this exact send."""
    return _authorized_send(desired, bambu_id, batch_id) is not None


def _authorized_send(desired: List[Dict], bambu_id: str, batch_id: str) -> Optional[dict]:
    """Return the fresh exact send command, or None when authorization changed."""
    for row in desired:
        if not isinstance(row, dict) or str(row.get("bambu_id") or "") != str(bambu_id):
            continue
        if _row_has_stop(row) or str(row.get("desired_status") or "IDLE") != "IDLE":
            return None
        send = row.get("send")
        if isinstance(send, dict) and str(send.get("batch_id") or "") == str(batch_id):
            return send
        return None
    return None


def _handle_desired(desired: List[Dict], fleet=None, applied_controls=None,
                    spool_dir: Optional[str] = None, router=None) -> None:
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
        _apply_control(fleet, str(bambu_id), control, applied_controls, spool_dir, router)


def _apply_control(fleet, bambu_id: str, control: dict, applied_controls,
                   spool_dir: Optional[str], router) -> None:
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
    result = None
    try:
        if callable(getattr(fleet, "apply_control", None)):
            result = fleet.apply_control(bambu_id, action)
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


def _handle_cloud_sends(desired: List[Dict], fleet, dpf, spool_dir: str,
                       started_sends=None, router=None) -> None:
    """Start a print only when the cloud Sliced Queue says so.

    A send that already physically started is only re-reported — never started twice
    while the cloud row is still SENDING (report 5xx / next poll / Link restart).
    """
    import os
    if started_sends is None:
        started_sends = set()
    live = set()
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
        key = (str(batch_id), str(bambu_id))
        live.add(key)
        dest = os.path.join(spool_dir, f"{batch_id}.3mf")
        started_path = dest + ".started"
        if key in started_sends or os.path.exists(started_path):
            started_sends.add(key)
            dpf.report_dispatched(batch_id, bambu_id)
            continue
        if _row_has_stop(row):
            logger.info("cloud send %s: live stop; not starting", batch_id)
            continue
        if str(row.get("desired_status") or "IDLE") != "IDLE":
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
            uploaded = fleet.upload(bambu_id, dest, remote_name=remote_name)
            latest = dpf.heartbeat() if hasattr(dpf, "heartbeat") else {}
            latest_rows = latest.get("printers") if isinstance(latest, dict) else None
            fresh_send = _authorized_send(latest_rows or [], bambu_id, batch_id)
            if fresh_send is None:
                logger.warning(
                    "cloud send %s: fresh desired-state no longer authorizes start; "
                    "leaving the uploaded file idle",
                    batch_id,
                )
                continue
            ams_mapping = _resolve_cloud_ams_mapping(fresh_send, fleet, bambu_id)
            if ams_mapping is None:
                logger.warning(
                    "cloud send %s: live AMS changed after upload; "
                    "leaving the uploaded file idle",
                    batch_id,
                )
                continue
            started = fleet.start_print(
                bambu_id, uploaded or remote_name or os.path.basename(dest),
                ams_mapping, plate_index,
            )
        else:
            started = fleet.dispatch(
                bambu_id, dest, ams_mapping, plate_index,
                remote_name=remote_name,
            )
        if started:
            started_sends.add(key)
            try:
                with open(started_path, "w"):
                    pass
            except OSError:
                pass
            if router is not None:
                router.record_assignment(str(bambu_id), str(batch_id), plate_index)
            dpf.report_dispatched(batch_id, bambu_id)
        else:
            logger.warning("printer %s did not start batch %s", bambu_id, batch_id)
    for key in list(started_sends):
        _batch_id, bambu_id = key
        if bambu_id in seen_serials and key not in live:
            started_sends.discard(key)
            leftover = os.path.join(spool_dir, f"{_batch_id}.3mf.started")
            try:
                os.unlink(leftover)
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
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < -1
            or item > MAX_BAMBU_AMS_TRAY_INDEX
        ):
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
        if (
            family is None
            or isinstance(slot_number, bool)
            or not isinstance(slot_number, int)
            or slot_number < 1
            or slot_number > MAX_BAMBU_AMS_TRAY_INDEX + 1
        ):
            return None
        candidates.setdefault((color, family), []).append(slot_number - 1)
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


def _resolve_cloud_ams_mapping(send: dict, fleet, bambu_id: str) -> Optional[list]:
    """Validate the sparse contract and use live slots whenever a snapshot exists.

    Missing/legacy contracts, stale live mismatches, and compact mappings fail
    closed so profile positions cannot silently bind to the wrong material.
    """
    logical_required = _required_filaments(send)
    if logical_required is None:
        return None
    validated = _validate_sparse_ams_mapping(send.get("ams_mapping"), logical_required)
    if validated is None:
        return None
    live = _live_snapshot(fleet, bambu_id)
    if live is not None:
        return _mapping_from_live_slots(logical_required, live)
    return validated


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
