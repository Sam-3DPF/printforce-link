"""HMS hygiene: status indicators are not faults, cancel echoes stay on the
legacy wire fields, and a printer that refuses commands is flagged.

`hms_severity` / `hms_code` / `hms_count` / `print_error` still carry cancel
echoes (`0300_400C`, `0500_400E`, `50348044`). 3DPF detects a user cancel
from those fields. The filtered view is additive: `hms_faults`,
`fault_print_error`, `commands_rejected`.
"""

from bridge.bambu_alerts import lookup_bambu_alert
from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter, decode_hms, parse_telemetry
from tests.test_telemetry import FakeClock, _printer

_BED_LEVEL = {"attr": 0x03000A00, "code": 0x00010005}  # 0300_0A00_0001_0005 FATAL
_REJECT = {"attr": 0x05000500, "code": 0x00010007}  # 0500_0500_0001_0007
_STATUS = {"attr": 0x07000100, "code": 0x00001234}  # severity 0
_CANCEL_HMS = {"attr": 0x0300400C, "code": 0x00020001}  # 0300_400C, SERIOUS
_CANCEL_HMS_STATUS = {"attr": 0x0300400C, "code": 0x00000001}  # severity 0 cancel
_REFUSING = (
    "MQTT command verification failed. Status still updates, but starts and "
    "controls are ignored. Power-cycle the printer or re-check LAN-only mode "
    "and the access code."
)


def test_severity_zero_is_ignored():
    """`code >> 16 == 0` is a status indicator. It must not win, and it must
    not count, even when it is the only entry."""
    decoded = decode_hms([_STATUS, _BED_LEVEL])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_code"] == "0300_0A00_0001_0005"
    assert decoded["hms_count"] == 1
    assert decode_hms([_STATUS]) == {
        "hms_severity": None, "hms_code": None, "hms_count": 0,
    }


def test_hms_code_uses_all_sixteen_hex_digits():
    """The key is `attr` as 8 hex digits plus `code` as 8 hex digits."""
    decoded = decode_hms([_BED_LEVEL])
    assert decoded["hms_code"] == "0300_0A00_0001_0005"
    assert decoded["hms_code"].replace("_", "") == "03000A0000010005"
    assert len(decoded["hms_code"].replace("_", "")) == 16
    assert decode_hms([{"attr": 1, "code": 0x00010001}])["hms_code"] == "0000_0001_0001_0001"


def test_bed_level_fatal_stays_fatal():
    decoded = decode_hms([_BED_LEVEL])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_code"] == "0300_0A00_0001_0005"


def test_cancel_echoes_stay_on_the_legacy_fields():
    """3DPF reads `hms_code` for `0300400C` / `0500400E` and `print_error` for
    `50348044`. A severity-0 cancel echo is still one of those codes."""
    assert decode_hms([_CANCEL_HMS])["hms_code"] == "0300_400C_0002_0001"
    assert decode_hms([_CANCEL_HMS])["hms_count"] == 1
    zero = decode_hms([_CANCEL_HMS_STATUS])
    assert zero["hms_code"] == "0300_400C_0000_0001"
    assert zero["hms_count"] == 1
    other = {"attr": 0x0500400E, "code": 0x00010005}
    decoded = decode_hms([_CANCEL_HMS, other])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_code"] == "0500_400E_0001_0005"
    assert decoded["hms_count"] == 2

    snapshot = _printer([{
        "print": {
            "gcode_state": "FAILED",
            "print_error": 50348044,
            "hms": [_CANCEL_HMS],
        },
    }]).snapshot()
    assert snapshot["status"] == "IDLE"
    assert snapshot["print_error"] == "50348044"
    assert snapshot["hms_code"] == "0300_400C_0002_0001"
    assert snapshot["fault_print_error"] is None
    assert snapshot["hms_faults"] == []
    assert snapshot["commands_rejected"] is False


def test_print_error_low_word_below_0x4000_is_ignored():
    """0 and a low word below 0x4000 are status. Cancel codes are not."""
    assert parse_telemetry({"print": {"print_error": 0}})["print_error"] is None
    assert parse_telemetry({"print": {"print_error": 12345}})["print_error"] is None
    assert parse_telemetry({"print": {"print_error": 0x3FFF}})["print_error"] is None
    assert parse_telemetry({"print": {"print_error": 0x4000}})["print_error"] == "16384"
    assert parse_telemetry({"print": {"print_error": 50348044}})["print_error"] == "50348044"
    assert parse_telemetry({"print": {"print_error": "0300400C"}})["print_error"] == "0300400C"
    assert parse_telemetry({"print": {"print_error": "0300_400C"}})["print_error"] == "0300400C"


def test_fault_print_error_drops_cancel_codes_too():
    from bridge.bambu.hms import fault_print_error

    assert fault_print_error(0) is None
    assert fault_print_error(12345) is None
    assert fault_print_error(0x3FFF) is None
    assert fault_print_error(50348044) is None
    assert fault_print_error("0300400C") is None
    assert fault_print_error("0300_400C") is None
    assert fault_print_error(0x4000) == "16384"
    # 0x03008004 is filament runout, low word 0x8004, not a cancel.
    assert fault_print_error(0x03008004) == "50364420"


def test_hms_faults_drop_status_and_cancel_and_keep_the_fatal():
    from bridge.bambu.hms import hms_faults

    faults = hms_faults([_STATUS, _CANCEL_HMS, _CANCEL_HMS_STATUS, _BED_LEVEL])
    assert faults == [{"code": "0300_0A00_0001_0005", "severity": "FATAL"}]


def test_hms_faults_are_worst_first_and_bounded():
    from bridge.bambu.hms import hms_faults

    infos = [{"attr": i + 1, "code": 0x00040001} for i in range(11)]
    entries = [
        _CANCEL_HMS,  # cancel echo, not a fault
        _STATUS,
        *infos,
        {"attr": 0x03000100, "code": 0x00070001},  # UNKNOWN, ranks last
        _BED_LEVEL,
    ]
    faults = hms_faults(entries)
    assert len(faults) == 10
    assert faults[0] == {"code": "0300_0A00_0001_0005", "severity": "FATAL"}
    assert faults[-1]["severity"] == "INFO"
    assert all(item["severity"] != "UNKNOWN" for item in faults)
    assert all("400C" not in item["code"] and "400E" not in item["code"] for item in faults)
    assert all(len(item["code"].replace("_", "")) == 16 for item in faults)


def test_commands_rejected_follows_the_verification_code():
    from bridge.bambu.hms import commands_rejected

    assert commands_rejected([_REJECT]) is True
    assert commands_rejected([_REJECT, _STATUS, _CANCEL_HMS]) is True
    assert commands_rejected([_BED_LEVEL]) is False
    assert commands_rejected([]) is False
    assert commands_rejected(None) is False
    assert commands_rejected([{"attr": 0x05000500}]) is False  # malformed
    telemetry = parse_telemetry({"print": {"hms": [_REJECT]}})
    assert telemetry["commands_rejected"] is True
    assert telemetry["hms_code"] == "0500_0500_0001_0007"
    assert telemetry["hms_severity"] == "FATAL"
    assert telemetry["hms_faults"] == [{
        "code": "0500_0500_0001_0007", "severity": "FATAL",
    }]


def test_refusing_commands_has_a_plain_english_title():
    copy = lookup_bambu_alert("0500_0500_0001_0007")
    assert copy["title"] == "Printer is refusing commands"
    assert copy["detail"] == _REFUSING
    assert lookup_bambu_alert("0500050000010007")["title"] == "Printer is refusing commands"
    snap = parse_telemetry({"print": {"hms": [_REJECT]}})
    assert snap["hms_title"] == "Printer is refusing commands"
    assert snap["hms_detail"] == _REFUSING


def test_commands_rejected_clears_and_records_the_edge():
    """First contact that is not refusing is not an edge. The flag follows the
    merged `hms`, so a delta that omits the key does not clear it."""
    printer = _printer([])
    assert printer.commands_rejected is None
    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "hms": []}})
    assert printer.commands_rejected is False

    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "hms": [_REJECT]}})
    assert printer.commands_rejected is True
    held = printer.snapshot()
    assert held["connection"] == "live"
    assert held["commands_rejected"] is True

    # A partial report with no `hms` key keeps the last list.
    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "mc_percent": 1}})
    assert printer.commands_rejected is True

    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "hms": []}})
    assert printer.commands_rejected is False
    cleared = printer.snapshot()
    assert cleared["commands_rejected"] is False
    assert cleared["hms_faults"] == []

    kinds = [
        event["kind"] for event in printer.collect_log()["events"]
        if event["kind"] in ("commands_rejected", "commands_accepted")
    ]
    assert kinds == ["commands_rejected", "commands_accepted"]


def test_stale_report_keeps_fault_fields():
    clock = FakeClock(10.0)
    printer = _printer(
        [{"print": {
            "gcode_state": "FAILED",
            "print_error": 50348044,
            "hms": [_BED_LEVEL, _CANCEL_HMS, _STATUS],
        }}],
        monotonic=clock.now,
    )
    live = printer.snapshot()
    assert live["connection"] == "live"
    assert live["status"] == "IDLE"
    assert live["print_error"] == "50348044"
    assert live["hms_code"] == "0300_0A00_0001_0005"
    assert live["hms_faults"] == [{"code": "0300_0A00_0001_0005", "severity": "FATAL"}]
    assert live["fault_print_error"] is None
    assert live["commands_rejected"] is False

    clock.advance(46)
    stale = printer.snapshot()
    assert stale["connection"] == "stale"
    assert stale["hms_faults"] == live["hms_faults"]
    assert stale["fault_print_error"] is None
    assert stale["commands_rejected"] is False
    assert stale["print_error"] == "50348044"


def test_offline_report_has_no_fault_information():
    """Same rule as `slots: None`: an offline report is not a reading."""
    printer = BambuPrinter(PrinterConfig(
        bambu_id="01P00A123456789", ip="10.0.0.5", access_code="x", name="P1S-1",
    ))
    snapshot = printer.snapshot()
    assert snapshot["connection"] == "offline"
    assert snapshot["slots"] is None
    assert snapshot["hms_faults"] is None
    assert snapshot["fault_print_error"] is None
    assert snapshot["commands_rejected"] is None
    assert printer.commands_rejected is None
    absent = parse_telemetry(None)
    assert absent["hms_faults"] is None
    assert absent["fault_print_error"] is None
    assert absent["commands_rejected"] is None
