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
from .config import Config, PrinterConfig, load_config
from .discovery_reporter import DiscoveryReporter
from .dpf_client import DpfClient
from .fleet import Fleet
from .pairing import ensure_paired, repair
from .reconciler import ConfigReconciler
from .router import Dispatcher, Router
from .store import PrinterStore
from .updater import SelfUpdater

logger = logging.getLogger(__name__)

# When the cloud rejects our credential, don't hammer the pair/exchange endpoint every
# loop — attempt a re-pair at most this often.
_REPAIR_RETRY_SECONDS = 60.0


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
            "Re-issue a pair token in 3DPF (Integrations -> PrintForce Link) and re-run the "
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
    updater = SelfUpdater(__version__)
    logger.info("PrintForce Link %s — %d printer(s) at startup (%d from config.toml, %d from the store)",
                __version__, len(printer_configs), len(cfg.printers), len(store.configs()))

    # Print-host accepts OrcaSlicer uploads and forwards them into the cloud
    # Sliced Queue. Local auto-dispatch is off; start is a cloud send command.
    router = _start_printhost(cfg, dpf)
    dispatcher = None
    if router is not None:
        dispatcher = Dispatcher(router, fleet, dpf)
        logger.info("print-host enabled; %d job(s) restored from the queue",
                    len(router.pending()))

    last_heartbeat = 0.0
    last_repair_attempt: Optional[float] = None   # throttles re-pairing on a revoked token
    started_sends = set()
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

            # If the cloud rejected our credential (the operator hit Disconnect, revoking
            # it, then re-ran the installer with a fresh pair token), re-pair with that
            # token instead of knocking forever with the dead key — the difference between
            # a card that reconnects itself and one stuck on "Not set up". Only in pairing
            # mode (a hand-authored config.toml token is the operator's to fix), and
            # throttled so a spent/expired token can't spam the exchange endpoint.
            if dpf.unauthorized and not cfg.cloud_token and pair_token:
                now = time.monotonic()
                if last_repair_attempt is None or now - last_repair_attempt >= _REPAIR_RETRY_SECONDS:
                    last_repair_attempt = now
                    new_token = repair(store, cfg.dpf_base_url, pair_token)
                    if new_token:
                        dpf.set_token(new_token)
                        logger.info("re-paired after a rejected credential; resuming")
                    else:
                        logger.error(
                            "credential rejected and re-pairing did not complete — the pair "
                            "code may be expired or already used. In 3DPF, click Disconnect, "
                            "then Get install command, and re-run the install command.")

            desired = response.get("printers") if isinstance(response, dict) else None
            # scan_requested (U7): true for a short TTL after the operator's "Add Printer"
            # click (U8) POSTs /api/bridge/scan. Drives discovery_reporter.tick() below —
            # the bridge scans once at startup, then goes quiet, then reopens exactly one
            # bounded burst per request instead of scanning forever.
            scan_requested = bool(response.get("scan_requested")) if isinstance(response, dict) else False
            _handle_desired(desired or [])
            _handle_cloud_sends(
                desired or [], fleet, dpf, spool_dir, started_sends, router=router,
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

            # Keep the agent current from GitHub Releases (U9). Throttled; a packaged build
            # swaps itself in and restarts, a source checkout is a no-op.
            updater.tick()

            now = time.monotonic()
            if now - last_heartbeat >= cfg.heartbeat_interval_seconds:
                dpf.heartbeat()
                last_heartbeat = now
        except Exception:
            # Never let one bad iteration kill the long-running reporter — nothing
            # supervises/restarts it. Log and keep polling.
            logger.exception("bridge loop iteration failed; continuing")

        time.sleep(cfg.state_interval_seconds)


def _handle_desired(desired: List[Dict]) -> None:
    """Act on the authoritative desired-state 3DPF returns."""
    for d in desired:
        logger.debug("desired-state: %s -> %s", d.get("bambu_id"), d.get("desired_status"))


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
        if str(row.get("desired_status") or "IDLE") != "IDLE":
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
        started = fleet.dispatch(bambu_id, dest, [], plate_index)
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


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
