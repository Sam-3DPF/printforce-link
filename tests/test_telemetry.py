"""Telemetry, status mapping, the delta merge, and the observed print duration.

Every function under test here is pure or clock-injected, so none of this needs a
live printer. `BambuPrinter` ingests reports through its session callback, so a
fake session delivers one payload per snapshot — the same cadence the old dump
poll used — and commands go out through `publish`.
"""

import logging
import sys

import pytest

from bridge.config import PrinterConfig
from bridge.printer import (
    _DEFAULT_STALE_AFTER_SECONDS,
    BambuPrinter,
    PrintStopwatch,
    decode_hms,
    merge_status_payload,
    parse_telemetry,
)

_BAMBU_ID = "01P00A123456789"


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self._now = now

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSession:
    """Stands in for `LinkSession`. One queued payload is delivered per snapshot,
    through the printer's report callback, and then the queue stops — a printer
    that has stopped changing. An empty dict is silence, not a report. Set
    `connected` False to pull the printer off the LAN.

    `after_pushall` / `absorb_dumps` are reports that arrive while a pushall is
    being absorbed, in the order the printer sends them.
    """

    def __init__(self, payloads, connected=True, after_pushall=None,
                 absorb_dumps=None):
        self._payloads = list(payloads)
        self._connected = connected
        self.after_pushall = after_pushall
        self.absorb_dumps = list(absorb_dumps or [])
        self._absorb_left = []
        self.on_report = None
        self.on_publish = None
        self.published = []
        self.pushall_calls = 0
        self._in_publish = False
        self._in_emit = False
        self.last_message_at = None
        self.last_connect_error = None

    @property
    def connected(self):
        # A snapshot reads this once. That read is the poll: one report arrives.
        self._deliver_poll_payload()
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = bool(value)

    def publish(self, payload):
        if not self._connected:
            return False
        self.published.append(payload)
        self._in_publish = True
        try:
            pushing = payload.get("pushing") if isinstance(payload, dict) else None
            if isinstance(pushing, dict) and pushing.get("command") == "pushall":
                self.pushall_calls += 1
                self._begin_absorb()
            if self.on_publish is not None:
                self.on_publish(payload)
        finally:
            self._in_publish = False
        return True

    def push(self, payload):
        """The printer sends a new report, either now (during a command) or on the next poll."""
        if self._in_publish:
            self._emit(payload)
        else:
            self._payloads.append(payload)

    def on_sample(self):
        if self._absorb_left:
            self._emit(self._absorb_left.pop(0))

    def _begin_absorb(self):
        self._absorb_left = list(self.absorb_dumps)
        if self.after_pushall is not None and not self.absorb_dumps:
            self._emit(self.after_pushall)
            return
        if self._absorb_left:
            self._emit(self._absorb_left.pop(0))

    def _deliver_poll_payload(self):
        if self._in_emit or not self._connected or not self._payloads:
            return
        payload = self._payloads.pop(0)
        self._emit(payload)

    def _emit(self, payload):
        if not isinstance(payload, dict) or not payload:
            return
        if self.on_report is None or self._in_emit:
            return
        self._in_emit = True
        try:
            self.on_report(payload)
        finally:
            self._in_emit = False


def _stopwatch(monotonic=None, wall_clock=None) -> PrintStopwatch:
    return PrintStopwatch(
        _BAMBU_ID,
        monotonic=monotonic or (lambda: 0.0),
        wall_clock=wall_clock or (lambda: 0.0),
    )


def _printer(payloads, monotonic=None, wall_clock=None, connected=True,
             stale_after_seconds=_DEFAULT_STALE_AFTER_SECONDS,
             after_pushall=None, absorb_dumps=None) -> BambuPrinter:
    cfg = PrinterConfig(bambu_id=_BAMBU_ID, ip="10.0.0.5",
                        access_code="secret", name="P1S-1")
    # The printer's clock defaults to a *frozen* one, so a test that says nothing about
    # time can never accidentally age its own payload into staleness. The tests that care
    # pass a FakeClock and advance it themselves.
    printer = BambuPrinter(cfg, stopwatch=_stopwatch(monotonic, wall_clock),
                           stale_after_seconds=stale_after_seconds,
                           monotonic=monotonic or (lambda: 0.0),
                           sleep=lambda _seconds: None)
    _attach(printer, payloads, connected=connected, after_pushall=after_pushall,
            absorb_dumps=absorb_dumps)
    return printer


def _attach(printer, payloads, **kwargs):
    session = FakeSession(payloads, **kwargs)
    session.on_report = printer._on_mqtt_report
    printer._session = session
    printer._sleep = lambda _seconds: session.on_sample()
    return printer


# --------------------------------------------------------------------------- status
# (`map_status` itself is unit-tested in test_printer_map.py; these cover the
#  snapshot-level behavior built on top of it.)

def test_fresh_bridge_reports_offline_not_idle_even_mid_print():
    """No report yet is OFFLINE, not IDLE. `slots` is None on that report: `[]`
    would tell the cloud the AMS is unplugged and delete every slot row. None
    means this cycle has no AMS information."""
    printer = _printer([{}])
    snapshot = printer.snapshot()
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["connection"] == "offline"
    assert snapshot["slots"] is None
    assert snapshot["nozzle_temper"] is None
    assert snapshot["last_message_age_seconds"] is None


def test_unreadable_printer_reports_offline_and_never_raises():
    """The library-raised path only. **This is not how a printer dies** — read the
    liveness section at the bottom of this file before trusting it as OFFLINE coverage.
    `mqtt_dump()` does not raise once a printer has connected (it is a dict read), so a
    printer that was live and then went away never reaches here."""
    class Unreachable:
        @property
        def connected(self):
            raise OSError("no route to host")

    printer = _printer([])
    printer._session = Unreachable()
    assert printer.snapshot()["status"] == "OFFLINE"


def test_disconnected_printer_reports_offline():
    """A printer that never connected. Also not the dominant failure mode — see below."""
    cfg = PrinterConfig(bambu_id=_BAMBU_ID, ip="10.0.0.5", access_code="x")
    assert BambuPrinter(cfg).snapshot()["status"] == "OFFLINE"  # never connected


def test_malformed_payloads_report_offline_and_never_raise():
    for payload in ({"print": "not-a-dict"},
                    {"print": {"ams": "not-a-dict"}},
                    {"print": {"gcode_state": 7}},
                    {"unexpected": "shape"}):
        snapshot = _printer([payload]).snapshot()
        assert snapshot["status"] == "OFFLINE", payload
        assert snapshot["print_duration_seconds"] is None


# ------------------------------------------------------------------------ telemetry

_FULL_PAYLOAD = {"print": {
    "gcode_state": "RUNNING",
    "layer_num": 42,
    "total_layer_num": 300,
    "mc_percent": 14,
    "mc_remaining_time": 23,            # MINUTES on the wire
    "nozzle_temper": 219.5,
    "nozzle_target_temper": 220.0,
    "bed_temper": 59.8,
    "bed_target_temper": 60.0,
    "chamber_temper": 31.0,
    "gcode_file": "Metadata/plate_1.gcode",
    "subtask_name": "dragon_v3",
    "nozzle_diameter": "0.4",           # a STRING on the wire
    "stg_cur": -1,
    "hms": [],
    "ams": {"tray_exist_bits": "f", "ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}]},
    ]},
}}


def test_snapshot_is_the_full_flat_wire_contract():
    """The whole report, pinned. The telemetry is FLAT on the report, not nested: that
    is what the cloud's ingest_printer_state reads (`{bambu_id, status, slots, plus the
    telemetry fields}`) — nesting it would silently persist a row of NULLs."""
    assert _printer([_FULL_PAYLOAD]).snapshot() == {
        "bambu_id": _BAMBU_ID,
        "status": "PRINTING",
        "slots": [{"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"}],
        "progress_percent": 14,
        "layer_num": 42,
        "total_layer_num": 300,
        "remaining_seconds": 1380,      # 23 minutes, NOT 23 seconds
        "nozzle_temper": 219.5,
        "nozzle_target_temper": 220.0,
        "bed_temper": 59.8,
        "bed_target_temper": 60.0,
        "chamber_temper": 31.0,
        "gcode_file": "Metadata/plate_1.gcode",
        "subtask_name": "dragon_v3",
        "nozzle_diameter": 0.4,
        "stage": None,                  # stg_cur -1 is "no stage" — an absence, not a stage
        "stage_name": None,
        "spd_lvl": None,
        "cooling_fan_percent": None,
        "big_fan1_percent": None,
        "big_fan2_percent": None,
        "heatbreak_fan_percent": None,
        "door_open": None,
        "sdcard": None,
        "chamber_light": None,
        "wifi_signal": None,
        "wifi_wired": None,
        "store_to_sdcard": None,
        "ipcam_record": None,
        "lights_report": None,
        "airduct": None,
        "tray_now": None,
        "tray_tar": None,
        "tray_pre": None,
        "ams_status": None,
        "dry_time": None,
        "dry_status": None,
        "dry_sf_reason": None,
        "drying_unit": None,
        "firmware_version": None,
        "unit_versions": None,
        "external_spool": None,
        "tray_exist_bits": "f",
        "hms_severity": None,
        "hms_code": None,
        "hms_count": 0,
        "hms_title": None,
        "hms_detail": None,
        "print_error": None,
        # Additive. Legacy hms_* / print_error still carry cancel echoes.
        "hms_faults": [],
        "fault_print_error": None,
        "commands_rejected": False,
        "gcode_state": "RUNNING",
        "hms_present": True,
        "hms_empty": True,
        "has_active_file": True,
        "has_active_task": False,
        "has_active_project": False,
        "stage_queue_empty": None,
        "print_type": None,
        "historical_failed_ready": False,
        "user_cancelled": False,
        "print_duration_seconds": None,
        "print_duration_source": None,
        "local_ip": "10.0.0.5",
        "connection": "live",
        "last_message_age_seconds": 0.0,
        "connect_error": None,
        "session_started_at": None,
        # First contact is already RUNNING, so there is no start edge.
        # The print is not one Link submitted.
        "events": [],
        "print_origin": "external",
        # No registered submission, so the matched id is absent. This printer's
        # fake session has no CONNACK, so the session token is still 0; the
        # payload did carry gcode_state.
        "print_submission_id": None,
        "session_seq": 0,
        "session_gcode_seen": True,
    }


def test_remaining_time_is_minutes_converted_to_seconds():
    """The wire value is minutes. A client docstring that called it seconds was wrong; ha-bambulab reads the
    field as minutes. A 60x error on the number the operator looks at most."""
    assert parse_telemetry({"print": {"mc_remaining_time": 23}})["remaining_seconds"] == 1380
    assert parse_telemetry({"print": {"mc_remaining_time": 0}})["remaining_seconds"] == 0


def test_remaining_time_absent_or_nonsense_is_null_not_zero():
    assert parse_telemetry({"print": {}})["remaining_seconds"] is None
    # the library types this field `int | str | None` — it reports "Unknown" sometimes
    assert parse_telemetry({"print": {"mc_remaining_time": "Unknown"}})["remaining_seconds"] is None
    assert parse_telemetry({"print": {"mc_remaining_time": -1}})["remaining_seconds"] is None


def test_nozzle_diameter_string_is_coerced_to_a_number():
    assert parse_telemetry({"print": {"nozzle_diameter": "0.4"}})["nozzle_diameter"] == 0.4
    assert parse_telemetry({"print": {"nozzle_diameter": "junk"}})["nozzle_diameter"] is None


def test_cancel_failed_snapshot_is_idle_not_error():
    snapshot = _printer([{
        "print": {"gcode_state": "FAILED", "print_error": 50348044},
    }]).snapshot()
    assert snapshot["status"] == "IDLE"
    assert snapshot["print_error"] == "50348044"


def test_cancel_flash_latches_through_later_failed_with_cleared_error():
    printer = _printer([
        {"print": {"gcode_state": "RUNNING", "print_error": 0}},
        {"print": {"gcode_state": "PAUSE", "print_error": 50348044}},
        {"print": {"gcode_state": "FAILED", "print_error": 0, "hms": []}},
    ])
    assert printer.snapshot()["status"] == "PRINTING"
    flash = printer.snapshot()
    assert flash["user_cancelled"] is True
    later = printer.snapshot()
    assert later["status"] == "IDLE"
    assert later["print_error"] is None
    assert later["user_cancelled"] is True
    assert later["historical_failed_ready"] is False


def test_real_failed_snapshot_stays_error():
    """FAILED without a cancel code stays ERROR.

    12345 is 0x3039. A low word below 0x4000 is a status indicator, so the
    wire field is empty. The gcode state is still a real fail.
    """
    snapshot = _printer([{
        "print": {"gcode_state": "FAILED", "print_error": 12345},
    }]).snapshot()
    assert snapshot["status"] == "ERROR"
    assert snapshot["print_error"] is None


_P1S_10_HISTORICAL_FAILED = {
    "print": {
        "gcode_state": "FAILED",
        "print_error": 0,
        "hms": [],
        "gcode_file": "",
        "subtask_name": "",
        "subtask_id": "0",
        "task_id": "0",
        "project_id": "0",
        "mc_percent": 0,
        "nozzle_target_temper": 0,
        "bed_target_temper": 0,
        "stg": [],
        "print_type": "idle",
        # These fields remain sticky after an old print and are deliberately ignored.
        "stg_cur": 255,
        "layer_num": 147,
        "total_layer_num": 147,
        "mc_remaining_time": 23,
    },
}


def _historical_failed_observation(sequence_id, **print_overrides):
    payload = {
        "sequence_id": sequence_id,
        "print": dict(_P1S_10_HISTORICAL_FAILED["print"]),
    }
    payload["print"].update(print_overrides)
    return payload


def test_p1s_10_historical_failed_requires_two_consecutive_fresh_observations():
    printer = _printer([
        _historical_failed_observation("first"),
        _historical_failed_observation("second"),
    ])

    first = printer.snapshot()
    second = printer.snapshot()

    assert first["status"] == second["status"] == "IDLE"
    assert first["historical_failed_ready"] is False
    assert second["historical_failed_ready"] is True
    assert second["gcode_state"] == "FAILED"
    assert second["hms_present"] is True
    assert second["hms_empty"] is True
    assert second["has_active_file"] is False
    assert second["has_active_task"] is False
    assert second["has_active_project"] is False
    assert second["stage_queue_empty"] is True
    assert second["print_type"] == "idle"


def test_historical_failed_allows_absent_zero_error_and_empty_print_type():
    first = _historical_failed_observation("first", print_type="")
    second = _historical_failed_observation("second", print_type="")
    del first["print"]["print_error"]
    del second["print"]["print_error"]
    printer = _printer([first, second])

    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is True


def test_raw_readiness_labels_and_error_code_are_safely_bounded():
    telemetry = parse_telemetry({"print": {
        "gcode_state": "failed-" + ("x" * 100),
        "print_error": "e" * 100,
        "print_type": "mode-" + ("y" * 100),
    }})

    assert len(telemetry["gcode_state"]) == 64
    assert len(telemetry["print_error"]) == 64
    assert len(telemetry["print_type"]) == 64


@pytest.mark.parametrize("disqualifier", [
    {"hms": [{"attr": 1, "code": 1}]},
    {"print_error": 12345},
    {"gcode_file": "Metadata/plate_1.gcode"},
    {"task_id": "task-1"},
    {"project_id": "project-1"},
    {"mc_percent": 1},
    {"nozzle_target_temper": 220},
    {"bed_target_temper": 60},
    {"stg": [1]},
    {"print_type": "normal"},
])
def test_historical_failed_disqualifier_resets_the_fresh_streak(disqualifier):
    printer = _printer([
        _historical_failed_observation("first"),
        _historical_failed_observation("disqualified", **disqualifier),
        _historical_failed_observation("restart-one"),
        _historical_failed_observation("restart-two"),
    ])

    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is True


def test_duplicate_historical_failed_payload_resets_the_fresh_streak():
    first = _historical_failed_observation("same")
    printer = _printer([
        first,
        first,
        _historical_failed_observation("fresh-after-duplicate"),
        _historical_failed_observation("second-fresh-after-duplicate"),
    ])

    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is False
    assert printer.snapshot()["historical_failed_ready"] is True


def _p1s_11_leftover_idle(**print_overrides):
    """P1S-11 on 2026-09-22: sticky FAILED, cooled, no file, stage 0, empty HMS."""
    payload = {
        "print": {
            "gcode_state": "FAILED",
            "print_error": 0,
            "hms": [],
            "gcode_file": "",
            "subtask_name": "",
            "mc_percent": 0,
            "nozzle_temper": 23.75,
            "nozzle_target_temper": 0,
            "bed_temper": 20.41,
            "bed_target_temper": 0,
            "stg_cur": 0,
        },
    }
    payload["print"].update(print_overrides)
    return payload


def test_leftover_idle_failed_is_idle_on_first_observation():
    snapshot = _printer([_p1s_11_leftover_idle()]).snapshot()
    assert snapshot["status"] == "IDLE"
    assert snapshot["historical_failed_ready"] is False
    assert snapshot["gcode_state"] == "FAILED"
    assert snapshot["progress_percent"] == 0
    assert snapshot["stage"] == 0


def test_leftover_idle_failed_allows_absent_hms_and_p1_idle_stage():
    payload = _p1s_11_leftover_idle(stg_cur=255)
    del payload["print"]["hms"]
    snapshot = _printer([payload]).snapshot()
    assert snapshot["status"] == "IDLE"
    assert snapshot["stage"] is None


def test_leftover_idle_failed_with_file_or_heat_or_progress_stays_error():
    assert _printer([_p1s_11_leftover_idle(gcode_file="plate.gcode")]).snapshot()["status"] == "ERROR"
    assert _printer([_p1s_11_leftover_idle(nozzle_target_temper=220)]).snapshot()["status"] == "ERROR"
    assert _printer([_p1s_11_leftover_idle(mc_percent=40)]).snapshot()["status"] == "ERROR"
    assert _printer([_p1s_11_leftover_idle(print_error=12345)]).snapshot()["status"] == "ERROR"
    assert _printer([_p1s_11_leftover_idle(stg_cur=6)]).snapshot()["status"] == "ERROR"
    assert _printer([_p1s_11_leftover_idle(hms=[{"attr": 1, "code": 1}])]).snapshot()["status"] == "ERROR"


def test_stage_says_why_a_print_paused():
    """`gcode_state` only ever says PAUSE; `stg_cur` is the only field that says why
    (6 = filament runout, 16 = user, 35 = nozzle clog)."""
    snapshot = _printer([{"print": {"gcode_state": "PAUSE", "stg_cur": 6}}]).snapshot()
    assert snapshot["status"] == "PAUSED"
    assert snapshot["stage"] == 6


def test_the_no_stage_sentinel_is_null_not_a_literal_minus_one():
    """-1 is Bambu's "no stage" sentinel, and it is what *every idle printer* reports.
    `printer_telemetry.stage` is a TEXT column and the ingest coerces an int to text, so
    passing the sentinel through persists the literal string "-1" — a value that reads as
    a real stage, makes `stage IS NOT NULL` true for the entire farm, and forces every
    "why did this print pause?" query to know about a magic string. An absence is
    reported as one, exactly like a negative ETA or a zero start time.
    """
    assert parse_telemetry({"print": {"stg_cur": -1}})["stage"] is None
    assert parse_telemetry({"print": {"stg_cur": "-1"}})["stage"] is None   # a string on the wire
    assert parse_telemetry({"print": {"stg_cur": 255}})["stage"] is None    # P1 idle
    assert parse_telemetry({"print": {"stg_cur": "255"}})["stage"] is None
    assert parse_telemetry({"print": {}})["stage"] is None
    assert parse_telemetry({"print": {"stg_cur": "junk"}})["stage"] is None

    # ...but 0 is a real stage (it is "printing"), so this must not become a falsy check.
    assert parse_telemetry({"print": {"stg_cur": 0}})["stage"] == 0
    assert parse_telemetry({"print": {"stg_cur": 6}})["stage"] == 6         # filament runout


def test_telemetry_of_an_absent_payload_is_all_null():
    telemetry = parse_telemetry(None)
    assert telemetry["remaining_seconds"] is None
    assert telemetry["progress_percent"] is None
    assert telemetry["hms_count"] == 0
    assert telemetry["tray_exist_bits"] is None


# ------------------------------------------------------------------------------ HMS

def test_decode_hms_reports_the_worst_alarm_and_a_count():
    """Severity is `code >> 16` (1 fatal ... 4 info). The UI needs to know whether
    something is wrong, how bad, and how many — not the whole array."""
    decoded = decode_hms([
        {"attr": 0x03000200, "code": 0x00040001},   # severity 4 -> info
        {"attr": 0x03000100, "code": 0x00010002},   # severity 1 -> fatal
    ])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_count"] == 2
    assert decoded["hms_code"] == "0300_0100_0001_0002"   # the fatal one, not the info one


def test_decode_hms_ranks_by_the_severity_number_not_by_its_name():
    """Ranking by the severity's *name* sorts alphabetically, so "COMMON" beats "FATAL"
    and "INFO" beats "SERIOUS" — a machine reporting a fatal alarm alongside a lesser
    one would report the lesser one as its worst, and the UI would under-alarm."""
    fatal_and_common = decode_hms([
        {"attr": 0x03000100, "code": 0x00010002},   # severity 1 -> FATAL
        {"attr": 0x03000200, "code": 0x00030001},   # severity 3 -> COMMON
    ])
    assert fatal_and_common["hms_severity"] == "FATAL"
    assert fatal_and_common["hms_code"] == "0300_0100_0001_0002"   # the fatal one
    assert fatal_and_common["hms_count"] == 2

    serious_and_info = decode_hms([
        {"attr": 0x03000100, "code": 0x00020002},   # severity 2 -> SERIOUS
        {"attr": 0x03000200, "code": 0x00040001},   # severity 4 -> INFO
    ])
    assert serious_and_info["hms_severity"] == "SERIOUS"


def test_decode_hms_an_unrecognized_severity_sorts_last_and_never_raises():
    """`severity` is the top 16 bits of an arbitrary int, so a value outside 1-4 is
    entirely possible. It must rank below every real severity — and the ranking must not
    mix int and str keys while doing it, or comparing them raises TypeError.
    """
    decoded = decode_hms([
        {"attr": 0x03000100, "code": 0x00010002},   # severity 1 -> FATAL
        {"attr": 0x03000200, "code": 0x00070001},   # severity 7 -> unrecognized
    ])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_count"] == 2

    # ...and on its own it is reported, not dropped
    assert decode_hms([{"attr": 1, "code": 0x00070001}])["hms_severity"] == "UNKNOWN"


def test_an_unrecognized_severity_does_not_knock_a_live_printer_offline():
    """The ranking runs inside parse_telemetry, inside _build_snapshot, inside
    snapshot()'s blanket `except Exception`. A TypeError there never surfaces as an
    error: it silently becomes an OFFLINE report with no slots and null telemetry for a
    printer that is *actively printing*, and the only trace is one "unreadable" log
    line. Two alarms are needed to provoke it — min() over a single-element list never
    compares its keys.
    """
    payload = {"print": {
        "gcode_state": "RUNNING",
        "mc_percent": 40,
        "hms": [{"attr": 0x03000100, "code": 0x00010002},    # FATAL
                {"attr": 0x03000200, "code": 0x00070001}],   # an unrecognized severity
        "ams": {"ams": [
            {"id": "0", "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}]},
        ]},
    }}
    snapshot = _printer([payload]).snapshot()

    assert snapshot["status"] == "PRINTING"         # not OFFLINE
    assert snapshot["progress_percent"] == 40       # not None
    assert snapshot["slots"] == [
        {"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"},
    ]
    assert snapshot["hms_severity"] == "FATAL"


def test_decode_hms_severity_ladder():
    for code, severity in ((0x00010001, "FATAL"), (0x00020001, "SERIOUS"),
                           (0x00030001, "COMMON"), (0x00040001, "INFO")):
        assert decode_hms([{"attr": 1, "code": code}])["hms_severity"] == severity


def test_decode_hms_no_alarms():
    assert decode_hms([]) == {"hms_severity": None, "hms_code": None, "hms_count": 0}
    assert decode_hms(None)["hms_count"] == 0


def test_decode_hms_ignores_malformed_entries():
    decoded = decode_hms([{"attr": 1, "code": 0x00010001}, {"attr": 1}, "junk", {}])
    assert decoded["hms_severity"] == "FATAL"
    assert decoded["hms_count"] == 1


def test_snapshot_surfaces_an_active_alarm():
    payload = {"print": {"gcode_state": "FAILED",
                         "hms": [{"attr": 0x03000100, "code": 0x00010002}]}}
    snapshot = _printer([payload]).snapshot()
    assert snapshot["hms_severity"] == "FATAL"
    assert snapshot["hms_count"] == 1


# ---------------------------------------------------------------------- delta merge

def test_delta_does_not_blank_the_last_known_scalars():
    """Most Bambu reports are partial deltas; only a `pushall` carries the whole object.
    Without the merge, temperatures and the ETA flicker to null between polls."""
    printer = _printer([
        {"print": {"gcode_state": "RUNNING", "nozzle_temper": 219.5, "mc_percent": 40,
                   "mc_remaining_time": 23}},
        {"print": {"gcode_state": "RUNNING", "mc_percent": 41}},   # a delta: no temps, no ETA
    ])
    printer.snapshot()
    snapshot = printer.snapshot()
    assert snapshot["nozzle_temper"] == 219.5      # would be None without the merge
    assert snapshot["remaining_seconds"] == 1380
    assert snapshot["progress_percent"] == 41      # ...but the delta's own value wins


def test_partial_ipcam_delta_keeps_the_record_setting():
    full = {"print": {"ipcam": {"ipcam_record": "enable", "timelapse": "disable"}}}
    delta = {"print": {"ipcam": {"timelapse": "enable"}}}
    merged = merge_status_payload(merge_status_payload(None, full), delta)
    assert merged["print"]["ipcam"] == {"ipcam_record": "enable", "timelapse": "enable"}


def test_delta_without_an_ams_key_keeps_the_last_known_trays():
    printer = _printer([
        {"print": {"gcode_state": "IDLE", "ams": {"ams": [
            {"id": "0", "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}]}]}}},
        {"print": {"gcode_state": "RUNNING", "nozzle_temper": 200.0}},   # no `ams` at all
    ])
    printer.snapshot()
    assert printer.snapshot()["slots"] == [
        {"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"},
    ]


def test_a_printer_that_has_never_reported_ams_says_none_not_no_trays():
    """**The slot-wipe regression.** The sibling test above covers a printer whose `ams`
    was seen once and then omitted from a delta — the merge keeps it. This is the window
    BEFORE that: connected, already RUNNING, and the first full push (the only one
    carrying `ams`) has not landed. Bambu sends one full status then deltas, and
    `mqtt_dump()` accumulates only one level deep, so this state is routine, not exotic.

    Reporting `slots: []` here told the cloud the AMS was empty, and because the printer
    is PRINTING rather than OFFLINE it walked straight past the OFFLINE-only guard in
    `_reconcile_slots` and DELETED every slot row. Four of the dev farm's seven AMS
    printers were sitting at zero slots from exactly this — no colors in the fleet view,
    and unroutable, since dispatch matches a batch's colors against what a printer
    reports holding.
    """
    printer = _printer([{"print": {"gcode_state": "RUNNING", "nozzle_temper": 220.0}}])
    snapshot = printer.snapshot()

    assert snapshot["status"] == "PRINTING"      # live, so the OFFLINE guard cannot help
    assert snapshot["slots"] is None             # "no information", NOT "no trays"


def test_a_printer_reporting_zero_ams_units_still_says_no_trays():
    """The other side of the same coin: an unplugged AMS is a real, authoritative report
    that there are no trays, and it must still reconcile the stale rows away. None must
    not swallow this case — see `_reconcile_slots`'s staleness argument."""
    printer = _printer([{"print": {"gcode_state": "RUNNING", "ams": {"ams": []}}}])
    assert printer.snapshot()["slots"] == []


def test_partial_ams_with_loaded_bits_asks_the_printer_for_a_full_dump():
    printer = _printer([{
        "print": {
            "gcode_state": "FINISH",
            "ams": {
                "tray_exist_bits": "f",
                "ams": [{"id": "0", "tray": [
                    {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                    {"id": "1"},
                    {"id": "2"},
                    {"id": "3"},
                ]}],
            },
        },
    }])
    snapshot = printer.snapshot()
    assert snapshot["slots"] == [
        {"slot_number": 1, "color_hex": "E8AFCFFF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": None, "filament_type": None},
        {"slot_number": 3, "color_hex": None, "filament_type": None},
        {"slot_number": 4, "color_hex": None, "filament_type": None},
    ]
    assert printer._session.pushall_calls == 1


def test_p1s6_partial_dump_does_not_store_empty_for_loaded_trays():
    """Live Main after Refresh on 0.1.13: P1S-6 slot 1 #E8AFCF PLA, slots 2-4
    hex/type null, tray_exist_bits null. The app showed Empty because Link stored it."""
    printer = _printer([{
        "print": {
            "gcode_state": "FINISH",
            "ams": {
                "ams": [{"id": "0", "tray": [
                    {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                    {"id": "1"},
                    {"id": "2"},
                    {"id": "3"},
                ]}],
            },
        },
    }])
    snapshot = printer.snapshot()
    assert snapshot["slots"] is None
    assert snapshot["tray_exist_bits"] is None
    assert printer._session.pushall_calls == 1


def test_integer_tray_exist_bits_are_reported_as_hex():
    printer = _printer([{
        "print": {
            "gcode_state": "IDLE",
            "ams": {
                "tray_exist_bits": 15,
                "ams": [{"id": "0", "tray": [
                    {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                    {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
                    {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
                    {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
                ]}],
            },
        },
    }])
    snapshot = printer.snapshot()
    assert snapshot["tray_exist_bits"] == "f"
    assert [slot["color_hex"] for slot in snapshot["slots"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_pushall_absorbs_the_full_ams_dump_before_the_next_delta():
    """mqtt_dump is one-level-deep. A later P1 delta can overwrite the pushall
    dump before the 15s poll. Absorb the dump right after asking."""
    full = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": 15, "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
                {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
                {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
            ]},
        ]},
    }}
    printer = _printer(
        [{
            "print": {
                "gcode_state": "FINISH",
                "ams": {"ams": [{"id": "0", "tray": [
                    {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                    {"id": "1"},
                    {"id": "2"},
                    {"id": "3"},
                ]}]},
            },
        }],
        after_pushall=full,
    )
    snapshot = printer.snapshot()
    assert [slot["color_hex"] for slot in snapshot["slots"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]
    assert snapshot["tray_exist_bits"] == "f"


def test_mid_print_absorb_keeps_full_ams_when_a_delta_overwrites_mqtt_dump():
    """While PRINTING, mqtt_dump is overwritten by frequent deltas. A single
    read after 2s sees the stub. Live P1S-6 on 0.1.14 stored bits `f` and left
    slots 2-4 Empty."""
    stub = {"print": {
        "gcode_state": "RUNNING",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]}]},
    }}
    full = {"print": {
        "gcode_state": "RUNNING",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
        ]}]},
    }}
    printer = _printer([stub], absorb_dumps=[stub, full, stub])
    snapshot = printer.snapshot()
    assert [slot["color_hex"] for slot in snapshot["slots"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]
    assert snapshot["tray_exist_bits"] == "f"


def test_mqtt_message_keeps_idle_hex_when_dump_is_already_a_stub():
    """Live P1S-6 on 0.1.15: mqtt_dump never held idle hex. The full AMS
    was on the MQTT message; the library `|=` replaced it before a poll."""
    stub = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]}]},
    }}
    full = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
            {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
            {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
        ]}]},
    }}
    printer = _printer([stub, stub])
    printer.state.ingest(full)
    printer.state.ingest(stub)
    snapshot = printer.snapshot()
    assert [slot["color_hex"] for slot in snapshot["slots"]] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_snapshot_asks_rfid_for_p1s9_blank_loaded_trays():
    """Live P1S-9 on 0.1.17: bits `ff`, slots 1-2 and 7-8 have hex, 3-6 do not.
    Automatic pushall used read_idle_rfid=False, so those four never got RFID."""
    stub = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": "ff", "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
                {"id": "1", "tray_color": "000000FF", "tray_type": "PLA"},
                {"id": "2"},
                {"id": "3"},
            ]},
            {"id": "1", "tray": [
                {"id": "0"},
                {"id": "1"},
                {"id": "2", "tray_color": "9B9EA0FF", "tray_type": "PLA"},
                {"id": "3", "tray_color": "042F56FF", "tray_type": "PLA"},
            ]},
        ]},
    }}
    printer = _printer([stub], absorb_dumps=[stub, stub, stub])
    printer.snapshot()
    published = getattr(printer._session, "published", [])
    rfid = [item for item in published if "print" in item]
    assert [(item["print"]["ams_id"], item["print"]["slot_id"]) for item in rfid] == [
        (0, 2), (0, 3), (1, 0), (1, 1),
    ]


def test_refresh_asks_ams_get_rfid_for_loaded_trays_without_hex():
    stub = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]}]},
    }}
    printer = _printer([stub], absorb_dumps=[stub, stub, stub])
    printer.snapshot()
    printer._session.published = []
    printer.request_full_status()
    published = getattr(printer._session, "published", [])
    commands = [item["print"] for item in published if "print" in item]
    assert [cmd["command"] for cmd in commands] == [
        "ams_get_rfid", "ams_get_rfid", "ams_get_rfid",
    ]
    assert [cmd["slot_id"] for cmd in commands] == [1, 2, 3]


def test_print_end_asks_for_a_full_ams_dump_again():
    """Refresh during a job may never get idle-tray hex. Ask again when the
    print ends, even if the connect-time pushall budget is spent."""
    stub = {"print": {
        "gcode_state": "RUNNING",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]}]},
    }}
    ended = {"print": {
        "gcode_state": "FINISH",
        "ams": {"tray_exist_bits": "f", "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
            {"id": "1"},
            {"id": "2"},
            {"id": "3"},
        ]}]},
    }}
    printer = _printer([stub, ended])
    printer.snapshot()
    printer._full_status_attempts = 3
    printer._session.pushall_calls = 0
    printer._session.published = []
    printer.snapshot()
    assert printer._session.pushall_calls >= 1
    assert [item["print"]["command"] for item in printer._session.published if "print" in item] == [
        "ams_get_rfid", "ams_get_rfid", "ams_get_rfid",
    ]


def test_remembered_ams_hex_survives_a_new_process_seeing_only_the_active_tray(tmp_path):
    cache = tmp_path / "ams-cache.json"
    cfg = PrinterConfig(bambu_id=_BAMBU_ID, ip="10.0.0.5", access_code="secret", name="P1S-6")
    first = BambuPrinter(
        cfg, stopwatch=_stopwatch(), monotonic=lambda: 0.0, ams_cache_path=str(cache),
        sleep=lambda _seconds: None,
    )
    _attach(first, [{
        "print": {"gcode_state": "IDLE", "ams": {"tray_exist_bits": "f", "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                {"id": "1", "tray_color": "A3D8E1FF", "tray_type": "PLA"},
                {"id": "2", "tray_color": "000000FF", "tray_type": "PLA"},
                {"id": "3", "tray_color": "FFFFFFFF", "tray_type": "PLA"},
            ]},
        ]}},
    }])
    first.snapshot()

    restarted = BambuPrinter(
        cfg, stopwatch=_stopwatch(), monotonic=lambda: 0.0, ams_cache_path=str(cache),
        sleep=lambda _seconds: None,
    )
    _attach(restarted, [{
        "print": {"gcode_state": "FINISH", "ams": {"tray_exist_bits": "f", "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "E8AFCFFF", "tray_type": "PLA"},
                {"id": "1"},
                {"id": "2"},
                {"id": "3"},
            ]},
        ]}},
    }])
    slots = restarted.snapshot()["slots"]
    assert [slot["color_hex"] for slot in slots] == [
        "E8AFCFFF", "A3D8E1FF", "000000FF", "FFFFFFFF",
    ]


def test_ams_delta_clears_a_tray_that_became_empty():
    """The merge must be able to express key *removal*. A naive deep merge cannot: a
    tray going from loaded to empty would keep its last-known color forever, telling the
    router the printer holds a color it does not. The bit clearing is the unload
    signal. An id-only tray without that bit is a P1 delta, not Empty."""
    printer = _printer([
        {"print": {"gcode_state": "IDLE", "ams": {"tray_exist_bits": "3", "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"},
                {"id": "1", "tray_color": "00AE42FF", "tray_type": "PLA"},
            ]}]}}},
        {"print": {"gcode_state": "IDLE", "ams": {"tray_exist_bits": "1", "ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"},
                {"id": "1"},
            ]}]}}},
    ])
    printer.snapshot()
    assert printer.snapshot()["slots"] == [
        {"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"},
        {"slot_number": 2, "color_hex": None, "filament_type": None},
    ]


def test_merge_takes_the_ams_wholesale_and_never_field_by_field():
    cached = {"print": {"nozzle_temper": 219.5,
                        "ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}]}}}
    merged = merge_status_payload(cached, {"print": {"ams": {"ams": []}}})
    assert merged["print"]["ams"] == {"ams": []}         # replaced whole, not merged into
    assert merged["print"]["nozzle_temper"] == 219.5     # ...while scalars still persist


def test_merge_does_not_alias_the_librarys_live_dict():
    """`mqtt_dump()` returns the library's internal dict by reference and the MQTT
    thread keeps mutating it. Caching it uncopied would make "last known" mean
    "current"."""
    live = {"print": {"nozzle_temper": 219.5}}
    merged = merge_status_payload(None, live)
    live["print"]["nozzle_temper"] = 25.0      # the MQTT thread moves on
    assert merged["print"]["nozzle_temper"] == 219.5


def test_the_cache_never_aliases_the_librarys_live_dict_across_polls():
    """`cached` is only ever a previous return value of this function, so it needs no
    deep copy — but everything arriving from `incoming` still does, at every depth. A
    shallow copy on the way IN would let the MQTT thread rewrite, after the fact, what
    we already reported as "last known"."""
    live = {"print": {"nozzle_temper": 219.5, "ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}]}}}
    merged = merge_status_payload(None, live)
    merged = merge_status_payload(merged, {"print": {"mc_percent": 41}})   # a later delta

    live["print"]["nozzle_temper"] = 25.0                                  # the MQTT thread
    live["print"]["ams"]["ams"][0]["tray"][0]["tray_type"] = "PETG"        # ...deep inside

    assert merged["print"]["nozzle_temper"] == 219.5
    assert merged["print"]["ams"]["ams"][0]["tray"][0]["tray_type"] == "PLA"


def test_merging_never_mutates_the_cached_payload_it_was_given():
    """`merged` starts as a shallow copy of `cached`, so `print` must be rebuilt rather
    than updated in place — otherwise the merge would write straight through into the
    dict it was handed."""
    cached = merge_status_payload(None, {"print": {"nozzle_temper": 219.5, "mc_percent": 40}})
    merge_status_payload(cached, {"print": {"mc_percent": 41}})
    assert cached["print"]["mc_percent"] == 40      # the caller's payload is untouched


def test_merge_tolerates_junk():
    assert merge_status_payload(None, None) == {}
    assert merge_status_payload(None, "junk") == {}
    assert merge_status_payload({"print": {"a": 1}}, {})["print"] == {"a": 1}


# ------------------------------------------------------------------- print stopwatch
# PrintStopwatch owns the measurement and is driven one poll at a time by observe(),
# so these need no printer and no client — just a clock.

def test_the_duration_is_latched_not_a_single_poll_blip():
    """The completion report retries until 3DPF acks it, so a duration that existed for
    exactly one poll would be lost to the first dropped POST — and nothing else in the
    system can reconstruct it."""
    clock = FakeClock()
    stopwatch = _stopwatch(monotonic=clock.now)
    stopwatch.observe("IDLE", {})
    stopwatch.observe("RUNNING", {})
    clock.advance(3600)

    stopwatch.observe("FINISH", {})
    assert stopwatch.duration_seconds == 3600
    stopwatch.observe("FINISH", {})                 # ...and every poll after this one
    assert stopwatch.duration_seconds == 3600
    stopwatch.observe("FINISH", {})
    assert stopwatch.duration_seconds == 3600


def test_the_next_print_clears_the_previous_duration():
    clock = FakeClock()
    stopwatch = _stopwatch(monotonic=clock.now)
    stopwatch.observe("IDLE", {})
    stopwatch.observe("RUNNING", {})
    clock.advance(60)
    stopwatch.observe("FINISH", {})
    assert stopwatch.duration_seconds == 60

    stopwatch.observe("IDLE", {})                   # plate cleared
    assert stopwatch.duration_seconds is None
    stopwatch.observe("RUNNING", {})                # the next print starts
    assert stopwatch.duration_seconds is None


def test_bridge_started_mid_print_falls_back_to_the_printers_start_time():
    """The bridge's stopwatch only saw the tail of this print, so it must not be the
    source — reporting the tail would silently under-report the cost. The printer's own
    `gcode_start_time` is the only source that covers the whole run."""
    wall = FakeClock(1_800_000_000.0)
    clock = FakeClock()
    stopwatch = _stopwatch(monotonic=clock.now, wall_clock=wall.now)

    # the first thing the bridge ever sees: a print already running, started 10,000s ago
    stopwatch.observe("RUNNING", {"gcode_start_time": "1799990000"})
    clock.advance(60)          # the bridge only ever watched 60s of it
    wall.advance(60)
    stopwatch.observe("FINISH", {})

    assert stopwatch.duration_seconds == 10_060     # not 60
    assert stopwatch.source == "printer"


def test_the_stopwatch_wins_when_the_bridge_watched_the_print_start():
    """Both sources are available here. The monotonic stopwatch is exact and immune to
    printer clock skew, so it is preferred whenever it covers the whole run."""
    wall = FakeClock(1_800_000_000.0)
    clock = FakeClock()
    stopwatch = _stopwatch(monotonic=clock.now, wall_clock=wall.now)
    stopwatch.observe("IDLE", {})
    stopwatch.observe("RUNNING", {"gcode_start_time": "1799990000"})   # skewed clock
    clock.advance(1800)
    wall.advance(1800)
    stopwatch.observe("FINISH", {})

    assert stopwatch.duration_seconds == 1800
    assert stopwatch.source == "bridge"


def test_a_zero_gcode_start_time_is_not_a_1970_print():
    """Printers that never set it report "0"."""
    wall = FakeClock(1_800_000_000.0)
    stopwatch = _stopwatch(monotonic=FakeClock().now, wall_clock=wall.now)
    stopwatch.observe("RUNNING", {"gcode_start_time": "0"})
    stopwatch.observe("FINISH", {})

    assert stopwatch.duration_seconds is None
    assert stopwatch.source is None


# ------------------------------------------------ the duration, through the snapshot
# ...and the wiring that carries it onto the report.

def test_running_to_finish_carries_the_observed_duration():
    clock = FakeClock()
    printer = _printer([
        {"print": {"gcode_state": "IDLE"}},
        {"print": {"gcode_state": "RUNNING"}},
        {"print": {"gcode_state": "FINISH"}},
    ], monotonic=clock.now)

    assert printer.snapshot()["status"] == "IDLE"
    assert printer.snapshot()["status"] == "PRINTING"
    clock.advance(7200)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "NEEDS_CLEARING"
    assert snapshot["print_duration_seconds"] == 7200
    assert snapshot["print_duration_source"] == "bridge"


def test_a_failed_print_also_carries_its_duration():
    clock = FakeClock()
    printer = _printer([
        {"print": {"gcode_state": "IDLE"}},
        {"print": {"gcode_state": "RUNNING"}},
        {"print": {"gcode_state": "FAILED"}},
    ], monotonic=clock.now)
    printer.snapshot()
    printer.snapshot()
    clock.advance(600)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "ERROR"
    assert snapshot["print_duration_seconds"] == 600   # partial hours still wear the machine


def test_a_pause_counts_toward_the_observed_duration():
    """Wall-clock, as the operator experienced it — and as the printer's own
    `gcode_start_time` would measure it, so the two sources stay comparable."""
    clock = FakeClock()
    printer = _printer([
        {"print": {"gcode_state": "IDLE"}},
        {"print": {"gcode_state": "RUNNING"}},
        {"print": {"gcode_state": "PAUSE"}},
        {"print": {"gcode_state": "RUNNING"}},
        {"print": {"gcode_state": "FINISH"}},
    ], monotonic=clock.now)
    printer.snapshot()
    printer.snapshot()
    clock.advance(100)
    assert printer.snapshot()["status"] == "PAUSED"
    clock.advance(100)
    printer.snapshot()
    clock.advance(100)

    assert printer.snapshot()["print_duration_seconds"] == 300


def test_no_duration_when_the_bridge_never_saw_the_print_run():
    """Booting to find an uncleared plate from yesterday. We do not know how long that
    print took, and a fabricated number would poison the cost data — the cost snapshot
    falls back to the slicer's estimate on a null, but would believe a wrong value."""
    printer = _printer([{"print": {"gcode_state": "FINISH"}}])
    snapshot = printer.snapshot()

    assert snapshot["status"] == "NEEDS_CLEARING"
    assert snapshot["print_duration_seconds"] is None
    assert snapshot["print_duration_source"] is None


# --------------------------------------------------------- liveness: the cache is bounded
# The failure mode the OFFLINE path actually exists for: a printer that CONNECTS and then
# dies — unplugged, powered off, knocked off the WiFi. **Nothing raises when that
# happens.** `mqtt_dump()` is a dict read on a cache the library owns, so it keeps
# returning the last payload (or {}) forever. Every OFFLINE test above it reaches OFFLINE
# either by making the library raise or by never connecting, and neither can happen to a
# printer that was live a moment ago — so none of them cover this.
#
# Two independent signals bound the cache, and each is tested on its own:
#   * the MQTT link (authoritative — in LAN mode the printer *is* the broker), and
#   * the payload still moving (the backstop; needs no library support at all).

_PRINTING = {"print": {
    "gcode_state": "RUNNING",
    "mc_percent": 47,
    "nozzle_temper": 220.0,
    "ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}]},
    ]},
}}


def test_a_gap_between_pushes_does_not_knock_a_live_printer_offline():
    """First, the thing the guard must NOT do. Pushes are irregular deltas — that is why
    the merge exists — so a poll that lands between them is not a dead printer. The bound
    has to be a window, not a tripwire, or the fix for a fail-open bug is a fleet that is
    permanently OFFLINE.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS - 1)     # quiet, but not yet suspicious
    snapshot = printer.snapshot()

    assert snapshot["status"] == "PRINTING"
    assert snapshot["progress_percent"] == 47           # ...still reported from the cache


def test_a_printer_that_dies_mid_print_goes_offline_not_printing_forever():
    """Someone unplugs a printer that is PRINTING at 47%, 220°C, and the socket
    still looks up. Status stays OFFLINE so this is not a live print and not an
    authorization to dispatch. The session has not been called unreachable, so
    the report is ``connection: stale`` and keeps the last telemetry and the
    last AMS slots. Nulling those, or sending ``slots: []``, would either hide
    the last picture or tell the cloud the AMS was unplugged.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)   # then it repeats, i.e. freezes
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)        # the plug comes out
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["connection"] == "stale"
    assert snapshot["progress_percent"] == 47
    assert snapshot["nozzle_temper"] == 220.0
    assert snapshot["slots"] == [
        {"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"},
    ]


def test_stale_historical_failed_payload_never_becomes_ready():
    clock = FakeClock()
    printer = _printer(
        [_historical_failed_observation("first")],
        monotonic=clock.now,
    )
    assert printer.snapshot()["historical_failed_ready"] is False

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    stale = printer.snapshot()

    assert stale["status"] == "OFFLINE"
    assert stale["historical_failed_ready"] is False


def test_a_frozen_idle_printer_reports_offline_and_never_idle():
    """The one that costs a print. IDLE is the *sole authorization for dispatch*, so a
    printer that was IDLE when it died and keeps reporting IDLE is a job sent to an
    unplugged machine — filament deducted, batch stamped PRINTING, nothing printing.
    `map_status`'s OFFLINE default exists to stop precisely this, and replaying a stale
    cache walks straight around it.
    """
    clock = FakeClock()
    printer = _printer([{"print": {"gcode_state": "IDLE"}}], monotonic=clock.now)
    assert printer.snapshot()["status"] == "IDLE"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    status = printer.snapshot()["status"]

    assert status != "IDLE"        # spelled out, because THIS is the value that dispatches
    assert status == "OFFLINE"


def test_an_empty_dump_after_a_live_poll_is_not_replayed_from_the_cache():
    """An empty dump is silence, not news. Status stays OFFLINE so the last
    payload is not presented as a live print. It is still the last-known
    picture, reported as stale rather than wiped."""
    clock = FakeClock()
    printer = _printer([_PRINTING, {}], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()                          # mqtt_dump() -> {}

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["connection"] == "stale"
    assert snapshot["progress_percent"] == 47


def test_offline_does_not_flap_back_to_printing_while_the_printer_stays_dead():
    """Silence stays OFFLINE. The merged payload is kept, and the freshness
    baseline is not reset, so a quiet printer is not reported live again just
    because the same dict is still sitting there.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    printer.snapshot()
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    assert printer.snapshot()["status"] == "OFFLINE"

    for _ in range(5):                                     # it is still dead; it stays OFFLINE
        clock.advance(15)
        assert printer.snapshot()["status"] == "OFFLINE"


def test_a_printer_that_comes_back_reports_live_telemetry_again():
    """A new report after the quiet gap is live again. The gap does not drop
    the merged payload, so a partial IDLE delta keeps the last percent. That
    percent is still between 0 and 100, and ``promote_live_idle`` treats gcode
    IDLE plus that percent as PRINTING — the same rule a live delta follows.
    A full post-CONNACK recovery that must not look live *before* the new
    message is covered in test_report_contract.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    printer.snapshot()
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    quiet = printer.snapshot()
    assert quiet["status"] == "OFFLINE"
    assert quiet["connection"] == "stale"

    printer._session.push({"print": {"gcode_state": "IDLE", "nozzle_temper": 24.0}})
    clock.advance(15)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "live"
    assert snapshot["status"] == "PRINTING"
    assert snapshot["nozzle_temper"] == 24.0
    assert snapshot["progress_percent"] == 47


_FINISHED = {"print": {
    "gcode_state": "FINISH",
    "mc_percent": 100,
    "nozzle_temper": 29.0,
    "bed_temper": 28.0,
    "subtask_name": "batch-2026-09-19-8DZGHF0z-1.3mf",
}}


def test_a_finished_printer_gone_quiet_reports_offline_without_publishing():
    """A finished plate that stops talking is OFFLINE and stale: the last
    temperatures stay on the report, and snapshot does not publish a command
    to pretend they are a new reading.
    """
    clock = FakeClock()
    printer = _printer([_FINISHED], monotonic=clock.now)
    assert printer.snapshot()["status"] == "NEEDS_CLEARING"
    before = list(printer._session.published)

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    slept = []
    printer._sleep = lambda seconds: slept.append(seconds)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["connection"] == "stale"
    assert snapshot["nozzle_temper"] == 29.0
    assert snapshot["progress_percent"] == 100
    assert printer._session.published == before
    assert slept == []


def test_silence_on_a_live_socket_reports_offline_without_publishing():
    """Silence longer than the staleness window is OFFLINE even when the socket
    still looks up. Status stays OFFLINE so the frozen dump is not a live print,
    and snapshot must not publish while it decides that.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"
    before = list(printer._session.published)

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["connection"] == "stale"
    assert snapshot["progress_percent"] == 47
    assert printer._session.published == before


def test_an_identical_report_still_counts_as_hearing_the_printer():
    """A finished plate republishes the same temperatures. The merged dump does not
    change, but the MQTT message arrived. That is not silence.
    """
    clock = FakeClock()
    printer = _printer([_FINISHED], monotonic=clock.now)
    assert printer.snapshot()["status"] == "NEEDS_CLEARING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    printer._on_mqtt_report(_FINISHED)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "NEEDS_CLEARING"


def test_a_link_that_stays_down_does_not_publish():
    """A dropped socket is OFFLINE on the next report. Snapshot does not publish
    and does not sleep while it says so — recovery belongs to the session.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    printer._session.connected = False
    clock.advance(1)
    before = list(printer._session.published)
    assert printer.snapshot()["status"] == "OFFLINE"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    slept = []
    printer._sleep = lambda seconds: slept.append(seconds)
    assert printer.snapshot()["status"] == "OFFLINE"
    assert printer._session.published == before
    assert slept == []


def test_a_printer_that_never_pushes_stays_offline_without_a_command():
    """No report yet is OFFLINE. Snapshot must not invent a resume command to
    fill that gap.
    """
    clock = FakeClock()
    printer = _printer([{}], monotonic=clock.now)
    assert printer.snapshot()["status"] == "OFFLINE"
    assert printer._session.published == []

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["nozzle_temper"] is None
    assert printer._session.published == []


def test_a_quiet_socket_stays_offline_when_nothing_was_asked():
    """An empty first dump stays OFFLINE. A publish hook that would have
    answered a command must not run, because snapshot does not send one.
    """
    clock = FakeClock()
    printer = _printer([{}], monotonic=clock.now)
    printer.snapshot()

    def resume(_payload):
        printer._session.push({
            "print": {"gcode_state": "IDLE", "nozzle_temper": 31.0},
        })

    printer._session.on_publish = resume
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["nozzle_temper"] is None
    assert printer._session.published == []


def test_a_socket_that_never_comes_up_stays_offline():
    """A connect that never succeeds is OFFLINE immediately and still OFFLINE
    after the staleness window. Snapshot does not try to repair it.
    """
    clock = FakeClock()
    printer = _printer([{}], monotonic=clock.now, connected=False)
    assert printer.snapshot()["status"] == "OFFLINE"

    clock.advance(1)
    assert printer.snapshot()["status"] == "OFFLINE"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    before = list(printer._session.published)
    assert printer.snapshot()["status"] == "OFFLINE"
    assert printer._session.published == before


def test_a_dropped_mqtt_link_reports_offline_without_waiting_out_the_window():
    """The authoritative signal. In LAN-only mode the printer *is* the MQTT broker, so
    paho's keepalive is a liveness check on the machine itself: when it says the link is
    down there is nothing left to wait for — even though `mqtt_dump()` still answers, in
    full, with a printer that looks like it is printing.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    printer._session.connected = False                      # it drops off the LAN
    clock.advance(1)                                       # ...well inside the window
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["progress_percent"] is None


def test_the_staleness_window_is_configurable():
    """It is derived from the poll interval (Config.stale_after_seconds), because "three
    polls of silence" only means something relative to how often we poll."""
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now, stale_after_seconds=600)
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)        # stale under the default...
    assert printer.snapshot()["status"] == "PRINTING"      # ...but not under this one

    clock.advance(601)
    assert printer.snapshot()["status"] == "OFFLINE"


def test_a_parsing_bug_is_reported_as_a_bridge_bug_not_as_an_unreachable_printer(
        monkeypatch, caplog):
    """A blanket `except Exception` around the parse conflates "the printer is gone" with
    "my own code raised". Both still have to report OFFLINE — `snapshot()` must never
    raise, or one bad printer takes down the whole fleet's report — but a bridge bug that
    silently deletes a *live* printer from the UI has to be findable. So: OFFLINE + ERROR
    + a traceback means a bridge bug; OFFLINE + WARNING means an absent printer.
    """
    def boom(_payload):
        raise KeyError("a parsing bug")

    monkeypatch.setattr("bridge.printer.parse_ams", boom)
    printer = _printer([_PRINTING])

    with caplog.at_level(logging.DEBUG, logger="bridge.printer"):
        assert printer.snapshot()["status"] == "OFFLINE"   # never raises

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a bug in the bridge's own parsing must not be logged as a shrug"
    assert errors[0].exc_info is not None, "an ERROR without a traceback is not findable"


def test_an_absent_printer_is_a_warning_not_a_bridge_bug(caplog):
    """The other half of that distinction: a printer that goes away is an ordinary, WARNING
    fact of shop life. It must not cry ERROR, or the ERROR above stops meaning anything."""
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    printer.snapshot()
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)

    with caplog.at_level(logging.DEBUG, logger="bridge.printer"):
        assert printer.snapshot()["status"] == "OFFLINE"

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


# --------------------------------------------------------------------- design guard

def test_the_pure_logic_imports_without_the_vendor_library():
    """Parsing and status stay importable with no vendor MQTT stack in `sys.modules`."""
    assert "bambulabs_api" not in sys.modules


def test_live_report_fields_and_offline_nulls():
    door = 1 << 23
    payload = {"print": {
        "gcode_state": "PAUSE",
        "stg_cur": 6,
        "spd_lvl": 2,
        "cooling_fan_speed": 15,
        "big_fan1_speed": 15,
        "big_fan2_speed": 0,
        "heatbreak_fan_speed": 15,
        "stat": door,
        "sdcard": True,
        "wifi_signal": -90,
        "home_flag": 1 << 11,
        "ipcam": {"ipcam_record": "enable", "timelapse": "disable"},
        "lights_report": [{"node": "chamber_light", "mode": "on"}],
        "device": {"airduct": {"modeId": 1}},
        "ams": {
            "tray_now": "1",
            "tray_tar": "5",
            "tray_pre": "0",
            "ams_status": 8,
            "ams": [{
                "id": "128",
                "dry_time": 4,
                "dry_status": 1,
                "dry_sf_reason": "0",
                "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}],
            }],
            "vt_tray": {"id": "254", "tray_type": "PETG"},
        },
    }, "info": {"command": "get_version", "module": [
        {"name": "ota", "sw_ver": "01.08.02.00"},
        {"name": "ams/0", "sw_ver": "00.00.06.49"},
    ]}}
    snapshot = _printer([payload]).snapshot()
    assert snapshot["spd_lvl"] == 2
    assert snapshot["cooling_fan_percent"] == 100
    assert snapshot["big_fan1_percent"] == 100
    assert snapshot["heatbreak_fan_percent"] == 100
    assert snapshot["door_open"] is True
    assert snapshot["sdcard"] is True
    assert snapshot["chamber_light"] is True
    assert snapshot["wifi_signal"] == -90
    assert snapshot["wifi_wired"] is True
    assert snapshot["store_to_sdcard"] is True
    assert snapshot["ipcam_record"] == "enable"
    assert snapshot["lights_report"][0]["node"] == "chamber_light"
    assert snapshot["airduct"] == {"modeId": 1}
    assert snapshot["tray_now"] == 1
    assert snapshot["tray_tar"] == 5
    assert snapshot["tray_pre"] == 0
    assert snapshot["ams_status"] == 8
    assert snapshot["dry_time"] == 4
    assert snapshot["dry_status"] == 1
    assert snapshot["dry_sf_reason"] == "0"
    assert snapshot["drying_unit"] == "128"
    assert snapshot["stage"] == 6
    assert "runout" in snapshot["stage_name"]
    assert snapshot["firmware_version"] == "01.08.02.00"
    assert snapshot["unit_versions"]["ams/0"] == "00.00.06.49"
    assert snapshot["external_spool"]["tray_type"] == "PETG"
    assert snapshot["slots"] == [{"slot_number": 17, "color_hex": "FF6A13FF", "filament_type": "PLA"}]
    assert all(slot.get("filament_type") != "PETG" for slot in snapshot["slots"])
    assert "job_id" not in snapshot
    assert "subtask_id" not in snapshot

    printer = _printer([payload])
    live = printer.snapshot()
    assert live["firmware_version"] == "01.08.02.00"
    printer._session.connected = False
    offline = printer.snapshot()
    assert offline["connection"] == "offline"
    for key in (
        "spd_lvl", "cooling_fan_percent", "door_open", "sdcard", "chamber_light",
        "wifi_signal", "store_to_sdcard", "ipcam_record", "lights_report", "airduct",
        "tray_now",
        "dry_time", "dry_status", "dry_sf_reason", "drying_unit", "firmware_version",
        "unit_versions", "external_spool", "stage", "stage_name",
    ):
        assert offline[key] is None, key
    assert offline["slots"] is None


@pytest.mark.parametrize("ipcam, expected", [
    ({"ipcam_record": "enable"}, "enable"),
    ({"ipcam_record": "disable"}, "disable"),
    ({"ipcam_record": "on"}, None),
    ({"ipcam_record": True}, None),
    ({"timelapse": "enable"}, None),
    ("not-a-dict", None),
])
def test_ipcam_record_is_the_printer_word_or_null(ipcam, expected):
    """Camera Record, as the printer reports it. Anything else is unknown."""
    snapshot = parse_telemetry({"print": {"gcode_state": "IDLE", "ipcam": ipcam}})
    assert snapshot["ipcam_record"] == expected


def test_a_dump_without_ipcam_reports_ipcam_record_null():
    assert parse_telemetry({"print": {"gcode_state": "IDLE"}})["ipcam_record"] is None
    assert parse_telemetry(None)["ipcam_record"] is None


def test_stage_sentinels_stay_null_and_zero_stays_zero():
    assert parse_telemetry({"print": {"stg_cur": -1}})["stage_name"] is None
    assert parse_telemetry({"print": {"stg_cur": 255}})["stage"] is None
    assert parse_telemetry({"print": {"stg_cur": 255}})["stage_name"] is None
    assert parse_telemetry({"print": {"stg_cur": 0}})["stage"] == 0


def test_vir_slot_is_the_external_spool_and_not_a_slot_row():
    payload = {"print": {
        "gcode_state": "IDLE",
        "ams": {
            "ams": [],
            "vir_slot": {"id": "255", "tray_type": "ABS"},
            "vt_tray": {"id": "254", "tray_type": "PLA"},
        },
    }}
    snapshot = _printer([payload]).snapshot()
    assert snapshot["external_spool"]["id"] == "255"
    assert snapshot["slots"] == []
