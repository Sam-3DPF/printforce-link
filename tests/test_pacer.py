"""Change-driven reports (plan U10) and LAN upkeep off the report loop (plan U9)."""
import threading
import time

import pytest

from bridge.app import _Upkeep
from bridge.pacer import ReportPacer
from tests.test_telemetry import _printer


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# --- the pacer ----------------------------------------------------------------

def test_with_no_change_the_loop_waits_the_full_interval():
    pacer = ReportPacer(0.2, min_gap=0.0, settle=0.0)
    started = time.monotonic()
    assert pacer.wait(started) == "interval"
    assert time.monotonic() - started >= 0.19


def test_a_change_starts_the_next_pass_early():
    pacer = ReportPacer(5.0, min_gap=0.0, settle=0.0)
    started = time.monotonic()
    threading.Timer(0.05, pacer.poke).start()
    assert pacer.wait(started) == "change"
    assert time.monotonic() - started < 1.0


def test_a_change_during_the_pass_is_not_lost():
    pacer = ReportPacer(5.0, min_gap=0.0, settle=0.0)
    started = time.monotonic()
    pacer.poke()   # arrived while the pass was still posting
    assert pacer.wait(started) == "change"
    # Cleared for the pass that starts now.
    assert pacer.wait(time.monotonic() - 4.99) == "interval"


def test_passes_stay_at_least_min_gap_apart():
    clock = _Clock()
    slept = []
    pacer = ReportPacer(15.0, min_gap=1.0, settle=0.25, monotonic=clock, sleep=slept.append)
    pacer.poke()
    # The pass began 0.1 s ago; a poke must not start the next one before 1 s.
    assert pacer.wait(clock.t - 0.1) == "change"
    assert slept == [pytest.approx(0.9)]


def test_a_poke_waits_briefly_so_one_burst_is_one_report():
    clock = _Clock()
    slept = []
    pacer = ReportPacer(15.0, min_gap=1.0, settle=0.25, monotonic=clock, sleep=slept.append)
    pacer.poke()
    assert pacer.wait(clock.t - 10.0) == "change"
    assert slept == [pytest.approx(0.25)]


# --- the printer pokes only on a card-visible change ---------------------------

def test_a_printer_pokes_on_state_changes_not_on_temperatures():
    printer = _printer([])
    pokes = []
    printer.set_change_listener(lambda: pokes.append(1))

    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "nozzle_temper": 25}})
    assert len(pokes) == 1   # first report: the printer came live

    printer._on_mqtt_report({"print": {"nozzle_temper": 180, "mc_percent": 0}})
    printer._on_mqtt_report({"print": {"nozzle_temper": 200, "bed_temper": 60}})
    assert len(pokes) == 1   # heating only: no early report

    printer._on_mqtt_report({"print": {"gcode_state": "RUNNING", "stg_cur": 2}})
    assert len(pokes) == 2
    printer._on_mqtt_report({"print": {"stg_cur": 0, "mc_percent": 5}})
    assert len(pokes) == 3   # stage moved
    printer._on_mqtt_report({"print": {"mc_percent": 6}})
    assert len(pokes) == 3

    printer._on_mqtt_report({"print": {"hms": [{"attr": 0x07007000, "code": 0x00020008}]}})
    assert len(pokes) == 4   # alarm appeared
    printer._on_mqtt_report({"print": {"gcode_state": "PAUSE"}})
    assert len(pokes) == 5
    printer._on_mqtt_report({"print": {"hms": []}})
    assert len(pokes) == 6   # alarm gone


def test_a_listener_that_raises_does_not_break_ingest():
    printer = _printer([])

    def boom():
        raise RuntimeError("listener bug")

    printer.set_change_listener(boom)
    printer._on_mqtt_report({"print": {"gcode_state": "IDLE"}})
    assert printer.snapshot()["gcode_state"] == "IDLE"


# --- upkeep ---------------------------------------------------------------------

def test_one_failing_upkeep_step_does_not_skip_the_others():
    ran = []

    def broken():
        ran.append("broken")
        raise OSError("SSDP socket")

    upkeep = _Upkeep(15, [("scan", broken), ("reconnect", lambda: ran.append("reconnect"))])
    upkeep.run_once()
    assert ran == ["broken", "reconnect"]


def test_an_add_printer_click_starts_the_scan_without_waiting_a_tick():
    seen = []
    upkeep = None

    def scan():
        seen.append(upkeep.scan_requested)

    upkeep = _Upkeep(30, [("scan", scan)])
    upkeep.start()
    deadline = time.monotonic() + 1.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [False]
    upkeep.set_scan_requested(True)
    deadline = time.monotonic() + 1.0
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen[-1] is True
