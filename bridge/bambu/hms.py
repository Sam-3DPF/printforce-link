"""HMS entries, the legacy alarm summary, and the filtered fault view.

Severity is ``code >> 16`` (1 fatal … 4 info). A severity of 0 is a status
indicator, not a fault. The same class of code shows up as a ``print_error``
whose low 16 bits are below 0x4000.

Cancel echoes ``0300_400C`` and ``0500_400E`` are not faults either. The
legacy summary still reports them: 3DPF detects a user cancel from
``hms_code`` and ``print_error`` until it reads ``hms_faults``.
"""

from typing import Dict, List, Optional

from ..coerce import as_int

# User-cancel on a P1S often lands as FAILED plus one of these, not IDLE.
# 50348044 is the decimal print_error. 0300400C is that same code in hex.
# The code is gone again in about two seconds; the state latch catches the edge.
CANCEL_PRINT_ERRORS = frozenset({"50348044", "0300400C"})
# First 8 hex digits of an HMS code (the ``attr`` word). 3DPF matches these
# inside ``hms_code``.
CANCEL_HMS_CODES = frozenset({"0300400C", "0500400E"})
_CANCEL_HMS_ATTRS = frozenset({0x0300400C, 0x0500400E})

# HMS severity is the high half of ``code``. Lower is worse.
_HMS_SEVERITY = {1: "FATAL", 2: "SERIOUS", 3: "COMMON", 4: "INFO"}
_HMS_UNKNOWN_RANK = 99
# A print_error low word below this is a status indicator, same as HMS severity 0.
_STATUS_PRINT_ERROR_BELOW = 0x4000
# Real faults on the additive list. The legacy summary is still one code.
_MAX_HMS_FAULTS = 10
# ``0500_0500_0001_0007`` — MQTT command verification failed.
_COMMANDS_REJECTED_CODE = "0500050000010007"
# Same cap as ``_MAX_FIRMWARE_TEXT``: a code is firmware text on the report.
_ERROR_CODE_LIMIT = 64


class HmsEntry:
    """One parsed HMS row. ``full_code`` is all 16 hex digits, attr then code."""

    def __init__(self, attr: int, code: int, severity: int, severity_name: str, full_code: str):
        self.attr = attr
        self.code = code
        self.severity = severity
        self.severity_name = severity_name
        self.full_code = full_code


def _norm_error_code(value) -> str:
    if value is None:
        return ""
    return (
        str(value).strip().upper().replace("0X", "").replace("_", "").replace("-", "")
    )[:_ERROR_CODE_LIMIT]


def _full_code(attr: int, code: int) -> str:
    return (f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
            f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}")


def _rank(entry: HmsEntry) -> int:
    if entry.severity in _HMS_SEVERITY:
        return entry.severity
    return _HMS_UNKNOWN_RANK


def parse_hms(hms) -> List[HmsEntry]:
    """Parsed rows. Malformed entries are skipped, not fatal to the report."""
    entries = []
    for item in hms or []:
        if not isinstance(item, dict):
            continue
        attr = as_int(item.get("attr"), None)
        code = as_int(item.get("code"), None)
        if attr is None or code is None:
            continue
        severity = code >> 16
        entries.append(HmsEntry(
            attr=attr,
            code=code,
            severity=severity,
            severity_name=_HMS_SEVERITY.get(severity, "UNKNOWN"),
            full_code=_full_code(attr, code),
        ))
    return entries


def is_status_indicator(entry: HmsEntry) -> bool:
    """Severity 0. Codes in that class are status, not faults."""
    return entry.severity == 0


def is_cancel_echo(entry: HmsEntry) -> bool:
    """True when the first 8 hex digits are a user-cancel attr."""
    return (entry.attr & 0xFFFFFFFF) in _CANCEL_HMS_ATTRS


def _legacy_entries(entries: List[HmsEntry]) -> List[HmsEntry]:
    """Drop status indicators. Cancel echoes stay, even at severity 0.

    3DPF still matches ``0300400C`` / ``0500400E`` inside ``hms_code``.
    """
    kept = []
    for entry in entries:
        if is_cancel_echo(entry) or not is_status_indicator(entry):
            kept.append(entry)
    return kept


def decode_hms(hms) -> Dict:
    """Worst remaining alarm plus a count, for the legacy wire fields.

    Each entry is {"attr": int, "code": int}; severity is ``code >> 16``
    (1 fatal, 2 serious, 3 common, 4 info). ``hms_code`` is the 4-group hex
    code (the two halves of ``attr``, then the two halves of ``code``).

    Severity 0 is omitted. Cancel echoes are not: an older ingest still
    sees them on ``hms_code`` until it reads ``hms_faults``.
    """
    alarms = _legacy_entries(parse_hms(hms))
    if not alarms:
        return {"hms_severity": None, "hms_code": None, "hms_count": 0}

    # Rank by the severity NUMBER (lower is worse), not by its name. A value
    # outside 1-4 sorts last and must not crash the poll.
    worst = min(alarms, key=_rank)
    return {
        "hms_severity": worst.severity_name,
        "hms_code": worst.full_code,
        "hms_count": len(alarms),
    }


def hms_faults(hms) -> List[Dict]:
    """Real faults only, worst first, capped.

    Severity 0 and cancel echoes are absent. Each item is
    ``{"code": "XXXX_XXXX_XXXX_XXXX", "severity": ...}``.
    """
    faults = [
        entry for entry in parse_hms(hms)
        if not is_status_indicator(entry) and not is_cancel_echo(entry)
    ]
    faults.sort(key=_rank)
    return [
        {"code": entry.full_code, "severity": entry.severity_name}
        for entry in faults[:_MAX_HMS_FAULTS]
    ]


def commands_rejected(hms) -> bool:
    """True when ``0500050000010007`` is present.

    In that state the printer still answers queries and silently drops
    every ``project_file``.
    """
    for entry in parse_hms(hms):
        if entry.full_code.replace("_", "") == _COMMANDS_REJECTED_CODE:
            return True
    return False


def _low_word(code: str) -> Optional[int]:
    """Low 16 bits, or None when ``code`` is not a number.

    Digits-only text is the decimal integer Bambu puts in ``print_error``.
    A code that contains A-F is already hex (``0300400C``).
    """
    if not code:
        return None
    base = 16 if any(ch in "ABCDEF" for ch in code) else 10
    try:
        return int(code, base) & 0xFFFF
    except ValueError:
        return None


def reported_print_error(value) -> Optional[str]:
    """Legacy ``print_error``. 0 and a low word below 0x4000 are absent.

    Cancel codes stay. 3DPF still detects a user cancel from ``50348044``.
    """
    code = _norm_error_code(value)
    if not code or not code.strip("0"):
        return None
    low = _low_word(code)
    if low is not None and low < _STATUS_PRINT_ERROR_BELOW:
        return None
    return code


def fault_print_error(value) -> Optional[str]:
    """``reported_print_error``, with cancel codes removed too."""
    code = reported_print_error(value)
    if code is None or code in CANCEL_PRINT_ERRORS:
        return None
    return code


def is_cancel_failed(print_error=None, hms_code=None, hms=None) -> bool:
    """True when the printer is sitting on a user-cancel, not a real fail."""
    pe = _norm_error_code(print_error)
    if pe in CANCEL_PRINT_ERRORS:
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
        if normalized in CANCEL_HMS_CODES or any(code in normalized for code in CANCEL_HMS_CODES):
            return True
    return False
