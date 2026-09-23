"""Print lifecycle edges: one start and one terminal per print, with origin.

A terminal state that is already standing when this session's first report
arrives is not an edge. Completion of a batch Link submitted follows those
edges; a batch with no submission id still follows printer status, because
today's sends do not carry an id.
"""
import json
import logging

from bridge.bambu.state import PrinterState
from bridge.router import Dispatcher, Router
from tests.replay import load_fixture, replay_into_state
from tests.test_report_contract import _Session, _printer as _contract_printer
from tests.test_router import _FakeDpf, _FakeFleet, _snap
from tests.test_telemetry import FakeClock

_AT = 1_700_000_000.0
_REPLAY = "tests/fixtures/replay/p1s-synthetic.json"


def _state(bambu_id="P1"):
    return PrinterState(bambu_id, monotonic=lambda: 0.0, wall_clock=lambda: _AT)


def _doc(gcode_state, **fields):
    body = {"gcode_state": gcode_state}
    body.update(fields)
    return {"print": body}


def _types(events):
    return [event["type"] for event in events]


def _event(kind, submission_id, origin="link"):
    return {
        "id": f"evt-{kind}-{submission_id}",
        "type": kind,
        "submission_id": submission_id,
        "origin": origin,
        "gcode_file": "plate.gcode",
        "subtask_name": "job",
        "at": "2023-11-14T22:13:20Z",
        "observed": True,
    }


def test_idle_prepare_running_finish_emits_one_start_and_one_finish():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("PREPARE", gcode_file="plate.gcode", subtask_name="job"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode", subtask_name="job"))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode", subtask_name="job"))

    events = state.pending_events()
    assert _types(events) == ["print_started", "print_finished"]
    assert events[0]["id"] != events[1]["id"]
    for event in events:
        assert event["origin"] == "external"
        assert event["submission_id"] is None
        assert event["gcode_file"] == "plate.gcode"
        assert event["subtask_name"] == "job"
        assert event["observed"] is True
        assert event["at"].endswith("Z")
        assert "subtask_id" not in event
        assert "task_id" not in event
        assert "project_id" not in event


def test_first_push_of_finish_or_failed_emits_nothing():
    """The plate was already done when Link connected. That is not an edge."""
    finished = _state()
    finished.ingest(_doc("FINISH", gcode_file="plate.gcode", subtask_name="job"))
    assert finished.pending_events() == []

    failed = _state()
    failed.ingest(_doc("FAILED", gcode_file="plate.gcode", subtask_name="job"))
    assert failed.pending_events() == []


def test_running_then_idle_cancels_and_prepare_then_failed_fails():
    cancel = _state()
    cancel.ingest(_doc("RUNNING", gcode_file="plate.gcode", subtask_name="job"))
    cancel.ingest(_doc("IDLE"))
    assert _types(cancel.pending_events()) == ["print_cancelled"]

    failed = _state()
    failed.ingest(_doc("PREPARE", gcode_file="plate.gcode", subtask_name="job"))
    failed.ingest(_doc("FAILED", gcode_file="plate.gcode", subtask_name="job"))
    assert _types(failed.pending_events()) == ["print_failed"]

    slicing = _state()
    slicing.ingest(_doc("SLICING", gcode_file="plate.gcode", subtask_name="job"))
    slicing.ingest(_doc("FAILED", gcode_file="plate.gcode", subtask_name="job"))
    assert _types(slicing.pending_events()) == ["print_failed"]


def test_restart_mid_running_finishes_once_and_does_not_start_again():
    """The first RUNNING after a new session is not a new print. The FINISH
    that follows a RUNNING we saw in this session is the one finish. The
    persisted submission id is what keeps the finish owned by Link."""
    state = _state()
    state.register_submission("99")
    state.new_session()
    state.ingest(_doc(
        "RUNNING", subtask_id=99, gcode_file="plate.gcode", subtask_name="job",
    ))
    assert state.pending_events() == []
    assert state.view()["print_origin"] == "link"

    state.ingest(_doc(
        "FINISH", subtask_id=99, gcode_file="plate.gcode", subtask_name="job",
    ))
    events = state.pending_events()
    assert _types(events) == ["print_finished"]
    assert events[0]["origin"] == "link"
    assert events[0]["submission_id"] == "99"
    assert events[0]["gcode_file"] == "plate.gcode"
    assert events[0]["subtask_name"] == "job"


def test_finish_resent_after_a_new_session_does_not_emit_again():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode", subtask_name="job"))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode", subtask_name="job"))
    first = state.pending_events()
    assert _types(first) == ["print_started", "print_finished"]

    state.new_session()
    state.ingest(_doc("FINISH", gcode_file="plate.gcode", subtask_name="job"))
    again = state.pending_events()
    assert [event["id"] for event in again] == [event["id"] for event in first]
    assert _types(again) == ["print_started", "print_finished"]


def test_unregistered_subtask_is_external_and_does_not_complete_a_batch(tmp_path):
    state = _state()
    state.register_submission("mine")
    state.ingest(_doc("IDLE"))
    state.ingest(_doc(
        "RUNNING", subtask_id="555", gcode_file="other.gcode", subtask_name="other",
    ))
    state.ingest(_doc(
        "FINISH", subtask_id="555", gcode_file="other.gcode", subtask_name="other",
    ))
    events = state.pending_events()
    assert _types(events) == ["print_started", "print_finished"]
    assert all(event["origin"] == "external" for event in events)
    assert all(event["submission_id"] is None for event in events)

    path = str(tmp_path / "queue.json")
    router = Router(path)
    router.record_assignment("P1", "B1", submission_id="mine")
    dpf = _FakeDpf({})
    snap = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    snap["events"] = events
    Dispatcher(router, _FakeFleet(), dpf).drain([snap])
    assert dpf.completed == []
    assert dpf.failed == []
    assert "P1" in router.assignments_snapshot()


def test_a_different_submission_finish_does_not_close_this_assignment(tmp_path):
    path = str(tmp_path / "queue.json")
    router = Router(path)
    router.record_assignment("P1", "B1", submission_id="mine")
    dpf = _FakeDpf({})
    snap = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    snap["events"] = [_event("print_finished", "someone-else")]
    Dispatcher(router, _FakeFleet(), dpf).drain([snap])
    assert dpf.completed == []
    assert "P1" in router.assignments_snapshot()


def test_file_change_while_running_starts_a_new_print_without_finishing_the_old():
    """No non-zero subtask or task id, so the file is the print identity.
    A gcode_file change while RUNNING is a new start. The previous print gets
    no terminal: FINISH, FAILED, and IDLE-from-RUNNING are the only terminal
    edges, and a file change is none of them. A stable subtask_id keeps one
    print even if the file string changes."""
    changed = _state()
    changed.ingest(_doc("IDLE"))
    changed.ingest(_doc("RUNNING", gcode_file="a.gcode", subtask_name="a"))
    changed.ingest(_doc("RUNNING", gcode_file="b.gcode", subtask_name="b"))
    events = changed.pending_events()
    assert _types(events) == ["print_started", "print_started"]
    assert [event["gcode_file"] for event in events] == ["a.gcode", "b.gcode"]
    assert all(event["type"] == "print_started" for event in events)

    same_id = _state()
    same_id.register_submission("7")
    same_id.ingest(_doc("IDLE"))
    same_id.ingest(_doc("RUNNING", subtask_id="7", gcode_file="a.gcode", subtask_name="a"))
    same_id.ingest(_doc("RUNNING", subtask_id="7", gcode_file="b.gcode", subtask_name="b"))
    assert _types(same_id.pending_events()) == ["print_started"]
    assert same_id.pending_events()[0]["submission_id"] == "7"


def test_events_wait_for_the_report_post_and_keep_their_id():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode", subtask_name="job"))
    first = state.pending_events()
    assert _types(first) == ["print_started"]
    # A failed POST does not ack. The next report sends the same id.
    assert state.pending_events()[0]["id"] == first[0]["id"]

    state.ack_events([first[0]["id"]])
    assert state.pending_events() == []
    state.ack_events([first[0]["id"]])
    assert state.pending_events() == []


def test_lifecycle_queue_drops_the_oldest_event(caplog):
    caplog.set_level(logging.WARNING)
    state = _state()
    # One IDLE, then a new file on every RUNNING. Each file change is a start
    # and not a cancel, so the queue fills with starts.
    state.ingest(_doc("IDLE"))
    for i in range(51):
        state.ingest(_doc("RUNNING", gcode_file=f"p{i}.gcode", subtask_name=f"p{i}"))
    events = state.pending_events()
    assert len(events) == 50
    assert events[0]["gcode_file"] == "p1.gcode"
    assert events[-1]["gcode_file"] == "p50.gcode"
    assert any("lifecycle" in record.message for record in caplog.records)


def test_replay_of_the_synthetic_p1s_capture_emits_no_events():
    """The fixture's first inbound report is already RUNNING. That is not a start,
    and the capture never leaves RUNNING, so there is no terminal edge either."""
    state = _state(bambu_id="01P00C4A0000001")
    events = replay_into_state(state, load_fixture(_REPLAY))
    assert events == []
    payload = state.view()["payload"]["print"]
    assert payload["gcode_state"] == "RUNNING"
    assert payload["subtask_name"] == "benchy"


def test_snapshot_queues_events_until_ack_and_a_new_connack_does_not_repeat_them():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, session_started_at="2026-09-22T12:00:00Z")
    printer = _contract_printer(clock, session)
    printer.register_submission("99")
    printer._on_mqtt_report(_doc("IDLE"))
    printer._on_mqtt_report(_doc(
        "RUNNING", subtask_id="99", gcode_file="plate.gcode", subtask_name="job",
    ))
    printer._on_mqtt_report(_doc(
        "FINISH", subtask_id="99", gcode_file="plate.gcode", subtask_name="job",
    ))
    first = printer.snapshot()
    assert _types(first["events"]) == ["print_started", "print_finished"]
    assert first["print_origin"] == "link"
    assert all(event["submission_id"] == "99" for event in first["events"])

    session.connack_at = 2000.0
    printer._on_mqtt_report(_doc(
        "FINISH", subtask_id="99", gcode_file="plate.gcode", subtask_name="job",
    ))
    resent = printer.snapshot()
    assert [event["id"] for event in resent["events"]] == [
        event["id"] for event in first["events"]
    ]

    printer.ack_events([event["id"] for event in first["events"]])
    assert printer.snapshot()["events"] == []


def test_report_ack_drops_sent_ids_only_after_a_non_empty_response():
    from bridge.app import _ack_reported_events

    class _Fleet:
        def __init__(self):
            self.acks = None

        def ack_events(self, acks):
            self.acks = acks

    fleet = _Fleet()
    reports = [{
        "bambu_id": "P1",
        "events": [{"id": "a"}, {"id": "b"}, "nope"],
    }, {
        "bambu_id": "P2",
        "events": [],
    }]
    _ack_reported_events(fleet, {}, reports)
    assert fleet.acks is None
    _ack_reported_events(fleet, {"ok": True}, reports)
    assert fleet.acks == {"P1": ["a", "b"]}


def test_persisted_submission_is_registered_before_reports():
    from bridge.app import _register_persisted_submissions

    class _Printer:
        def __init__(self):
            self.ids = []

        def register_submission(self, submission_id):
            self.ids.append(submission_id)

    class _Fleet:
        def __init__(self):
            self._printer = _Printer()

        def register_submission(self, bambu_id, submission_id):
            assert bambu_id == "P1"
            self._printer.register_submission(submission_id)

    fleet = _Fleet()

    class _Router:
        def assignments_snapshot(self):
            return {
                "P1": {"submission_id": "mine", "observed_running": True},
                "P2": {"submission_id": None},
            }

    _register_persisted_submissions(fleet, _Router())
    assert fleet._printer.ids == ["mine"]


def test_submission_assignment_completes_from_events_not_status(tmp_path):
    path = str(tmp_path / "queue.json")
    router = Router(path)
    router.record_assignment("P1", "B1", plate_number=2, submission_id="mine")
    dpf = _FakeDpf({})
    dispatcher = Dispatcher(router, _FakeFleet(), dpf)

    standing = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    standing["events"] = []
    dispatcher.drain([standing])
    assert dpf.completed == []
    assert router.assignments_snapshot()["P1"]["observed_running"] is False

    started = _snap("P1", "PRINTING", [(1, "FF6A13FF")])
    started["events"] = [_event("print_started", "mine")]
    dispatcher.drain([started])
    assert router.assignments_snapshot()["P1"]["observed_running"] is True
    assert dpf.completed == []

    reloaded = Router(path).assignments_snapshot()["P1"]
    assert reloaded["submission_id"] == "mine"
    assert reloaded["observed_running"] is True

    finished = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    finished["events"] = [_event("print_finished", "mine")]
    dispatcher.drain([finished])
    assert dpf.completed == [("B1", 2)]
    assert dpf.failed == []
    assert "P1" not in router.assignments_snapshot()

    dispatcher.drain([finished])
    assert dpf.completed == [("B1", 2)]


def test_restarted_assignment_finishes_from_the_persisted_submission(tmp_path):
    path = str(tmp_path / "queue.json")
    with open(path + ".assignments", "w", encoding="utf-8") as handle:
        json.dump({
            "P1": {
                "batch_id": "B1",
                "plate_number": None,
                "terminal": None,
                "started_at": 10.0,
                "observed_active": True,
                "submission_id": "mine",
                "observed_running": True,
            },
        }, handle)
    router = Router(path)
    dpf = _FakeDpf({})
    dispatcher = Dispatcher(router, _FakeFleet(), dpf)

    # A standing FINISH with no edge must not complete, even though the
    # assignment was already observed running before the restart.
    standing = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    standing["events"] = []
    dispatcher.drain([standing])
    assert dpf.completed == []
    assert router.assignments_snapshot()["P1"]["observed_running"] is True

    finished = _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])
    finished["events"] = [_event("print_finished", "mine")]
    dispatcher.drain([finished])
    assert dpf.completed == [("B1", None)]
    assert "P1" not in router.assignments_snapshot()


def test_submission_failure_and_cancel_follow_the_matching_event(tmp_path):
    path = str(tmp_path / "queue.json")
    router = Router(path)
    router.record_assignment("P1", "B1", submission_id="mine")
    dpf = _FakeDpf({})
    failed = _snap("P1", "ERROR", [(1, "FF6A13FF")])
    failed["events"] = [_event("print_failed", "mine")]
    Dispatcher(router, _FakeFleet(), dpf).drain([failed])
    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []

    router.record_assignment("P2", "B2", submission_id="other")
    cancelled = _snap("P2", "IDLE", [])
    cancelled["events"] = [_event("print_cancelled", "other")]
    Dispatcher(router, _FakeFleet(), dpf).drain([cancelled])
    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []
    assert "P2" not in router.assignments_snapshot()


def test_loaded_assignment_copies_observed_active_into_observed_running(tmp_path):
    path = str(tmp_path / "queue.json")
    with open(path + ".assignments", "w", encoding="utf-8") as handle:
        json.dump({
            "P1": {
                "batch_id": "B1",
                "plate_number": None,
                "terminal": None,
                "started_at": 10.0,
                "observed_active": True,
            },
            "P2": {
                "batch_id": "B2",
                "plate_number": 1,
                "terminal": None,
            },
        }, handle)
    assignments = Router(path).assignments_snapshot()
    assert assignments["P1"]["observed_running"] is True
    assert assignments["P1"]["submission_id"] is None
    assert assignments["P1"]["observed_active"] is True
    assert assignments["P2"]["observed_active"] is True
    assert assignments["P2"]["observed_running"] is True
    assert assignments["P2"]["submission_id"] is None


def test_task_id_matches_a_registered_submission_when_subtask_id_is_empty():
    state = _state()
    state.register_submission("44")
    state.ingest(_doc("IDLE"))
    state.ingest(_doc(
        "RUNNING", subtask_id=0, task_id=44, gcode_file="plate.gcode", subtask_name="job",
    ))
    events = state.pending_events()
    assert _types(events) == ["print_started"]
    assert events[0]["origin"] == "link"
    assert events[0]["submission_id"] == "44"
    assert state.view()["print_origin"] == "link"


def test_the_same_file_printed_twice_is_two_prints():
    state = _state()
    for _ in range(2):
        state.ingest(_doc("IDLE"))
        state.ingest(_doc("RUNNING", gcode_file="plate.gcode", subtask_name="job"))
        state.ingest(_doc("FINISH", gcode_file="plate.gcode", subtask_name="job"))
    assert _types(state.pending_events()) == [
        "print_started", "print_finished", "print_started", "print_finished",
    ]


def test_pause_and_resume_is_one_print():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode"))
    state.ingest(_doc("PAUSE", gcode_file="plate.gcode"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode"))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode"))
    assert _types(state.pending_events()) == ["print_started", "print_finished"]


def test_a_finish_repeated_in_the_same_session_emits_once():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode"))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode"))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode", bed_temper=50.0))
    state.ingest(_doc("FINISH", gcode_file="plate.gcode", bed_temper=40.0))
    assert _types(state.pending_events()) == ["print_started", "print_finished"]


def test_a_user_cancel_that_lands_as_failed_is_a_cancel():
    state = _state()
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate.gcode"))
    state.ingest(_doc("FAILED", gcode_file="plate.gcode", print_error=50348044))
    assert _types(state.pending_events()) == ["print_started", "print_cancelled"]
