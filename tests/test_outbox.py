"""Durable lifecycle events (plan U3): on disk, numbered, gone only when 3DPF has them."""
import json

from bridge.app import _ack_reported_events
from bridge.bambu.state import PrinterState
from bridge.outbox import MAX_EVENTS, EventOutbox

_AT = 1_758_000_000.0


def _doc(state, **fields):
    return {"print": {"gcode_state": state, **fields}}


def _state(outbox, bambu_id="P1"):
    return PrinterState(bambu_id, monotonic=lambda: 0.0, wall_clock=lambda: _AT, outbox=outbox)


def _types(events):
    return [e["type"] for e in events]


def _run_print(state, end="FINISH", **fields):
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate_1.gcode", subtask_name="Widget", **fields))
    state.ingest(_doc(end, gcode_file="plate_1.gcode", subtask_name="Widget", **fields))


def test_events_are_numbered_and_written_to_disk(tmp_path):
    path = str(tmp_path / "events.json")
    state = _state(EventOutbox(path))
    _run_print(state)
    on_disk = json.load(open(path))
    assert [e["type"] for e in on_disk["events"]] == ["print_started", "print_finished"]
    assert [e["seq"] for e in on_disk["events"]] == [1, 2]
    assert on_disk["next_seq"] == 3
    assert all(e["bambu_id"] == "P1" for e in on_disk["events"])


def test_unsent_events_survive_a_restart(tmp_path):
    path = str(tmp_path / "events.json")
    _run_print(_state(EventOutbox(path)))
    # Link restarts: a new outbox reads the same file.
    restarted = _state(EventOutbox(path))
    assert _types(restarted.pending_events()) == ["print_started", "print_finished"]


def test_seq_keeps_rising_after_a_restart_and_an_ack(tmp_path):
    path = str(tmp_path / "events.json")
    first = EventOutbox(path)
    state = _state(first)
    _run_print(state)
    first.ack([e["id"] for e in first.pending()])
    second = EventOutbox(path)
    _run_print(_state(second))
    assert [e["seq"] for e in second.pending()] == [3, 4]


def test_events_are_per_printer(tmp_path):
    outbox = EventOutbox(str(tmp_path / "events.json"))
    _run_print(_state(outbox, "A"))
    _run_print(_state(outbox, "B"), end="FAILED")
    assert _types(_state(outbox, "A").pending_events()) == ["print_started", "print_finished"]
    assert _types(_state(outbox, "B").pending_events()) == ["print_started", "print_failed"]


def test_a_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "events.json"
    path.write_text("{not json")
    assert len(EventOutbox(str(path))) == 0


def test_the_outbox_is_capped(tmp_path, monkeypatch):
    import bridge.outbox as outbox_module
    assert MAX_EVENTS >= 1000  # a day of farm prints fits with room to spare
    monkeypatch.setattr(outbox_module, "MAX_EVENTS", 10)
    outbox = EventOutbox(str(tmp_path / "events.json"))
    for i in range(15):
        outbox.append("P1", {"id": f"e{i}", "type": "print_started"})
    pending = outbox.pending()
    assert len(pending) == 10
    assert pending[0]["id"] == "e5"


# --- what an event says -----------------------------------------------------

def test_a_link_stop_is_a_cancel_by_link(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate_1.gcode"))
    state.note_link_stop()
    state.ingest(_doc("FAILED", gcode_file="plate_1.gcode"))
    last = state.pending_events()[-1]
    assert last["type"] == "print_cancelled"
    assert last["by"] == "link"


def test_a_cancel_code_without_a_link_stop_is_a_cancel_on_the_printer(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate_1.gcode"))
    state.ingest(_doc("RUNNING", gcode_file="plate_1.gcode", print_error=50348044))
    state.ingest(_doc("FAILED", gcode_file="plate_1.gcode", print_error=50348044))
    last = state.pending_events()[-1]
    assert last["type"] == "print_cancelled"
    assert last["by"] == "printer"


def test_a_real_failure_is_a_failure_with_its_error(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    _run_print(state, end="FAILED", print_error=50364420)
    last = state.pending_events()[-1]
    assert last["type"] == "print_failed"
    assert "by" not in last
    assert last["print_error"] == "50364420"


def test_a_link_stop_does_not_leak_into_the_next_print(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="a.gcode"))
    state.note_link_stop()
    state.ingest(_doc("FAILED", gcode_file="a.gcode"))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="b.gcode"))
    state.ingest(_doc("FAILED", gcode_file="b.gcode", print_error=50364420))
    assert state.pending_events()[-1]["type"] == "print_failed"


def test_events_for_a_link_start_name_the_batch_and_plate(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.register_submission("1700000000123", batch_id="b-42", plate=2)
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="plate_2.gcode", subtask_id="1700000000123"))
    state.ingest(_doc("FINISH", gcode_file="plate_2.gcode", subtask_id="1700000000123",
                      mc_percent=100))
    events = state.pending_events()
    assert [(e["type"], e["origin"], e["batch_id"], e["plate"]) for e in events] == [
        ("print_started", "link", "b-42", 2),
        ("print_finished", "link", "b-42", 2),
    ]
    assert events[-1]["progress"] == 100


def test_an_external_print_names_no_batch(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    _run_print(state)
    assert all(e["origin"] == "external" and e["batch_id"] is None
               for e in state.pending_events())


def test_pause_carries_its_stage(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="p.gcode"))
    state.ingest(_doc("PAUSE", gcode_file="p.gcode", stg_cur=6))
    paused = state.pending_events()[-1]
    assert paused["type"] == "print_paused"
    assert paused["stage"] == 6


def test_a_first_push_at_pause_is_not_a_pause_event(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("PAUSE", gcode_file="p.gcode", stg_cur=16))
    assert state.pending_events() == []


# --- acknowledgment ---------------------------------------------------------

class _Fleet:
    def __init__(self):
        self.acked = []

    def ack_events(self, by_printer):
        self.acked.append(by_printer)


def _reports():
    return [{"bambu_id": "P1", "events": [{"id": "a"}, {"id": "b"}]},
            {"bambu_id": "P2", "events": [{"id": "c"}]}]


def test_a_cloud_that_applies_events_acks_only_what_it_stored():
    fleet = _Fleet()
    _ack_reported_events(fleet, {"printers": [], "events_acked": ["a", "c"]}, _reports())
    assert fleet.acked == [{"P1": ["a"], "P2": ["c"]}]


def test_an_empty_events_acked_list_acks_nothing():
    fleet = _Fleet()
    _ack_reported_events(fleet, {"printers": [], "events_acked": []}, _reports())
    assert fleet.acked == []


def test_an_older_cloud_acks_everything_the_accepted_post_carried():
    fleet = _Fleet()
    _ack_reported_events(fleet, {"printers": []}, _reports())
    assert fleet.acked == [{"P1": ["a", "b"], "P2": ["c"]}]


def test_a_failed_post_acks_nothing():
    fleet = _Fleet()
    _ack_reported_events(fleet, {}, _reports())
    assert fleet.acked == []


# --- stuck job (plan A7) ----------------------------------------------------

def test_clearing_a_stuck_job_is_labelled_so_the_cloud_keeps_the_plate_free(tmp_path):
    state = _state(EventOutbox(str(tmp_path / "e.json")))
    state.ingest(_doc("IDLE"))
    state.ingest(_doc("RUNNING", gcode_file="old.gcode", mc_percent=0))
    state.note_link_stop("stuck_job")
    state.ingest(_doc("IDLE"))
    last = state.pending_events()[-1]
    assert last["type"] == "print_cancelled"
    assert last["by"] == "link_cleared_stuck_job"


def test_app_clears_a_stuck_job_once_per_cooldown():
    from bridge import app

    class _Printer:
        def __init__(self):
            self.stops = 0

        def clear_stuck_job(self):
            self.stops += 1
            return True

    class _Fleet:
        def __init__(self, printer):
            self.printer = printer

        def by_id(self, bambu_id):
            return self.printer

    printer = _Printer()
    fleet = _Fleet(printer)
    app._STUCK_JOB_STOPS.clear()
    clock = [100.0]
    assert app._clear_stuck_job(fleet, "P1", "b", monotonic=lambda: clock[0]) is True
    assert app._clear_stuck_job(fleet, "P1", "b", monotonic=lambda: clock[0]) is False
    clock[0] += 31
    assert app._clear_stuck_job(fleet, "P1", "b", monotonic=lambda: clock[0]) is True
    assert printer.stops == 2


def test_only_a_live_stuck_report_counts():
    from bridge import app
    assert app._snapshot_stuck_job({"v2": {"stuck_job": True, "connection": "live"}})
    assert not app._snapshot_stuck_job({"v2": {"stuck_job": True, "connection": "stale"}})
    assert not app._snapshot_stuck_job({"v2": {"stuck_job": False, "connection": "live"}})
    assert not app._snapshot_stuck_job({"status": "IDLE"})
    assert not app._snapshot_stuck_job(None)
