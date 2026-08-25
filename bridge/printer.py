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


def _norm_error_code(value) -> str:
    if value is None:
        return ""
    return str(value).strip().upper().replace("0X", "").replace("_", "").replace("-", "")


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
               hms_code=None, hms=None) -> str:
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
    if mapped == "ERROR" and is_cancel_failed(
        print_error=print_error, hms_code=hms_code, hms=hms,
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
        # says PAUSE. Bambu's "no stage" sentinel is -1, normalised to None here.
        "stage": _valid_stage(print_obj.get("stg_cur")),
        "tray_exist_bits": parse_tray_exist_bits(status),
        # print.print_error is 0 when nothing is wrong. Persist the non-zero code so
        # ingest can tell a user-cancel (50348044) from a real fail.
        "print_error": _print_error_str(print_obj.get("print_error")),
    }
    telemetry.update(decode_hms(print_obj.get("hms")))
    return telemetry
