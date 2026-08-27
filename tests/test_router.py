"""Tests for the durable job queue (U7) and the Dispatcher that drains it (U9).

U7's one guarantee: a job enqueued before a bridge restart is still there after it.
U9's dispatch matches a queued job's required colors to an idle printer's live AMS and
starts it — exercised here with fakes for the fleet (upload+start) and the cloud client
(resolve + report), so the matching, the explicit AMS mapping, restart-safety, and the
failure paths are all proven without a real printer or network.
"""
import json

from bridge.router import (
    ASSIGNMENT_STARTUP_GRACE_SECONDS,
    DISPATCHED,
    QUEUED,
    UNRESOLVED,
    Dispatcher,
    Job,
    Router,
)


def test_enqueue_persists_and_survives_restart(tmp_path):
    path = str(tmp_path / "queue.json")
    r1 = Router(path)
    job = r1.enqueue(Job.new(
        stored_path="/spool/abc.3mf",
        correlation_key="batch-2026-02-20-JyBIcozw-{plate_num}.3mf",
        print_flag=True,
        status=QUEUED,
        now=1000.0,
    ))

    # A fresh Router (simulating a bridge restart) reloads the job from disk.
    r2 = Router(path)
    reloaded = r2.pending()
    assert len(reloaded) == 1
    assert reloaded[0].id == job.id
    assert reloaded[0].correlation_key == job.correlation_key
    assert reloaded[0].print_flag is True
    assert reloaded[0].status == QUEUED


def test_multiple_jobs_kept_in_order(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", False, QUEUED, now=1.0))
    r.enqueue(Job.new("/spool/b.3mf", None, False, UNRESOLVED, now=2.0))
    ids = [j.stored_path for j in Router(path).pending()]
    assert ids == ["/spool/a.3mf", "/spool/b.3mf"]


def test_corrupt_queue_file_starts_empty_not_crash(tmp_path):
    path = str(tmp_path / "queue.json")
    with open(path, "w") as f:
        f.write("{ this is not valid json")
    r = Router(path)  # must not raise
    assert r.pending() == []
    # And it can still enqueue after recovering.
    r.enqueue(Job.new("/spool/x.3mf", "batch-x", False, QUEUED, now=1.0))
    assert len(Router(path).pending()) == 1


def test_persist_is_atomic_valid_json(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", False, QUEUED, now=1.0))
    with open(path) as f:
        data = json.load(f)  # parseable = not truncated
    assert data[0]["stored_path"] == "/spool/a.3mf"


# --- U9: Dispatcher ----------------------------------------------------------

class _FakeDpf:
    """Stand-in for DpfClient: canned resolve results + records of what was reported."""

    def __init__(self, resolve_map):
        self._resolve_map = resolve_map      # correlation_key -> resolve dict ({} = 404)
        self.resolve_calls = []
        self.dispatched = []                 # (batch_id, bambu_id) reported PRINTING
        self.completed = []                  # (batch_id, plate) reported complete
        self.failed = []                     # (batch_id, plate) reported failed

    def resolve_batch(self, key):
        self.resolve_calls.append(key)
        return self._resolve_map.get(key, {})

    def report_dispatched(self, batch_id, bambu_id):
        self.dispatched.append((batch_id, bambu_id))
        return {"batch_id": batch_id}

    def report_complete(self, batch_id, plate_number=None):
        self.completed.append((batch_id, plate_number))
        return {"batch_id": batch_id, "status": "COMPLETED"}

    def report_failed(self, batch_id, plate_number=None, reason=None):
        self.failed.append((batch_id, plate_number))
        return {"batch_id": batch_id, "status": "FAILED"}


class _FakeFleet:
    """Stand-in for Fleet.dispatch: records calls; can refuse or raise."""

    def __init__(self, result=True, raises=False):
        self._result = result
        self._raises = raises
        self.calls = []                      # (bambu_id, file_path, ams_mapping, plate)

    def dispatch(self, bambu_id, file_path, ams_mapping, plate_number=1, remote_name=None):
        self.calls.append((bambu_id, file_path, list(ams_mapping), plate_number, remote_name))
        if self._raises:
            raise RuntimeError("ftps boom")
        return self._result


def _snap(bambu_id, status, colors):
    """A fleet snapshot for one printer. `colors` = [(slot_number, tray_color_hex)]."""
    return {
        "bambu_id": bambu_id,
        "status": status,
        "slots": [{"slot_number": n, "color_hex": h} for n, h in colors],
    }


def _router_with_job(tmp_path, correlation_key="batch-a", status=QUEUED, stored="/spool/a.3mf"):
    r = Router(str(tmp_path / "queue.json"))
    r.enqueue(Job.new(stored, correlation_key, True, status, now=1.0))
    return r


def test_drain_does_not_auto_dispatch_a_queued_job(tmp_path):
    # Cloud Sliced Queue owns start. Local color auto-match is off (KTD6).
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([
        _snap("P1", "IDLE", [(1, "FF6A13FF"), (2, "1A1A1AFF")]),
    ])
    assert fleet.calls == []
    assert dpf.dispatched == []
    assert dpf.resolve_calls == []
    assert len(r.pending()) == 1
    assert r.pending()[0].status == QUEUED


def test_desired_idle_does_not_auto_dispatch_locally(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain(
        [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])],
        desired=[{"bambu_id": "P1", "desired_status": "IDLE"}],
    )
    assert fleet.calls == []
    assert len(r.pending()) == 1


def test_unresolved_status_job_is_never_auto_dispatched(tmp_path):
    r = _router_with_job(tmp_path, correlation_key=None, status=UNRESOLVED, stored="/spool/x.3mf")
    dpf = _FakeDpf({})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF"), (2, "000000FF")])])
    assert fleet.calls == []
    assert len(r.pending()) == 1


# --- U9: durable dispatch report (a failed report is retried, never stranded) ---

class _FailingThenOkDpf(_FakeDpf):
    """Resolves normally, but report_dispatched fails (returns {}) the first `fail_reports`
    times — a cloud blip at report time — then acks."""

    def __init__(self, resolve_map, fail_reports=1):
        super().__init__(resolve_map)
        self._fail_reports = fail_reports

    def report_dispatched(self, batch_id, bambu_id):
        self.dispatched.append((batch_id, bambu_id))
        if self._fail_reports > 0:
            self._fail_reports -= 1
            return {}  # cloud unreachable / 5xx exhausted / 4xx
        return {"batch_id": batch_id}


def test_failed_report_keeps_job_dispatched_then_retries_until_acked(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    job = r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    r.mark_resolved(job.id, "B1", ["#FF6A13"])
    r.mark_dispatched(job.id, "P1")
    dpf = _FailingThenOkDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                            fail_reports=1)
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)
    snaps = [_snap("P1", "PRINTING", [(1, "FF6A13FF")])]

    # Pass 1: physical start already happened; the owed report fails -> stays DISPATCHED.
    d.drain(snaps)
    assert fleet.calls == []
    held = r.pending()
    assert len(held) == 1
    assert held[0].status == DISPATCHED
    assert held[0].dispatched_to == "P1"
    assert dpf.dispatched == [("B1", "P1")]
    assert Router(path).pending()[0].status == DISPATCHED

    # Pass 2: no physical dispatch; the owed report is retried and now acks -> removed.
    d.drain(snaps)
    assert fleet.calls == []
    assert dpf.dispatched == [("B1", "P1"), ("B1", "P1")]
    assert r.pending() == []


# --- U11: completion / failure detection + durable report --------------------

def _dispatch_one(tmp_path, dpf=None, *, started_at=None):
    """Record a cloud-started print on P1. Local drain no longer starts jobs."""
    r = _router_with_job(tmp_path)
    job = r.pending()[0]
    r.mark_resolved(job.id, "B1", ["#FF6A13"])
    r.mark_dispatched(job.id, "P1")
    r.record_assignment("P1", "B1", started_at=started_at)
    dpf = dpf or _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    assert r.assignments_snapshot()["P1"]["batch_id"] == "B1"
    return r, dpf


def test_finished_print_reports_complete_and_clears_assignment(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    # Active evidence belongs to this assignment; its later FINISH is safe.
    d.drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])
    assert dpf.completed == [("B1", None)]
    assert dpf.failed == []
    assert "P1" not in r.assignments_snapshot()  # cleared on ack


def test_failed_print_reports_failed_and_clears_assignment(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    d.drain([_snap("P1", "ERROR", [(1, "FF6A13FF")])])
    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


def test_historical_failed_signal_does_not_suppress_an_assigned_batch_failure(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    snap = _snap("P1", "ERROR", [(1, "FF6A13FF")])
    snap["historical_failed_ready"] = True

    d.drain([snap])

    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


def test_same_pass_start_ignores_old_error_snapshot(tmp_path):
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    d = Dispatcher(r, _FakeFleet(), dpf, now_fn=lambda: 100.0)

    d.drain([_snap("P1", "ERROR", [(1, "FF6A13FF")])])

    assignment = r.assignments_snapshot()["P1"]
    assert assignment["observed_active"] is False
    assert assignment["started_at"] == 100.0
    assert assignment["terminal"] is None
    assert dpf.failed == []


def test_no_active_persistent_error_fails_after_startup_grace_and_restart(tmp_path):
    path = str(tmp_path / "queue.json")
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    error = [_snap("P1", "ERROR", [(1, "FF6A13FF")])]

    before_grace = 100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS - 1
    Dispatcher(r, _FakeFleet(), dpf, now_fn=lambda: before_grace).drain(error)
    assert dpf.failed == []
    assert r.assignments_snapshot()["P1"]["terminal"] is None

    restarted = Router(path)
    assignment = restarted.assignments_snapshot()["P1"]
    assert assignment["started_at"] == 100.0
    assert assignment["observed_active"] is False
    grace_end = 100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS
    Dispatcher(restarted, _FakeFleet(), dpf, now_fn=lambda: grace_end).drain(error)

    assert dpf.failed == [("B1", None)]
    assert "P1" not in restarted.assignments_snapshot()


def test_observed_active_survives_restart_then_real_error_fails(tmp_path):
    path = str(tmp_path / "queue.json")
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    Dispatcher(r, _FakeFleet(), dpf, now_fn=lambda: 101.0).drain([
        _snap("P1", "PAUSED", [(1, "FF6A13FF")]),
    ])

    restarted = Router(path)
    assignment = restarted.assignments_snapshot()["P1"]
    assert assignment["started_at"] == 100.0
    assert assignment["observed_active"] is True
    Dispatcher(restarted, _FakeFleet(), dpf, now_fn=lambda: 102.0).drain([
        _snap("P1", "ERROR", [(1, "FF6A13FF")]),
    ])

    assert dpf.failed == [("B1", None)]
    assert "P1" not in restarted.assignments_snapshot()


def test_prestart_finish_ignored_during_grace_then_active_finish_completes(tmp_path):
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    d = Dispatcher(
        r, _FakeFleet(), dpf,
        now_fn=lambda: 100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS - 1,
    )
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]

    d.drain(finished)
    assert dpf.completed == []
    assert "P1" in r.assignments_snapshot()

    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    d.drain(finished)
    assert dpf.completed == [("B1", None)]
    assert "P1" not in r.assignments_snapshot()


def test_pre_active_finish_after_startup_grace_reports_failed_not_complete(tmp_path):
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    d = Dispatcher(
        r, _FakeFleet(), dpf,
        now_fn=lambda: 100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS,
    )

    d.drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])

    assert dpf.completed == []
    assert dpf.failed == [("B1", None)]
    assert "P1" not in r.assignments_snapshot()


def test_legacy_assignment_is_normalized_as_active_and_can_report_finish(tmp_path):
    path = str(tmp_path / "queue.json")
    with open(path + ".assignments", "w") as handle:
        json.dump({
            "P1": {
                "batch_id": "B1",
                "plate_number": 2,
                "terminal": None,
            },
        }, handle)

    r = Router(path)
    assignment = r.assignments_snapshot()["P1"]
    assert assignment["started_at"] == 0.0
    assert assignment["observed_active"] is True
    with open(path + ".assignments") as handle:
        assert json.load(handle)["P1"] == assignment

    dpf = _FakeDpf({})
    Dispatcher(r, _FakeFleet(), dpf).drain([
        _snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")]),
    ])

    assert dpf.completed == [("B1", 2)]
    assert "P1" not in r.assignments_snapshot()


def test_assignment_proof_annotation_is_strict_and_precedes_current_pass(tmp_path):
    r, _dpf = _dispatch_one(tmp_path, started_at=100.0)
    printing = _snap("P1", "PRINTING", [(1, "FF6A13FF")])

    first = r.annotate_reports([printing])
    assert first[0]["assignment_observed_active"] is False

    Dispatcher(r, _FakeFleet(), _FakeDpf({}), now_fn=lambda: 101.0).drain([printing])
    second = r.annotate_reports([{
        **printing,
        "assignment_observed_active": "hostile-overwrite",
    }])
    assert second[0]["assignment_observed_active"] is True

    unassigned = r.annotate_reports([_snap("P2", "IDLE", [])])
    assert unassigned[0]["assignment_observed_active"] is False


def test_non_boolean_assignment_proof_is_normalized_false_and_persisted(tmp_path):
    path = str(tmp_path / "queue.json")
    with open(path + ".assignments", "w") as handle:
        json.dump({
            "P1": {
                "batch_id": "B1",
                "plate_number": None,
                "terminal": None,
                "started_at": 100.0,
                "observed_active": "true",
            },
        }, handle)

    assignment = Router(path).assignments_snapshot()["P1"]

    assert assignment["observed_active"] is False
    with open(path + ".assignments") as handle:
        assert json.load(handle)["P1"]["observed_active"] is False


def test_still_printing_does_not_report(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    assert dpf.completed == [] and dpf.failed == []
    assert "P1" in r.assignments_snapshot()  # still tracked


def test_completion_reported_exactly_once_across_passes(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    d.drain(finished)                 # reports + clears
    d.drain(finished)                 # assignment gone -> no second report
    assert dpf.completed == [("B1", None)]


class _CompleteFailsOnceDpf(_FakeDpf):
    def __init__(self, resolve_map, fail_completes=1):
        super().__init__(resolve_map)
        self._fail_completes = fail_completes

    def report_complete(self, batch_id, plate_number=None):
        self.completed.append((batch_id, plate_number))
        if self._fail_completes > 0:
            self._fail_completes -= 1
            return {}  # cloud blip at completion-report time
        return {"batch_id": batch_id}


def test_failed_completion_report_is_retried_until_acked(tmp_path):
    dpf = _CompleteFailsOnceDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                                fail_completes=1)
    r, dpf = _dispatch_one(tmp_path, dpf)
    d = Dispatcher(r, _FakeFleet(), dpf)
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]

    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    d.drain(finished)                 # terminal latched, report FAILS -> stays owed
    assert r.assignments_snapshot()["P1"]["terminal"] == "complete"
    d.drain(finished)                 # retried, now acks -> cleared
    assert "P1" not in r.assignments_snapshot()
    assert dpf.completed == [("B1", None), ("B1", None)]


def test_assignment_and_latch_survive_a_restart(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    job = r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    r.mark_resolved(job.id, "B1", ["#FF6A13"])
    r.mark_dispatched(job.id, "P1")
    r.record_assignment("P1", "B1")
    dpf = _CompleteFailsOnceDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                                fail_completes=1)
    Dispatcher(r, _FakeFleet(), dpf).drain([
        _snap("P1", "PRINTING", [(1, "FF6A13FF")]),
    ])
    # Print finishes, but the completion report fails this pass -> latched, persisted.
    Dispatcher(r, _FakeFleet(), dpf).drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])
    assert r.assignments_snapshot()["P1"]["terminal"] == "complete"

    # A fresh Router (bridge restart) reloads the latched assignment and re-reports it.
    r2 = Router(path)
    assert r2.assignments_snapshot()["P1"]["terminal"] == "complete"
    d2 = Dispatcher(r2, _FakeFleet(), dpf)
    d2.drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])
    assert "P1" not in r2.assignments_snapshot()  # acked on retry -> cleared


# --- U13: clear-plate resume -------------------------------------------------

def test_desired_idle_does_not_resume_local_auto_dispatch(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]
    d.drain(finished)
    d.drain(finished, desired=[{"bambu_id": "P1", "desired_status": "IDLE"}])
    assert fleet.calls == []
    assert len(r.pending()) == 1


def test_printer_owing_a_completion_report_is_not_re_dispatched(tmp_path):
    # A printer whose completion report is still owed (latched but the report keeps failing)
    # must NOT take a new job — dispatching would overwrite its single assignment slot and
    # lose the owed completion. It's held back until the report acks and clears.
    dpf = _CompleteFailsOnceDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                                fail_completes=99)  # completion report never acks
    r, dpf = _dispatch_one(tmp_path, dpf)
    # A second matching job is queued.
    r.enqueue(Job.new("/spool/b.3mf", "batch-b", True, QUEUED, now=2.0))
    dpf._resolve_map["batch-b"] = {"batch_id": "B2", "required_colors": ["#FF6A13"]}
    d = Dispatcher(r, _FakeFleet(), dpf)

    # The printer finished (NEEDS_CLEARING) and the operator cleared it (desired IDLE), but
    # its completion report is still owed -> it must not be re-dispatched.
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    fleet_calls_before = len(d._fleet.calls)
    d.drain(finished, desired=[{"bambu_id": "P1", "desired_status": "IDLE"}])
    assert len(d._fleet.calls) == fleet_calls_before   # NOT re-dispatched while owed
    assert r.assignments_snapshot()["P1"]["terminal"] == "complete"  # still owed


def test_cancel_failed_print_error_does_not_report_failed(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    snap = _snap("P1", "ERROR", [(1, "FF6A13FF")])
    snap["print_error"] = "50348044"
    d.drain([snap])
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


def test_cancel_failed_hms_does_not_report_failed(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    snap = _snap("P1", "ERROR", [(1, "FF6A13FF")])
    snap["hms_code"] = "0300_400C_0000_0000"
    d.drain([snap])
    assert dpf.failed == []
    assert "P1" not in r.assignments_snapshot()


def test_cancel_failed_idle_snapshot_clears_assignment_without_failure(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "PRINTING", [(1, "FF6A13FF")])])
    snap = _snap("P1", "IDLE", [(1, "FF6A13FF")])
    snap["print_error"] = "50348044"
    d.drain([snap])
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


def test_pre_active_cancel_after_startup_grace_reports_failed_not_requeue(tmp_path):
    r, dpf = _dispatch_one(tmp_path, started_at=100.0)
    now = [100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS - 1]
    d = Dispatcher(r, _FakeFleet(), dpf, now_fn=lambda: now[0])
    snap = _snap("P1", "IDLE", [(1, "FF6A13FF")])
    snap["print_error"] = "50348044"

    d.drain([snap])
    assert "P1" in r.assignments_snapshot()

    now[0] = 100.0 + ASSIGNMENT_STARTUP_GRACE_SECONDS
    d.drain([snap])
    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


def test_sticky_cancel_code_does_not_clear_live_printing_assignment(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    snap = _snap("P1", "PRINTING", [(1, "FF6A13FF")])
    snap["print_error"] = "50348044"
    d.drain([snap])
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" in r.assignments_snapshot()


def test_stop_then_later_finish_does_not_report_complete(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain(
        [_snap("P1", "ERROR", [(1, "FF6A13FF")])],
        desired=[{"bambu_id": "P1", "control": {"id": "c-stop", "action": "stop"}}],
    )
    assert dpf.failed == []
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()
    d.drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])
    assert dpf.completed == []


def test_desired_idle_with_no_queued_match_does_not_dispatch(tmp_path):
    r = Router(str(tmp_path / "queue.json"))  # empty queue
    dpf = _FakeDpf({})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain(
        [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])],
        desired=[{"bambu_id": "P1", "desired_status": "IDLE"}],
    )
    assert fleet.calls == []  # cleared, but nothing queued -> no spurious dispatch
