from bridge.bambu_alerts import describe_hms, lookup_bambu_alert, normalize_bambu_code
from bridge.printer import decode_hms, parse_telemetry


def test_p1s5_live_dump_is_slot_3_cant_feed():
    # Farm dump 2026-09-22: P1S-5 paused at 57% with HMS 0700_6200_0002_0001.
    copy = lookup_bambu_alert("0700_6200_0002_0001")
    assert copy["title"] == "AMS A slot 3 can't feed"
    assert "tangled or stuck spool" in copy["detail"].lower()


def test_ams_runout_reads_as_filament_runout():
    copy = lookup_bambu_alert("0700_2000_0002_0001")
    assert copy["title"] == "Filament runout"
    assert "AMS A slot 1" in copy["detail"]
    assert lookup_bambu_alert("0701_2300_0002_0001")["title"] == "Filament runout"
    assert "AMS B slot 4" in lookup_bambu_alert("0701_2300_0002_0001")["detail"]


def test_print_error_runout_and_cancel():
    assert lookup_bambu_alert("03008004")["title"] == "Filament runout"
    assert lookup_bambu_alert("50348044")["title"] == "Print canceled"
    assert lookup_bambu_alert("0300_400C")["title"] == "Print canceled"


def test_p1s3_bed_level_fatal():
    copy = lookup_bambu_alert("0300_0A00_0001_0005")
    assert copy["title"] == "Bed leveling failed"
    assert "clear the plate" in copy["detail"].lower()


def test_unknown_code_has_no_title():
    assert lookup_bambu_alert("FFFF_FFFF_0002_0001") is None
    assert describe_hms(hms_code="FFFF_FFFF_0002_0001") == {
        "hms_title": None, "hms_detail": None,
    }


def test_normalize_accepts_wiki_and_table_forms():
    assert normalize_bambu_code("HMS_0700-2000-0002-0001") == "0700_2000_0002_0001"
    assert normalize_bambu_code("0700620000020001") == "0700_6200_0002_0001"
    assert normalize_bambu_code("50348044") == "0300_400C"


def test_parse_telemetry_attaches_title_for_p1s5_code():
    # attr 0x07006200, code 0x00020001 → 0700_6200_0002_0001
    snap = parse_telemetry({
        "print": {
            "gcode_state": "PAUSE",
            "mc_percent": 57,
            "hms": [{"attr": 0x07006200, "code": 0x00020001}],
        },
    })
    decoded = decode_hms([{"attr": 0x07006200, "code": 0x00020001}])
    assert decoded["hms_code"] == "0700_6200_0002_0001"
    assert snap["hms_code"] == "0700_6200_0002_0001"
    assert snap["hms_title"] == "AMS A slot 3 can't feed"
    assert snap["hms_severity"] == "SERIOUS"


def test_parse_telemetry_attaches_filament_runout_title():
    snap = parse_telemetry({
        "print": {
            "gcode_state": "PAUSE",
            "hms": [{"attr": 0x07002000, "code": 0x00020001}],
        },
    })
    assert snap["hms_code"] == "0700_2000_0002_0001"
    assert snap["hms_title"] == "Filament runout"
