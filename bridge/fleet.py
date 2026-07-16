"""Manage the printer fleet: connect all printers and produce the aggregated
state report the bridge POSTs to 3DPF."""

import logging
import time
from typing import List, Dict

from .config import PrinterConfig
from .discover import DiscoveredPrinter, discover
from .printer import _DEFAULT_STALE_AFTER_SECONDS, BambuPrinter

logger = logging.getLogger(__name__)

# How often, at most, to run an SSDP re-discovery scan while a printer is offline (U1).
# A whole farm can be legitimately offline (bridge just started, a power blip), and a
# scan every poll would add latency for nothing.
_DEFAULT_REDISCOVER_INTERVAL_SECONDS = 60.0
# A short listen is enough: a printer that just changed IP is actively broadcasting SSDP.
_DEFAULT_DISCOVER_TIMEOUT_SECONDS = 5.0


def _default_discover(timeout: float) -> List[DiscoveredPrinter]:
    return discover(timeout=timeout)


class Fleet:
    def __init__(self, printer_configs: List[PrinterConfig],
                 stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
                 *,
                 printer_factory=BambuPrinter,
                 discover_fn=None,
                 rediscover_interval_seconds: float = _DEFAULT_REDISCOVER_INTERVAL_SECONDS,
                 discover_timeout_seconds: float = _DEFAULT_DISCOVER_TIMEOUT_SECONDS,
                 monotonic=time.monotonic):
        # `stale_after_seconds` is how long a printer may say nothing new before it is
        # presumed gone (Config.stale_after_seconds). It is per-fleet because it is
        # derived from the poll interval — see BambuPrinter.snapshot.
        self._stale_after_seconds = stale_after_seconds
        # `printer_factory` and `discover_fn` are injectable so the fleet's connection
        # logic (U1/U2) is testable without the library or real SSDP.
        self._make_printer = printer_factory
        self._printers = [printer_factory(c, stale_after_seconds=stale_after_seconds)
                          for c in printer_configs]
        self._discover = discover_fn if discover_fn is not None else _default_discover
        self._rediscover_interval = rediscover_interval_seconds
        self._discover_timeout = discover_timeout_seconds
        self._monotonic = monotonic
        self._last_discovery_monotonic = None

    def connect_all(self) -> None:
        for p in self._printers:
            try:
                p.connect()
            except Exception as e:
                # A printer that won't connect is reported OFFLINE via snapshot();
                # don't let one bad printer stop the fleet from starting.
                logger.warning("could not connect to %s: %s", p.bambu_id, type(e).__name__)

    def by_id(self, bambu_id: str):
        """The BambuPrinter with this serial, or None. The dispatcher (U9) needs the
        live connection object (not just a snapshot) to FTPS-upload + MQTT-start."""
        return next((p for p in self._printers if p.bambu_id == bambu_id), None)

    def dispatch(self, bambu_id: str, file_path: str, ams_mapping, plate_number: int = 1) -> bool:
        """Upload + start `file_path` on the named printer. False if that printer isn't in
        the fleet; otherwise the printer's start result. Raises on a transport error so
        the router re-queues rather than dropping the job."""
        printer = self.by_id(bambu_id)
        if printer is None:
            logger.error("dispatch requested for unknown printer %s", bambu_id)
            return False
        return printer.upload_and_start(file_path, ams_mapping, plate_number)

    def snapshot(self) -> List[Dict]:
        """One state report per printer — the bridge's wire contract with 3DPF:

            [{
                "bambu_id": str,
                "status": IDLE | PRINTING | PAUSED | NEEDS_CLEARING | ERROR | OFFLINE,
                "slots": [{slot_number, color_hex, filament_type}],  # empty slots too

                # telemetry, FLAT on the report (this is what the cloud's
                # ingest_printer_state reads — not a nested object):
                "progress_percent", "layer_num", "total_layer_num", "remaining_seconds",
                "nozzle_temper", "nozzle_target_temper", "bed_temper",
                "bed_target_temper", "chamber_temper", "gcode_file", "subtask_name",
                "nozzle_diameter", "stage", "tray_exist_bits",
                "hms_severity", "hms_code", "hms_count",

                "print_duration_seconds": int | None,
                "print_duration_source": "bridge" | "printer" | None,
            }, ...]

        `bambu_id` / `status` / `slots` are unchanged from the shipped contract, so an
        older ingest keeps working; everything else is additive and unknown keys are
        ignored on the far side. A printer that cannot be read reports OFFLINE with null
        telemetry rather than being omitted — a missing printer and an unreachable one
        are different facts. See `BambuPrinter.snapshot`.
        """
        return [p.snapshot() for p in self._printers]

    def add_printer(self, cfg: PrinterConfig) -> None:
        """Add a printer to a running fleet without a restart (U2) — the precondition for
        the web wizard to make a printer appear live. Idempotent by serial. A connect
        failure does not stop the add: the printer joins OFFLINE and self-heals via
        reconcile_connections()."""
        if self.by_id(cfg.bambu_id) is not None:
            return
        printer = self._make_printer(cfg, stale_after_seconds=self._stale_after_seconds)
        try:
            printer.connect()
        except Exception as e:
            logger.warning("could not connect to newly added %s: %s",
                           cfg.bambu_id, type(e).__name__)
        self._printers.append(printer)
        logger.info("added printer %s (%s) to the fleet", cfg.bambu_id, cfg.name)

    def remove_printer(self, bambu_id: str) -> None:
        """Remove a printer from a running fleet (U2), closing its connection. No-op if
        the serial isn't in the fleet."""
        printer = self.by_id(bambu_id)
        if printer is None:
            return
        printer.disconnect()
        self._printers = [p for p in self._printers if p.bambu_id != bambu_id]
        logger.info("removed printer %s from the fleet", bambu_id)

    def reconcile_connections(self) -> None:
        """Self-heal dropped connections (U1). A printer reports OFFLINE when it is
        unreachable — which, after a DHCP lease change, means the bridge is dialing an
        address the printer no longer holds. Re-discover offline printers by serial via
        SSDP and, when a serial now answers at a DIFFERENT IP, rebuild its client there.
        A same-IP outage needs nothing: paho keeps retrying the pinned host and snapshot()
        recovers on its own.

        Scans only when at least one printer is offline AND `rediscover_interval` has
        elapsed since the last scan — a healthy farm pays nothing, and a whole farm that
        is briefly down is not hammered."""
        offline = [p for p in self._printers if p.is_offline]
        if not offline:
            return
        now = self._monotonic()
        if (self._last_discovery_monotonic is not None
                and now - self._last_discovery_monotonic < self._rediscover_interval):
            return
        self._last_discovery_monotonic = now
        try:
            found = {d.serial: d for d in self._discover(self._discover_timeout)}
        except Exception as e:
            logger.warning("re-discovery scan failed (%s); will retry next interval",
                           type(e).__name__)
            return
        for p in offline:
            d = found.get(p.bambu_id)
            if d is None or not d.ip:
                continue                      # not on the LAN right now — keep scanning
            if d.ip == p.current_ip:
                continue                      # same address; paho is already retrying it
            logger.info("printer %s answered at %s (was %s); reconnecting",
                        p.bambu_id, d.ip, p.current_ip)
            try:
                p.reconnect(new_ip=d.ip)
            except Exception as e:
                logger.warning("printer %s reconnect to %s failed (%s); will retry",
                               p.bambu_id, d.ip, type(e).__name__)
