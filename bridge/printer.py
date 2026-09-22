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
import json
import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

from .ams import (
    ams_has_color,
    ams_needs_pushall,
    idle_trays_needing_rfid,
    load_remembered_ams,
    merge_ams,
    parse_ams,
    parse_tray_exist_bits,
    save_remembered_ams,
)
from .bambu_alerts import describe_hms
from .coerce import as_float, as_int, clean_str
from .config import PrinterConfig
from .transfer import lan_start_url, store_on_printer

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

# User-cancel on a P1S often lands as FAILED plus one of these, not IDLE. Mapped to
# IDLE so the next Start is not blocked and the router does not report_failed.
# 50348044 is print.print_error; 0300_400C / 0500_400E are HMS index codes.
_CANCEL_PRINT_ERRORS = frozenset({"50348044", "0300400C"})
_CANCEL_HMS_CODES = frozenset({"0300400C", "0500400E"})

# stg_cur values that need retry_filament_action before resume_print (KTD6).
# 6 = runout, 17/20 = load, 21 = unload / AMS, 24 = AMS lost, 35 = clog.
_FILAMENT_RETRY_STAGES = frozenset({6, 17, 20, 21, 24, 35})

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
# A P1 that has stopped pushing still answers `pushing.start` on a live socket.
# That is the cheap resume (not `pushall`, which lags a P1 if it is repeated).
# Sample briefly so the reply can land in this poll; a dead printer does not answer.
_NUDGE_SAMPLES = 3
_NUDGE_SAMPLE_SECONDS = 0.2
# pushall is expensive on the printer. Ask a few times after connect / a partial AMS,
# then wait for Refresh.
_MAX_FULL_STATUS_ATTEMPTS = 3
# mqtt_dump is one-level-deep. Sample across this window after pushall so a
# mid-print delta cannot hide the full AMS behind one late read.
_ABSORB_SAMPLES = 6
_ABSORB_SAMPLE_SECONDS = 0.4
# Raw firmware labels/codes cross the bridge boundary only in this bounded form.
_MAX_FIRMWARE_TEXT = 64


def _norm_error_code(value) -> str:
    if value is None:
        return ""
    return (
        str(value).strip().upper().replace("0X", "").replace("_", "").replace("-", "")
    )[:_MAX_FIRMWARE_TEXT]


def _bounded_text(value, *, upper: bool = False) -> Optional[str]:
    text = clean_str(value)
    if text is None:
        return None
    text = text.upper() if upper else text
    return text[:_MAX_FIRMWARE_TEXT]


def _identifier_present(value) -> bool:
    """Redact firmware identifiers to presence only; zero is Bambu's empty sentinel."""
    if value is None or value == 0:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    # An unexpected non-zero/non-string value cannot safely be treated as empty.
    return True


def _failed_ready_fields(print_obj: Dict) -> Dict:
    """Bounded/redacted evidence used to identify a historical sticky FAILED state."""
    hms_present = "hms" in print_obj
    hms = print_obj.get("hms")
    stg = print_obj.get("stg")
    return {
        "gcode_state": _bounded_text(print_obj.get("gcode_state"), upper=True),
        "hms_present": hms_present,
        "hms_empty": hms_present and isinstance(hms, list) and not hms,
        # Never send task/project ids. Their presence is enough for the safety gate.
        "has_active_file": (
            _identifier_present(print_obj.get("gcode_file"))
            or _identifier_present(print_obj.get("subtask_name"))
        ),
        "has_active_task": (
            _identifier_present(print_obj.get("task_id"))
            or _identifier_present(print_obj.get("subtask_id"))
        ),
        "has_active_project": _identifier_present(print_obj.get("project_id")),
        # None means the field was absent; only an explicitly empty queue qualifies.
        "stage_queue_empty": isinstance(stg, list) and not stg if "stg" in print_obj else None,
        "print_type": _bounded_text(print_obj.get("print_type")),
    }


def _is_zero_or_absent_error(value) -> bool:
    code = _norm_error_code(value)
    return not code or not code.strip("0")


def _is_historical_failed_candidate(print_obj: Dict, fields: Dict) -> bool:
    print_type = (fields.get("print_type") or "").strip().lower()
    return (
        fields.get("gcode_state") == "FAILED"
        and _is_zero_or_absent_error(print_obj.get("print_error"))
        and fields.get("hms_present") is True
        and fields.get("hms_empty") is True
        and fields.get("has_active_file") is False
        and fields.get("has_active_task") is False
        and fields.get("has_active_project") is False
        and as_int(print_obj.get("mc_percent"), None) == 0
        and as_float(print_obj.get("nozzle_target_temper"), None) == 0
        and as_float(print_obj.get("bed_target_temper"), None) == 0
        and fields.get("stage_queue_empty") is True
        and print_type in ("", "idle")
    )


# stg_cur leftovers that still mean "no work on the machine."
# X1 idle is -1. P1 idle is 255. A cooled P1 after a sticky FAILED often
# keeps 0 (Bambu's "printing" id) with no file, 0% progress, and 0 targets —
# P1S-11 on 2026-09-22. That is leftover idle, not a live fault.
_IDLE_STG_CUR = frozenset({None, 0, -1, 255})
_LEFTOVER_BLOCKING_HMS = frozenset({"FATAL", "SERIOUS"})


def _is_cool_target(value) -> bool:
    target = as_float(value, None)
    return target is None or target == 0


def _is_leftover_idle_failed(print_obj: Dict, fields: Dict) -> bool:
    """Sticky FAILED after the machine is already idle.

    historical_failed_ready stays a separate, stricter signal for the cloud
    assignment gate. This remap is the farm-visible status: do not leave
    ERROR on a cooled printer with no file, no print_error, and no HMS.
    """
    if (fields.get("gcode_state") or "").upper() != "FAILED":
        return False
    if not _is_zero_or_absent_error(print_obj.get("print_error")):
        return False
    if fields.get("has_active_file") is True:
        return False
    # Explicit 0 — a FAILED dump that omits percent is not leftover idle.
    if as_int(print_obj.get("mc_percent"), None) != 0:
        return False
    if as_int(print_obj.get("stg_cur"), None) not in _IDLE_STG_CUR:
        return False
    if not _is_cool_target(print_obj.get("nozzle_target_temper")):
        return False
    if not _is_cool_target(print_obj.get("bed_target_temper")):
        return False
    if "hms" in print_obj:
        hms = print_obj.get("hms")
        if not (isinstance(hms, list) and not hms):
            return False
    if (fields.get("hms_count") or 0) > 0:
        return False
    if fields.get("hms_severity") in _LEFTOVER_BLOCKING_HMS:
        return False
    return True


def is_cancel_failed(print_error=None, hms_code=None, hms=None) -> bool:
    """True when the printer is sitting on a user-cancel, not a real fail."""
    pe = _norm_error_code(print_error)
    if pe in _CANCEL_PRINT_ERRORS:
        return True
    candidates = []
    if hms_code is not None:
        candidates.append(hms_code)
    if isinstance(hms, str):
        candidates.append(hms)
    elif isinstance(hms, list):
        for item in hms:
            if isinstance(item, dict):
                candidates.append(item.get("code"))
            else:
                candidates.append(item)
    for raw in candidates:
        normalized = _norm_error_code(raw)
        if not normalized:
            continue
        if normalized in _CANCEL_HMS_CODES or any(code in normalized for code in _CANCEL_HMS_CODES):
            return True
    return False


def map_status(gcode_state: Optional[str], *, print_error=None,
               hms_code=None, hms=None, user_cancelled=False) -> str:
    """Map a Bambu gcode_state to a 3DPF printer status.

    Unknown and blank states map to **OFFLINE, never IDLE**. IDLE is the sole
    authorization for dispatch, so it has to be positively asserted by the printer: a
    fail-open default would dispatch a job onto a busy printer, deduct its filament,
    and stamp the batch PRINTING for a print that never starts. The window is real,
    not theoretical — `mqtt_dump()` returns {} until the first MQTT push lands, so on
    every bridge start there is an interval in which each printer, *including one
    mid-print*, has no gcode_state at all.

    A cancel-failed print (`50348044` / HMS `0300_400C`) maps to IDLE, not ERROR,
    so the next Start is not blocked. FAILED without a cancel code stays ERROR.
    Only remap ERROR — a leftover cancel code on RUNNING must not hide a live print.
    """
    mapped = _STATE_MAP.get((gcode_state or "").strip().upper(), "OFFLINE")
    if mapped == "ERROR" and (
        user_cancelled
        or is_cancel_failed(print_error=print_error, hms_code=hms_code, hms=hms)
    ):
        return "IDLE"
    return mapped


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
        # says PAUSE. X1 sends -1 and P1 sends 255 for "no stage"; both are None.
        "stage": _valid_stage(print_obj.get("stg_cur")),
        "tray_exist_bits": parse_tray_exist_bits(status),
        # print.print_error is 0 when nothing is wrong. Persist the non-zero code so
        # ingest can tell a user-cancel (50348044) from a real fail.
        "print_error": _print_error_str(print_obj.get("print_error")),
    }
    telemetry.update(decode_hms(print_obj.get("hms")))
    telemetry.update(describe_hms(
        hms_code=telemetry.get("hms_code"),
        print_error=telemetry.get("print_error"),
    ))
    telemetry.update(_failed_ready_fields(print_obj))
    return telemetry


def merge_status_payload(cached: Optional[dict], incoming: Optional[dict]) -> Dict:
    """Merge a (possibly partial) Bambu MQTT payload into the last-known one.

    Most Bambu reports are partial deltas — only a `pushall` carries the whole object —
    so without this, a poll that lands between deltas blanks the temperatures and the
    ETA.

    The merge is deliberately **shallow at the `print` level**:

      * scalars merge key-by-key, so a delta that omits `nozzle_temper` keeps the last
        known value rather than blanking it;
      * `ams` is merged by `merge_ams`: a P1 print delta that only details the
        active tray must not blank RFID colours on trays `tray_exist_bits` still
        marks loaded. A real unload (bit cleared, or no bits and an id-only tray)
        still replaces.

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
            incoming_print = copy.deepcopy(value)
            if "ams" in incoming_print:
                previous_ams = previous.get("ams") if isinstance(previous, dict) else None
                incoming_print["ams"] = merge_ams(previous_ams, incoming_print.get("ams"))
            print_obj.update(incoming_print)
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
                 monotonic=time.monotonic, ams_cache_path: Optional[str] = None,
                 sleep=time.sleep):
        self._cfg = cfg
        # IP is a cache, the serial (bambu_id) is the identity. Seeded from config, then
        # updated by reconnect() when SSDP finds the serial at a new address (U1) — so a
        # DHCP lease change self-heals instead of stranding the printer at a stale IP.
        self._ip = cfg.ip
        self._client = None
        self._cached: Optional[Dict] = None       # last-known merged payload
        self._payload_lock = threading.Lock()
        self._stopwatch = stopwatch or PrintStopwatch(cfg.bambu_id)
        self._monotonic = monotonic               # injectable — staleness is otherwise untestable
        self._sleep = sleep
        self._stale_after_seconds = stale_after_seconds

        # Liveness of `_cached`. `_last_raw` is the last payload the printer actually
        # sent, and `_last_fresh_monotonic` is when it changed — the pair is what tells
        # a live printer apart from a frozen one. **Neither is cleared when the printer
        # goes OFFLINE** (`_cached` is): a dead printer keeps handing back the same dict,
        # so resetting the freshness baseline on the way out would make the next poll
        # read that dict as new data and flap the printer back to PRINTING.
        self._last_raw: Optional[Dict] = None
        self._last_fresh_monotonic: Optional[float] = None
        # Any MQTT report, even one whose merged dump did not change. A finished
        # plate often republishes the same temperatures; that is still a heartbeat.
        self._last_message_monotonic: Optional[float] = None
        self._offline = False                     # for logging the edge, not every poll
        # Set when silence or a down socket outlasts paho's own retry. The fleet
        # rebuilds that session even if SSDP still answers at the current IP.
        self._needs_session_rebuild = False
        self._link_down_monotonic: Optional[float] = None
        self._awaiting_first_report_monotonic: Optional[float] = None
        self._last_nudge_monotonic: Optional[float] = None
        self._warned_no_connection_probe = False
        self._historical_failed_streak = 0
        self._asked_full_status = False
        self._full_status_attempts = 0
        self._last_gcode_state: Optional[str] = None
        self._ams_cache_path = ams_cache_path
        # User-cancel on a P1 is print_error 50348044 for about two seconds, then
        # the code clears. Latch the rising edge so a later FAILED with 0 is still
        # a cancel, not report_failed.
        self._user_cancelled = False
        self._last_print_error = ""

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

    @property
    def needs_session_rebuild(self) -> bool:
        """The MQTT session is wedged, not merely between paho retries.

        True after a printer we have heard from goes silent on a socket that still
        looks up, or stays disconnected past the staleness window. The fleet rebuilds
        that client even when SSDP reports the same IP. A brief drop leaves this
        false so an in-progress paho retry is not thrown away.
        """
        return self._needs_session_rebuild

    def connect(self) -> None:
        self._connect(self._ip)

    def _connect(self, ip: str) -> None:
        import bambulabs_api as bl  # lazy: pure tests don't need the library
        self._client = bl.Printer(ip, self._cfg.access_code, self._cfg.bambu_id)
        self._attach_mqtt_listener()
        self._client.connect()
        # Commit the address only after the client is up. If connect() raised, current_ip
        # stays at the old value, so reconcile_connections still sees a mismatch and retries
        # — instead of concluding "paho is already retrying it" about a client that never
        # actually started, which would strand the printer OFFLINE until a restart (U1).
        self._ip = ip
        logger.info("connected to printer %s (%s) at %s", self.bambu_id, self._cfg.name, ip)
        self._asked_full_status = False
        self._full_status_attempts = 0
        self._request_ams_if_needed()

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
        self._last_message_monotonic = None
        self._needs_session_rebuild = False
        self._link_down_monotonic = None
        self._awaiting_first_report_monotonic = None
        self._last_nudge_monotonic = None
        self._historical_failed_streak = 0
        self._last_gcode_state = None
        self._connect(target_ip)

    def disconnect(self) -> None:
        """Best-effort close of the MQTT client (used by reconnect and fleet removal).
        Never raises — a printer being torn down must not take the loop down with it."""
        client = self._client
        self._client = None
        self._asked_full_status = False
        self._full_status_attempts = 0
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
        name = self.upload_file(file_path, remote_name=remote_name)
        return self.start_print(name, ams_mapping, plate_number)

    def upload_file(self, file_path: str, remote_name: Optional[str] = None) -> str:
        """FTPS-upload only. Split from start so a live stop can abort after the push."""
        if self._client is None:
            raise RuntimeError("printer not connected")
        name = remote_name or os.path.basename(file_path)
        try:
            store_on_printer(self._ip, self._cfg.access_code, file_path, name)
        except Exception:
            # Shop upload owns TLS-close and SIZE. The library is a fallback when
            # this host cannot open implicit FTPS (tests, or a missing listener).
            library_upload = getattr(self._client, "upload_file", None)
            if not callable(library_upload):
                raise
            fh = open(file_path, "rb")
            library_upload(fh, name)
        logger.info("printer %s: uploaded %s", self.bambu_id, name)
        return name

    def start_print(self, remote_name: str, ams_mapping, plate_number: int = 1) -> bool:
        """MQTT-start a file already on the printer. A True return is not an ack."""
        if self._client is None:
            raise RuntimeError("printer not connected")
        url = lan_start_url(remote_name)
        started = self._publish_command({
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": f"Metadata/plate_{int(plate_number)}.gcode",
                "url": url,
                "subtask_name": remote_name,
                "use_ams": True,
                "ams_mapping": list(ams_mapping),
            },
        })
        if not started:
            library_start = getattr(self._client, "start_print", None)
            if callable(library_start):
                started = bool(library_start(
                    remote_name, plate_number, use_ams=True,
                    ams_mapping=list(ams_mapping),
                ))
        logger.info("printer %s: started %s (plate %s, ams_mapping=%s, url=%s) -> %s",
                    self.bambu_id, remote_name, plate_number, list(ams_mapping),
                    url, started)
        return bool(started)

    def pause_print(self) -> bool:
        """Publish pause. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("pause_print")

    def resume_print(self) -> bool:
        """Publish resume. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("resume_print")

    def stop_print(self) -> bool:
        """Publish stop. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("stop_print")

    def request_full_status(self, *, read_idle_rfid: bool = True) -> bool:
        """Ask the printer for a full MQTT dump so AMS trays land in the next snapshot.

        P1-series printers only send AMS on `pushing.pushall`, not on the incremental
        reports the poll already reads. True is not an ack — the next `snapshot()`
        that carries a `slots` list is the confirmation. Automatic pushall
        (connect, snapshot, print-end) and Refresh both send `ams_get_rfid`
        when loaded trays still have no hex.
        """
        if self._client is None:
            raise RuntimeError("printer not connected")
        pushall = getattr(self._client, "pushall", None)
        if not callable(pushall):
            mqtt = None
            for name in ("mqtt_client", "_mqtt_client"):
                mqtt = getattr(self._client, name, None)
                if mqtt is not None:
                    break
            pushall = getattr(mqtt, "pushall", None) if mqtt is not None else None
        if not callable(pushall):
            raise RuntimeError("printer client has no pushall()")
        result = pushall()
        logger.info("printer %s: pushall -> %s", self.bambu_id, result)
        self._absorb_status_after_pushall(read_idle_rfid=read_idle_rfid)
        return bool(result)

    def _absorb_status_after_pushall(self, *, read_idle_rfid: bool = False) -> None:
        """Merge the pushall window, then ask idle trays to read RFID if still blank.

        mqtt_dump is one-level-deep (`_data[k] |= v`). A P1 delta replaces `print.ams`
        before a poll can copy it. Live messages are merged in `_ingest_status`; this
        loop only waits for those merges and for `ams_get_rfid` replies.
        """
        try:
            if self._absorb_until_complete():
                return
            if read_idle_rfid and self._request_idle_rfid():
                self._absorb_until_complete()
        finally:
            finish = getattr(self._client, "finish_absorb", None)
            if callable(finish):
                finish()

    def _absorb_until_complete(self) -> bool:
        for i in range(_ABSORB_SAMPLES):
            if i:
                self._sleep(_ABSORB_SAMPLE_SECONDS)
            try:
                raw = self._raw_status()
            except Exception:
                continue
            self._ingest_status(raw)
            if self._ams_complete():
                return True
        return self._ams_complete()

    def _ams_complete(self) -> bool:
        with self._payload_lock:
            return not ams_needs_pushall(self._cached or {})

    def _ingest_status(self, raw) -> bool:
        """Merge one MQTT document into `_cached`. The library dump is not the source
        of truth for AMS: it has already dropped idle-tray hex by the time we poll.
        """
        if not isinstance(raw, dict) or not raw:
            return False
        with self._payload_lock:
            if self._cached is None:
                self._seed_remembered_ams()
            self._cached = merge_status_payload(self._cached, raw)
            self._note_cancel_edge(self._cached)
            return self._note_freshness(raw)

    def _note_cancel_edge(self, payload) -> None:
        """Latch user-cancel on print_error rising to 50348044.

        The code lasts about two seconds. A later FAILED dump can already have
        print_error 0. Without the latch that looks like a real fail.
        """
        print_obj = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(print_obj, dict):
            return
        state = print_obj.get("gcode_state")
        gcode = state.strip().upper() if isinstance(state, str) else ""
        if gcode in {"PREPARE", "SLICING", "RUNNING"}:
            self._user_cancelled = False
            self._last_print_error = ""
        current = _norm_error_code(print_obj.get("print_error"))
        previous = self._last_print_error
        if current in _CANCEL_PRINT_ERRORS and previous not in _CANCEL_PRINT_ERRORS:
            self._user_cancelled = True
        self._last_print_error = current

    def _attach_mqtt_listener(self) -> None:
        mqtt = getattr(self._client, "mqtt_client", None) if self._client else None
        if mqtt is None:
            return
        mqtt.on_message_handler = self._on_library_mqtt_message

    def _on_library_mqtt_message(self, mqtt_client, client, userdata, msg) -> None:
        try:
            payload = getattr(msg, "payload", msg)
            if isinstance(payload, (bytes, bytearray)):
                doc = json.loads(payload)
            elif isinstance(payload, str):
                doc = json.loads(payload)
            elif isinstance(payload, dict):
                doc = payload
            else:
                return
        except (TypeError, ValueError):
            return
        self._last_message_monotonic = self._monotonic()
        self._ingest_status(doc)

    def _publish_command(self, payload: dict, *, wait: bool = True) -> bool:
        client = self._client
        if client is None:
            return False
        method = getattr(client, "publish_command", None)
        if callable(method):
            return bool(method(payload))
        mqtt = getattr(client, "mqtt_client", None)
        if mqtt is None:
            return False
        # The library's publish waits until the broker acks. A half-open socket
        # never acks, and that wait runs on the fleet's one reporter thread.
        # Callers that are only nudging a quiet printer must not block on it.
        if wait:
            library_publish = getattr(mqtt, "_PrinterMQTTClient__publish_command", None)
            if callable(library_publish):
                return bool(library_publish(payload))
        paho = getattr(mqtt, "_client", None)
        topic = getattr(mqtt, "command_topic", None)
        if paho is None or not topic:
            return False
        try:
            result = paho.publish(topic, json.dumps(payload))
        except Exception:
            return False
        if not wait:
            return True
        wait_fn = getattr(result, "wait_for_publish", None)
        if callable(wait_fn):
            try:
                wait_fn()
            except Exception:
                return False
        published = getattr(result, "is_published", None)
        return bool(published()) if callable(published) else True

    def _request_idle_rfid(self) -> bool:
        """`ams_get_rfid` is the printer command HA uses to read one P1 tray."""
        with self._payload_lock:
            trays = list(idle_trays_needing_rfid(self._cached or {}))
        if not trays:
            return False
        asked = False
        for ams_id, slot_id in trays:
            ok = self._publish_command({
                "print": {
                    "sequence_id": "0",
                    "command": "ams_get_rfid",
                    "ams_id": ams_id,
                    "slot_id": slot_id,
                },
            })
            if ok:
                asked = True
                logger.info(
                    "printer %s: ams_get_rfid ams=%s slot=%s",
                    self.bambu_id, ams_id, slot_id,
                )
        return asked

    def _request_ams_if_needed(self) -> None:
        """Ask for a full dump after connect, and again while loaded trays have no hex.

        `pushall` alone does not read idle P1 RFID. When the dump still has
        bit-present trays with no hex, follow it with `ams_get_rfid` the same
        way Refresh does. Print-end resets the attempt budget so a Refresh
        that ran during the job can try again once the printer is idle.
        """
        if self._full_status_attempts >= _MAX_FULL_STATUS_ATTEMPTS:
            return
        try:
            if self.request_full_status(read_idle_rfid=True):
                self._full_status_attempts += 1
                self._asked_full_status = True
        except Exception:
            logger.info("printer %s: full AMS dump not available yet", self.bambu_id)

    def _seed_remembered_ams(self) -> None:
        if self._cached is not None:
            return
        remembered = load_remembered_ams(self._ams_cache_path, self.bambu_id)
        if remembered:
            self._cached = {"print": {"ams": remembered}}

    def _remember_ams(self) -> None:
        if not isinstance(self._cached, dict):
            return
        print_obj = self._cached.get("print")
        ams = print_obj.get("ams") if isinstance(print_obj, dict) else None
        if ams_has_color(ams):
            save_remembered_ams(self._ams_cache_path, self.bambu_id, ams)

    def retry_filament_action(self) -> bool:
        """Retry a halted AMS / load / runout action, then the caller resumes."""
        return self._mqtt_command("retry_filament_action")

    def resume_from_stage(self, stage: Optional[int] = None) -> bool:
        """Cloud sent `resume`. Pick MQTT from live stg_cur (KTD6)."""
        if stage is None:
            try:
                stage = self.snapshot().get("stage")
            except Exception:
                stage = None
        if stage in _FILAMENT_RETRY_STAGES:
            self.retry_filament_action()
        return self.resume_print()

    def _mqtt_command(self, method_name: str) -> bool:
        if self._client is None:
            raise RuntimeError("printer not connected")
        method = getattr(self._client, method_name, None)
        if not callable(method):
            raise RuntimeError(f"printer client has no {method_name}()")
        result = method()
        logger.info("printer %s: %s -> %s", self.bambu_id, method_name, result)
        return bool(result)

    def snapshot(self) -> Dict:
        """One state report for this printer (never raises):

            {
              "bambu_id": str,
              "status": IDLE | PRINTING | PAUSED | NEEDS_CLEARING | ERROR | OFFLINE,
              "slots": [{slot_number, color_hex, filament_type}] | None,  # see below
              <the telemetry fields, flat>,                        # see parse_telemetry
              "gcode_state": str | None,                            # bounded firmware state
              "hms_present": bool, "hms_empty": bool,
              "has_active_file": bool, "has_active_task": bool,
              "has_active_project": bool,                           # ids are never exposed
              "stage_queue_empty": bool | None, "print_type": str | None,
              "historical_failed_ready": bool,                      # separate from status
              "print_duration_seconds": int | None,                # observed, not estimated
              "print_duration_source": "bridge" | "printer" | None,
            }

        The telemetry is **flat on the report, not nested** — that is what
        `bridge_state_service.ingest_printer_state` reads
        (`{bambu_id, status, slots, plus the telemetry fields}`). `status` and `slots`
        keep their existing shape, so an older ingest keeps working and the new fields
        are purely additive; unknown keys are ignored on the far side.

        **`slots` is None while this printer has reported no AMS unit list**, which is
        its normal state from connecting until the first full Bambu push lands. `[]`
        would claim the AMS is empty and make the cloud delete every slot row for a
        live printer; None says "no information" and leaves them alone. See `parse_ams`.

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
          * **the printer is still talking** (`_stale_for`) — a new report, or a merged
            dump that changed. A finished plate often repeats the same temperatures, so
            an unchanged dump is not by itself a dead printer. A socket that still looks
            up but has gone quiet is asked once, with `pushing.start`, to resume. No
            answer reports OFFLINE and asks the fleet to rebuild the session, including
            at the same IP: paho will not reconnect a socket it still considers healthy.

        A link that is actually down reports OFFLINE immediately. paho is given the
        staleness window to retry; if it is still down after that, the session is
        rebuilt too, including a session that has not heard the printer yet. A restart
        clears that memory, and a quiet P1 does not push until asked, so "never heard"
        used to skip the same-IP rebuild and leave the printer OFFLINE at the address
        SSDP still reports. A printer that never answers stays OFFLINE — this does not
        turn a frozen dump into IDLE. A false OFFLINE costs a poll of dispatch
        (visible, and fails closed); a false IDLE costs a print.
        """
        try:
            raw = self._raw_status()
        except Exception as e:
            # The library/transport boundary — not connected, dead socket, missing
            # accessor. This is the ONLY exception that may be read as "unreachable",
            # and (see the class docstring) it is not how a printer normally dies.
            # A dump that keeps throwing is not a paho retry in progress.
            self._note_link_down()
            return self._go_offline(f"unreadable ({type(e).__name__})")

        connected = self._is_connected()
        if connected is False:
            # Authoritative. In LAN mode the printer *is* the MQTT broker, so paho's
            # keepalive (60s, set by the library) is a liveness check on the printer
            # itself, not on some intermediary. A brief drop is left for paho; a socket
            # that stays down is not actually retrying (see `_note_link_down`).
            self._note_link_down()
            return self._go_offline("MQTT link is down")
        if connected is True:
            self._link_down_monotonic = None

        fresh = self._ingest_status(raw) if isinstance(raw, dict) and raw else False

        if not self._cached:
            # No MQTT push has landed yet — mqtt_dump() returns {} until the first
            # one. We know nothing about this printer, and nothing is not IDLE.
            # A quiet P1 will not send that first push on its own. After the same
            # window we give a dead printer, ask it to start, then rebuild the
            # session if it still says nothing. Inside the window, leave the
            # in-progress connect alone.
            if connected is not False and self._first_report_overdue():
                if not (self._nudge_recovered() and self._cached):
                    self._needs_session_rebuild = True
                    return self._go_offline("no MQTT payload received yet")
            else:
                return self._go_offline("no MQTT payload received yet")

        silent_for = self._stale_for()
        if silent_for is not None and connected is not False and self._nudge_recovered():
            silent_for = None
        if silent_for is not None:
            # Heard before, quiet now, and a resume request did not bring a report.
            # The socket may still say "up". paho will not rebuild that session.
            if self._last_fresh_monotonic is not None or self._last_message_monotonic is not None:
                self._needs_session_rebuild = True
            return self._go_offline(
                f"nothing new for {silent_for:.0f}s (> {self._stale_after_seconds:.0f}s) — "
                f"the printer is gone, or the MQTT session is wedged")

        if self._offline:
            # Covers both a recovery and the first payload after a bridge start.
            logger.info("printer %s: online — reporting live telemetry", self.bambu_id)
            self._offline = False
        self._needs_session_rebuild = False
        self._awaiting_first_report_monotonic = None
        self._last_nudge_monotonic = None

        try:
            gcode_state = None
            print_obj = self._cached.get("print")
            if isinstance(print_obj, dict):
                raw_state = print_obj.get("gcode_state")
                if isinstance(raw_state, str):
                    gcode_state = raw_state.strip().upper()
            if (
                self._last_gcode_state in _PRINT_IN_PROGRESS
                and gcode_state in (_PRINT_ENDED | {"IDLE", "PAUSE"})
            ):
                self._full_status_attempts = 0
            self._last_gcode_state = gcode_state
            if ams_needs_pushall(self._cached):
                self._request_ams_if_needed()
            self._remember_ams()
            return self._build_snapshot(self._cached, fresh=fresh)
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
        with self._payload_lock:
            self._cached = None
        self._historical_failed_streak = 0
        return self._offline_snapshot()

    def _note_freshness(self, raw) -> bool:
        """Stamp the clock when the printer says something NEW.

        Keyed on the payload *changing*, never on `mqtt_dump()` merely returning
        something: a dead printer's dict is still there, still non-empty, and still
        readable — it just stops changing. That is the whole signal.

        The comparison needs a copy, not a reference: `mqtt_dump()` hands back the
        library's live internal dict and the MQTT thread mutates it in place, so a stored
        reference would compare equal to itself forever and nothing would ever look stale.
        """
        if not isinstance(raw, dict) or not raw:
            return False                # silence, not news
        if raw == self._last_raw:
            return False                # the same frozen payload, not a new one
        self._last_raw = copy.deepcopy(raw)
        self._last_fresh_monotonic = self._monotonic()
        return True

    def _heard_monotonic(self) -> Optional[float]:
        """When this printer last proved it was talking, or None if it never has."""
        heard = self._last_fresh_monotonic
        message = self._last_message_monotonic
        if heard is None:
            return message
        if message is None:
            return heard
        return max(heard, message)

    def _stale_for(self) -> Optional[float]:
        """Seconds of silence, if the printer has been quiet too long — else None."""
        heard = self._heard_monotonic()
        if heard is None:
            return None                 # nothing has ever arrived; the empty cache says so
        silent_for = self._monotonic() - heard
        return silent_for if silent_for > self._stale_after_seconds else None

    def _note_link_down(self) -> None:
        """Give paho one staleness window, then ask the fleet to rebuild the client.

        A same-IP outage used to be left to paho forever. When that retry never
        lands, the printer stays OFFLINE until someone restarts Link. A session
        that has never heard the printer is included: a restart forgets that the
        printer used to answer, and paho will not replace a socket that never
        came up if SSDP still reports the same address.
        """
        now = self._monotonic()
        if self._link_down_monotonic is None:
            self._link_down_monotonic = now
            return
        if now - self._link_down_monotonic > self._stale_after_seconds:
            self._needs_session_rebuild = True

    def _first_report_overdue(self) -> bool:
        """True once a connected session has produced no MQTT message for the window.

        The first poll only starts the clock, so a connect that is still receiving
        its first push is not rebuilt. A printer we have already heard is not on
        this path.
        """
        if self._heard_monotonic() is not None:
            self._awaiting_first_report_monotonic = None
            return False
        now = self._monotonic()
        started = self._awaiting_first_report_monotonic
        if started is None:
            self._awaiting_first_report_monotonic = now
            return False
        return now - started > self._stale_after_seconds

    def _nudge_recovered(self) -> bool:
        """Ask a quiet printer to resume reports. True only if one actually arrives.

        At most once per staleness window, and only a few short samples: the reporter
        loop is shared by the whole fleet. `pushing.start` is the documented resume
        for a P1 that silently stopped pushing; it is not a `pushall`.
        """
        now = self._monotonic()
        if (
            self._last_nudge_monotonic is not None
            and now - self._last_nudge_monotonic < self._stale_after_seconds
        ):
            return False
        self._last_nudge_monotonic = now
        sent = self._publish_command(
            {"pushing": {"sequence_id": "0", "command": "start"}},
            wait=False,
        )
        if not sent:
            return False
        logger.info("printer %s: reports went quiet; requested pushing.start", self.bambu_id)
        heard_before = self._heard_monotonic()
        for i in range(_NUDGE_SAMPLES):
            if i:
                self._sleep(_NUDGE_SAMPLE_SECONDS)
            try:
                raw = self._raw_status()
            except Exception:
                return False
            if isinstance(raw, dict) and raw and self._ingest_status(raw):
                return True
            # Never-heard is also `_stale_for() is None`. Only a message that
            # arrived during this ask counts as the printer answering.
            heard_now = self._heard_monotonic()
            if heard_now is not None and heard_now != heard_before:
                return True
        return False

    def _build_snapshot(self, payload: Dict, *, fresh: bool) -> Dict:
        print_obj = payload.get("print")
        if not isinstance(print_obj, dict):
            print_obj = {}
        gcode_state = print_obj.get("gcode_state")
        self._stopwatch.observe(gcode_state, print_obj)
        telemetry = parse_telemetry(payload)
        if fresh and _is_historical_failed_candidate(print_obj, telemetry):
            self._historical_failed_streak += 1
        else:
            # Duplicate/non-fresh reports and every disqualifier break consecutiveness.
            self._historical_failed_streak = 0
        status = map_status(
            gcode_state if isinstance(gcode_state, str) else None,
            print_error=telemetry.get("print_error"),
            hms_code=telemetry.get("hms_code"),
            hms=print_obj.get("hms"),
            user_cancelled=self._user_cancelled,
        )
        if status == "ERROR" and _is_leftover_idle_failed(print_obj, telemetry):
            status = "IDLE"
        return {
            "bambu_id": self.bambu_id,
            "status": status,
            # None — not [] — while this printer's payload carries no AMS unit list,
            # which is its normal state between connecting and the first full push. The
            # cloud DELETES slot rows to match a reported list, so an `[]` here wipes the
            # trays of a live machine that simply has not been asked yet. See `parse_ams`.
            "slots": parse_ams(payload),
            **telemetry,   # flat, not nested — see snapshot()
            # Leftover idle FAILED is remapped to IDLE above. This stricter
            # signal still lets the cloud check assignment state on dumps that
            # do not meet leftover-idle (file leftover, heat, HMS).
            "historical_failed_ready": self._historical_failed_streak >= 2,
            "user_cancelled": self._user_cancelled,
            "print_duration_seconds": self._stopwatch.duration_seconds,
            "print_duration_source": self._stopwatch.source,
        }

    def _offline_snapshot(self) -> Dict:
        """Null telemetry and no slots: the printer is unreadable, so we report nothing
        we cannot currently see. A stale temperature or ETA on an unreachable printer
        would be actively misleading, and the ingest clears what stops being reported.
        """
        self._historical_failed_streak = 0
        return {
            "bambu_id": self.bambu_id,
            "status": "OFFLINE",
            "slots": [],
            **parse_telemetry(None),
            "historical_failed_ready": False,
            "user_cancelled": False,
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


def _print_error_str(value) -> Optional[str]:
    """`print_error` is 0 when nothing is wrong. That is an absence, not a code."""
    code = _norm_error_code(value)
    if not code or not code.strip("0"):
        return None
    return code


def _valid_stage(value) -> Optional[int]:
    """`stg_cur` is Bambu's print stage. Idle sentinels are not a pause reason.

    X1 sends -1. P1 sends 255. Both mean "no stage." Persisted verbatim they
    become a fake stage in `printer_telemetry.stage` (a TEXT column): every idle
    P1 would look paused. Nulled here, at the same boundary where a negative
    ETA and a zero start time become None.
    """
    stage = as_int(value, None)
    if stage is None or stage < 0 or stage == 255:
        return None
    return stage
