from bridge.ams import (
    ams_needs_pushall,
    ams_slot_number,
    idle_trays_needing_rfid,
    merge_ams,
    parse_ams,
    parse_tray_exist_bits,
    normalize_hex,
    remain_percent,
    load_remembered_ams,
    save_remembered_ams,
)


# normalize_hex is a hand-kept copy of the cloud's canonical matcher; these cases are the
# contract the two sides must agree on so routing colors never drift (R-C). If one side
# changes, both must — and this test plus the cloud's mirror both have to stay green.
def test_normalize_hex_canonicalizes_like_the_cloud():
    # 8-digit RGBA drops alpha; lowercase + missing '#' both canonicalize; and a Bambu
    # AMS tray_color equals a Material hex for the same color.
    assert normalize_hex("FF6A13FF") == "#FF6A13"
    assert normalize_hex("#ff6a13") == "#FF6A13"
    assert normalize_hex("ff6a13") == "#FF6A13"
    assert normalize_hex("FF6A13FF") == normalize_hex("#ff6a13")


def test_normalize_hex_rejects_invalid():
    assert normalize_hex(None) is None
    assert normalize_hex("") is None
    assert normalize_hex("not-a-hex") is None
    assert normalize_hex("12345") is None      # wrong length
    assert normalize_hex(0xFF6A13) is None      # non-string (a raw payload value)


def test_ams_ht_slot_is_not_unit_times_four():
    assert ams_slot_number(0, 0) == 1
    assert ams_slot_number(1, 0) == 5
    assert ams_slot_number(128, 0) == 17
    assert ams_slot_number(129, 0) == 18
    assert ams_slot_number(128, 0) != 128 * 4 + 1
    status = {"print": {"ams": {"ams": [
        {"id": "128", "tray": [{"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"}]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 17, "color_hex": "E8AFCFFF", "filament_type": "PLA"},
    ]


def test_remain_unknown_is_not_empty():
    assert remain_percent(-1) is None
    assert remain_percent(0) is None
    assert remain_percent("0") is None
    assert remain_percent(45) == 45
    status = {"print": {"ams": {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "1A1A1AFF", "tray_type": "PLA", "remain": -1},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "1A1A1AFF", "filament_type": "PLA"},
    ]
    calibrated = {"print": {"ams": {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "1A1A1AFF", "tray_type": "PLA", "remain": 40},
        ]},
    ]}}}
    assert parse_ams(calibrated) == [
        {"slot_number": 1, "color_hex": "1A1A1AFF", "filament_type": "PLA",
         "remain_percent": 40},
    ]


def test_parse_ams_single_unit():
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "FF6A13FF", "tray_type": "PLA"},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "000000FF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": "FF6A13FF", "filament_type": "PLA"},
    ]


def test_parse_ams_multi_unit_slot_numbering():
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PETG"}]},
        {"id": "1", "tray": [{"id": "0", "tray_color": "00AE42FF", "tray_type": "PLA"}]},
    ]}}}
    # unit0/tray3 -> slot 4 ; unit1/tray0 -> slot 5
    assert [s["slot_number"] for s in parse_ams(status)] == [4, 5]


def test_parse_ams_three_units_number_slots_one_through_twelve():
    status = {"print": {"ams": {"ams": [
        {"id": str(unit), "tray": [
            {"id": str(tray), "tray_color": "000000FF", "tray_type": "PLA"}
            for tray in range(4)
        ]}
        for unit in range(3)
    ]}}}
    assert [s["slot_number"] for s in parse_ams(status)] == list(range(1, 13))


def test_parse_ams_emits_empty_only_when_the_bit_says_the_tray_is_gone():
    """A P1 idle tray is also `{id}` only. That is not Empty unless the bitmask
    clears the slot. Live P1S-6 stored slots 2-4 as Empty from this mix."""
    status = {"print": {"ams": {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "1A1A1AFF", "tray_type": "PLA"},
            {"id": "1"},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "1A1A1AFF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": None, "filament_type": None},
    ]


def test_parse_ams_empty_tray_keeps_its_slot_number():
    status = {"print": {"ams": {"tray_exist_bits": "0", "ams": [
        {"id": "0", "tray": [{"id": "2"}]},
    ]}}}
    assert parse_ams(status) == [{"slot_number": 3, "color_hex": None, "filament_type": None}]


def test_parse_ams_does_not_store_empty_for_a_partial_p1_dump():
    """P1S-6 after Refresh on 0.1.13: one color, three id-only trays, no bits.
    Emitting Empty here is what Main stored and the app displayed."""
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}
    assert parse_ams(status) is None


def test_parse_ams_emits_the_first_connect_tray_list_when_bits_say_loaded():
    """First-connect and Refresh share pushall. Returning None here dropped the
    full tray list, so a later RFID hex on one idle tray never reached the cloud.
    Emit every bit-present tray. Empty only when the bit is cleared."""
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "E8AFCFFF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": None, "filament_type": None},
        {"slot_number": 3, "color_hex": None, "filament_type": None},
        {"slot_number": 4, "color_hex": None, "filament_type": None},
    ]


def test_parse_ams_first_connect_dual_ams_emits_every_bit_present_tray():
    """P1S-9 live 2026-09-17: bits `ff`, zero slot rows. A new pair has no
    last-known list to keep. Returning None leaves the card empty."""
    trays = (
        [{"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"}]
        + [{"id": str(i)} for i in range(1, 4)]
    )
    status = {"print": {"ams": {"tray_exist_bits": "ff", "ams": [
        {"id": "0", "tray": trays},
        {"id": "1", "tray": [{"id": str(i)} for i in range(4)]},
    ]}}}
    slots = parse_ams(status)
    assert [slot["slot_number"] for slot in slots] == list(range(1, 9))
    assert slots[0]["color_hex"] == "E8AFCFFF"
    assert [slot["color_hex"] for slot in slots[1:]] == [None] * 7


def test_parse_ams_keeps_a_sibling_rfid_hex_when_other_idle_trays_are_still_blank():
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "E8AFCFFF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": "A3D8E1FF", "filament_type": "PLA"},
        {"slot_number": 3, "color_hex": None, "filament_type": None},
        {"slot_number": 4, "color_hex": None, "filament_type": None},
    ]


def test_parse_ams_loaded_black_spool_is_not_mistaken_for_empty():
    """A loaded black spool reports an opaque black. The old parser inferred "empty"
    from an all-zero color, which is exactly the confusion the manual slot override
    exists to fix: a black spool whose RFID read failed still reports a color."""
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "000000FF", "tray_type": "PLA"}]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "000000FF", "filament_type": "PLA"},
    ]


def test_parse_ams_reads_color_from_cols_when_tray_color_is_missing():
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "1", "cols": ["AE96D4FF"], "tray_type": "PLA"}]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 2, "color_hex": "AE96D4FF", "filament_type": "PLA"},
    ]


def test_parse_ams_blank_type_keeps_the_reported_color():
    """A tray the printer knows a color for but no type (a dark RFID read) is not an
    empty slot — keep the color so the override has something to correct."""
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "00AE42FF", "tray_type": ""}]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "00AE42FF", "filament_type": None},
    ]


def test_parse_ams_no_unit_list_is_none_not_empty():
    """**The distinction that stops a live printer's slots being deleted.**

    None means "this payload tells me nothing about the trays"; [] means "the printer
    says it has no AMS units". The cloud deletes slot rows to match a reported list, so
    conflating the two wipes the trays — and the dark-RFID overrides stored on those
    rows — of a printer that simply has not sent its first full push yet.
    """
    assert parse_ams({}) is None
    assert parse_ams(None) is None
    assert parse_ams({"print": {}}) is None
    assert parse_ams({"print": "not-a-dict"}) is None
    assert parse_ams({"print": {"ams": "not-a-dict"}}) is None
    # The container exists to hold the bitmasks too, so one carrying only
    # `tray_exist_bits` still says nothing about which trays are loaded.
    assert parse_ams({"print": {"ams": {"tray_exist_bits": "f"}}}) is None


def test_parse_ams_reports_empty_when_the_printer_has_no_ams_units():
    """An unplugged AMS — a real, authoritative "there are no trays", which SHOULD
    reconcile the slot rows away. This is the case None must not swallow."""
    assert parse_ams({"print": {"ams": {"ams": []}}}) == []


def test_parse_ams_unplaceable_tray_is_skipped_but_still_a_report():
    """A tray we cannot place has no slot number to report under, so it is skipped —
    but the printer did report its unit list, so this is [] (a real answer), not None."""
    assert parse_ams(
        {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "x", "tray_type": "PLA"}]}]}}}
    ) == []


def test_merge_ams_keeps_rfid_colors_when_print_delta_only_details_the_active_tray():
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert [tray.get("tray_color") for tray in merged["ams"][0]["tray"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_merge_ams_keeps_hex_when_idle_trays_only_repeat_rfid_ids():
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "tray_info_idx": "GFL07"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA", "tray_info_idx": "GFL06"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA", "tray_info_idx": "GFL01"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA", "tray_info_idx": "GFL00"},
        ]},
    ]}
    incoming = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "tray_info_idx": "GFL07"},
            {"id": "1", "tray_info_idx": "GFL06"},
            {"id": "2", "tray_info_idx": "GFL01"},
            {"id": "3", "tray_info_idx": "GFL00"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert [tray.get("tray_color") for tray in merged["ams"][0]["tray"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_merge_ams_clears_a_tray_when_the_bit_says_it_is_gone():
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert merged["ams"][0]["tray"][1] == {"id": "1"}


def test_merge_ams_keeps_hex_when_a_delta_omits_tray_exist_bits():
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert merged["tray_exist_bits"] == "f"
    assert [tray.get("tray_color") for tray in merged["ams"][0]["tray"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_merge_ams_keeps_hex_when_bits_are_missing_on_both_sides():
    previous = {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert [tray.get("tray_color") for tray in merged["ams"][0]["tray"]] == [
        "E8AFCFFF", "A3D8E1FF",
    ]


def test_merge_ams_reads_integer_tray_exist_bits():
    previous = {"tray_exist_bits": 15, "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"tray_exist_bits": 15, "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert merged["tray_exist_bits"] == "f"
    assert merged["ams"][0]["tray"][1]["tray_color"] == "A3D8E1FF"


def test_parse_tray_exist_bits():
    """The bitmask is reported as-is: it is the only signal that detects a spool swap
    in a slot whose RFID is dark, since such a slot's reported color never changes."""
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": []}}}
    assert parse_tray_exist_bits(status) == "f"


def test_parse_tray_exist_bits_accepts_an_integer_bitmask():
    """Firmware can leave this as int 15. clean_str dropped that, so every
    shop printer on Main stored tray_exist_bits null and keep-hex never fired."""
    status = {"print": {"ams": {"tray_exist_bits": 15, "ams": []}}}
    assert parse_tray_exist_bits(status) == "f"


def test_parse_tray_exist_bits_absent_or_malformed():
    assert parse_tray_exist_bits({}) is None
    assert parse_tray_exist_bits(None) is None
    assert parse_tray_exist_bits({"print": {"ams": {}}}) is None
    assert parse_tray_exist_bits({"print": "not-a-dict"}) is None
    assert parse_tray_exist_bits({"print": {"ams": {"tray_exist_bits": True}}}) is None


def test_idle_trays_needing_rfid_skips_colored_and_empty_bits():
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}
    assert idle_trays_needing_rfid(status) == [(0, 1), (0, 2), (0, 3)]
    empty = {"print": {"ams": {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "E8AFCFFF"}, {"id": "1"}]},
    ]}}}
    assert idle_trays_needing_rfid(empty) == []


def test_ams_needs_pushall_when_loaded_bits_have_no_color():
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}
    assert ams_needs_pushall(status) is True
    assert ams_needs_pushall({"print": {"gcode_state": "RUNNING"}}) is True
    assert ams_needs_pushall({"print": {"ams": {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]},
    ]}}}) is True
    assert ams_needs_pushall({"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
        ]},
    ]}}}) is False


def test_remembered_ams_round_trip(tmp_path):
    path = str(tmp_path / "ams-cache.json")
    ams = {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
        {"id": "0", "tray_color": "E8AFCFFF"},
    ]}]}
    save_remembered_ams(path, "P1", ams)
    assert load_remembered_ams(path, "P1")["ams"][0]["tray"][0]["tray_color"] == "E8AFCFFF"
    assert load_remembered_ams(path, "other") is None


def test_clear_exist_bit_blanks_a_tray_that_still_has_a_color():
    """Bit 0 wipes type, color, and remain even when the tray object still
    carries them. A firmware echo of the old spool must not stay loaded."""
    status = {"print": {"ams": {"tray_exist_bits": "0", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "remain": 40},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": None, "filament_type": None},
    ]
    previous = {"tray_exist_bits": "1", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "remain": 40},
        ]},
    ]}
    incoming = {"tray_exist_bits": "0", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "remain": 40},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    tray = merged["ams"][0]["tray"][0]
    assert tray.get("tray_color") is None
    assert tray.get("tray_type") is None
    assert tray.get("remain") is None


def test_regular_state_other_than_loaded_clears_remembered_color():
    """A regular `{id, state}` update with state other than 11 is an unload.
    State 11 is loaded and keeps the remembered color."""
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "state": 0},
            {"id": "1", "state": 11},
        ]},
    ]}
    merged = merge_ams(previous, incoming)
    assert "tray_color" not in merged["ams"][0]["tray"][0]
    assert merged["ams"][0]["tray"][1]["tray_color"] == "A3D8E1FF"
    # The unloaded tray is empty, so it is not a dark spool waiting on RFID.
    # The loaded tray (state 11) with no color still is.
    assert idle_trays_needing_rfid({"print": {"ams": incoming}}) == [(0, 1)]


def test_regular_state_unload_blanks_an_echoed_color():
    """State other than 11 is empty even when the tray object still has a color.
    The first payload has no remembered tray to compare against."""
    echoed = {"id": "0", "state": 0, "tray_color": "E8AFCFFF", "tray_type": "PLA", "remain": 40}
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [echoed]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": None, "filament_type": None},
    ]
    incoming = {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [echoed]}]}
    previous = {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA", "remain": 40},
        ]},
    ]}
    for merged in (merge_ams(previous, incoming), merge_ams(None, incoming)):
        tray = merged["ams"][0]["tray"][0]
        assert tray.get("tray_color") is None
        assert tray.get("tray_type") is None
        assert tray.get("remain") is None


def test_ams_ht_state_9_with_a_type_stays_slot_17():
    """A loaded AMS-HT tray reports state 9, not 11. That state stays occupied."""
    status = {"print": {"ams": {"ams": [
        {"id": "128", "tray": [
            {"id": "0", "state": 9, "tray_color": "E8AFCFFF", "tray_type": "PLA"},
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 17, "color_hex": "E8AFCFFF", "filament_type": "PLA"},
    ]
    previous = {"ams": [
        {"id": "128", "tray": [
            {"id": "0", "state": 9, "tray_color": "E8AFCFFF", "tray_type": "PLA"},
        ]},
    ]}
    incoming = {"ams": [
        {"id": "128", "tray": [{"id": "0", "state": 9}]},
    ]}
    merged = merge_ams(previous, incoming)
    assert merged["ams"][0]["tray"][0]["tray_color"] == "E8AFCFFF"
    assert merged["ams"][0]["tray"][0]["tray_type"] == "PLA"


def test_a2l_unit_16_uses_the_unit_6_slot_and_exist_bit():
    """Physical unit 16 is read as unit 6. Slots are 25–28. The exist bit is
    the unit-6 base (bit 24 for tray 0), not bit 64."""
    assert ams_slot_number(16, 0) == 25
    assert ams_slot_number(16, 1) == 26
    assert ams_slot_number(16, 2) == 27
    assert ams_slot_number(16, 3) == 28
    unit_6_bit = {"print": {"ams": {
        "tray_exist_bits": format(1 << 24, "x"),
        "ams": [{"id": "16", "tray": [
            {"id": "0", "tray_color": "FF0000FF", "tray_type": "PLA"},
        ]}],
    }}}
    assert parse_ams(unit_6_bit) == [
        {"slot_number": 25, "color_hex": "FF0000FF", "filament_type": "PLA"},
    ]
    bit_64 = {"print": {"ams": {
        "tray_exist_bits": format(1 << 64, "x"),
        "ams": [{"id": "16", "tray": [
            {"id": "0", "tray_color": "FF0000FF", "tray_type": "PLA"},
        ]}],
    }}}
    assert parse_ams(bit_64) == [
        {"slot_number": 25, "color_hex": None, "filament_type": None},
    ]


def test_missing_unit_list_is_none_and_vt_tray_is_not_a_slot():
    """No unit list is None. A unit list with no trays is []. The external
    spool is not appended to slots."""
    assert parse_ams({"print": {}}) is None
    assert parse_ams({"print": {"vt_tray": {
        "id": "254", "tray_color": "FF0000FF", "tray_type": "PLA",
    }}}) is None
    assert parse_ams({"print": {"ams": {"ams": []}}}) == []
    status = {"print": {
        "vt_tray": {"id": "254", "tray_color": "FF0000FF", "tray_type": "PETG"},
        "vir_slot": [{"id": "255", "tray_color": "00FF00FF", "tray_type": "PLA"}],
        "ams": {"ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "000000FF", "tray_type": "PLA"},
            ]},
        ]},
    }}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "000000FF", "filament_type": "PLA"},
    ]
