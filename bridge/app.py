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

from .config import Config, PrinterConfig, load_config
from .discovery_reporter import DiscoveryReporter
from .dpf_client import DpfClient
from .fleet import Fleet
from .pairing import ensure_paired
from .reconciler import ConfigReconciler
from .router import Dispatcher, Router
from .store import PrinterStore

logger = logging.getLogger(__name__)


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
        try:
            reports = fleet.snapshot()
            response = dpf.report_state(reports)
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
                heartbeat = dpf.heartbeat()
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
    for row in desired:
        if not isinstance(row, dict) or str(row.get("bambu_id") or "") != str(bambu_id):
            continue
        if _row_has_stop(row):
            return False
        send = row.get("send")
        return isinstance(send, dict) and str(send.get("batch_id") or "") == str(batch_id)
    return False


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
    try:
        if action == "pause":
            printer.pause_print()
        elif action == "resume":
            if hasattr(printer, "resume_from_stage"):
                printer.resume_from_stage()
            else:
                printer.resume_print()
        elif action == "stop":
            printer.stop_print()
            if router is not None and hasattr(router, "clear_assignment"):
                router.clear_assignment(bambu_id)
    except Exception:
        logger.exception("printer %s: %s failed; will retry this control.id",
                         bambu_id, action)
        return
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
        required = _required_filament_hexes(send)
        ams_mapping = _resolve_cloud_ams_mapping(send, fleet, bambu_id)
        if required and not ams_mapping:
            logger.warning(
                "cloud send %s: no AMS mapping for %s; not starting",
                batch_id, required,
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
            if not _desired_allows_send(latest_rows or [], bambu_id, batch_id):
                logger.warning(
                    "cloud send %s: fresh desired-state no longer authorizes start; "
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


def _required_filament_hexes(send: dict) -> list:
    raw = send.get("required_filament_hexes") or []
    if not isinstance(raw, list):
        return []
    return [hex_color for hex_color in raw if isinstance(hex_color, str) and hex_color]


def _cloud_remote_name(send: dict):
    raw = send.get("filename")
    if not isinstance(raw, str):
        return None
    from .printhost import sanitize_upload_filename
    return sanitize_upload_filename(raw)


def _int_trays(value):
    if not isinstance(value, list) or not value:
        return None
    trays = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        trays.append(item)
    return trays


def _live_snapshot(fleet, bambu_id: str):
    by_id = getattr(fleet, "by_id", None)
    if not callable(by_id):
        return None
    printer = by_id(bambu_id)
    if printer is None or not hasattr(printer, "snapshot"):
        return None
    try:
        snap = printer.snapshot()
    except Exception:
        return None
    return snap if isinstance(snap, dict) else None


def _resolve_cloud_ams_mapping(send: dict, fleet, bambu_id: str) -> list:
    """Live AMS first (same rule as local dispatch), then the cloud payload.

    An empty mapping for a color-tagged file is HMS 0700_7000_0002_0008 — the
    caller must refuse to start rather than send `use_ams=True` with `[]`.
    """
    required = _required_filament_hexes(send)
    live = _live_snapshot(fleet, bambu_id)
    if required and live is not None:
        computed = Dispatcher._ams_mapping(required, live)
        if len(computed) == len(required):
            return computed
    provided = _int_trays(send.get("ams_mapping"))
    if provided is not None and (not required or len(provided) == len(required)):
        return provided
    return []


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
