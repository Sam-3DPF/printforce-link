"""Parse a Bambu MQTT status payload's AMS section into flat slot states.

The bridge reports each slot to 3DPF as {slot_number, color_hex, filament_type}. We
keep the raw AMS-reported color/type here; color normalization and Material matching
happen server-side.

Bambu status shape (subset):
    status["print"]["ams"] = {
        "tray_exist_bits": "f",        # hex bitmask: bit N set == tray N is present
        "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "RRGGBBAA", "tray_type": "PLA"},  # loaded
                {"id": "1"},                                                # EMPTY
                ...
            ]},
            ...
        ],
    }
Slot numbers are global across regular AMS units: unit_index * 4 + tray_index + 1.
AMS HT units use ids 128–135 and have one tray each. Those map to slots 17–24
(`16 + unit_offset + 1`). `unit * 4 + tray + 1` on id 128 invents slot 513.
A2L physical unit 16 is read as unit 6 before slot numbers and exist bits, so
its four trays are slots 25–28 and tray 0 uses bit 24, not bit 64.

`remain` is never emptiness. Official dumps send `-1` for unread / third-party
spools and `0` when remaining is not calibrated. Empty comes from
`tray_exist_bits` (or a regular AMS state other than loaded), not from remain.

**An empty tray is a slot whose `tray_exist_bits` bit is cleared.** A clear bit
blanks type, color, and remain even when the tray object still carries them.
An `{id}`-only tray on a P1 is also how idle loaded trays arrive, so that shape
alone is not Empty. A regular AMS update of `{id, state}` is empty when state
is anything other than 11 (loaded). AMS-HT state 9 stays occupied. When the
bits say the spool is in, we still emit that tray (null hex) so Refresh sends the
same full list first-connect would. A mixed filled-plus-blank dump with no bits
is incomplete: `parse_ams` returns None so the cloud does not store Empty.

Never infer "empty" from an all-zero color. A loaded black spool whose RFID read
failed still reports a color, and conflating the two is precisely the failure mode
the manual slot override exists to fix. A clear exist bit is what blanks that
tray, including when the object still carries a stale color.

The external spool (`vt_tray` / `vir_slot`) is deliberately NOT parsed. It sits
outside the `ams` array at global index 254, and the cloud's slot upsert keys on
(printer_id, slot_number) with no notion of a non-AMS slot — so it would persist as
a phantom swatch in the AMS strip and become a candidate slot for routing, where
tray 254 does not exist. Neither object is appended to `slots`.
"""

import copy
import json
import os
import tempfile
from typing import Dict, List, Optional

from .coerce import as_int, clean_str

TRAYS_PER_AMS = 4
AMS_HT_ID_MIN = 128
AMS_HT_ID_MAX = 135
# 1-based. Regular AMS occupies 1–16; HT units occupy 17–24.
AMS_HT_FIRST_SLOT = 17
# A2L Lite's physical unit id. Exist bits and reported slots use unit 6.
A2L_PHYSICAL_UNIT_ID = 16
A2L_NORMALIZED_UNIT_ID = 6
# Regular AMS. 11 is loaded. Anything else on an `{id, state}` update is empty.
REGULAR_AMS_LOADED_STATE = 11
_LOADED_TRAY_FIELDS = ("tray_color", "cols", "tray_type", "remain")

_HEX_DIGITS = set("0123456789ABCDEF")


def ams_slot_number(unit_id: int, tray_id: int) -> int:
    """1-based slot for a unit/tray pair. HT units are not `unit * 4 + tray`.

    A2L physical unit 16 is numbered as unit 6 (slots 25–28). The exist-bit
    index is that slot minus one, so tray 0 is bit 24 rather than bit 64.
    """
    if AMS_HT_ID_MIN <= unit_id <= AMS_HT_ID_MAX:
        return AMS_HT_FIRST_SLOT + (unit_id - AMS_HT_ID_MIN)
    if unit_id == A2L_PHYSICAL_UNIT_ID:
        unit_id = A2L_NORMALIZED_UNIT_ID
    return unit_id * TRAYS_PER_AMS + tray_id + 1


def remain_percent(value) -> Optional[int]:
    """Calibrated remaining percent, or None when the printer does not know.

    `-1` is unread / third-party. `0` is uncalibrated. Neither is an empty tray.
    """
    amount = as_int(value, None)
    if amount is None or amount <= 0 or amount > 100:
        return None
    return amount


def normalize_hex(value: Optional[str]) -> Optional[str]:
    """Canonicalize a color to `#RRGGBB` uppercase, or None if not a valid hex.

    **This is a deliberate byte-for-byte copy of the cloud's canonical matcher**
    (`backend/shared/services/bridge_state_service.normalize_hex`). The bridge is a
    separate deployable and cannot import it, so the two must stay identical by hand:
    routing here compares a printer's AMS `tray_color` against the batch's
    `required_colors`, and the cloud derives BOTH the reported slot colors and the
    required colors through its copy. If the two normalizers ever drift, a printer that
    genuinely holds a color reads as "no match" and every job for it stalls in the queue
    (R-C). Any change here MUST be mirrored there, and vice versa.

    Accepts values with/without a leading `#` and 8-digit RGBA (alpha dropped), so a
    Bambu AMS `FF6A13FF` and a Material `#ff6a13` compare equal. Non-strings are rejected
    (a raw tray_color can be any type from a malformed payload).
    """
    if not isinstance(value, str) or not value:
        return None
    h = value.strip().lstrip("#").upper()
    if len(h) == 8:  # RGBA -> RGB
        h = h[:6]
    if len(h) != 6 or any(c not in _HEX_DIGITS for c in h):
        return None
    return "#" + h


def parse_ams(status: dict) -> Optional[List[Dict]]:
    """Return [{'slot_number', 'color_hex', 'filament_type'}] for every tray the
    printer reports — loaded *and* empty. An empty tray comes back with a null
    color and a null type. Malformed input yields **None** rather than raising.

    **None and [] are different answers, and the difference is destructive.**

    `[]` means the printer told us about its AMS units and there are none — an AMS
    that has been unplugged. The cloud treats that as authoritative and DELETES the
    printer's slot rows (`bridge_state_service._reconcile_slots`), which is correct:
    the spools are genuinely gone, and anything stored on the row goes with them.

    `None` means we do not have a finished AMS reading. That includes a payload with
    no unit list, and a P1 dump that lists trays as `{id}` only without
    `tray_exist_bits`. Emitting Empty for that mix is what stored P1S-6 slots 2-4 as
    Empty on Main. Returning `[]` for "no information" is what let a healthy
    printer's slots be wiped:

      Bambu pushes a full status once and then sends deltas. `mqtt_dump()` accumulates
      only one level deep, so between connecting and the first full push a printer's
      merged payload legitimately has no `print.ams` key while `gcode_state` is already
      RUNNING. The bridge reported `slots: []` with status PRINTING, that sailed past
      the cloud's OFFLINE-only guard, and every slot row for a live, printing machine
      was deleted. Four of seven AMS printers on the dev farm sat with zero slots from
      this — invisible in the fleet view, and unroutable, since the dispatcher matches a
      batch's required colors against the slots a printer reports (`router._color_set`).

    Today the deleted row only costs the reported color, which the next real AMS report
    rebuilds. The reason this is a data-loss bug and not a display one is U10: the manual
    filament override is specified to live on this row (`override_material_id`,
    `override_set_at`, `override_of_reported_hex`), and it exists precisely for slots
    whose RFID is dark — the population nothing can detect or restore. Shipping U10 onto
    a row that a routine reconnect can delete would make that loss permanent.

    Returning None makes the report say "no information", which the cloud already
    handles: a `slots` value that is not a list skips both the upsert and the
    reconcile, leaving the rows alone until a real AMS report arrives.

    The discriminator is the unit LIST, not the container: `print.ams` exists to hold
    both the unit array and the AMS-wide bitmasks, so a container carrying only
    `tray_exist_bits` still tells us nothing about which trays are loaded.
    """
    units = _ams_container(status).get("ams")
    if not isinstance(units, list):
        return None
    bits = parse_tray_exist_bits(status)
    slots: List[Dict] = []
    incomplete = False
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                continue  # a tray we cannot place has no slot number to report under
            slot_number = ams_slot_number(unit_index, tray_index)
            present = _bit_present(bits, slot_number)
            if present is False or _regular_state_unloaded(unit_index, tray):
                # A clear exist bit, or a regular state other than loaded,
                # wins over a stale color, type, or remain.
                slots.append({
                    "slot_number": slot_number,
                    "color_hex": None,
                    "filament_type": None,
                })
                continue
            color_hex = clean_str(_tray_color(tray))
            filament_type = clean_str(tray.get("tray_type"))
            if color_hex or filament_type:
                slot = {
                    "slot_number": slot_number,
                    "color_hex": color_hex,
                    "filament_type": filament_type,
                }
                remaining = remain_percent(tray.get("remain"))
                if remaining is not None:
                    slot["remain_percent"] = remaining
                slots.append(slot)
            elif bits is not None:
                # Bit-present idle trays stay on the first-connect list. Returning
                # None here swallowed a sibling RFID hex (0.1.16).
                slots.append({
                    "slot_number": slot_number,
                    "color_hex": None,
                    "filament_type": None,
                })
            else:
                incomplete = True
    if incomplete:
        return None
    return slots


def _tray_color(tray: dict):
    """`tray_color`, or the first `cols` entry when the named field is blank.

    P1 trays with a set colour but a dark RFID often omit `tray_color` and only
    send `cols`.
    """
    color = tray.get("tray_color")
    if color:
        return color
    cols = tray.get("cols")
    if isinstance(cols, list) and cols:
        return cols[0]
    return None


def idle_trays_needing_rfid(status) -> List[tuple]:
    """(ams_id, tray_id) for trays the bitmask says are loaded with no color/type.

    `ams_get_rfid` takes those two indexes. Pushall does not read idle P1 RFID;
    this is the printer command that does.
    """
    units = _ams_container(status).get("ams")
    if not isinstance(units, list):
        return []
    bits = parse_tray_exist_bits(status)
    needed = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                continue
            slot_number = ams_slot_number(unit_index, tray_index)
            if _regular_state_unloaded(unit_index, tray):
                continue
            if clean_str(_tray_color(tray)) or clean_str(tray.get("tray_type")):
                continue
            if _bit_present(bits, slot_number) is False:
                continue
            needed.append((unit_index, tray_index))
    return needed


def ams_needs_pushall(status) -> bool:
    """True when a full MQTT dump is still needed to know loaded tray colours.

    `parse_ams` is None when there is no unit list yet. A bit-present idle
    tray with no hex still needs `pushall` so Refresh can ask RFID.
    """
    if parse_ams(status) is None:
        return True
    return bool(idle_trays_needing_rfid(status))


def merge_ams(previous, incoming):
    """Keep RFID tray readings across a P1 print delta that only details the active tray.

    Incremental `print.ams` payloads still carry `tray_exist_bits` and a full tray
    list, but idle trays arrive as `{id}` only, or as RFID identity without hex.
    Replacing the AMS object wholesale then blanks hex the printer already sent.
    Keep the last colour unless the bitmask clears that slot, or a regular AMS
    tray arrives as `{id, state}` with a state other than loaded (11). A clear
    bit blanks type, color, and remain on the stored tray even when the object
    still carries them. AMS-HT state 9 stays occupied. A missing bitmask is not
    an unload. Persist bits onto the outgoing object so a later delta that
    omits them does not store null.
    """
    if not isinstance(incoming, dict):
        return copy.deepcopy(previous) if isinstance(previous, dict) else None
    incoming = copy.deepcopy(incoming)
    if not isinstance(previous, dict):
        bits = _normalize_tray_exist_bits(incoming.get("tray_exist_bits"))
        if bits:
            incoming["tray_exist_bits"] = bits
        _blank_unloaded_trays(incoming, bits)
        return incoming
    incoming_units = incoming.get("ams")
    bits = (
        _normalize_tray_exist_bits(incoming.get("tray_exist_bits"))
        or _normalize_tray_exist_bits(previous.get("tray_exist_bits"))
    )
    if incoming_units == []:
        if bits:
            incoming["tray_exist_bits"] = bits
        return incoming
    if not isinstance(incoming_units, list):
        outgoing = copy.deepcopy(previous)
        for key, value in incoming.items():
            if key != "ams":
                outgoing[key] = copy.deepcopy(value)
        if bits:
            outgoing["tray_exist_bits"] = bits
        return outgoing
    previous_units = previous.get("ams")
    if not isinstance(previous_units, list):
        if bits:
            incoming["tray_exist_bits"] = bits
        _blank_unloaded_trays(incoming, bits)
        return incoming
    prev_by_id = {}
    for unit in previous_units:
        if isinstance(unit, dict):
            prev_by_id[as_int(unit.get("id"), default=None)] = unit
    merged_units = []
    for unit in incoming_units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        prev_unit = prev_by_id.get(unit_index) or {}
        prev_trays = {}
        for tray in prev_unit.get("tray") or []:
            if isinstance(tray, dict):
                prev_trays[as_int(tray.get("id"), default=None)] = tray
        trays = []
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                continue
            slot_number = ams_slot_number(unit_index, tray_index)
            prev_tray = prev_trays.get(tray_index)
            if (
                _bit_present(bits, slot_number) is False
                or _regular_state_unloaded(unit_index, tray)
            ):
                trays.append(_blank_loaded_fields(tray))
            elif (
                not _tray_color(tray)
                and not _regular_state_unloaded(unit_index, tray)
                and isinstance(prev_tray, dict)
                and _tray_color(prev_tray)
            ):
                kept = copy.deepcopy(prev_tray)
                for key, value in tray.items():
                    if key in ("tray_color", "cols"):
                        continue
                    if value not in (None, ""):
                        kept[key] = copy.deepcopy(value)
                trays.append(kept)
            else:
                trays.append(tray)
        merged = dict(unit)
        merged["tray"] = trays
        merged_units.append(merged)
    incoming["ams"] = merged_units
    if bits:
        incoming["tray_exist_bits"] = bits
    return incoming


def ams_has_color(ams) -> bool:
    """True when any tray in a `print.ams` object already carries a colour."""
    if not isinstance(ams, dict):
        return False
    for unit in ams.get("ams") or []:
        if not isinstance(unit, dict):
            continue
        for tray in unit.get("tray") or []:
            if isinstance(tray, dict) and _tray_color(tray):
                return True
    return False


def _regular_state_unloaded(unit_id: int, tray: dict) -> bool:
    """True when a regular AMS `{id, state}` update is not the loaded state.

    State 11 keeps a remembered color. AMS-HT is excluded: a loaded HT tray
    reports state 9, and treating that as an unload would blank a present spool.
    """
    if AMS_HT_ID_MIN <= unit_id <= AMS_HT_ID_MAX:
        return False
    if "state" not in tray:
        return False
    return as_int(tray.get("state"), default=None) != REGULAR_AMS_LOADED_STATE


def _blank_loaded_fields(tray: dict) -> dict:
    """Drop type, color, and remain. An unload makes those stale."""
    blanked = copy.deepcopy(tray)
    for key in _LOADED_TRAY_FIELDS:
        blanked.pop(key, None)
    return blanked


def _blank_unloaded_trays(ams_obj: dict, bits: Optional[str]) -> None:
    """Blank unloaded trays on a payload that has no previous unit list."""
    units = ams_obj.get("ams")
    if not isinstance(units, list):
        return
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        trays = []
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                trays.append(tray)
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                trays.append(tray)
                continue
            slot_number = ams_slot_number(unit_index, tray_index)
            if (
                _bit_present(bits, slot_number) is False
                or _regular_state_unloaded(unit_index, tray)
            ):
                trays.append(_blank_loaded_fields(tray))
            else:
                trays.append(tray)
        unit["tray"] = trays


def _tray_has_reading(tray: dict) -> bool:
    if _tray_color(tray):
        return True
    if clean_str(tray.get("tray_type")):
        return True
    if clean_str(tray.get("tray_info_idx")):
        return True
    return False


def _bit_present(bits: Optional[str], slot_number: int) -> Optional[bool]:
    if not bits or any(c not in _HEX_DIGITS for c in bits.upper()):
        return None
    value = int(bits, 16)
    return ((value >> (slot_number - 1)) & 1) == 1


def load_remembered_ams(path: Optional[str], bambu_id: str):
    """Last `print.ams` object saved for this serial, or None."""
    if not path or not bambu_id:
        return None
    try:
        with open(path, "r") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    remembered = raw.get(str(bambu_id))
    return copy.deepcopy(remembered) if isinstance(remembered, dict) else None


def save_remembered_ams(path: Optional[str], bambu_id: str, ams) -> None:
    """Remember `print.ams` so a restart can keep RFID hex across a partial dump."""
    if not path or not bambu_id or not isinstance(ams, dict):
        return
    raw = {}
    try:
        with open(path, "r") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            raw = loaded
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        raw = {}
    raw[str(bambu_id)] = copy.deepcopy(ams)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ams-cache-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(raw, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_tray_exist_bits(status: dict) -> Optional[str]:
    """The AMS's `tray_exist_bits` bitmask as a hex string (bit N == tray N is present).

    Firmware may leave this as an int (15) or a hex string ("f").
    `clean_str` drops non-strings, which stored null bits on every shop printer and
    disabled keep-hex. Normalize both shapes here.
    """
    return _normalize_tray_exist_bits(_ams_container(status).get("tray_exist_bits"))


def _normalize_tray_exist_bits(value) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        return format(value, "x")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(c not in _HEX_DIGITS for c in text.upper()):
        return None
    return text


def _ams_container(status: dict) -> dict:
    """`status["print"]["ams"]` — the object holding both the AMS unit array and the
    AMS-wide bitmasks. Returns {} for any malformed shape rather than raising."""
    if not isinstance(status, dict):
        return {}
    print_obj = status.get("print")
    if not isinstance(print_obj, dict):
        return {}
    ams = print_obj.get("ams")
    return ams if isinstance(ams, dict) else {}
