from bridge.ams import (
    ams_needs_pushall,
    merge_ams,
    parse_ams,
    parse_tray_exist_bits,
    normalize_hex,
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
        {"id": str(unit), "tray": [{"id": str(tray)} for tray in range(4)]}
        for unit in range(3)
    ]}}}
    assert [s["slot_number"] for s in parse_ams(status)] == list(range(1, 13))


def test_parse_ams_emits_empty_trays_rather_than_dropping_them():
    """An empty tray is a dict carrying only an `id`. Skip those and a live P1S
    reporting trays 2/3/4 simply has no slot 1, which makes "show me the empty slots"
    impossible."""
    status = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_color": "1A1A1AFF", "tray_type": "PLA"},  # loaded
            {"id": "1"},                                                # EMPTY
        ]},
    ]}}}
    assert parse_ams(status) == [
        {"slot_number": 1, "color_hex": "1A1A1AFF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": None, "filament_type": None},
    ]


def test_parse_ams_empty_tray_keeps_its_slot_number():
    status = {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "2"}]}]}}}
    assert parse_ams(status) == [{"slot_number": 3, "color_hex": None, "filament_type": None}]


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


def test_parse_tray_exist_bits():
    """The bitmask is reported as-is: it is the only signal that detects a spool swap
    in a slot whose RFID is dark, since such a slot's reported color never changes."""
    status = {"print": {"ams": {"tray_exist_bits": "f", "ams": []}}}
    assert parse_tray_exist_bits(status) == "f"


def test_parse_tray_exist_bits_absent_or_malformed():
    assert parse_tray_exist_bits({}) is None
    assert parse_tray_exist_bits(None) is None
    assert parse_tray_exist_bits({"print": {"ams": {}}}) is None
    assert parse_tray_exist_bits({"print": "not-a-dict"}) is None


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
