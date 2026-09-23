"""project_file is refused while a print is on the machine, and the payload
carries a fresh submission id plus an AMS map that does not turn an
unresolved tray into the external spool.
"""

import pytest

from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


_BUSY = ("PREPARE", "SLICING", "RUNNING", "PAUSE")
_READY = ("IDLE", "FINISH", "FAILED")
_ID_CAP = 2147483647


class _Session:
    def __init__(self, state="live"):
        self.state = state
        self.payloads = []

    def publish(self, payload):
        self.payloads.append(payload)
        return True


def _printer(*, model="C12", session_state="live"):
    cfg = PrinterConfig(
        bambu_id="01P00C000000001", ip="10.0.0.5", access_code="x",
        name="P1S", model=model,
    )
    printer = BambuPrinter(cfg)
    printer._session = _Session(session_state)
    return printer


def _body(printer):
    assert len(printer._session.payloads) == 1
    return printer._session.payloads[0]["print"]


def _set_state(printer, gcode_state):
    printer.state.ingest({"print": {"gcode_state": gcode_state}}, 1.0)


@pytest.mark.parametrize("gcode_state", _BUSY)
def test_project_file_is_refused_while_busy_even_if_the_session_is_stale(gcode_state):
    printer = _printer(session_state="stale")
    _set_state(printer, gcode_state)
    assert printer.start_print("job.3mf", [0], 1) is False
    assert printer._session.payloads == []


@pytest.mark.parametrize("gcode_state", _READY)
def test_idle_finish_and_failed_start_without_a_stop(gcode_state):
    printer = _printer()
    _set_state(printer, gcode_state)
    assert printer.start_print("job.3mf", [0], 1) is True
    commands = [
        payload.get("print", {}).get("command")
        for payload in printer._session.payloads
    ]
    assert commands == ["project_file"]


def test_an_unknown_state_may_still_start():
    printer = _printer()
    assert printer.start_print("job.3mf", [0], 1) is True
    assert _body(printer)["command"] == "project_file"


def test_consecutive_starts_get_different_ids_inside_the_firmware_cap():
    printer = _printer()
    assert printer.start_print("one.3mf", [0], 1) is True
    assert printer.start_print("two.3mf", [0], 1) is True
    seen = []
    for payload in printer._session.payloads:
        body = payload["print"]
        assert body["task_id"] == body["subtask_id"] == body["project_id"]
        value = int(body["task_id"])
        assert 0 < value <= _ID_CAP
        seen.append(value)
    assert seen[0] != seen[1]


def test_unresolved_tray_maps_to_255_255_and_does_not_force_external():
    printer = _printer()
    assert printer.start_print("job.3mf", [-1, 0], 1) is True
    body = _body(printer)
    assert body["ams_mapping"] == [-1, 0]
    assert body["ams_mapping2"] == [
        {"ams_id": 255, "slot_id": 255},
        {"ams_id": 0, "slot_id": 0},
    ]
    assert body["use_ams"] is True


def test_all_unresolved_keeps_use_ams_true():
    printer = _printer()
    assert printer.start_print("job.3mf", [-1, -1], 1) is True
    body = _body(printer)
    assert body["use_ams"] is True
    assert body["ams_mapping2"] == [
        {"ams_id": 255, "slot_id": 255},
        {"ams_id": 255, "slot_id": 255},
    ]


def test_use_ams_is_false_only_when_every_entry_is_external():
    external = _printer()
    assert external.start_print("job.3mf", [254, 255], 1) is True
    assert _body(external)["use_ams"] is False

    mixed = _printer()
    assert mixed.start_print("job.3mf", [254, 0], 1) is True
    assert _body(mixed)["use_ams"] is True


def test_start_url_comes_from_the_p1s_profile():
    printer = _printer(model="C12")
    assert printer.profile.start_url_scheme == "file:///sdcard/"
    assert printer.start_print("batch-a.3mf", [0], 1) is True
    assert _body(printer)["url"] == "file:///sdcard/batch-a.3mf"
    assert _body(printer)["vibration_cali"] is True


def test_a_sent_start_is_remembered_so_the_print_is_origin_link():
    printer = _printer()
    assert printer.start_print("benchy.3mf", [0], 1) is True
    submission_id = _body(printer)["subtask_id"]
    printer.state.ingest({"print": {"gcode_state": "IDLE"}}, 1.0)
    printer.state.ingest({
        "print": {
            "gcode_state": "RUNNING",
            "subtask_id": submission_id,
            "gcode_file": "benchy.3mf",
            "subtask_name": "benchy.3mf",
        },
    }, 2.0)
    view = printer.state.view()
    assert view["print_origin"] == "link"
    assert view["print_submission_id"] == submission_id
    kinds = [event["type"] for event in view["events"]]
    assert kinds == ["print_started"]


def test_a_refused_start_is_not_registered():
    printer = _printer()
    _set_state(printer, "RUNNING")
    assert printer.start_print("job.3mf", [0], 1) is False
    printer.state.ingest({
        "print": {
            "gcode_state": "RUNNING",
            "subtask_id": "1",
            "gcode_file": "job.3mf",
        },
    }, 2.0)
    assert printer.state.view()["print_origin"] != "link"
