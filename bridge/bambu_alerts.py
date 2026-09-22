"""Plain-English titles for Bambu HMS / print_error codes.

3DPF's UI source of truth is frontend/src/pages/printers/bambuAlerts.ts.
This copy lets Link attach `hms_title` / `hms_detail` on the report so a
log or an older UI still names the fault. Wording is ours — not HA /
OctoPrint / BambuHelper JSON.

Codes confirmed from Bambu's public P1S table (e.bambulab.com, prefix 01P)
and the HMS wiki. Do not invent codes.
"""

from __future__ import annotations

import re
from typing import Optional

_AMS_LETTERS = "ABCDEFGH"

# P1S-5 live dump 2026-09-22 plus farm-common print_error / printer-body HMS.
_EXPLICIT = {
    "0700620000020001": (
        "AMS A slot 3 can't feed",
        "The AMS motor hit too much resistance. Usually a tangled or stuck spool; an empty reel can trip this too.",
    ),
    "0300400C": ("Print canceled", "Someone stopped the job. Not a machine fault."),
    "0500400E": ("Print canceled", "Someone stopped the job. Not a machine fault."),
    "03008004": ("Filament runout", "The printer ran out of filament. Load a new reel and resume."),
    "07008011": ("Filament runout", "AMS filament ran out. Put a new reel in the same slot and resume."),
    "07018011": ("Filament runout", "AMS filament ran out. Put a new reel in the same slot and resume."),
    "07028011": ("Filament runout", "AMS filament ran out. Put a new reel in the same slot and resume."),
    "07038011": ("Filament runout", "AMS filament ran out. Put a new reel in the same slot and resume."),
    "07FF8011": ("Filament runout", "The external spool ran out. Load a new reel and resume."),
    "07FF200000020001": ("Filament runout", "The external spool ran out. Load a new reel and resume."),
    "03001A0000020002": ("Nozzle clogged", "Filament is stuck in the nozzle."),
    "0300010000010007": ("Bed temperature fault", "Bed sensor may be open-circuit."),
    "03004000": ("Z-homing failed", "The printer stopped because it could not home Z."),
    "03000A0000010005": (
        "Bed leveling failed",
        "The printer could not level the bed. Clear the plate and check for debris, then retry.",
    ),
}


def normalize_bambu_code(raw) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    value = re.sub(r"^HMS[_-]?", "", value, flags=re.I)
    if value == "50348044":
        value = "0300400C"
    elif value.isdigit() and len(value) < 8:
        value = f"{int(value):08X}"
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    if len(hex_only) == 16:
        return f"{hex_only[0:4]}_{hex_only[4:8]}_{hex_only[8:12]}_{hex_only[12:16]}"
    if len(hex_only) == 8:
        return f"{hex_only[0:4]}_{hex_only[4:8]}"
    groups = re.findall(r"[0-9A-F]{4}", value.upper())
    if len(groups) >= 2:
        return "_".join(groups)
    return value.upper().replace("-", "_")


def _code_key(raw) -> Optional[str]:
    normalized = normalize_bambu_code(raw)
    return normalized.replace("_", "") if normalized else None


def _slot_phrase(unit: int, slot: int) -> str:
    letter = _AMS_LETTERS[unit] if unit < len(_AMS_LETTERS) else str(unit + 1)
    return f"AMS {letter} slot {slot}"


def _ams_family(second: str, severity: str, issue: str, unit: int):
    if len(second) != 4 or second[2:] != "00" or second[1] not in "0123":
        return None
    slot = int(second[1]) + 1
    phrase = _slot_phrase(unit, slot)
    family = second[0]
    if family == "2" and severity == "0002" and issue == "0001":
        return "Filament runout", f"{phrase} ran out. Put a new reel in that slot and resume."
    if family == "2" and severity == "0002" and issue == "0002":
        return f"{phrase} is empty", f"{phrase} has no filament. Insert a reel and retry."
    if family == "2" and severity == "0003" and issue == "0001":
        return "Filament runout", f"{phrase} ran out. The printer is purging leftover filament."
    if family == "2" and severity == "0003" and issue == "0002":
        return "Filament runout", f"{phrase} ran out. The AMS switched to a matching spool."
    if family == "2" and severity == "0002" and issue == "0005":
        return "Filament runout", f"{phrase} ran out and the purge failed. Check for a jam in the toolhead."
    if family == "2" and severity == "0002" and issue == "0003":
        return f"{phrase} filament broken", f"Filament may have snapped inside {phrase}."
    if family == "6" and severity == "0002" and issue == "0001":
        return f"{phrase} can't feed", f"{phrase} motor overloaded. Usually a tangled or stuck spool."
    return None


def lookup_bambu_alert(raw) -> Optional[dict]:
    hex_key = _code_key(raw)
    if not hex_key:
        return None
    if hex_key in _EXPLICIT:
        title, detail = _EXPLICIT[hex_key]
        return {"title": title, "detail": detail}
    if len(hex_key) == 16 and hex_key[:8] in _EXPLICIT:
        title, detail = _EXPLICIT[hex_key[:8]]
        return {"title": title, "detail": detail}
    if len(hex_key) != 16:
        return None
    module, second, severity, issue = hex_key[0:4], hex_key[4:8], hex_key[8:12], hex_key[12:16]
    match = re.fullmatch(r"070([0-3])", module)
    if match:
        found = _ams_family(second, severity, issue, int(match.group(1)))
        if found:
            return {"title": found[0], "detail": found[1]}
    if module in ("0700", "0701", "0702", "0703") and second == "7000" and severity == "0002" and issue == "0007":
        return {
            "title": "Filament runout",
            "detail": "AMS filament ran out. Put a new reel in the same slot and resume.",
        }
    return None


def describe_hms(hms_code=None, print_error=None) -> dict:
    """Titles to attach to a telemetry snapshot. Nulls when we have no wording."""
    copy = lookup_bambu_alert(hms_code) or lookup_bambu_alert(print_error)
    if not copy:
        return {"hms_title": None, "hms_detail": None}
    return {"hms_title": copy["title"], "hms_detail": copy["detail"]}
