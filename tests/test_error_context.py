"""PrintErrorWatch on its own, with a fake clock (plan U7, R13).

The printer-level tests in test_printer_log.py drive it through reports.
These pin the ranking, the windows and the edge rules directly.
"""
import pytest

from bridge.bambu.commands import gcode_state_of, normalize_gcode_state
from bridge.bambu.error_context import WINDOW_SECONDS, PrintErrorWatch


def _watch(state="PREPARE", now=0.0):
    watch = PrintErrorWatch()
    watch.note_report(now)
    watch.observe(state, None, now)
    return watch


def test_an_absent_mapped_tray_is_the_suspect_when_nothing_else_happened():
    watch = _watch()
    watch.note_start([0, 2], 0.0)
    context = watch.context(5.0, tray_exist_bits="3")
    assert context["trays"] == [{"tray": 0, "present": True}, {"tray": 2, "present": False}]
    assert context["suspect"] == "mapped tray 2 absent"
    assert "origin" not in context


def test_an_unexpected_start_is_the_suspect_and_keeps_its_own_origin():
    watch = _watch()
    watch.note_start([0, 2], 0.0)
    events = [{"kind": "unexpected_start", "t": 25.0, "origin": "link_earlier"}]
    context = watch.context(30.0, session_events=events, tray_exist_bits="3")
    assert context["suspect"] == "unexpected_start 5s before"
    assert context["session_events"] == [
        {"kind": "unexpected_start", "age": 5.0, "origin": "link_earlier"},
    ]


def test_a_reset_outranks_a_later_stale():
    watch = _watch()
    events = [{"kind": "reset", "t": 10.0}, {"kind": "stale", "t": 20.0}]
    assert watch.context(30.0, session_events=events)["suspect"] == "reset 20s before"


@pytest.mark.parametrize("kind", ["stale", "redial", "disconnect", "reset_held"])
def test_any_recorded_session_event_is_named_rather_than_none(kind):
    watch = _watch()
    events = [{"kind": kind, "t": 10.0, "reason": "PREPARE"}]
    context = watch.context(30.0, session_events=events, tray_exist_bits="0")
    assert context["suspect"] == f"{kind} 20s before"


def test_a_session_event_outranks_a_long_gap_and_an_absent_tray():
    watch = _watch(now=0.0)
    watch.note_start([2], 0.0)
    watch.note_report(60.0)
    events = [{"kind": "disconnect", "t": 55.0}]
    context = watch.context(60.0, session_events=events, tray_exist_bits="3")
    assert context["suspect"] == "disconnect 5s before"


def test_nothing_recorded_names_no_preceding_session_event():
    watch = _watch()
    watch.note_start([0], 0.0)
    assert watch.context(10.0, tray_exist_bits="1")["suspect"] == "no preceding session event"


@pytest.mark.parametrize("silence, suspect", [
    (44.0, "no preceding session event"),
    (46.0, "46s status gap during PREPARE"),
])
def test_only_a_gap_over_45s_while_preparing_is_suspect(silence, suspect):
    watch = _watch(now=100.0)
    watch.note_report(100.0 + silence)
    assert watch.context(100.0 + silence)["suspect"] == suspect


def test_a_long_gap_outside_prepare_or_slicing_is_not_suspect():
    watch = _watch(state="RUNNING", now=100.0)
    watch.note_report(170.0)
    context = watch.context(170.0)
    assert context["longest_gap"] == {"seconds": 70.0, "state": "RUNNING", "ago": 0.0}
    assert context["suspect"] == "no preceding session event"


def test_gaps_older_than_the_window_are_dropped():
    watch = _watch(now=0.0)
    watch.note_report(60.0)  # a 60s gap in PREPARE, ending at 60
    assert watch.context(60.0 + WINDOW_SECONDS)["suspect"] == "60s status gap during PREPARE"

    later = 60.0 + WINDOW_SECONDS + 1
    context = watch.context(later)
    assert context["longest_gap"] is None
    assert context["suspect"] == "no preceding session event"


def test_a_change_from_one_code_to_another_is_an_edge():
    watch = PrintErrorWatch()
    # The first code seen is a baseline, not an edge.
    assert watch.observe("FAILED", "83902467", 0.0) is False
    assert watch.observe("FAILED", "83902467", 1.0) is False
    assert watch.observe("FAILED", "83886080", 2.0) is True
    assert watch.observe("IDLE", None, 3.0) is False
    assert watch.observe("FAILED", "83886080", 4.0) is True


def test_note_start_starts_the_state_timeline_over():
    watch = PrintErrorWatch()
    watch.observe("IDLE", None, 0.0)
    watch.observe("PREPARE", None, 10.0)
    watch.observe("failed", None, 20.0)
    watch.note_start([0], 30.0)
    watch.observe("PREPARE", None, 31.0)
    context = watch.context(40.0)
    assert context["states"] == [["FAILED", 1.0], ["PREPARE", 9.0]]
    assert context["since_start"] == 10.0
    assert context["mapping"] == [0]


@pytest.mark.parametrize("value", [" prepare ", "RUNNING", "", None, 3, "idle"])
def test_normalize_gcode_state_matches_gcode_state_of(value):
    assert normalize_gcode_state(value) == gcode_state_of({"print": {"gcode_state": value}})
