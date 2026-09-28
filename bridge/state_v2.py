"""Printer State v2: what the machine is doing, decided once, here.

PrintForce Link is the only place that turns a printer's raw report into a
state. 3D PrintForce stores this object as-is and adds only farm facts (plate
needs clearing, which file is assigned). See
``docs/references/link-state-contract-v2.md``.

Rules:

* Machine facts only. No farm words: FINISH is ``activity=ended`` with
  ``job.outcome=finished``, never "needs clearing". Whether the plate has been
  cleared is the cloud's to track, from lifecycle events.
* Unknown stays unknown. A missing or unrecognized ``gcode_state`` is
  ``activity=unknown``, never ``idle``.
* ``connection`` and ``activity`` are separate facts. A stale report keeps the
  last known activity; the reader decides what an offline printer shows.

The function is pure: every input is passed in, so each rule has a test.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .bambu.hms import (
    CANCEL_PRINT_ERRORS,
    _norm_error_code,
    fault_print_error,
    hms_faults,
    is_cancel_failed,
    reported_print_error,
)
from .bambu.models import ModelProfile
from .bambu_alerts import lookup_bambu_alert
from .coerce import as_float, as_int, clean_str

CONTRACT = "state_v2"

# Bambu stg_cur -> our label. Numbers are the firmware's; wording is ours.
STAGE_LABELS = {
    0: "Printing",
    1: "Auto bed leveling",
    2: "Heating bed",
    3: "Vibration compensation",
    4: "Changing filament",
    5: "Paused by G-code (M400)",
    6: "Paused: filament ran out",
    7: "Heating nozzle",
    8: "Calibrating extrusion",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate",
    12: "Calibrating lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking nozzle temperature",
    16: "Paused by user",
    17: "Paused: front cover fell off",
    18: "Calibrating lidar",
    19: "Calibrating flow ratio",
    20: "Paused: nozzle temperature fault",
    21: "Paused: bed temperature fault",
    22: "Unloading filament",
    23: "Paused: step loss",
    24: "Loading filament",
    25: "Motor noise calibration",
    26: "Paused: AMS offline",
    27: "Paused: heatbreak fan too slow",
    28: "Paused: chamber temperature fault",
    29: "Cooling chamber",
    30: "Paused by G-code",
    31: "Motor noise check",
    32: "Paused: filament clumped on nozzle",
    33: "Paused: cutter error",
    34: "Paused: first layer problem",
    35: "Paused: nozzle clogged",
}

# Stages that happen before the first layer. RUNNING at 0% in one of these is
# "preparing", not "printing".
_PREP_STAGES = frozenset({1, 2, 3, 7, 8, 9, 11, 12, 13, 14, 15, 18, 19, 25, 29, 31})

# Why a PAUSE happened, from the stage the firmware reports with it.
_PAUSE_REASONS = {
    5: "gcode",
    6: "filament_runout",
    16: "user",
    17: "door_open",
    20: "hardware",
    21: "hardware",
    23: "hardware",
    26: "ams",
    27: "hardware",
    28: "hardware",
    30: "gcode",
    32: "nozzle_clog",
    33: "hardware",
    34: "first_layer",
    35: "nozzle_clog",
}

# Stage values that mean "no stage": X1 sends -1, A1/P1 send 255.
_NO_STAGE = frozenset({-1, 255})

# A nozzle target at or below this is not a print heating up. Field lesson
# from the shop P1S (cloud ``_GHOST_NOZZLE_TARGET_MAX_C``, now retired there).
_COLD_NOZZLE_TARGET_C = 40.0

_FAN_KEYS = ("cooling_fan_speed", "big_fan1_speed", "big_fan2_speed", "heatbreak_fan_speed")

# Session down reasons -> the connection reason 3DPF shows.
_DOWN_REASONS = {
    "auth_rejected": "auth_rejected",
    "unreachable": "unreachable",
    "refused": "refused",
    "silent_session": "silent",
    "commands_ignored": "commands_ignored",
}


def build_state_v2(
    payload: Optional[Dict],
    *,
    connection: str,
    down_reason: Optional[str] = None,
    profile: ModelProfile,
    model_code: Optional[str] = None,
    known_model: bool = False,
    user_cancelled: bool = False,
    state_seq: int = 0,
) -> Dict:
    """One printer's v2 state. Never raises on a malformed payload."""
    print_obj = _print_obj(payload)
    has_payload = bool(print_obj)
    stage_code = _stage(print_obj.get("stg_cur"), profile) if has_payload else None
    activity, stuck = _activity(print_obj, stage_code, user_cancelled) if has_payload else ("unknown", False)
    errors = _errors(print_obj) if has_payload else []
    return {
        "contract": CONTRACT,
        "state_seq": int(state_seq),
        "connection": connection if connection in ("live", "stale", "offline") else "offline",
        "connection_reason": _connection_reason(connection, down_reason, has_payload),
        "activity": activity,
        "stuck_job": stuck,
        "stage": _stage_obj(stage_code, activity),
        "pause_reason": _pause_reason(stage_code, errors) if activity == "paused" else None,
        "job": _job(print_obj, activity, user_cancelled) if has_payload else None,
        "errors": errors,
        "commands_rejected": _commands_rejected(print_obj) if has_payload else None,
        "model": {
            "code": clean_str(model_code),
            "name": profile.name,
            "family": profile.family,
            "known": bool(known_model),
            "verified": bool(known_model and profile.verified),
        },
        "capabilities": list(profile.capabilities),
        "raw": {
            "gcode_state": _gcode_state(print_obj) or None,
            "stg_cur": as_int(print_obj.get("stg_cur"), None) if has_payload else None,
            "print_error": reported_print_error(print_obj.get("print_error")) if has_payload else None,
        },
    }


# --- activity ---------------------------------------------------------------

def _activity(print_obj: Dict, stage: Optional[int], user_cancelled: bool):
    """(activity, stuck_job) from the merged print object."""
    state = _gcode_state(print_obj)
    progress = as_int(print_obj.get("mc_percent"), None)
    if state in ("PREPARE", "SLICING"):
        return "preparing", False
    if state == "RUNNING":
        if _is_stuck_running(print_obj, progress):
            # The firmware still holds a job that is not printing (cold, 0%,
            # nothing queued). Link clears it itself before a start.
            return "idle", True
        if stage in _PREP_STAGES and not progress and not as_int(print_obj.get("layer_num"), 0):
            return "preparing", False
        return "printing", False
    if state == "PAUSE":
        return "paused", False
    if state in ("FINISH", "FAILED"):
        return "ended", False
    if state == "IDLE":
        # Bambu can sit at IDLE while it heats for a file already on the
        # machine, and for a beat mid-print (field lesson, promote_live_idle).
        if progress is not None and 0 < progress < 100:
            return "printing", False
        if progress != 100 and not _start_faulted(print_obj) and _heating_for_a_file(print_obj):
            return "preparing", False
        return "idle", False
    return "unknown", False


def _is_stuck_running(print_obj: Dict, progress: Optional[int]) -> bool:
    """RUNNING label at 0% with a cold nozzle target, empty stage queue, no fan.

    Every field must be present: a real start often omits them for a beat.
    Bed heat does not count. A cancel code is not this.
    """
    if progress != 0:
        return False
    if is_cancel_failed(print_error=print_obj.get("print_error"), hms=print_obj.get("hms")):
        return False
    stg = print_obj.get("stg")
    if not (isinstance(stg, list) and not stg):
        return False
    if "nozzle_target_temper" not in print_obj:
        return False
    nozzle = as_float(print_obj.get("nozzle_target_temper"), None)
    if nozzle is None or nozzle > _COLD_NOZZLE_TARGET_C:
        return False
    for key in _FAN_KEYS:
        speed = as_int(print_obj.get(key), None)
        if speed:
            return False
    return True


def _heating_for_a_file(print_obj: Dict) -> bool:
    nozzle = as_float(print_obj.get("nozzle_target_temper"), None) or 0
    bed = as_float(print_obj.get("bed_target_temper"), None) or 0
    if nozzle <= 0 and bed <= 0:
        return False
    return bool(clean_str(print_obj.get("gcode_file")) or clean_str(print_obj.get("subtask_name")))


def _start_faulted(print_obj: Dict) -> bool:
    """A print error or a serious HMS is up. Heat-up does not carry either."""
    if fault_print_error(print_obj.get("print_error")):
        return True
    return any(f.get("severity") in ("FATAL", "SERIOUS") for f in hms_faults(print_obj.get("hms")))


# --- stage, pause, job, errors ----------------------------------------------

def _stage(value, profile: ModelProfile) -> Optional[int]:
    stage = as_int(value, None)
    if stage is None or stage in _NO_STAGE:
        return None
    return stage


def _stage_obj(stage: Optional[int], activity: str) -> Dict:
    if stage is None:
        return {"code": None, "label": None}
    # A stage is only meaningful during a job. Idle and ended printers keep the
    # last stage number (shop P1S-10 idle at stg 1, P1S-3 ended at stg 2), and
    # A1/P1 report 0 ("printing") while idle.
    if activity not in ("preparing", "printing", "paused"):
        return {"code": None, "label": None}
    if stage == 0 and activity == "preparing":
        return {"code": None, "label": None}
    label = STAGE_LABELS.get(stage)
    if label is None and activity in ("preparing", "printing", "paused"):
        label = "Preparing"
    return {"code": stage, "label": label}


def _pause_reason(stage: Optional[int], errors: List[Dict]) -> str:
    if stage in _PAUSE_REASONS:
        return _PAUSE_REASONS[stage]
    if any(_is_ams_code(e.get("code")) for e in errors):
        return "ams"
    if errors:
        return "error"
    return "unknown"


# HMS codes start with the module that raised them. 07xx is the AMS
# (shop P1S-8 paused on 0700_7000_0002_0008).
def _is_ams_code(code) -> bool:
    return isinstance(code, str) and code.replace("_", "").upper().startswith("07")


def _fallback_title(code) -> str:
    """Title for a code our wording table does not know yet. The code stays beside it."""
    return "AMS alarm" if _is_ams_code(code) else "Printer alarm"


def _job(print_obj: Dict, activity: str, user_cancelled: bool) -> Optional[Dict]:
    name = clean_str(print_obj.get("subtask_name"))
    file = clean_str(print_obj.get("gcode_file"))
    if activity in ("idle", "unknown") and not (name or file):
        return None
    outcome = None
    if activity == "ended":
        state = _gcode_state(print_obj)
        if state == "FINISH":
            outcome = "finished"
        elif user_cancelled or _norm_error_code(print_obj.get("print_error")) in CANCEL_PRINT_ERRORS \
                or is_cancel_failed(hms=print_obj.get("hms")):
            outcome = "cancelled"
        else:
            outcome = "failed"
    remaining = as_int(print_obj.get("mc_remaining_time"), None)
    total = as_int(print_obj.get("total_layer_num"), None)
    return {
        "name": name,
        "file": file,
        "plate": _plate_from_file(file),
        "progress": as_int(print_obj.get("mc_percent"), None),
        "layer": as_int(print_obj.get("layer_num"), None),
        "total_layers": total if total and total > 0 else None,
        "remaining_s": remaining * 60 if remaining is not None and remaining >= 0 else None,
        "outcome": outcome,
    }


def _plate_from_file(file: Optional[str]) -> Optional[int]:
    if not file:
        return None
    lower = file.lower()
    marker = "plate_"
    at = lower.rfind(marker)
    if at < 0:
        return None
    digits = ""
    for ch in lower[at + len(marker):]:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def _errors(print_obj: Dict) -> List[Dict]:
    """Real faults only. Cancel echoes and status indicators are dropped."""
    out: List[Dict] = []
    for fault in hms_faults(print_obj.get("hms")):
        code = fault.get("code")
        copy = lookup_bambu_alert(code) or {}
        out.append({
            "code": code,
            "severity": fault.get("severity"),
            "title": copy.get("title") or _fallback_title(code),
            "detail": copy.get("detail"),
            "source": "hms",
        })
    pe = _print_error_hex(fault_print_error(print_obj.get("print_error")))
    if pe:
        copy = lookup_bambu_alert(pe) or {}
        out.append({
            "code": pe[:4] + "_" + pe[4:] if len(pe) == 8 else pe,
            "severity": "SERIOUS",
            "title": copy.get("title") or _fallback_title(pe),
            "detail": copy.get("detail"),
            "source": "print_error",
        })
    return out


def _print_error_hex(code: Optional[str]) -> Optional[str]:
    """Bambu sends print_error as a decimal integer; its code tables are hex.

    ``50364420`` is ``0300_8004`` (filament runout). Text that already has
    hex letters is left as it is.
    """
    if not code:
        return None
    if code.isdigit():
        try:
            return f"{int(code):08X}"
        except ValueError:
            return code
    return code


def _commands_rejected(print_obj: Dict) -> bool:
    from .bambu.hms import commands_rejected
    return commands_rejected(print_obj.get("hms"))


def _connection_reason(connection: str, down_reason: Optional[str], has_payload: bool) -> str:
    if connection == "live":
        return "ok"
    if down_reason in _DOWN_REASONS:
        return _DOWN_REASONS[down_reason]
    if connection == "stale":
        return "silent"
    if not has_payload:
        return "no_data"
    return "unreachable"


def _print_obj(payload) -> Dict:
    if not isinstance(payload, dict):
        return {}
    print_obj = payload.get("print")
    return print_obj if isinstance(print_obj, dict) else {}


def _gcode_state(print_obj: Dict) -> str:
    state = print_obj.get("gcode_state") if isinstance(print_obj, dict) else None
    return state.strip().upper() if isinstance(state, str) else ""
