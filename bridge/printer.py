"""One Bambu printer, wrapping `bambulabs_api`.

The library is imported lazily inside `connect()` so the pure logic (`map_status`,
`decode_hms`, `parse_telemetry`, `merge_status_payload`, and `ams.parse_ams`) can be
unit-tested without it installed. All library-specific accessor names live in this
one file — confirmed against real P1S hardware (2026-07-13, bambulabs-api 2.6.6) and
isolated here on purpose, so a naming difference only touches `_raw_status()` (the
status payload) and `_is_connected()` (the MQTT link). Those two methods are the
entire coupling to `bambulabs_api`; everything else consumes plain dicts.
"""

import copy
import logging
import os
import time
from typing import Dict, Optional, Tuple

from .ams import parse_ams, parse_tray_exist_bits
from .coerce import as_float, as_int, clean_str
from .config import PrinterConfig

logger = logging.getLogger(__name__)

# Bambu gcode_state -> the Printer.status vocabulary 3DPF accepts
# (IDLE / PRINTING / PAUSED / NEEDS_CLEARING / ERROR / OFFLINE).
#
# FINISH -> NEEDS_CLEARING: the print is done but the plate isn't cleared yet.
# PAUSE  -> PAUSED: Bambu's enum spells it PAUSE. It is NOT a flavour of PRINTING —
#           a paused printer is making no progress and is waiting on the operator.
_STATE_MAP = {
    "IDLE": "IDLE",
    "PREPARE": "PRINTING",
    "SLICING": "PRINTING",
    "RUNNING": "PRINTING",
    "PAUSE": "PAUSED",
    "FINISH": "NEEDS_CLEARING",
    "FAILED": "ERROR",
}

# HMS severity is the high half of `code`. Lower is worse.
_HMS_SEVERITY = {1: "FATAL", 2: "SERIOUS", 3: "COMMON", 4: "INFO"}
_HMS_UNKNOWN_RANK = 99  # rank unrecognized severities last so a real FATAL still wins

# gcode_states in which a print is on the machine and its clock should be running...
_PRINT_IN_PROGRESS = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
# ...and the ones that end it.
_PRINT_ENDED = frozenset({"FINISH", "FAILED"})
# We trust the bridge's own stopwatch only if we positively saw the machine NOT
# printing on the poll before the print began. Assert, never assume — a blank or
# unknown prior state is not evidence of an idle machine.
_PRINT_START_EVIDENCE = frozenset({"IDLE", "FINISH", "FAILED"})

# A print runs for hours, occasionally days — never months. Anything past this is a
# corrupt timestamp, not a print.
_MAX_PLAUSIBLE_PRINT_SECONDS = 30 * 24 * 60 * 60
# Epoch floor (2001-09-09). `gcode_start_time` is "0" on a printer that never set it.
_MIN_PLAUSIBLE_EPOCH = 1_000_000_000
# The stopwatch and the printer's own start time should agree to within a poll or two.
# Past this, something is wrong and we say so out loud — see _warn_if_sources_disagree.
_DURATION_DISAGREEMENT_SECONDS = 120

# How long a printer may say nothing NEW before the bridge stops believing its last
# payload (see `BambuPrinter.snapshot`). The real window comes from config
# (`Config.stale_after_seconds` = state_interval x offline_after_stale_polls); this is
# the fallback for a `BambuPrinter` built without one, and equals that default (15s x 3).
_DEFAULT_STALE_AFTER_SECONDS = 45


def map_status(gcode_state: Optional[str]) -> str:
    """Map a Bambu gcode_state to a 3DPF printer status.

    Unknown and blank states map to **OFFLINE, never IDLE**. IDLE is the sole
    authorization for dispatch, so it has to be positively asserted by the printer: a
    fail-open default would dispatch a job onto a busy printer, deduct its filament,
    and stamp the batch PRINTING for a print that never starts. The window is real,
    not theoretical — `mqtt_dump()` returns {} until the first MQTT push lands, so on
    every bridge start there is an interval in which each printer, *including one
    mid-print*, has no gcode_state at all.
    """
    return _STATE_MAP.get((gcode_state or "").strip().upper(), "OFFLINE")


def decode_hms(hms) -> Dict:
    """Reduce Bambu's `hms` array to the worst active alarm plus a count.

    Each entry is {"attr": int, "code": int}; severity is `code >> 16` (1 fatal,
    2 serious, 3 common, 4 info). The detail page needs to know *is something wrong,
    how bad, and how many* — not the whole array — so that is all we report.

    `hms_code` is the 4-group hex code Bambu publishes its error index under (the two
    halves of `attr`, then the two halves of `code`), so the UI can name the fault.
    """
    alarms = []
    for entry in hms or []:
        if not isinstance(entry, dict):
            continue
        attr = as_int(entry.get("attr"), None)
        code = as_int(entry.get("code"), None)
        if attr is None or code is None:
            continue
        alarms.append((code >> 16, attr, code))

    if not alarms:
        return {"hms_severity": None, "hms_code": None, "hms_count": 0}

    # Rank by the severity NUMBER (lower is worse), not by its name. `severity` is the
    # top 16 bits of an arbitrary int, so a value outside 1-4 is entirely possible and
    # must sort last rather than crash the poll.
    severity, attr, code = min(
        alarms, key=lambda a: a[0] if a[0] in _HMS_SEVERITY else _HMS_UNKNOWN_RANK)
    return {
        "hms_severity": _HMS_SEVERITY.get(severity, "UNKNOWN"),
        "hms_code": (f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
                     f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"),
        "hms_count": len(alarms),
    }


def parse_telemetry(status: dict) -> Dict:
    """Extract the live telemetry the printer already reports.

    Every field is optional. Feed this the *merged* payload (see
    `merge_status_payload`), never a raw one: Bambu reports are partial deltas, so any
    key can be missing from any single push.
    """
    print_obj = (status or {}).get("print") if isinstance(status, dict) else None
    if not isinstance(print_obj, dict):
        print_obj = {}

    telemetry = {
        "progress_percent": as_int(print_obj.get("mc_percent"), None),
        "layer_num": as_int(print_obj.get("layer_num"), None),
        "total_layer_num": as_int(print_obj.get("total_layer_num"), None),
        "remaining_seconds": _minutes_to_seconds(print_obj.get("mc_remaining_time")),
        "nozzle_temper": as_float(print_obj.get("nozzle_temper"), None),
        "nozzle_target_temper": as_float(print_obj.get("nozzle_target_temper"), None),
        "bed_temper": as_float(print_obj.get("bed_temper"), None),
        "bed_target_temper": as_float(print_obj.get("bed_target_temper"), None),
        "chamber_temper": as_float(print_obj.get("chamber_temper"), None),
        "gcode_file": clean_str(print_obj.get("gcode_file")),
        "subtask_name": clean_str(print_obj.get("subtask_name")),  # the human-friendly job name
        "nozzle_diameter": as_float(print_obj.get("nozzle_diameter"), None),
        # The print stage. It is the only field that says *why* a print paused
        # (6 = filament runout, 16 = user, 35 = nozzle clog) — gcode_state only ever
        # says PAUSE. Bambu's "no stage" sentinel is -1, normalised to None here.
        "stage": _valid_stage(print_obj.get("stg_cur")),
        "tray_exist_bits": parse_tray_exist_bits(status),
    }
    telemetry.update(decode_hms(print_obj.get("hms")))
    return telemetry


def merge_status_payload(cached: Optional[dict], incoming: Optional[dict]) -> Dict:
    """Merge a (possibly partial) Bambu MQTT payload into the last-known one.

    Most Bambu reports are partial deltas — only a `pushall` carries the whole object —
    so without this, a poll that lands between deltas blanks the temperatures and the
    ETA.

    The merge is deliberately **shallow at the `print` level**:

      * scalars merge key-by-key, so a delta that omits `nozzle_temper` keeps the last
        known value rather than blanking it;
      * `ams` is taken **wholesale** from any payload that carries one, never merged
        tray-by-tray.

    That second rule is load-bearing — do not "improve" this into a deep merge. A deep
    merge cannot express key *removal*, so a tray going from loaded to empty would keep
    its last-known color forever, telling the router the printer holds a color it does
    not.

    Nothing from `incoming` is ever stored by reference. `mqtt_dump()` hands back the
    library's live internal dict *by reference* (`MqttClient.dump()` is literally
    `return self._data`) and the MQTT thread keeps mutating it, so caching it without
    copying would alias it and "last known" would silently become "current". `cached`
    needs only a shallow copy: it is a previous return value of this function, so
    everything reachable from it is already a bridge-owned copy that nothing mutates in
    place.
    """
    merged = dict(cached) if isinstance(cached, dict) else {}
    if not isinstance(incoming, dict):
        return merged

    for key, value in incoming.items():
        if key == "print" and isinstance(value, dict):
            previous = merged.get("print")
            print_obj = dict(previous) if isinstance(previous, dict) else {}
            print_obj.update(copy.deepcopy(value))  # shallow: `ams` is replaced whole
            merged["print"] = print_obj             # rebuilt, so cached["print"] is untouched
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class PrintStopwatch:
    """How long the print on this machine actually ran, measured rather than estimated.

    Nothing else in the system knows the observed duration — it is what replaces the
    slicer's estimate in the cost snapshot — so a missing value is preferable to a wrong
    one, and every branch here prefers reporting nothing over inventing a number.
    """

    def __init__(self, printer_id: str, monotonic=time.monotonic, wall_clock=time.time):
        self._printer_id = printer_id
        # Injectable clocks — the stopwatch is otherwise untestable.
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self._prev_gcode_state: Optional[str] = None
        self._started_monotonic: Optional[float] = None
        self._started_epoch: Optional[int] = None
        self._start_observed = False
        self._duration_seconds: Optional[int] = None
        self._source: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[int]:
        return self._duration_seconds

    @property
    def source(self) -> Optional[str]:
        # Which clock produced `duration_seconds`: "bridge" | "printer" | None.
        return self._source

    def observe(self, gcode_state, print_obj: Dict) -> None:
        """Take one poll's reading. The duration is measured on the in-progress -> ended
        edge, then **latched**: it is reported on every subsequent poll for as long as
        the printer stays in the ended state, not just on the single poll where the edge
        happened. The completion report retries until 3DPF acks it, so a duration that
        existed for exactly one poll would be lost to the first dropped POST and could
        never be recovered.
        """
        state = (gcode_state or "").strip().upper() if isinstance(gcode_state, str) else ""

        if state in _PRINT_IN_PROGRESS:
            if self._started_monotonic is None:
                self._started_monotonic = self._monotonic()
                self._start_observed = self._prev_gcode_state in _PRINT_START_EVIDENCE
                logger.info("printer %s: print in progress (%s)", self._printer_id, state)
            if self._started_epoch is None:
                self._started_epoch = _valid_epoch(print_obj.get("gcode_start_time"))
            # A new print invalidates the previous one's measurement.
            self._duration_seconds = None
            self._source = None

        elif state in _PRINT_ENDED:
            if self._started_monotonic is not None or self._started_epoch is not None:
                self._duration_seconds, self._source = self._measure()
                self._started_monotonic = None
                self._started_epoch = None
                self._start_observed = False
                logger.info("printer %s: print %s after %ss (source=%s)", self._printer_id,
                            state, self._duration_seconds, self._source)
            # Otherwise the bridge came up to find an already-ended print (an uncleared
            # plate from yesterday). We never saw it run, so we do not know how long it
            # took and we say so, rather than inventing a number.

        else:  # IDLE, blank, unknown: no print on the machine.
            self._started_monotonic = None
            self._started_epoch = None
            self._start_observed = False
            self._duration_seconds = None
            self._source = None

        self._prev_gcode_state = state

    def _measure(self) -> Tuple[Optional[int], Optional[str]]:
        """(seconds, source) for the print that just ended.

        The bridge's own stopwatch wins **when the bridge watched the print start**: it
        is a monotonic delta, so no clock skew, timezone, or firmware quirk can corrupt
        it, and it is exactly what the operator experienced (pauses included).

        The printer's `gcode_start_time` (epoch seconds) covers the one case the
        stopwatch cannot: the bridge restarted, or connected, while a print was already
        running, so its stopwatch only ever saw the tail. Reporting that tail as the
        print's duration would silently *under*-report — worse than reporting nothing,
        because the cost snapshot falls back to the slicer's estimate on a null but will
        happily believe a wrong number.
        """
        if self._start_observed and self._started_monotonic is not None:
            elapsed = int(self._monotonic() - self._started_monotonic)
            if elapsed > 0:
                self._warn_if_sources_disagree(elapsed)
                return elapsed, "bridge"

        if self._started_epoch is not None:
            elapsed = int(self._wall_clock() - self._started_epoch)
            if 0 < elapsed <= _MAX_PLAUSIBLE_PRINT_SECONDS:
                return elapsed, "printer"
            logger.warning("printer %s: gcode_start_time implies an implausible duration "
                           "(%ss) — reporting no duration rather than a wrong one",
                           self._printer_id, elapsed)

        return None, None

    def _warn_if_sources_disagree(self, stopwatch_seconds: int) -> None:
        """Make a stopwatch/printer disagreement visible instead of silent.

        The two should agree to within a poll interval whenever the bridge watched the
        whole print. A material gap means one of the assumptions underneath this is
        wrong — the printer's clock is skewed, or (more likely) the bridge was
        unreachable at the moment the print actually began, so its stopwatch missed the
        head of the run and is under-reporting. Either way it silently understates the
        job's cost, which is precisely what the observed duration exists to fix.

        Logged rather than acted on: which source to believe cannot be settled without a
        real print to check against. Watch this line on the first live one.
        """
        if self._started_epoch is None:
            return
        printer_seconds = int(self._wall_clock() - self._started_epoch)
        if abs(printer_seconds - stopwatch_seconds) > _DURATION_DISAGREEMENT_SECONDS:
            logger.warning(
                "printer %s: print duration sources disagree — bridge stopwatch %ss vs "
                "printer gcode_start_time %ss. Reporting the stopwatch. If the printer's "
                "figure is the right one, the bridge missed the start of this print.",
                self._printer_id, stopwatch_seconds, printer_seconds)


class BambuPrinter:
    """A printer connection plus the state the bridge must keep for it: the merged MQTT
    payload, that payload's liveness, and the running print's stopwatch."""

    def __init__(self, cfg: PrinterConfig, stopwatch: Optional[PrintStopwatch] = None,
                 stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
                 monotonic=time.monotonic):
        self._cfg = cfg
        # IP is a cache, the serial (bambu_id) is the identity. Seeded from config, then
        # updated by reconnect() when SSDP finds the serial at a new address (U1) — so a
        # DHCP lease change self-heals instead of stranding the printer at a stale IP.
        self._ip = cfg.ip
        self._client = None
        self._cached: Optional[Dict] = None       # last-known merged payload
        self._stopwatch = stopwatch or PrintStopwatch(cfg.bambu_id)
        self._monotonic = monotonic               # injectable — staleness is otherwise untestable
        self._stale_after_seconds = stale_after_seconds

        # Liveness of `_cached`. `_last_raw` is the last payload the printer actually
        # sent, and `_last_fresh_monotonic` is when it changed — the pair is what tells
        # a live printer apart from a frozen one. **Neither is cleared when the printer
        # goes OFFLINE** (`_cached` is): a dead printer keeps handing back the same dict,
        # so resetting the freshness baseline on the way out would make the next poll
        # read that dict as new data and flap the printer back to PRINTING.
        self._last_raw: Optional[Dict] = None
        self._last_fresh_monotonic: Optional[float] = None
        self._offline = False                     # for logging the edge, not every poll
        self._warned_no_connection_probe = False

    @property
    def bambu_id(self) -> str:
        return self._cfg.bambu_id

    @property
    def current_ip(self) -> str:
        """The address the bridge is currently dialing — seeded from config, then updated
        by reconnect() when SSDP finds the serial somewhere new (U1)."""
        return self._ip

    @property
    def is_offline(self) -> bool:
        """Whether the last snapshot reported this printer OFFLINE. The fleet reads this
        to decide which printers to re-discover and reconnect (U1)."""
        return self._offline

    def connect(self) -> None:
        self._connect(self._ip)

    def _connect(self, ip: str) -> None:
        import bambulabs_api as bl  # lazy: pure tests don't need the library
        self._client = bl.Printer(ip, self._cfg.access_code, self._cfg.bambu_id)
        self._client.connect()
        # Commit the address only after the client is up. If connect() raised, current_ip
        # stays at the old value, so reconcile_connections still sees a mismatch and retries
        # — instead of concluding "paho is already retrying it" about a client that never
        # actually started, which would strand the printer OFFLINE until a restart (U1).
        self._ip = ip
        logger.info("connected to printer %s (%s) at %s", self.bambu_id, self._cfg.name, ip)

    def reconnect(self, new_ip: Optional[str] = None) -> None:
        """Rebuild the MQTT client, optionally at a new IP after the printer's DHCP lease
        moved (U1). Closes the old client best-effort, drops the cached payload and its
        freshness baseline (they described the old address), then connects fresh. The next
        snapshot rebuilds live state and flips the printer back online on its own — so this
        does not reset the `_offline` flag, leaving snapshot() to log the real recovery.

        `current_ip` advances only on a successful connect (see `_connect`): a reconnect
        that can't reach the new address leaves the printer targeting the old one, so the
        next reconcile retries rather than silently stranding it."""
        target_ip = new_ip or self._ip
        self.disconnect()
        self._cached = None
        self._last_raw = None
        self._last_fresh_monotonic = None
        self._connect(target_ip)

    def disconnect(self) -> None:
        """Best-effort close of the MQTT client (used by reconnect and fleet removal).
        Never raises — a printer being torn down must not take the loop down with it."""
        client = self._client
        self._client = None
        if client is None:
            return
        closer = getattr(client, "disconnect", None) or getattr(client, "mqtt_stop", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as e:
            logger.debug("printer %s: closing the MQTT client raised (%s)",
                         self.bambu_id, type(e).__name__)

    def upload_and_start(self, file_path: str, ams_mapping, plate_number: int = 1,
                         remote_name: Optional[str] = None) -> bool:
        """FTPS-upload the sliced `.3mf` and MQTT-start it (U9's dispatch primitive).

        Confirmed accessors (bambulabs-api 2.6.6, isolated here like `_raw_status`):
        `Printer.upload_file(fh, filename)` FTPS-pushes the file, and
        `Printer.start_print(filename, plate_number, use_ams, ams_mapping)` issues the
        MQTT `project_file` start. If a future library renames either, this is the only
        method that changes.

        `ams_mapping` is the **explicit** filament→AMS-tray mapping (R11), a `list[int]`
        of global 0-based tray indices in the sliced file's filament order — the
        deterministic override for the auto-map-by-color hang. The router computes it
        from the printer's own live slot colors (see Dispatcher._ams_mapping).

        **Load-bearing, verify on the first real prints (R-B):** this assumes the sliced
        file's filament order matches the batch's `required_colors` order. Bambu also
        auto-maps by color at start (U1 GO / KTD7), so a correct color *set* prints right
        even if the order is off — but a wrong explicit mapping could misroute a slot.
        Watch the first prints; if the order is wrong, THIS is the single place to fix.

        Raises on a transport/library error (bad FTPS, dropped MQTT) so the router
        re-queues the job rather than losing it; returns the printer's start result
        otherwise.
        """
        if self._client is None:
            raise RuntimeError("printer not connected")
        name = remote_name or os.path.basename(file_path)
        # upload_file closes the handle itself (its `finally: file.close()`).
        fh = open(file_path, "rb")
        self._client.upload_file(fh, name)
        started = self._client.start_print(
            name, plate_number, use_ams=True, ams_mapping=list(ams_mapping),
        )
        logger.info("printer %s: started %s (plate %s, ams_mapping=%s) -> %s",
                    self.bambu_id, name, plate_number, list(ams_mapping), started)
        return bool(started)

    def snapshot(self) -> Dict:
        """One state report for this printer (never raises):

            {
              "bambu_id": str,
              "status": IDLE | PRINTING | PAUSED | NEEDS_CLEARING | ERROR | OFFLINE,
              "slots": [{slot_number, color_hex, filament_type}],  # empty slots too
              <the telemetry fields, flat>,                        # see parse_telemetry
              "print_duration_seconds": int | None,                # observed, not estimated
              "print_duration_source": "bridge" | "printer" | None,
            }

        The telemetry is **flat on the report, not nested** — that is what
        `bridge_state_service.ingest_printer_state` reads
        (`{bambu_id, status, slots, plus the telemetry fields}`). `status` and `slots`
        keep their existing shape, so an older ingest keeps working and the new fields
        are purely additive; unknown keys are ignored on the far side.

        **The cache is bounded by liveness, and that is a safety property.** A printer
        that dies after connecting does not make anything raise: `mqtt_dump()` is a read
        of an accumulating dict the library owns, so an unplugged, powered-off, or
        off-the-LAN printer keeps handing back its last payload — or `{}` — indefinitely.
        Replaying `_cached` on the strength of "nothing threw" would report a printer
        that is PRINTING at 47%, 220°C, forever; a printer that was IDLE when it died
        would report IDLE forever, and IDLE is the sole authorization for dispatch, so
        the next job would be sent to an unplugged machine and its filament deducted.
        Both of the obvious liveness signals lie about this together — the payload never
        changes, so `reported_at` freezes, while the farm's `last_seen_at` stays green
        because the *bridge* is alive. So the last payload is only believed while the
        printer is demonstrably still there:

          * **the MQTT link is up** (`_is_connected`) — authoritative, and cheap; and
          * **the payload is still moving** (`_stale_for`) — the backstop that catches a
            wedged printer or a half-open socket the keepalive has not timed out yet, and
            the only signal at all if a future library drops the connection accessor.

        Either one failing reports OFFLINE and drops the cache. That direction is
        deliberate: a false OFFLINE costs a poll of dispatch (visible, and fails closed),
        a false IDLE costs a print.
        """
        try:
            raw = self._raw_status()
        except Exception as e:
            # The library/transport boundary — not connected, dead socket, missing
            # accessor. This is the ONLY exception that may be read as "unreachable",
            # and (see the class docstring) it is not how a printer normally dies.
            return self._go_offline(f"unreadable ({type(e).__name__})")

        if self._is_connected() is False:
            # Authoritative. In LAN mode the printer *is* the MQTT broker, so paho's
            # keepalive (60s, set by the library) is a liveness check on the printer
            # itself, not on some intermediary.
            return self._go_offline("MQTT link is down")

        self._note_freshness(raw)
        if isinstance(raw, dict) and raw:
            self._cached = merge_status_payload(self._cached, raw)

        if not self._cached:
            # No MQTT push has landed yet — mqtt_dump() returns {} until the first
            # one. We know nothing about this printer, and nothing is not IDLE.
            return self._go_offline("no MQTT payload received yet")

        silent_for = self._stale_for()
        if silent_for is not None:
            return self._go_offline(
                f"nothing new for {silent_for:.0f}s (> {self._stale_after_seconds:.0f}s) — "
                f"the printer is gone, or wedged")

        if self._offline:
            # Covers both a recovery and the first payload after a bridge start.
            logger.info("printer %s: online — reporting live telemetry", self.bambu_id)
            self._offline = False

        try:
            return self._build_snapshot(self._cached)
        except Exception:
            # NOT an unreachable printer — a bug in the bridge's own parsing. Letting it
            # pass for one would silently delete a *live* printer from the UI, leaving a
            # single "unreadable" line as the only trace. Loud, with a traceback, on
            # every poll it happens: an OFFLINE printer that logs ERROR is a bridge bug,
            # an OFFLINE printer that logs WARNING is an absent printer.
            logger.exception(
                "printer %s: parsing its telemetry raised — this is a BRIDGE BUG, not an "
                "unreachable printer. Reporting OFFLINE so nothing dispatches to it.",
                self.bambu_id)
            return self._offline_snapshot()

    def _go_offline(self, reason: str) -> Dict:
        """Report OFFLINE and **drop the cache**, so a recovering printer is rebuilt from
        what it actually says rather than from what it last said before it vanished.

        `_last_raw` / `_last_fresh_monotonic` deliberately survive — see `__init__`.

        Logged on the edge, not on every poll: a printer that has been unplugged for a
        week should not emit a warning every 15 seconds forever.
        """
        if not self._offline:
            logger.warning("printer %s -> OFFLINE: %s", self.bambu_id, reason)
            self._offline = True
        self._cached = None
        return self._offline_snapshot()

    def _note_freshness(self, raw) -> None:
        """Stamp the clock when the printer says something NEW.

        Keyed on the payload *changing*, never on `mqtt_dump()` merely returning
        something: a dead printer's dict is still there, still non-empty, and still
        readable — it just stops changing. That is the whole signal.

        The comparison needs a copy, not a reference: `mqtt_dump()` hands back the
        library's live internal dict and the MQTT thread mutates it in place, so a stored
        reference would compare equal to itself forever and nothing would ever look stale.
        """
        if not isinstance(raw, dict) or not raw:
            return                      # silence, not news
        if raw == self._last_raw:
            return                      # the same frozen payload, not a new one
        self._last_raw = copy.deepcopy(raw)
        self._last_fresh_monotonic = self._monotonic()

    def _stale_for(self) -> Optional[float]:
        """Seconds of silence, if the printer has been quiet too long — else None."""
        if self._last_fresh_monotonic is None:
            return None                 # nothing has ever arrived; the empty cache says so
        silent_for = self._monotonic() - self._last_fresh_monotonic
        return silent_for if silent_for > self._stale_after_seconds else None

    def _build_snapshot(self, payload: Dict) -> Dict:
        print_obj = payload.get("print")
        if not isinstance(print_obj, dict):
            print_obj = {}
        gcode_state = print_obj.get("gcode_state")
        self._stopwatch.observe(gcode_state, print_obj)
        return {
            "bambu_id": self.bambu_id,
            "status": map_status(gcode_state if isinstance(gcode_state, str) else None),
            "slots": parse_ams(payload),
            **parse_telemetry(payload),   # flat, not nested — see snapshot()
            "print_duration_seconds": self._stopwatch.duration_seconds,
            "print_duration_source": self._stopwatch.source,
        }

    def _offline_snapshot(self) -> Dict:
        """Null telemetry and no slots: the printer is unreadable, so we report nothing
        we cannot currently see. A stale temperature or ETA on an unreachable printer
        would be actively misleading, and the ingest clears what stops being reported.
        """
        return {
            "bambu_id": self.bambu_id,
            "status": "OFFLINE",
            "slots": [],
            **parse_telemetry(None),
            "print_duration_seconds": None,
            "print_duration_source": None,
        }

    def _raw_status(self) -> dict:
        """Return the raw Bambu MQTT status dict.

        Confirmed on real P1S hardware (2026-07-13): `mqtt_dump()` returns the status
        payload — `["print"]["gcode_state"]` drives status and `["print"]["ams"]["ams"]`
        holds the AMS trays. It accumulates only one level deep
        (`MqttClient.manual_update` does `self._data[k] |= v` per top-level key) and
        hands back its live internal dict by reference, which is why callers merge it
        into a snapshot of their own — see `merge_status_payload`. If a future
        `bambulabs_api` version renames this accessor, adjust ONLY this method (the rest
        of the bridge consumes the raw dict shape)."""
        if self._client is None:
            raise RuntimeError("printer not connected")
        return self._client.mqtt_dump()

    def _is_connected(self) -> Optional[bool]:
        """Is the MQTT session to this printer actually up? True / False / **None =
        unknown** (the library did not tell us, so the caller must fall back to staleness).

        This is the authoritative liveness signal and the reason the OFFLINE path is
        reachable at all. Read against bambulabs-api 2.6.6 (the pinned version, and the
        one confirmed on a live P1S):

            Printer.mqtt_client_connected()  ->  PrinterMQTTClient.is_connected()
                                             ->  paho `Client.is_connected()`

        It is a real check on *this printer*: in LAN-only mode the printer runs the MQTT
        broker itself, so paho's keepalive (the library passes `timeout=60` to
        `connect_async`, which is paho's keepalive) is pinging the machine. Unplug it and
        paho stops getting PINGRESPs, drops the session, and this goes False — while
        `mqtt_dump()` happily keeps serving the last payload.

        Probed rather than called outright, and a failure degrades to "unknown" rather
        than to False: a renamed accessor in a future version must not silently mark a
        whole healthy farm OFFLINE. Staleness still covers us in that case — it needs no
        library support at all — so this is the only place that has to change.
        """
        if self._client is None:
            return False
        probe = getattr(self._client, "mqtt_client_connected", None)
        if not callable(probe):
            if not self._warned_no_connection_probe:
                logger.warning(
                    "printer %s: this bambulabs_api has no mqtt_client_connected() — the "
                    "bridge cannot see the MQTT link and is falling back to payload "
                    "staleness alone to detect a dead printer (slower, but safe).",
                    self.bambu_id)
                self._warned_no_connection_probe = True
            return None
        try:
            return bool(probe())
        except Exception as e:
            logger.warning("printer %s: mqtt_client_connected() raised (%s); falling back "
                           "to payload staleness", self.bambu_id, type(e).__name__)
            return None


def _minutes_to_seconds(value) -> Optional[int]:
    """`mc_remaining_time` is in MINUTES, and everything downstream of the bridge speaks
    seconds — so it is converted once, here, at the boundary.

    `bambulabs_api`'s own docstring says seconds and is wrong: its `get_remaining_time()`
    returns this field verbatim while promising seconds, and ha-bambulab reads it as
    `timedelta(minutes=...)`. Getting this wrong is a silent 60x error on the single
    number the operator looks at most.
    """
    minutes = as_int(value, None)
    if minutes is None or minutes < 0:
        return None
    return minutes * 60


def _valid_epoch(value) -> Optional[int]:
    """`gcode_start_time` is Unix epoch seconds, and a string on the wire. Printers
    that never set it report "0", which would otherwise measure a print as having
    started in 1970."""
    epoch = as_int(value, None)
    if epoch is None or epoch < _MIN_PLAUSIBLE_EPOCH:
        return None
    return epoch


def _valid_stage(value) -> Optional[int]:
    """`stg_cur` is Bambu's print stage, and **-1 is its "no stage" sentinel** — the
    value every idle printer reports.

    "No stage" is an absence, so it is reported as one. Persisted verbatim it becomes
    the literal string "-1" in `printer_telemetry.stage` (a TEXT column), which reads
    as a real stage: `stage IS NOT NULL` would be true for every idle printer in the
    farm, and any "why did this print pause?" lookup would have to know to special-case
    a magic string. Nulled here, at the same boundary where `_minutes_to_seconds` nulls
    a negative ETA and `_valid_epoch` nulls a zero start time.
    """
    stage = as_int(value, None)
    if stage is None or stage < 0:
        return None
    return stage
