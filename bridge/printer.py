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

_STATE_MAP = {
    "IDLE": "IDLE",
    "PREPARE": "PRINTING",
    "SLICING": "PRINTING",
    "RUNNING": "PRINTING",
    "PAUSE": "PAUSED",
    "FINISH": "NEEDS_CLEARING",
    "FAILED": "ERROR",
}

_HMS_SEVERITY = {1: "FATAL", 2: "SERIOUS", 3: "COMMON", 4: "INFO"}
_HMS_UNKNOWN_RANK = 99
_PRINT_IN_PROGRESS = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
_PRINT_ENDED = frozenset({"FINISH", "FAILED"})
_PRINT_START_EVIDENCE = frozenset({"IDLE", "FINISH", "FAILED"})
_CANCEL_PRINT_ERRORS = frozenset({"50348044", "0300400C"})
_CANCEL_HMS_CODES = frozenset({"0300400C", "0500400E"})
_FILAMENT_RETRY_STAGES = frozenset({6, 17, 20, 21, 24, 35})
_MAX_PLAUSIBLE_PRINT_SECONDS = 30 * 24 * 60 * 60
_MIN_PLAUSIBLE_EPOCH = 1_000_000_000
_DURATION_DISAGREEMENT_SECONDS = 120
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
    mapped = _STATE_MAP.get((gcode_state or "").strip().upper(), "OFFLINE")
    if mapped == "ERROR" and is_cancel_failed(
        print_error=print_error, hms_code=hms_code, hms=hms,
    ):
        return "IDLE"
    return mapped
