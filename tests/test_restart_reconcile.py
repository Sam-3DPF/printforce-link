"""Restart reconcile closes a stranded assignment once per session.

The first live report that actually carries ``gcode_state`` is the only one
that may decide. CONNACK is too early: the state is still unknown. A close
waits for the same state on two live reports at least 30s apart, and for the
assignment to be older than the startup grace. A disconnected report never
decides and never counts toward that pair.
"""
from bridge.bambu.state import PrinterState
from bridge.printer import _DEFAULT_STALE_AFTER_SECONDS
from bridge.router import (
    ASSIGNMENT_STARTUP_GRACE_SECONDS,
    Dispatcher,
    Router,
)

# Two live reports at least this far apart are a steady terminal state.
_STEADY_SECONDS = 30.0
from tests.test_report_contract import _Session, _printer as _contract_printer
from tests.test_telemetry import FakeClock

_AT = 1_700_000_000.0


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _Dpf:
    def __init__(self):
        self.completed = []
        self.failed = []
        self.fail_acks_remaining = 0

    def report_complete(self, batch_id, plate_number=None):
        self.completed.append((batch_id, plate_number))
        return {"batch_id": batch_id}

    def report_failed(self, batch_id, plate_number=None, reason=None):
        self.failed.append((batch_id, plate_number, reason))
        if self.fail_acks_remaining > 0:
            self.fail_acks_remaining -= 1
            return {}
        return {"batch_id": batch_id}


class _Fleet:
    def __init__(self, state=None):
        self.state = state
        self.emitted = []

    def emit_recovered_event(self, bambu_id, kind, submission_id):
        self.emitted.append((bambu_id, kind, submission_id))
        if self.state is not None:
            self.state.emit_recovered(kind, submission_id)


def _state():
    return PrinterState("P1", monotonic=lambda: 0.0, wall_clock=lambda: _AT)


def _router(tmp_path, *, submission_id="mine", started_at=0.0, plate_number=1,
            name="queue.json", confirmed=True):
    """A persisted assignment. ``confirmed`` is a print Link already saw running,
    which is the case a restart strands."""
    router = Router(str(tmp_path / name))
    router.record_assignment(
        "P1", "B1", plate_number=plate_number, started_at=started_at,
        submission_id=submission_id,
    )
    if confirmed:
        router.mark_assignment_running("P1")
    return router


def _report(gcode_state, *, submission="mine", connection="live", session=1,
            seen=True, events=None, status=None):
    if status is None:
        status = {
            "IDLE": "IDLE",
            "PREPARE": "PRINTING",
            "SLICING": "PRINTING",
            "RUNNING": "PRINTING",
            "PAUSE": "PAUSED",
            "FINISH": "NEEDS_CLEARING",
            "FAILED": "ERROR",
        }.get(gcode_state, "OFFLINE")
    return {
        "bambu_id": "P1",
        "status": status,
        "connection": connection,
        "gcode_state": gcode_state,
        "print_submission_id": submission,
        "session_seq": session,
        "session_gcode_seen": seen,
        "events": list(events or []),
        "slots": [],
    }


def _dispatcher(router, dpf, clock, fleet):
    return Dispatcher(router, fleet, dpf, now_fn=clock, monotonic=clock)


def _doc(gcode_state, **fields):
    body = {"gcode_state": gcode_state}
    body.update(fields)
    return {"print": body}


def test_view_exposes_the_matched_submission_and_the_session_flag():
    """The report may carry the Link id that matched. It may not carry the
    printer's own task id. The session flag flips only when a report in this
    session included ``gcode_state``, and a new CONNACK clears it."""
    state = _state()
    state.register_submission("99")
    assert state.view()["session_seq"] == 0
    assert state.view()["session_gcode_seen"] is False
    assert state.view()["print_submission_id"] is None

    state.ingest({"print": {"nozzle_temper": 20.0}})
    assert state.view()["session_gcode_seen"] is False

    state.ingest(_doc("RUNNING", subtask_id=99, task_id="555", project_id="777"))
    view = state.view()
    assert view["print_submission_id"] == "99"
    assert view["print_origin"] == "link"
    assert view["session_gcode_seen"] is True
    assert "subtask_id" not in view
    assert "task_id" not in view

    state.ingest(_doc("RUNNING", subtask_id="555", task_id="555"))
    assert state.view()["print_submission_id"] is None

    state.new_session()
    assert state.view()["session_seq"] == 1
    assert state.view()["session_gcode_seen"] is False

    state.ingest(_doc("IDLE", task_id="99"))
    assert state.view()["session_gcode_seen"] is True
    assert state.view()["print_submission_id"] == "99"


def test_report_carries_print_submission_id_on_live_and_stale_not_offline():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, session_started_at="2026-09-22T12:00:00Z")
    printer = _contract_printer(clock, session)
    printer.register_submission("99")
    printer._on_mqtt_report({
        "print": {
            "gcode_state": "RUNNING",
            "subtask_id": "99",
            "task_id": "555",
            "project_id": "777",
            "mc_percent": 10,
        },
    })
    live = printer.snapshot()
    assert live["connection"] == "live"
    assert live["print_submission_id"] == "99"
    assert live["session_seq"] == 1
    assert live["session_gcode_seen"] is True
    assert "subtask_id" not in live
    assert "task_id" not in live
    assert "project_id" not in live

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    session.state = "stale"
    stale = printer.snapshot()
    assert stale["connection"] == "stale"
    assert stale["print_submission_id"] == "99"
    assert stale["session_seq"] == 1

    session.connected = False
    session.state = "offline"
    offline = printer.snapshot()
    assert offline["connection"] == "offline"
    assert offline["print_submission_id"] is None
    assert offline["session_seq"] == 1

    clock.advance(5)
    session.connack_at = clock.now()
    session.connected = True
    session.state = "live"
    printer._on_mqtt_report({"print": {"nozzle_temper": 21.0}})
    partial = printer.snapshot()
    assert partial["connection"] == "live"
    assert partial["session_seq"] == 2
    assert partial["session_gcode_seen"] is False


def test_emit_recovered_queues_an_unobserved_link_finish():
    state = _state()
    state.register_submission("mine")
    state.new_session()
    state.ingest(_doc("FINISH", subtask_id="mine", gcode_file="plate.gcode", subtask_name="job"))
    assert state.pending_events() == []

    state.emit_recovered("print_finished", "mine")
    events = state.pending_events()
    assert len(events) == 1
    assert events[0]["type"] == "print_finished"
    assert events[0]["origin"] == "link"
    assert events[0]["submission_id"] == "mine"
    assert events[0]["observed"] is False
    assert events[0]["gcode_file"] == "plate.gcode"
    assert "subtask_id" not in events[0]


def test_running_with_matching_submission_keeps_the_assignment(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    _dispatcher(router, dpf, clock, _Fleet()).drain([
        _report("RUNNING", submission="mine"),
    ])

    assignment = router.assignments_snapshot()["P1"]
    assert assignment["recovered"] is True
    assert assignment["observed_running"] is True
    assert assignment["terminal"] is None
    assert dpf.completed == []
    assert dpf.failed == []
    reloaded = Router(str(tmp_path / "queue.json")).assignments_snapshot()["P1"]
    assert reloaded["recovered"] is True
    assert reloaded["observed_running"] is True


def test_active_matching_states_keep_without_waiting_for_a_second_report(tmp_path):
    clock = Clock(1000.0)
    for index, state in enumerate(("PREPARE", "SLICING", "PAUSE")):
        router = _router(tmp_path, started_at=0.0, name=f"q{index}.json")
        dpf = _Dpf()
        _dispatcher(router, dpf, clock, _Fleet()).drain([
            _report(state, submission="mine"),
        ])
        assignment = router.assignments_snapshot()["P1"]
        assert assignment["recovered"] is True, state
        assert assignment["observed_running"] is True, state
        assert dpf.failed == []


def test_finish_with_matching_id_emits_one_unobserved_finish_then_completes_once(tmp_path):
    """The recovered finish is queued during the pass that decides. The
    completion report reads the snapshot from before that pass, so the batch
    completes on the next drain, once, and a later FINISH does not emit again."""
    clock = Clock(1000.0)
    state = _state()
    state.register_submission("mine")
    fleet = _Fleet(state)
    router = _router(tmp_path, started_at=0.0, plate_number=2)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, fleet)

    dispatcher.drain([_report("FINISH", submission="mine")])
    assert fleet.emitted == []
    assert dpf.completed == []
    assert "P1" in router.assignments_snapshot()

    clock.advance(_STEADY_SECONDS - 1)
    dispatcher.drain([_report("FINISH", submission="mine")])
    assert fleet.emitted == []

    clock.advance(1)
    dispatcher.drain([_report("FINISH", submission="mine")])
    assert fleet.emitted == [("P1", "print_finished", "mine")]
    events = state.pending_events()
    assert len(events) == 1
    assert events[0]["observed"] is False
    assert events[0]["origin"] == "link"
    assert events[0]["type"] == "print_finished"
    assert dpf.completed == []

    clock.advance(_STEADY_SECONDS)
    finished = _report("FINISH", submission="mine", events=state.pending_events())
    dispatcher.drain([finished])
    assert dpf.completed == [("B1", 2)]
    assert dpf.failed == []
    assert fleet.emitted == [("P1", "print_finished", "mine")]
    assert len(state.pending_events()) == 1
    assert "P1" not in router.assignments_snapshot()

    dispatcher.drain([finished])
    assert dpf.completed == [("B1", 2)]
    assert fleet.emitted == [("P1", "print_finished", "mine")]


def test_idle_closes_as_ended_unobserved_only_after_two_reports_30s_apart(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission="mine")])
    assert dpf.failed == []
    assert "P1" in router.assignments_snapshot()

    clock.advance(_STEADY_SECONDS - 1)
    dispatcher.drain([_report("IDLE", submission="mine")])
    assert dpf.failed == []
    assert router.assignments_snapshot()["P1"]["terminal"] is None

    clock.advance(1)
    dispatcher.drain([_report("IDLE", submission="mine")])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert dpf.completed == []
    assert "P1" not in router.assignments_snapshot()

    dispatcher.drain([_report("IDLE", submission="mine")])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]


def test_ended_unobserved_is_retried_until_acked(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dpf.fail_acks_remaining = 1
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("FAILED", submission=None)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("FAILED", submission=None)])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert router.assignments_snapshot()["P1"]["terminal"] == "ended_unobserved"

    dispatcher.drain([_report("FAILED", submission=None)])
    assert dpf.failed == [
        ("B1", 1, "ended_unobserved"),
        ("B1", 1, "ended_unobserved"),
    ]
    assert "P1" not in router.assignments_snapshot()


def test_a_different_submission_closes_the_assignment(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("RUNNING", submission="other")])
    assert dpf.failed == []
    assert router.assignments_snapshot()["P1"].get("recovered") is not True

    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("RUNNING", submission="other")])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert "P1" not in router.assignments_snapshot()


def test_a_young_idle_assignment_is_not_closed_during_startup_grace(tmp_path):
    """A just-sent file still reads IDLE while it downloads. Samples during
    the grace count toward the steady pair, but the close waits until the
    assignment is old enough."""
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=clock.t)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None)])
    assert dpf.failed == []
    assert clock() - 1000.0 < ASSIGNMENT_STARTUP_GRACE_SECONDS
    assert "P1" in router.assignments_snapshot()

    clock.advance(ASSIGNMENT_STARTUP_GRACE_SECONDS - _STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None)])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert "P1" not in router.assignments_snapshot()


def test_offline_makes_no_decision_and_does_not_consume_the_session(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None, connection="offline")])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, connection="stale")])
    assert dpf.failed == []
    assert router.assignments_snapshot()["P1"].get("recovered") is not True

    dispatcher.drain([_report("RUNNING", submission="mine")])
    assignment = router.assignments_snapshot()["P1"]
    assert assignment["recovered"] is True
    assert assignment["terminal"] is None
    assert dpf.failed == []


def test_a_live_report_without_gcode_in_this_session_makes_no_decision(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None, seen=False)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, seen=False)])
    assert dpf.failed == []
    assert "P1" in router.assignments_snapshot()

    dispatcher.drain([_report("RUNNING", submission="mine", seen=True)])
    assert router.assignments_snapshot()["P1"]["recovered"] is True


def test_reconcile_runs_once_per_session_later_idle_is_not_a_restart_close(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("RUNNING", submission="mine")])
    assert router.assignments_snapshot()["P1"]["recovered"] is True

    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None)])
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" in router.assignments_snapshot()
    assert router.assignments_snapshot()["P1"]["terminal"] is None


def test_a_new_session_reconciles_again(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("RUNNING", submission="mine", session=1)])
    assert router.assignments_snapshot()["P1"]["recovered"] is True

    dispatcher.drain([_report("IDLE", submission=None, session=2)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, session=2)])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert "P1" not in router.assignments_snapshot()


def test_stranded_record_closes_only_after_a_steady_terminal_and_never_while_disconnected(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None, connection="offline")])
    clock.advance(_STEADY_SECONDS + 10)
    dispatcher.drain([_report("FAILED", submission=None, connection="offline")])
    assert dpf.failed == []
    assert "P1" in router.assignments_snapshot()

    dispatcher.drain([_report("IDLE", submission=None, connection="live")])
    assert dpf.failed == []

    clock.advance(_STEADY_SECONDS - 1)
    dispatcher.drain([_report("IDLE", submission=None, connection="stale")])
    assert dpf.failed == []

    dispatcher.drain([_report("IDLE", submission=None, connection="live")])
    assert dpf.failed == []

    clock.advance(1)
    dispatcher.drain([_report("IDLE", submission=None, connection="live")])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert "P1" not in router.assignments_snapshot()


def test_a_state_change_resets_the_steady_wait(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None)])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("FAILED", submission=None)])
    assert dpf.failed == []

    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("FAILED", submission=None)])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]


def test_legacy_confirmed_idle_closes_as_ended_unobserved(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, submission_id=None, started_at=0.0)
    router.mark_assignment_active("P1")
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    assert dpf.failed == []
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    assert dpf.failed == [("B1", 1, "ended_unobserved")]
    assert "P1" not in router.assignments_snapshot()


def test_legacy_unconfirmed_idle_stays_on_the_status_path(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, submission_id=None, started_at=0.0, confirmed=False)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    clock.advance(ASSIGNMENT_STARTUP_GRACE_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" in router.assignments_snapshot()
    assert router.assignments_snapshot()["P1"]["terminal"] is None


def test_legacy_running_then_finish_still_completes_from_status(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, submission_id=None, started_at=0.0)
    router.mark_assignment_active("P1")
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("RUNNING", submission=None, status="PRINTING")])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    clock.advance(_STEADY_SECONDS)
    dispatcher.drain([_report("IDLE", submission=None, status="IDLE")])
    assert dpf.failed == []
    assert "P1" in router.assignments_snapshot()

    dispatcher.drain([_report("FINISH", submission=None, status="NEEDS_CLEARING")])
    assert dpf.completed == [("B1", 1)]
    assert dpf.failed == []
    assert "P1" not in router.assignments_snapshot()


def test_legacy_confirmed_finish_completes_instead_of_ending_unobserved(tmp_path):
    clock = Clock(1000.0)
    router = _router(tmp_path, submission_id=None, started_at=0.0)
    router.mark_assignment_active("P1")
    dpf = _Dpf()
    _dispatcher(router, dpf, clock, _Fleet()).drain([
        _report("FINISH", submission=None, status="NEEDS_CLEARING"),
    ])
    assert dpf.completed == [("B1", 1)]
    assert dpf.failed == []
    assert "P1" not in router.assignments_snapshot()


def test_an_unconfirmed_start_is_left_to_the_send_path(tmp_path):
    """A reset while a start is still being confirmed is a new session too.
    Closing the assignment here would race the send path's retry."""
    clock = Clock(1000.0)
    router = _router(tmp_path, started_at=0.0, confirmed=False)
    dpf = _Dpf()
    dispatcher = _dispatcher(router, dpf, clock, _Fleet())

    dispatcher.drain([_report("IDLE", submission=None)])
    clock.advance(_STEADY_SECONDS + 1)
    dispatcher.drain([_report("IDLE", submission=None)])

    assert dpf.failed == []
    assert router.assignments_snapshot()["P1"]["terminal"] is None
