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
Slot numbers are global across AMS units: unit_index * 4 + tray_index + 1.

**An empty tray is a dict carrying only an `id` key.** That structural fact is the
empty signal — corroborated independently by `tray_exist_bits` — and we emit those
trays with a null color and a null type so the UI can render an empty slot and the
router can see the slot is free.

Never infer "empty" from an all-zero color. A loaded black spool whose RFID read
failed still reports a color, and conflating the two is precisely the failure mode
the manual slot override exists to fix. Emitting exactly what the tray carries gets
this right for free: the empty tray is the one with nothing to emit.

The external spool (`vt_tray`) is deliberately NOT parsed. It sits outside the `ams`
array at global index 254, and the cloud's slot upsert keys on
(printer_id, slot_number) with no notion of a non-AMS slot — so it would persist as
a phantom swatch in the AMS strip and become a candidate slot for routing, where
tray 254 does not exist.
"""

import copy
from typing import Dict, List, Optional

from .coerce import as_int, clean_str

TRAYS_PER_AMS = 4

_HEX_DIGITS = set("0123456789ABCDEF")


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

    `None` means the payload carries no AMS unit list at all, so we know nothing about
    this printer's trays right now. That is NOT the same claim, and returning `[]` for
    it is what let a healthy printer's slots be wiped:

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
    slots: List[Dict] = []
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
            slots.append({
                "slot_number": unit_index * TRAYS_PER_AMS + tray_index + 1,
                "color_hex": clean_str(_tray_color(tray)),
                "filament_type": clean_str(tray.get("tray_type")),
            })
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


def merge_ams(previous, incoming):
    """Keep RFID tray readings across a P1 print delta that only details the active tray.

    Incremental `print.ams` payloads still carry `tray_exist_bits` and a full tray
    list, but idle trays arrive as `{id}` only. Replacing the AMS object wholesale
    then blanks hex the printer already sent. If the bitmask still says that tray
    is loaded and we already have a colour, keep it. A real unload is either an
    id-only tray with the bit cleared, or no bitmask and an id-only tray.
    """
    if not isinstance(incoming, dict):
        return copy.deepcopy(previous) if isinstance(previous, dict) else None
    incoming = copy.deepcopy(incoming)
    if not isinstance(previous, dict):
        return incoming
    incoming_units = incoming.get("ams")
    if incoming_units == []:
        return incoming
    if not isinstance(incoming_units, list):
        outgoing = copy.deepcopy(previous)
        for key, value in incoming.items():
            if key != "ams":
                outgoing[key] = copy.deepcopy(value)
        return outgoing
    previous_units = previous.get("ams")
    if not isinstance(previous_units, list):
        return incoming
    prev_by_id = {}
    for unit in previous_units:
        if isinstance(unit, dict):
            prev_by_id[as_int(unit.get("id"), default=None)] = unit
    bits = clean_str(incoming.get("tray_exist_bits")) or clean_str(previous.get("tray_exist_bits"))
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
            slot_number = unit_index * TRAYS_PER_AMS + tray_index + 1
            prev_tray = prev_trays.get(tray_index)
            if (
                not _tray_has_reading(tray)
                and _bit_present(bits, slot_number) is True
                and isinstance(prev_tray, dict)
                and _tray_has_reading(prev_tray)
            ):
                trays.append(copy.deepcopy(prev_tray))
            else:
                trays.append(tray)
        merged = dict(unit)
        merged["tray"] = trays
        merged_units.append(merged)
    incoming["ams"] = merged_units
    return incoming


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


def parse_tray_exist_bits(status: dict) -> Optional[str]:
    """The AMS's `tray_exist_bits` hex bitmask (bit N == tray N is present), as-is.

    Reported verbatim rather than decoded because it is a *corroborating* signal, not
    a derived one: it is the only thing that detects a spool swap in a slot whose RFID
    is dark, since such a slot's reported color never changes.
    """
    return clean_str(_ams_container(status).get("tray_exist_bits"))


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
