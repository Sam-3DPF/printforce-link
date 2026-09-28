"""Real shop P1S AMS frames, pinned (captured 2026-09-28 from Link 0.1.41 logs).

Every loaded tray on these printers reports ``state: 3``. Link 0.1.41 read any
state other than 11 as "unloaded" and blanked the colour, so these frames
produced zero colours. Bits in ``tray_exist_bits`` decide presence, not state.
"""

import json
import os

from bridge.ams import parse_ams
from bridge.bambu.state import merge_status_payload

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "replay", "p1s-shop-ams-2026-09-28.json",
)
with open(_FIXTURE, encoding="utf-8") as _handle:
    SHOP = json.load(_handle)["printers"]


def _by_slot(slots):
    return {slot["slot_number"]: slot for slot in slots}


def test_every_loaded_state_3_tray_keeps_its_colour():
    slots = _by_slot(parse_ams(SHOP["p1s_1ams"]["full"]))

    assert sorted(slots) == [1, 2, 3, 4]
    for slot in slots.values():
        assert slot["color_hex"], slot
        assert slot["filament_type"] == "PLA"


def test_four_ams_printer_reports_fourteen_colours_and_two_empty_sockets():
    slots = _by_slot(parse_ams(SHOP["p1s_4ams"]["full"]))

    assert len(slots) == 16
    empty = sorted(n for n, slot in slots.items() if slot["color_hex"] is None)
    assert empty == [9, 13]  # tray_exist_bits eeff: bits 8 and 12 clear
    for number in empty:
        assert slots[number]["filament_type"] is None


def test_two_ams_printer_whose_cache_was_blanked_reads_every_colour():
    slots = _by_slot(parse_ams(SHOP["p1s_2ams_partly_blanked_cache"]["full"]))

    assert len(slots) == 8
    assert all(slot["color_hex"] for slot in slots.values())


def test_a_delta_after_the_full_dump_keeps_every_colour():
    for name in ("p1s_1ams", "p1s_2ams_partly_blanked_cache", "p1s_4ams"):
        frames = SHOP[name]
        merged = merge_status_payload(None, frames["full"])
        merged = merge_status_payload(merged, frames["delta"])

        before = _by_slot(parse_ams(frames["full"]))
        after = _by_slot(parse_ams(merged))
        assert {n: s["color_hex"] for n, s in after.items()} == {
            n: s["color_hex"] for n, s in before.items()
        }, name


def test_an_ams_blip_that_zeroes_two_trays_is_not_black_and_then_recovers():
    frames = SHOP["p1s_1ams_rfid_read"]
    merged = merge_status_payload(None, frames["full"])
    merged = merge_status_payload(merged, frames["reading"])

    during = _by_slot(parse_ams(merged))
    assert during[3]["color_hex"] is None  # "00000000" is Bambu's no-colour value
    assert during[4]["color_hex"] is None

    merged = merge_status_payload(merged, frames["after_reading"])
    after = _by_slot(parse_ams(merged))
    assert after[3]["color_hex"] == "D3B7A7FF"
    assert after[4]["color_hex"] == "68724DFF"
