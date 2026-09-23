"""The send watchdog confirms on an active state, retries on a budget, and
never uploads a file twice or onto a printer that is already printing.
"""

from bridge.send_pipeline import (
    MAX_ATTEMPTS,
    PHASE_A_SECONDS,
    PHASE_B_SECONDS,
    decide,
    failure_latched,
    failure_reason,
    latch_failure,
    printer_is_held,
    ready_for_upload,
    snapshot_is_active,
)
from bridge.app import (
    _apply_desired,
    _cloud_send_started_path,
    _handle_cloud_sends,
)
from bridge.router import Router

from test_cloud_sends import (
    _ConfirmFleet,
    _FakeDpf,
    _FakeFleet,
    _desired,
    _legacy_ready_snapshot,
)


def _record(phase="A", attempts=1, started=0.0, submission_id=None):
    return {
        "phase": phase,
        "attempts": attempts,
        "phase_started_at": started,
        "submission_id": submission_id,
        "last_failure": None,
        "uploaded": True,
    }


def test_an_echo_in_phase_a_then_running_in_phase_b_is_confirmed():
    record = _record(submission_id="42")
    assert decide(record, {"print_submission_id": "42", "status": "IDLE"}, 10) == "enter_b"
    record["phase"] = "B"
    record["phase_started_at"] = 10
    assert decide(record, {"status": "IDLE", "gcode_state": "RUNNING"}, 20) == "confirm"


def test_no_echo_within_90s_asks_for_one_reset_and_attempt_2():
    record = _record()
    assert decide(record, {"status": "IDLE"}, PHASE_A_SECONDS - 1) == "wait"
    assert decide(record, {"status": "IDLE"}, PHASE_A_SECONDS) == "reset_retry"


def test_phase_b_timeout_retries_without_a_reset():
    record = _record(phase="B", attempts=1, submission_id="42")
    assert decide(record, {"status": "IDLE"}, PHASE_B_SECONDS - 1) == "wait"
    assert decide(record, {"status": "IDLE"}, PHASE_B_SECONDS) == "retry"


def test_the_third_quiet_attempt_fails_with_the_last_reason():
    record = _record(attempts=MAX_ATTEMPTS, started=0.0)
    record["last_failure"] = "no_echo"
    assert decide(record, {"status": "IDLE"}, PHASE_A_SECONDS) == "fail"
    assert failure_reason(record, {"status": "IDLE"}) == "no_echo"


def test_commands_rejected_fails_immediately():
    record = _record()
    snapshot = {"status": "IDLE", "commands_rejected": True}
    assert decide(record, snapshot, 1) == "fail"
    assert failure_reason(record, snapshot) == "commands_rejected"


def test_finish_or_idle_is_not_an_active_start():
    assert snapshot_is_active({"status": "IDLE", "gcode_state": "FINISH"}) is False
    assert snapshot_is_active({"status": "NEEDS_CLEARING", "gcode_state": "FINISH"}) is False
    assert decide(_record(), {"status": "IDLE", "gcode_state": "FINISH"}, 1) == "wait"


def test_a_printing_printer_is_not_ready_for_an_upload():
    assert ready_for_upload({"status": "PRINTING", "connection": "live"}) is False
    assert ready_for_upload({
        "status": "IDLE", "gcode_state": "IDLE", "connection": "stale",
    }) is False
    assert ready_for_upload({
        "status": "NEEDS_CLEARING", "gcode_state": "FINISH", "connection": "live",
    }) is True


def test_one_in_flight_send_holds_the_printer():
    live = {("B1", "P1", 1), ("B2", "P1", 1)}
    started = [("B1", "P1", 1)]
    assert printer_is_held(started, "P1", ("B2", "P1", 1), live) is True
    assert printer_is_held(started, "P1", ("B2", "P1", 1), {("B2", "P1", 1)}) is False


def test_commands_rejected_fails_the_send_without_an_upload(tmp_path):
    fleet = _ConfirmFleet()
    fleet._printer._snapshot = _legacy_ready_snapshot(
        commands_rejected=True, connection="live",
    )
    dpf = _FakeDpf()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set())
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set())
    assert fleet.uploads == []
    assert fleet.starts == []
    assert dpf.failed == [("B1", 2, "commands_rejected")]


def test_unacked_failure_is_reported_again_then_latched(tmp_path):
    fleet = _ConfirmFleet()
    fleet._printer._snapshot = _legacy_ready_snapshot(
        commands_rejected=True, connection="live",
    )

    class _RetryAck(_FakeDpf):
        def report_failed(self, batch_id, plate_number=None, reason=None):
            self.failed.append((batch_id, plate_number, reason))
            if len(self.failed) == 1:
                return {}
            return {"batch_id": batch_id}

    dpf = _RetryAck()
    spool = str(tmp_path)
    started_path = _cloud_send_started_path(spool, ("B1", "P1", 2))
    _handle_cloud_sends(_desired(), fleet, dpf, spool, set())
    assert dpf.failed == [("B1", 2, "commands_rejected")]
    assert failure_latched(started_path) is False
    _handle_cloud_sends(_desired(), fleet, dpf, spool, set())
    assert dpf.failed == [
        ("B1", 2, "commands_rejected"),
        ("B1", 2, "commands_rejected"),
    ]
    assert failure_latched(started_path) is True
    _handle_cloud_sends(_desired(), fleet, dpf, spool, set())
    assert len(dpf.failed) == 2


def test_a_printing_printer_never_gets_an_upload(tmp_path):
    fleet = _ConfirmFleet(status="PRINTING")
    fleet._printer._snapshot["connection"] = "live"
    fleet._printer._snapshot["gcode_state"] = "RUNNING"
    dpf = _FakeDpf()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set())
    assert fleet.uploads == []
    assert fleet.starts == []
    assert dpf.failed == []


def test_a_second_send_waits_until_the_first_watchdog_resolves(tmp_path):
    fleet = _FakeFleet()
    second = _desired()
    second[0] = dict(second[0])
    second[0]["send"] = dict(second[0]["send"])
    second[0]["send"]["batch_id"] = "B2"
    second[0]["send"]["item_id"] = "B2:2"
    _handle_cloud_sends(_desired() + second, fleet, _FakeDpf(), str(tmp_path), set())
    assert len(fleet.uploads) == 1
    assert len(fleet.calls) == 1


def test_stop_during_phase_a_abandons_without_a_failure(tmp_path):
    fleet = _ConfirmFleet()
    dpf = _FakeDpf()
    started = set()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started)
    assert len(fleet.starts) == 1
    stopped = _desired()
    stopped[0]["control"] = {"id": "c-stop", "action": "stop"}
    _handle_cloud_sends(stopped, fleet, dpf, str(tmp_path), started)
    assert dpf.failed == []
    assert len(fleet.starts) == 1
    assert started == set()


def test_pending_republish_waits_then_resets_or_fails():
    record = _record(submission_id="42")
    record["pending_republish"] = True
    idle = {"status": "IDLE"}
    assert decide(record, idle, PHASE_A_SECONDS - 1) == "republish"
    assert decide(
        record, {"status": "IDLE", "gcode_state": "RUNNING"}, 1,
    ) == "confirm"
    assert decide(
        record, {"status": "IDLE", "print_submission_id": "42"}, 1,
    ) == "republish"
    assert decide(record, idle, PHASE_A_SECONDS) == "reset_retry"
    record["attempts"] = MAX_ATTEMPTS
    assert decide(record, idle, 1) == "fail"


def test_phase_a_timeout_resets_once_and_does_not_upload_again(tmp_path):
    class _Session:
        def __init__(self):
            self.connected = True
            self.resets = 0

        def hard_reset(self):
            self.resets += 1
            self.connected = False

    class _ResetFleet(_ConfirmFleet):
        def __init__(self):
            super().__init__()
            self.resets = 0
            self._printer._session = _Session()

        def hard_reset(self):
            self.resets += 1

    clock = _Clock()
    fleet = _ResetFleet()
    session = fleet._printer._session
    dpf = _FakeDpf()
    router = Router(str(tmp_path / "queue.json"))
    started = set()
    kwargs = {
        "router": router,
        "wall_time": lambda: clock.now,
    }
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    clock.advance(PHASE_A_SECONDS)
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    stored = router.assignments_snapshot()["P1"]
    assert session.resets == 1
    assert fleet.resets == 0
    assert len(fleet.starts) == 1
    assert dpf.failed == []
    assert stored["attempts"] == 1
    assert stored["pending_republish"] is True

    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    assert len(fleet.starts) == 1
    assert dpf.failed == []
    assert session.resets == 1
    assert fleet.resets == 0

    session.connected = True
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    stored = router.assignments_snapshot()["P1"]
    assert len(fleet.starts) == 2
    assert stored["attempts"] == 2
    assert stored["pending_republish"] is False
    assert len(fleet.uploads) == 1
    assert fleet.resets == 0
    assert dpf.failed == []


def test_phase_b_timeout_retries_without_resetting(tmp_path):
    class _EchoFleet(_ConfirmFleet):
        def start_print(self, bambu_id, remote_name, mapping, plate_index=1):
            started = super().start_print(
                bambu_id, remote_name, mapping, plate_index,
            )
            self._printer.last_submission_id = "4242"
            return started

        def hard_reset(self):
            raise AssertionError("phase B must not reset the session")

    clock = _Clock()
    fleet = _EchoFleet()
    dpf = _FakeDpf()
    started = set()
    kwargs = {"wall_time": lambda: clock.now}
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    fleet._printer._snapshot["print_submission_id"] = "4242"
    clock.advance(1)
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    assert len(fleet.starts) == 1
    clock.advance(PHASE_B_SECONDS)
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started, **kwargs)
    assert len(fleet.starts) == 2
    assert len(fleet.uploads) == 1
    assert dpf.failed == []


def test_one_printer_pass_does_not_clear_another_printers_failure(tmp_path):
    key_a = ("B1", "P1", 1)
    key_b = ("B2", "P2", 1)
    latch_failure(_cloud_send_started_path(str(tmp_path), key_a), key_a, "no_echo")
    latch_failure(_cloud_send_started_path(str(tmp_path), key_b), key_b, "no_echo")

    class _InlineSubmit:
        def submit(self, serial, fn, *args, **kwargs):
            fn(*args, **kwargs)

    def row(batch_id, bambu_id):
        return {
            "bambu_id": bambu_id,
            "send": {
                "batch_id": batch_id,
                "plate_index": 1,
                "file_url": "https://example/signed.3mf",
            },
        }

    both = [row("B1", "P1"), row("B2", "P2")]
    _apply_desired(both, _InlineSubmit(), _FakeDpf(), str(tmp_path), set(), set())
    assert failure_latched(_cloud_send_started_path(str(tmp_path), key_a))
    assert failure_latched(_cloud_send_started_path(str(tmp_path), key_b))

    _apply_desired(
        [row("B1", "P1")], _InlineSubmit(), _FakeDpf(), str(tmp_path), set(), set(),
    )
    assert failure_latched(_cloud_send_started_path(str(tmp_path), key_a))
    assert failure_latched(_cloud_send_started_path(str(tmp_path), key_b)) is False


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def advance(self, seconds):
        self.now += seconds


def test_a_changed_file_name_enters_phase_b_and_an_unchanged_name_resets():
    changed = _record()
    changed["gcode_file"] = "old.gcode"
    finish = {"status": "NEEDS_CLEARING", "gcode_state": "FINISH", "gcode_file": "new.gcode"}
    assert decide(changed, finish, PHASE_A_SECONDS) == "enter_b"
    same = _record()
    same["gcode_file"] = "old.gcode"
    finish["gcode_file"] = "old.gcode"
    assert decide(same, finish, PHASE_A_SECONDS) == "reset_retry"
    assert decide(same, {"status": "PRINTING", "gcode_state": "RUNNING", "gcode_file": "other.gcode"}, 1) == "confirm"
    phase_b = _record(phase="B", attempts=1)
    phase_b["gcode_file"] = "old.gcode"
    assert decide(phase_b, {"status": "IDLE", "gcode_file": "new.gcode"}, PHASE_B_SECONDS) == "retry"
    missing = _record()
    assert decide(missing, finish, PHASE_A_SECONDS) == "reset_retry"


def test_save_and_load_keep_the_pre_send_file_name(tmp_path):
    from bridge.send_pipeline import load_attempt, save_attempt

    router = Router(str(tmp_path / "queue.json"))
    router.record_assignment("P1", "B1", 1, started_at=1.0)
    marker = tmp_path / "B1.3mf.started"
    marker.write_text("commanded")
    record = _record(submission_id="9")
    record["gcode_file"] = "plate.gcode"
    record["uploaded"] = True
    save_attempt(str(marker), router, "P1", record)
    loaded = load_attempt(str(marker), router, "P1", 50.0)
    assert loaded["gcode_file"] == "plate.gcode"
    assert router.assignments_snapshot()["P1"]["gcode_file"] == "plate.gcode"


def test_a_final_failure_names_the_drying_unit_and_does_not_stop_it(tmp_path):
    from bridge.app import _advance_cloud_send

    class _DryFleet:
        def __init__(self):
            self.commands = []

        def by_id(self, _bambu_id):
            return self

        def snapshot(self):
            return {
                "status": "NEEDS_CLEARING",
                "gcode_state": "FINISH",
                "gcode_file": "same.gcode",
                "connection": "live",
                "dry_time": 5,
                "drying_unit": 7,
            }

        def apply_control(self, *_args):
            self.commands.append("control")
            return True

        def start_print(self, *_args):
            self.commands.append("start")
            return False

    key = ("B1", "P1", 1)
    spool = str(tmp_path)
    started = _cloud_send_started_path(spool, key)
    router = Router(str(tmp_path / "queue.json"))
    router.record_assignment("P1", "B1", 1, started_at=0.0)
    record = _record(attempts=MAX_ATTEMPTS)
    record["gcode_file"] = "same.gcode"
    record["last_failure"] = "no_echo"
    from bridge.send_pipeline import save_attempt
    save_attempt(started, router, "P1", record)
    fleet = _DryFleet()
    dpf = _FakeDpf()
    _advance_cloud_send(key, {}, fleet, dpf, spool, set(), router, lambda: PHASE_A_SECONDS)
    assert dpf.failed
    assert "drying unit 7" in dpf.failed[0][2]
    assert fleet.commands == []
    assert "ams_filament_drying" not in dpf.failed[0][2]
