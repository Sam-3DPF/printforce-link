"""Parse a Bambu MQTT status payload's AMS section into flat slot states.

The bridge reports each slot to 3DPF as {slot_number, color_hex, filament_type,
...}. We keep the raw AMS-reported color/type here; color normalization and Material matching
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
`tray_exist_bits`, not from remain.

**An empty tray is a slot whose `tray_exist_bits` bit is cleared.** A clear bit
blanks the reading and the spool identity even when the tray object still
carries them. `state` is not presence: shop P1S printers report every loaded
tray as state 3 and never 11 (Bambuddy documents the same), and Link 0.1.41
blanked every colour by reading state != 11 as an unload. See `tray_presence`.
An `{id}`-only tray on a P1 can be a loaded tray the delta did not detail, so
that shape alone is not Empty. When the bits say the spool is in, we still emit
that tray (null hex) so Refresh sends the same full list first-connect would. A
mixed filled-plus-blank dump with no bits is incomplete: `parse_ams` returns
None so the cloud does not store Empty.

Never infer "empty" from a colour. A loaded black spool reports `000000FF`.
`00000000` (alpha 0) is Bambu's "no colour": a reset slot, or a tray the AMS
briefly blanks, sends it, so it is read as no reading, never as black.

`merge_ams` follows Bambuddy's `_handle_ams_data`: omitted units and trays are
kept, a reading field the printer names replaces the stored one even when
blank, and spool identity changes only to a real value.

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
# Regular-AMS tray states that mean a spool is loaded. P1S and A1 mini report 3
# and never 11; other models report 11 (Bambuddy `ams_slot_presence.py`). Only
# read when `tray_exist_bits` is unknown and the tray arrives as `{id, state}`.
REGULAR_AMS_LOADED_STATES = frozenset({3, 11})
# Bambu's "no colour" value: what a reset slot or a blanked tray reports.
NO_COLOUR = "00000000"
# What the tray holds. A key the printer sends always wins, even blank
# (Bambuddy `always_update_fields`); a key it omits keeps the last value.
_READING_FIELDS = (
    "tray_color", "cols", "tray_type", "tray_sub_brands", "tray_info_idx",
    "tray_id_name", "remain",
)
# Which spool it is. Only a real (non-zero) value overwrites; the tray has to
# empty to clear it, so a blanked read does not look like a new spool.
_IDENTITY_FIELDS = ("tag_uid", "tray_uuid")

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
            present = tray_presence(unit_index, tray, bits)
            if present is False:
                # A clear exist bit wins over a stale color, type, or remain.
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
                slots.append(_with_spool_facts(slot, tray))
            elif present:
                # Bit-present trays with no reading stay on the list. Returning
                # None here swallowed a sibling RFID hex (0.1.16).
                slots.append(_with_spool_facts({
                    "slot_number": slot_number,
                    "color_hex": None,
                    "filament_type": None,
                }, tray))
            else:
                incomplete = True
    if incomplete:
        return None
    return slots


def _with_spool_facts(slot: dict, tray: dict) -> dict:
    """Add what the printer read off the spool, for a tray that is present.

    Each key is sent only when the printer reported the matching field, so a
    Link that reads no RFID data sends none of them. `spool_uid` is null for a
    tagless spool: the cloud reads a null against a stored uid as a new spool.
    Nozzle temperatures go only with a tag read, so a stale value on a tagless
    tray never becomes a reference for a slot write.
    """
    if "tray_sub_brands" in tray:
        slot["filament_name"] = _bounded(tray.get("tray_sub_brands"), 64)
    if "tray_info_idx" in tray:
        slot["filament_id"] = _bounded(tray.get("tray_info_idx"), 32)
    if "tray_uuid" in tray or "tag_uid" in tray:
        slot["spool_uid"] = next(
            (
                _bounded(tray.get(key), 64)
                for key in ("tray_uuid", "tag_uid")
                if _real_identity(tray.get(key))
            ),
            None,
        )
        if slot["spool_uid"]:
            for key in ("nozzle_temp_min", "nozzle_temp_max"):
                temp = _nozzle_temp(tray.get(key))
                if temp is not None:
                    slot[key] = temp
    return slot


def _nozzle_temp(value) -> Optional[int]:
    """A positive whole-degree temperature; the printer sends strings like "190"."""
    if isinstance(value, bool):
        return None
    temp = as_int(value, default=None)
    return temp if temp and temp > 0 else None


def _bounded(value, limit: int) -> Optional[str]:
    text = clean_str(value)
    return text[:limit] if text else None


def _tray_color(tray: dict):
    """`tray_color`, or the first `cols` entry when the named field is blank.

    P1 trays with a set colour but a dark RFID often omit `tray_color` and only
    send `cols`. `00000000` is Bambu's "no colour", not black: a reset slot and
    a tray the AMS briefly blanks both send it.
    """
    color = tray.get("tray_color")
    if color and not _is_no_colour(color):
        return color
    cols = tray.get("cols")
    if isinstance(cols, list) and cols and not _is_no_colour(cols[0]):
        return cols[0]
    return None


def _is_no_colour(value) -> bool:
    return isinstance(value, str) and value.strip().upper() == NO_COLOUR


def tray_presence(unit_id: int, tray: dict, bits: Optional[str]) -> Optional[bool]:
    """Is a spool in this tray? True, False, or None when nothing says.

    `tray_exist_bits` decides whenever it is known. `state` is firmware-variant
    (3 on a loaded P1S tray, 11 elsewhere, 9 on a loaded AMS-HT), so it never
    blanks a tray the bitmask calls present. Without bits, a colour or type
    means present. Otherwise follow Bambuddy: an explicit blank `tray_type` is
    empty, and so is a regular `{id, state}`-only tray whose state is not a
    loaded one.
    """
    tray_id = as_int(tray.get("id"), default=None)
    if tray_id is not None:
        bit = _bit_present(bits, ams_slot_number(unit_id, tray_id))
        if bit is not None:
            return bit
    if clean_str(_tray_color(tray)) or clean_str(tray.get("tray_type")):
        return True
    if "tray_type" in tray or _id_state_only_unloaded(unit_id, tray):
        return False
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
            if tray_presence(unit_index, tray, bits) is not True:
                continue
            if clean_str(_tray_color(tray)) or clean_str(tray.get("tray_type")):
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
    """Merge one `print.ams` delta into the remembered AMS, Bambuddy's way.

    Units and trays the delta omits are kept. Within a tray, reading fields
    the delta names replace the stored ones even when blank, so a new spool
    never inherits the previous spool's colour; spool identity only changes to
    a real value. A tray the bits (or, without bits, an `{id, state}` unload)
    call empty loses its reading and identity. A missing bitmask is not an
    unload; the last bits are kept on the outgoing object.
    """
    if not isinstance(incoming, dict):
        return copy.deepcopy(previous) if isinstance(previous, dict) else None
    # Copy both once. The helpers below edit these copies in place, so nothing
    # the caller holds is aliased or changed.
    incoming = copy.deepcopy(incoming)
    merged = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    bits = (
        _normalize_tray_exist_bits(incoming.get("tray_exist_bits"))
        or _normalize_tray_exist_bits(merged.get("tray_exist_bits"))
    )
    for key, value in incoming.items():
        if key != "ams":
            merged[key] = value
    incoming_units = incoming.get("ams")
    if incoming_units == []:
        merged["ams"] = []  # the printer says it has no AMS units
    elif isinstance(incoming_units, list):
        merged["ams"] = _merge_units(merged.get("ams"), incoming_units, bits)
    _drop_units_the_printer_no_longer_has(
        merged, _normalize_tray_exist_bits(incoming.get("ams_exist_bits")),
    )
    if bits:
        merged["tray_exist_bits"] = bits
    _blank_trays_the_bits_call_empty(merged, bits)
    return merged


def _drop_units_the_printer_no_longer_has(ams_obj: dict, unit_bits: Optional[str]) -> None:
    """Forget a regular AMS unit (id 0-3) whose `ams_exist_bits` bit is clear.

    Omitted units are kept between deltas, so without this an unplugged AMS
    would stay as empty slots. Full dumps carry `ams_exist_bits` (bit N = unit
    N). AMS-HT and A2L units are left alone: their bit layout here is unknown.
    """
    units = ams_obj.get("ams")
    if not unit_bits or not isinstance(units, list):
        return
    present = int(unit_bits, 16)
    kept = []
    for unit in units:
        unit_id = as_int(unit.get("id"), default=None) if isinstance(unit, dict) else None
        if unit_id is not None and 0 <= unit_id < 4 and not (present >> unit_id) & 1:
            continue
        kept.append(unit)
    ams_obj["ams"] = kept


def _merge_units(previous_units, incoming_units, bits):
    by_id = {}
    for unit in previous_units if isinstance(previous_units, list) else []:
        if isinstance(unit, dict):
            by_id[as_int(unit.get("id"), default=0)] = unit
    for unit in incoming_units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        stored = by_id.get(unit_index) or {}
        trays = {}
        for tray in stored.get("tray") or []:
            if isinstance(tray, dict):
                trays[as_int(tray.get("id"), default=None)] = tray
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                continue
            if tray_presence(unit_index, tray, bits) is False:
                trays[tray_index] = _blank_tray({**trays.get(tray_index, {}), **tray})
            else:
                trays[tray_index] = _merge_tray(trays.get(tray_index), tray)
        merged = {**stored, **{k: v for k, v in unit.items() if k != "tray"}}
        merged["tray"] = [trays[k] for k in sorted(trays, key=lambda k: (k is None, k))]
        by_id[unit_index] = merged
    return [by_id[k] for k in sorted(by_id)]


def _merge_tray(stored, incoming):
    merged = stored if isinstance(stored, dict) else {}
    if "tray_color" in incoming or "cols" in incoming:
        # One colour, two spellings: a new reading replaces both.
        merged.pop("tray_color", None)
        merged.pop("cols", None)
    # A real filament type with every tag zeroed is a tagless spool. The shop
    # logs only zero the tags alongside a blank type (an AMS blip), so a zeroed
    # tag next to a type means the old spool's identity no longer applies.
    tagless_reading = bool(clean_str(incoming.get("tray_type"))) and all(
        key in incoming and not _real_identity(incoming[key]) for key in _IDENTITY_FIELDS
    )
    for key, value in incoming.items():
        if key in _READING_FIELDS:
            merged[key] = value
        elif key in _IDENTITY_FIELDS:
            # A zeroed tag never hides a real stored one, except for a tagless
            # reading. It still lands where nothing real is stored, so the
            # report carries `spool_uid: null` for a tagless spool.
            if _real_identity(value) or tagless_reading or not _real_identity(merged.get(key)):
                merged[key] = value
        elif value not in (None, ""):
            merged[key] = value
    return merged


def _real_identity(value) -> bool:
    text = clean_str(value)
    return bool(text) and set(text) != {"0"}


def _blank_tray(tray: dict) -> dict:
    """An empty tray: drop what it held and which spool it was. Edits `tray`."""
    for key in _READING_FIELDS + _IDENTITY_FIELDS:
        tray.pop(key, None)
    return tray


def _blank_trays_the_bits_call_empty(ams_obj: dict, bits: Optional[str]) -> None:
    """Every stored tray whose bit is clear, including ones the delta omitted."""
    if not bits:
        return
    units = ams_obj.get("ams")
    if not isinstance(units, list):
        return
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_index = as_int(unit.get("id"), default=0)
        trays = unit.get("tray")
        if not isinstance(trays, list):
            continue
        for i, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            tray_index = as_int(tray.get("id"), default=None)
            if tray_index is None:
                continue
            if _bit_present(bits, ams_slot_number(unit_index, tray_index)) is False:
                trays[i] = _blank_tray(tray)


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


def _id_state_only_unloaded(unit_id: int, tray: dict) -> bool:
    """A regular-AMS `{id, state}` update whose state is not a loaded one.

    AMS-HT is excluded: a loaded HT tray reports state 9.
    """
    if AMS_HT_ID_MIN <= unit_id <= AMS_HT_ID_MAX:
        return False
    if "state" not in tray or not set(tray) <= {"id", "state"}:
        return False
    return as_int(tray.get("state"), default=None) not in REGULAR_AMS_LOADED_STATES


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


def parse_ams_exist_bits(status: dict) -> Optional[str]:
    """The AMS's `ams_exist_bits` bitmask as a hex string (bit N == unit N is present).

    Same shapes as `parse_tray_exist_bits`: an int or a hex string.
    """
    return _normalize_tray_exist_bits(_ams_container(status).get("ams_exist_bits"))


def mapped_tray_presence(tray, tray_exist_bits: Optional[str],
                         ams_exist_bits: Optional[str] = None) -> Optional[bool]:
    """Is a spool in this global tray (an `ams_mapping` value)? True, False, or None.

    Only regular AMS trays 0-15 are answered: unit `tray // 4`, tray bit `tray`.
    A cleared unit bit makes all four of its trays absent. Otherwise the tray
    bit decides. Missing or unparseable bits are None (unknown). External
    spools (254/255), AMS-HT (128+), A2L (24-27) and unused (-1) are None:
    their bit layout is not one Link reads here.
    """
    if isinstance(tray, bool) or not isinstance(tray, int):
        return None
    if not 0 <= tray < 4 * TRAYS_PER_AMS:
        return None
    if _bit_present(ams_exist_bits, tray // TRAYS_PER_AMS + 1) is False:
        return False
    return _bit_present(tray_exist_bits, tray + 1)


def first_absent_slot(snapshot, mapping) -> Optional[int]:
    """1-based slot of the first mapped tray the report's raw bits call absent.

    None when every mapped tray is present or unknown. Slot 9 is tray 8.
    """
    if not isinstance(snapshot, dict) or not isinstance(mapping, list):
        return None
    tray_bits = _normalize_tray_exist_bits(snapshot.get("tray_exist_bits"))
    unit_bits = _normalize_tray_exist_bits(snapshot.get("ams_exist_bits"))
    for tray in mapping:
        if mapped_tray_presence(tray, tray_bits, unit_bits) is False:
            return tray + 1
    return None


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
