"""Build the contract example payloads from Link's real code.

`tests/test_state_v2_contract.py` checks the checked-in JSON still matches what
this produces. 3DPF keeps a copy of the JSON and tests its ingest against it.
Regenerate with:  python -m tests.contract_examples > tests/fixtures/contract/examples.json
"""
import json

from bridge.bambu.models import profile_for
from bridge.bambu.state import PrinterState
from bridge.state_v2 import build_state_v2

_AT = 1_758_000_000.0
_SERIAL = "01P00C000000001"


def _v2(print_obj, connection="live", down_reason=None, user_cancelled=False):
    return build_state_v2(
        {"print": print_obj} if print_obj is not None else None,
        connection=connection, down_reason=down_reason,
        profile=profile_for("C12", serial=_SERIAL), model_code="C12", known_model=True,
        user_cancelled=user_cancelled, state_seq=1,
    )


def _events(frames, *, submission=None, link_stop_after=None):
    state = PrinterState(_SERIAL, monotonic=lambda: 0.0, wall_clock=lambda: _AT)
    if submission:
        state.register_submission(submission, batch_id="3f2a9c1e-8b4d-4e6a-9c2f-7d1e5b8a0c4f", plate=1)
    for i, frame in enumerate(frames):
        if link_stop_after is not None and i == link_stop_after:
            state.note_link_stop()
        state.ingest({"print": frame})
    events = state.pending_events()
    for n, event in enumerate(events, start=1):
        event["id"] = f"event-{n}"          # stable ids for the fixture
        event["seq"] = n
        event["bambu_id"] = _SERIAL
    return events


_SUB = "1758000000123"
_RUN = {"gcode_state": "RUNNING", "gcode_file": "/data/Metadata/plate_1.gcode",
        "subtask_name": "Widget", "subtask_id": _SUB, "mc_percent": 42, "layer_num": 30,
        "total_layer_num": 120, "mc_remaining_time": 55, "stg_cur": 0}


def examples():
    return {
        "printing": {"v2": _v2(_RUN), "events": _events(
            [{"gcode_state": "IDLE"}, _RUN], submission=_SUB)},
        "paused_filament_runout": {"v2": _v2({**_RUN, "gcode_state": "PAUSE", "stg_cur": 6}),
                                   "events": []},
        "finished_link_print": {
            "v2": _v2({**_RUN, "gcode_state": "FINISH", "mc_percent": 100, "stg_cur": 255}),
            "events": _events([{"gcode_state": "IDLE"}, _RUN,
                               {**_RUN, "gcode_state": "FINISH", "mc_percent": 100}],
                              submission=_SUB)},
        "failed_external_print": {
            "v2": _v2({**_RUN, "gcode_state": "FAILED", "subtask_id": "9", "print_error": 50364420}),
            "events": _events([{"gcode_state": "IDLE"}, {**_RUN, "subtask_id": "9"},
                               {**_RUN, "subtask_id": "9", "gcode_state": "FAILED",
                                "print_error": 50364420}])},
        "stopped_from_3dpf": {
            "v2": _v2({**_RUN, "gcode_state": "FAILED", "print_error": 50348044}, user_cancelled=True),
            "events": _events([{"gcode_state": "IDLE"}, _RUN,
                               {**_RUN, "gcode_state": "FAILED", "print_error": 50348044}],
                              submission=_SUB, link_stop_after=2)},
        "stuck_job": {"v2": _v2({"gcode_state": "RUNNING", "mc_percent": 0, "stg": [],
                                 "nozzle_target_temper": 0, "subtask_name": "old"}),
                      "events": []},
        "offline_wrong_access_code": {"v2": _v2(None, connection="offline",
                                                down_reason="auth_rejected"), "events": []},
        "stale_while_printing": {"v2": _v2(_RUN, connection="stale"), "events": []},
        "idle": {"v2": _v2({"gcode_state": "IDLE", "mc_percent": 0, "stg_cur": 255}), "events": []},
    }


if __name__ == "__main__":
    print(json.dumps(examples(), indent=2, sort_keys=True))
