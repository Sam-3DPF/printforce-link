"""Telemetry, status mapping, the delta merge, and the observed print duration.

Every function under test here is pure or clock-injected, so none of this needs
`bambulabs_api` installed — the same property `test_ams.py` relies on. `BambuPrinter`
only ever touches its client through `mqtt_dump()`, so a fake stands in for it.
"""

import logging
import sys

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


class FakeClient:
    """Stands in for `bambulabs_api.Printer`. Yields one payload per poll and then
    repeats the last one — which is a printer that has stopped changing, and *also*
    exactly what an unplugged printer looks like: `mqtt_dump()` is a dict read on a cache
    the library already holds, so it keeps answering, cheerfully, long after the machine
    is gone. It cannot raise. That is why the liveness tests below exist.

    It returns each payload **raw**, i.e. as the delta it arrived as — the worst case
    the merge exists to absorb — so the bridge's own merge is what these tests
    exercise.

    `mqtt_client_connected()` mirrors the real accessor (`Printer.mqtt_client_connected`
    -> `PrinterMQTTClient.is_connected` -> paho). Set `connected = False` to pull the
    printer off the LAN.
    """

    def __init__(self, payloads, connected=True):
        self._payloads = list(payloads)
        self._last = {}
        self.connected = connected

    def mqtt_dump(self):
        if self._payloads:
            self._last = self._payloads.pop(0)
        return self._last

    def mqtt_client_connected(self):
        return self.connected

    def push(self, payload):
        """The printer sends a new report."""
        self._payloads.append(payload)


def _stopwatch(monotonic=None, wall_clock=None) -> PrintStopwatch:
    return PrintStopwatch(
        _BAMBU_ID,
        monotonic=monotonic or (lambda: 0.0),
        wall_clock=wall_clock or (lambda: 0.0),
    )


def _printer(payloads, monotonic=None, wall_clock=None, connected=True,
             stale_after_seconds=_DEFAULT_STALE_AFTER_SECONDS) -> BambuPrinter:
    cfg = PrinterConfig(bambu_id=_BAMBU_ID, ip="10.0.0.5",
                        access_code="secret", name="P1S-1")
    # The printer's clock defaults to a *frozen* one, so a test that says nothing about
    # time can never accidentally age its own payload into staleness. The tests that care
    # pass a FakeClock and advance it themselves.
    printer = BambuPrinter(cfg, stopwatch=_stopwatch(monotonic, wall_clock),
                           stale_after_seconds=stale_after_seconds,
                           monotonic=monotonic or (lambda: 0.0))
    printer._client = FakeClient(payloads, connected=connected)
    return printer


# --------------------------------------------------------------------------- status
# (`map_status` itself is unit-tested in test_printer_map.py; these cover the
#  snapshot-level behavior built on top of it.)

def test_fresh_bridge_reports_offline_not_idle_even_mid_print():
    """`mqtt_dump()` returns {} until the first MQTT push lands. Every printer —
    including one mid-print — read IDLE during that window before this fix."""
    printer = _printer([{}])
    snapshot = printer.snapshot()
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["slots"] == []
    assert snapshot["nozzle_temper"] is None


def test_unreadable_printer_reports_offline_and_never_raises():
    """The library-raised path only. **This is not how a printer dies** — read the
    liveness section at the bottom of this file before trusting it as OFFLINE coverage.
    `mqtt_dump()` does not raise once a printer has connected (it is a dict read), so a
    printer that was live and then went away never reaches here."""
    class Unreachable:
        def mqtt_dump(self):
            raise OSError("no route to host")

    printer = _printer([])
    printer._client = Unreachable()
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
        "tray_exist_bits": "f",
        "hms_severity": None,
        "hms_code": None,
        "hms_count": 0,
        "print_duration_seconds": None,
        "print_duration_source": None,
    }


def test_remaining_time_is_minutes_converted_to_seconds():
    """bambulabs_api's own docstring says seconds and is wrong; ha-bambulab reads the
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


def test_ams_delta_clears_a_tray_that_became_empty():
    """The merge must be able to express key *removal*. A naive deep merge cannot: a
    tray going from loaded to empty would keep its last-known color forever, telling the
    router the printer holds a color it does not."""
    printer = _printer([
        {"print": {"gcode_state": "IDLE", "ams": {"ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"},
                {"id": "1", "tray_color": "00AE42FF", "tray_type": "PLA"},
            ]}]}}},
        {"print": {"gcode_state": "IDLE", "ams": {"ams": [
            {"id": "0", "tray": [
                {"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"},
                {"id": "1"},                     # the spool was pulled out
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
    """Someone unplugs a printer that is PRINTING at 47%, 220°C. `mqtt_dump()` cannot
    raise, so `except Exception -> OFFLINE` never fires; the cache is populated, so the
    "no payload yet" branch never fires either. Before this, the bridge replayed that
    same stale payload to the cloud every 10 seconds, forever.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)   # then it repeats, i.e. freezes
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)        # the plug comes out
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["progress_percent"] is None            # not a frozen 47%
    assert snapshot["nozzle_temper"] is None               # not a frozen 220°C
    assert snapshot["slots"] == []                         # not a colour it no longer holds


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
    """`mqtt_dump()` returning {} is silence, not news — and silence must not be answered
    with the last thing the printer happened to say."""
    clock = FakeClock()
    printer = _printer([_PRINTING, {}], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()                          # mqtt_dump() -> {}

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["progress_percent"] is None


def test_offline_does_not_flap_back_to_printing_while_the_printer_stays_dead():
    """Going OFFLINE drops `_cached` — but the *freshness baseline* must survive it.

    A dead printer hands back the same dict on every poll. If dropping the cache also
    reset "when did this last change?", the very next poll would merge that same dict into
    an empty cache, read it as new data, and flap the printer back to PRINTING — then
    OFFLINE, then PRINTING, every window, forever. The dispatcher would find an IDLE
    window on a machine that is not there.
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
    """Recovery. The cache was dropped on the way out, so the printer is rebuilt from what
    it says NOW — the pre-outage 47% must not be resurrected alongside it. (The real
    `mqtt_dump()` accumulates and the library re-pushes everything on connect, so a
    reconnected printer's first dump is a full picture.)
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    printer.snapshot()
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    assert printer.snapshot()["status"] == "OFFLINE"

    printer._client.push({"print": {"gcode_state": "IDLE", "nozzle_temper": 24.0}})
    clock.advance(15)
    snapshot = printer.snapshot()

    assert snapshot["status"] == "IDLE"
    assert snapshot["nozzle_temper"] == 24.0
    assert snapshot["progress_percent"] is None            # the stale 47% did not survive


def test_a_dropped_mqtt_link_reports_offline_without_waiting_out_the_window():
    """The authoritative signal. In LAN-only mode the printer *is* the MQTT broker, so
    paho's keepalive is a liveness check on the machine itself: when it says the link is
    down there is nothing left to wait for — even though `mqtt_dump()` still answers, in
    full, with a printer that looks like it is printing.
    """
    clock = FakeClock()
    printer = _printer([_PRINTING], monotonic=clock.now)
    assert printer.snapshot()["status"] == "PRINTING"

    printer._client.connected = False                      # it drops off the LAN
    clock.advance(1)                                       # ...well inside the window
    snapshot = printer.snapshot()

    assert snapshot["status"] == "OFFLINE"
    assert snapshot["progress_percent"] is None


def test_staleness_alone_still_reports_offline_when_the_library_has_no_link_accessor():
    """`_is_connected()` degrades to *unknown* if a future `bambulabs_api` renames or drops
    `mqtt_client_connected()` — it must never degrade to *connected*, which would mark a
    dead printer live. Staleness needs no library support at all, so the printer still goes
    OFFLINE; it just takes the window to get there.
    """
    class NoProbeClient:                                   # only mqtt_dump(), like an older lib
        def mqtt_dump(self):
            return _PRINTING

    clock = FakeClock()
    printer = _printer([], monotonic=clock.now)
    printer._client = NoProbeClient()
    assert printer.snapshot()["status"] == "PRINTING"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    assert printer.snapshot()["status"] == "OFFLINE"


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
    """`bambulabs_api` is imported lazily inside connect() on purpose, so the parsing
    and status logic stays unit-testable (and CI needs no printer). Keep it that way."""
    assert "bambulabs_api" not in sys.modules
