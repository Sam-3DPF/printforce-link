"""Manage the printer fleet: connect all printers and produce the aggregated
state report the bridge POSTs to 3DPF."""

import logging
import threading
import time
from typing import List, Dict, Optional

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
# A removed/re-added serial may need one current worker while one stale generation unwinds.
# Bound those stale lifetimes so repeated config churn cannot grow threads without limit.
_MAX_RECONNECT_WORKERS_PER_SERIAL = 2


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
        self._lock = threading.RLock()
        self._make_printer = printer_factory
        self._printers = [printer_factory(c, stale_after_seconds=stale_after_seconds)
                          for c in printer_configs]
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

    def connect_all(self) -> None:
        with self._lock:
            printers = list(self._printers)
        for p in printers:
            try:
                p.connect()
            except Exception as e:
                # A printer that won't connect is reported OFFLINE via snapshot();
                # don't let one bad printer stop the fleet from starting.
                logger.warning("could not connect to %s: %s", p.bambu_id, type(e).__name__)

    def by_id(self, bambu_id: str):
        """The BambuPrinter with this serial, or None. The dispatcher (U9) needs the
        live connection object (not just a snapshot) to FTPS-upload + MQTT-start."""
        with self._lock:
            return next((p for p in self._printers if p.bambu_id == bambu_id), None)

    def apply_control(self, bambu_id: str, action: str) -> bool:
        """Publish a control command without allowing a reconnect swap mid-command."""
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
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
            logger.warning("unknown control %s requested for printer %s", action, bambu_id)
            return False

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
            if printer is None:
                logger.error("dispatch requested for unknown printer %s", bambu_id)
                return False
            return printer.upload_and_start(
                file_path, ams_mapping, plate_number, remote_name=remote_name,
            )

    def upload(self, bambu_id: str, file_path: str,
               remote_name: Optional[str] = None) -> Optional[str]:
        """FTPS-upload only. None if that printer is not in the fleet."""
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
            if printer is None:
                logger.error("upload requested for unknown printer %s", bambu_id)
                return None
            return printer.upload_file(file_path, remote_name=remote_name)

    def start_print(self, bambu_id: str, remote_name: str, ams_mapping,
                    plate_number: int = 1) -> bool:
        """MQTT-start a file already on the printer."""
        with self._lock:
            printer = next((p for p in self._printers if p.bambu_id == bambu_id), None)
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
                "hms_severity", "hms_code", "hms_count", "print_error",
                "gcode_state", "hms_present", "hms_empty",
                "has_active_file", "has_active_task", "has_active_project",
                "stage_queue_empty", "print_type", "historical_failed_ready",

                "print_duration_seconds": int | None,
                "print_duration_source": "bridge" | "printer" | None,
            }, ...]

        `bambu_id` / `status` / `slots` are unchanged from the shipped contract, so an
        older ingest keeps working; everything else is additive and unknown keys are
        ignored on the far side. A printer that cannot be read reports OFFLINE with null
        telemetry rather than being omitted — a missing printer and an unreachable one
        are different facts. See `BambuPrinter.snapshot`.
        """
        with self._lock:
            return [p.snapshot() for p in self._printers]

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
        if cancelled:
            printer.disconnect()
            return
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
            if printer is not None:
                # Remove membership before network cleanup. Any late worker completion
                # sees a different generation and may only close its replacement.
                self._printers = [p for p in self._printers if p is not printer]
        if printer is not None:
            printer.disconnect()
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
        is briefly down is not hammered. Each serial owns at most one daemon reconnect
        worker, so a blocked connect cannot block the reporter or another printer."""
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
            self._schedule_reconnect(p, d.ip)

    def _schedule_reconnect(self, printer, new_ip: str) -> None:
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
        logger.info("printer %s answered at %s (was %s); reconnecting asynchronously",
                    bambu_id, new_ip, printer.current_ip)
        worker = threading.Thread(
            target=self._run_reconnect,
            args=(printer, replacement_cfg, generation, token),
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
                       generation: int, token) -> None:
        """Connect a replacement off-loop, then swap it in only if membership is unchanged."""
        bambu_id = printer.bambu_id
        replacement = None
        swapped = False
        try:
            with self._lock:
                if (
                    printer not in self._printers
                    or self._membership_generations.get(bambu_id) != generation
                    or self._reconnects_in_flight.get(bambu_id) != (generation, token)
                ):
                    return
            replacement = self._make_printer(
                replacement_cfg,
                stale_after_seconds=self._stale_after_seconds,
            )
            replacement.connect()
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
                printer.disconnect()

    def _release_reconnect_worker_slot(self, bambu_id: str) -> None:
        """Release one live-worker slot. Caller must hold ``self._lock``."""
        remaining = self._reconnect_worker_counts.get(bambu_id, 0) - 1
        if remaining > 0:
            self._reconnect_worker_counts[bambu_id] = remaining
        else:
            self._reconnect_worker_counts.pop(bambu_id, None)
