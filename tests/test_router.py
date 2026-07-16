"""Tests for the durable job queue (U7) and the Dispatcher that drains it (U9).

U7's one guarantee: a job enqueued before a bridge restart is still there after it.
U9's dispatch matches a queued job's required colors to an idle printer's live AMS and
starts it — exercised here with fakes for the fleet (upload+start) and the cloud client
(resolve + report), so the matching, the explicit AMS mapping, restart-safety, and the
failure paths are all proven without a real printer or network.
"""
import json

from bridge.router import Router, Job, Dispatcher, QUEUED, UNRESOLVED, DISPATCHED


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

    def dispatch(self, bambu_id, file_path, ams_mapping, plate_number=1):
        self.calls.append((bambu_id, file_path, list(ams_mapping), plate_number))
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


def test_dispatches_single_color_to_idle_matching_printer(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([
        _snap("P1", "IDLE", [(1, "FF6A13FF"), (2, "1A1A1AFF")]),
    ])
    # tray holding #FF6A13 is slot 1 -> global index 0
    assert fleet.calls == [("P1", "/spool/a.3mf", [0], 1)]
    assert dpf.dispatched == [("B1", "P1")]
    assert r.pending() == []  # removed from the queue on dispatch


def test_routes_to_printer_that_has_all_required_colors(tmp_path):
    r = _router_with_job(tmp_path)
    # pink + black required; printer B has only pink -> must skip B and pick A.
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF69B4", "#000000"]}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([
        _snap("B", "IDLE", [(1, "FF69B4FF"), (2, "FFFFFFFF")]),
        _snap("A", "IDLE", [(1, "FF69B4FF"), (2, "000000FF"), (3, "FFFFFFFF"), (4, "808080FF")]),
    ])
    assert fleet.calls[0][0] == "A"
    # explicit mapping: pink@slot1->0, black@slot2->1
    assert fleet.calls[0][2] == [0, 1]


def test_no_matching_printer_leaves_job_queued(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#00FF00"]}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF0000FF")])])  # only red
    assert fleet.calls == []
    assert dpf.dispatched == []
    assert len(r.pending()) == 1


def test_skips_non_idle_printer_even_with_matching_color(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#00FF00"]}})
    fleet = _FakeFleet()
    # A finished-but-uncleared printer holds the color but is NEEDS_CLEARING, not IDLE
    # (U13 gates dispatch on IDLE) — so it must be skipped.
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "NEEDS_CLEARING", [(1, "00FF00FF")])])
    assert fleet.calls == []
    assert len(r.pending()) == 1


def test_dispatches_when_printer_later_reloads_matching_color(tmp_path):
    # KTD3: matching is against each pass's FRESH snapshot, so a job re-tries and
    # dispatches only once the printer actually reports the required color.
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#00FF00"]}})
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)

    d.drain([_snap("P1", "IDLE", [(1, "FF0000FF")])])   # red loaded — no match
    assert fleet.calls == []
    assert len(r.pending()) == 1

    d.drain([_snap("P1", "IDLE", [(1, "00FF00FF")])])   # green now loaded — dispatches
    assert fleet.calls[0][0] == "P1"
    assert r.pending() == []


def test_failed_start_leaves_job_queued_and_unreported(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet(result=False)  # printer refused the start
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])
    assert len(r.pending()) == 1   # not removed — re-tried next pass
    assert dpf.dispatched == []    # never told the cloud it's printing


def test_transport_exception_leaves_job_queued_and_does_not_crash(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet(raises=True)  # FTPS/MQTT transport error
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])  # must not raise
    assert len(r.pending()) == 1
    assert dpf.dispatched == []


def test_dispatched_job_not_reloaded_after_restart(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    Dispatcher(r, _FakeFleet(), dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])
    # The removal persisted: a fresh Router (bridge restart) does not re-dispatch it.
    assert Router(path).pending() == []


def test_resolution_is_cached_after_first_resolve(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#00FF00"]}})
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)

    d.drain([_snap("P1", "IDLE", [(1, "FF0000FF")])])   # resolves, caches, no color match
    assert dpf.resolve_calls == ["batch-a"]
    assert r.pending()[0].batch_id == "B1"              # cached on the job + persisted

    d.drain([_snap("P1", "IDLE", [(1, "FF0000FF")])])   # cached — no second resolve call
    assert dpf.resolve_calls == ["batch-a"]


def test_unresolved_batch_stays_queued_and_uncached(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({})  # resolve 404s -> {}
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])
    assert fleet.calls == []
    assert len(r.pending()) == 1
    assert r.pending()[0].batch_id is None  # nothing cached, re-tries next pass


def test_unresolved_status_job_is_never_auto_dispatched(tmp_path):
    # A file with no correlatable batch is HELD (UNRESOLVED) for human triage — the
    # dispatcher never routes it, whatever colors an idle printer holds.
    r = _router_with_job(tmp_path, correlation_key=None, status=UNRESOLVED, stored="/spool/x.3mf")
    dpf = _FakeDpf({})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF"), (2, "000000FF")])])
    assert fleet.calls == []
    assert len(r.pending()) == 1


def test_one_job_per_printer_per_pass(tmp_path):
    r = Router(str(tmp_path / "queue.json"))
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    r.enqueue(Job.new("/spool/b.3mf", "batch-b", True, QUEUED, now=2.0))
    dpf = _FakeDpf({
        "batch-a": {"batch_id": "BA", "required_colors": ["#00FF00"]},
        "batch-b": {"batch_id": "BB", "required_colors": ["#00FF00"]},
    })
    fleet = _FakeFleet()
    # One idle green printer, two green jobs -> only one dispatches this pass.
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "00FF00FF")])])
    assert len(fleet.calls) == 1
    assert len(r.pending()) == 1


# --- U9: explicit AMS mapping (R11) -----------------------------------------

def test_ams_mapping_follows_required_color_order_not_slot_order(tmp_path):
    # The mapping is indexed by the sliced file's filament (required-color) order, and
    # each entry is the AMS tray holding that color — NOT the printer's slot order.
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#000000", "#FF69B4"]}})
    fleet = _FakeFleet()
    # black required first, but pink sits in slot 1 and black in slot 2.
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF69B4FF"), (2, "000000FF")])])
    assert fleet.calls[0][2] == [1, 0]  # black -> tray1(slot2), pink -> tray0(slot1)


def test_ams_mapping_uses_first_slot_when_a_color_repeats(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#00FF00"]}})
    fleet = _FakeFleet()
    # green loaded in BOTH slot 2 and slot 4 -> first (slot 2 -> tray 1) wins, deterministic.
    Dispatcher(r, fleet, dpf).drain([
        _snap("P1", "IDLE", [(1, "FF0000FF"), (2, "00FF00FF"), (3, "FFFFFFFF"), (4, "00FF00FF")]),
    ])
    assert fleet.calls[0][2] == [1]


def test_empty_required_colors_matches_any_idle_printer(tmp_path):
    # A batch whose materials carry no parseable color resolves to an empty required set;
    # it can run on any idle printer, with an empty mapping (Bambu auto-maps).
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": []}})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain([_snap("P1", "IDLE", [(1, "FF0000FF")])])
    assert fleet.calls[0][0] == "P1"
    assert fleet.calls[0][2] == []


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
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    dpf = _FailingThenOkDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                            fail_reports=1)
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)
    snaps = [_snap("P1", "IDLE", [(1, "FF6A13FF")])]

    # Pass 1: physical start succeeds, but the report fails -> the job stays DISPATCHED
    # (printing, report owed), NOT removed and NOT re-matchable to a printer.
    d.drain(snaps)
    assert len(fleet.calls) == 1
    held = r.pending()
    assert len(held) == 1
    assert held[0].status == DISPATCHED
    assert held[0].dispatched_to == "P1"
    assert dpf.dispatched == [("B1", "P1")]
    # DISPATCHED state is persisted, so a restart won't re-dispatch the running print.
    assert Router(path).pending()[0].status == DISPATCHED

    # Pass 2: no second physical dispatch; the owed report is retried and now acks -> removed.
    d.drain(snaps)
    assert len(fleet.calls) == 1  # NOT re-dispatched physically
    assert dpf.dispatched == [("B1", "P1"), ("B1", "P1")]  # re-reported
    assert r.pending() == []  # cleared once 3DPF acked


# --- U11: completion / failure detection + durable report --------------------

def _dispatch_one(tmp_path, dpf=None):
    """Dispatch a single job to printer P1 and return (router, dpf) with the assignment
    recorded — the starting point for completion tests."""
    r = _router_with_job(tmp_path)
    dpf = dpf or _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    Dispatcher(r, _FakeFleet(), dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])
    assert r.assignments_snapshot()["P1"]["batch_id"] == "B1"  # assignment recorded
    return r, dpf


def test_finished_print_reports_complete_and_clears_assignment(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    # The printer that was PRINTING now reports NEEDS_CLEARING (Bambu FINISH).
    d.drain([_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])])
    assert dpf.completed == [("B1", None)]
    assert dpf.failed == []
    assert "P1" not in r.assignments_snapshot()  # cleared on ack


def test_failed_print_reports_failed_and_clears_assignment(tmp_path):
    r, dpf = _dispatch_one(tmp_path)
    d = Dispatcher(r, _FakeFleet(), dpf)
    d.drain([_snap("P1", "ERROR", [(1, "FF6A13FF")])])
    assert dpf.failed == [("B1", None)]
    assert dpf.completed == []
    assert "P1" not in r.assignments_snapshot()


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

    d.drain(finished)                 # terminal latched, report FAILS -> stays owed
    assert r.assignments_snapshot()["P1"]["terminal"] == "complete"
    d.drain(finished)                 # retried, now acks -> cleared
    assert "P1" not in r.assignments_snapshot()
    assert dpf.completed == [("B1", None), ("B1", None)]


def test_assignment_and_latch_survive_a_restart(tmp_path):
    path = str(tmp_path / "queue.json")
    r = Router(path)
    r.enqueue(Job.new("/spool/a.3mf", "batch-a", True, QUEUED, now=1.0))
    dpf = _CompleteFailsOnceDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}},
                                fail_completes=1)
    Dispatcher(r, _FakeFleet(), dpf).drain([_snap("P1", "IDLE", [(1, "FF6A13FF")])])
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

def test_desired_idle_resumes_dispatch_to_a_needs_clearing_printer(tmp_path):
    r = _router_with_job(tmp_path)
    dpf = _FakeDpf({"batch-a": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})
    fleet = _FakeFleet()
    d = Dispatcher(r, fleet, dpf)
    # A finished-but-uncleared printer that holds the color: NOT dispatchable yet.
    finished = [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])]
    d.drain(finished)
    assert fleet.calls == []
    assert len(r.pending()) == 1

    # Operator marks it cleared -> 3DPF returns desired_status IDLE -> now dispatchable,
    # matched on the colors its snapshot still reports.
    d.drain(finished, desired=[{"bambu_id": "P1", "desired_status": "IDLE"}])
    assert fleet.calls[0][0] == "P1"


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
    fleet_calls_before = len(d._fleet.calls)
    d.drain(finished, desired=[{"bambu_id": "P1", "desired_status": "IDLE"}])
    assert len(d._fleet.calls) == fleet_calls_before   # NOT re-dispatched while owed
    assert r.assignments_snapshot()["P1"]["terminal"] == "complete"  # still owed


def test_desired_idle_with_no_queued_match_does_not_dispatch(tmp_path):
    r = Router(str(tmp_path / "queue.json"))  # empty queue
    dpf = _FakeDpf({})
    fleet = _FakeFleet()
    Dispatcher(r, fleet, dpf).drain(
        [_snap("P1", "NEEDS_CLEARING", [(1, "FF6A13FF")])],
        desired=[{"bambu_id": "P1", "desired_status": "IDLE"}],
    )
    assert fleet.calls == []  # cleared, but nothing queued -> no spurious dispatch
