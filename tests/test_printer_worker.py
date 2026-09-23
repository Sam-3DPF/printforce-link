"""Per-printer workers: one printer's network I/O must not stall the fleet report."""
import threading
import time

import pytest

from bridge.config import PrinterConfig
from bridge.fleet import Fleet
from bridge.printer import BambuPrinter


def _cfg(serial, ip="10.0.0.5"):
    return PrinterConfig(bambu_id=serial, ip=ip, access_code="x", name=serial)


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return bool(predicate())


class _Member:
    """Fleet member whose upload stands in for a long FTPS store."""

    def __init__(self, cfg, stale_after_seconds=None, upload=None, publish=None):
        self.bambu_id = cfg.bambu_id
        self.current_ip = cfg.ip
        self.is_offline = False
        self._upload = upload
        self._publish = publish
        self.snapshot_calls = 0

    def connect(self):
        return None

    def disconnect(self):
        return None

    def snapshot(self):
        self.snapshot_calls += 1
        if self._publish is not None:
            self._publish()
        return {"bambu_id": self.bambu_id, "status": "IDLE"}

    def upload_file(self, file_path, remote_name=None, cancel=None):
        if self._upload is None:
            return remote_name or "file.3mf"
        return self._upload(file_path, remote_name=remote_name, cancel=cancel)

    def pause_print(self):
        return True

    def request_full_status(self):
        if self._publish is not None:
            self._publish()
        return True


def _fleet(members):
    by_id = {printer.bambu_id: printer for printer in members}

    def factory(cfg, stale_after_seconds=None):
        return by_id[cfg.bambu_id]

    return Fleet(
        [_cfg(printer.bambu_id, printer.current_ip) for printer in members],
        printer_factory=factory,
        discover_fn=lambda _timeout, probe_ips=None: [],
    )


def test_blocked_upload_on_a_does_not_delay_snapshot_of_b(tmp_path):
    """A 120s-class upload on A is an Event wait. B's report must not wait on it."""
    entered = threading.Event()
    release = threading.Event()

    def upload(file_path, remote_name=None, cancel=None):
        entered.set()
        release.wait(30)
        return "a.3mf"

    printer_a = _Member(_cfg("A", "10.0.0.5"), upload=upload)
    printer_b = _Member(_cfg("B", "10.0.0.6"))
    fleet = _fleet([printer_a, printer_b])
    holder = threading.Thread(
        target=lambda: fleet.upload("A", str(tmp_path / "a.3mf")),
        daemon=True,
    )
    holder.start()
    assert entered.wait(1.0)
    done = threading.Event()
    box = {}

    def take():
        box["reports"] = fleet.snapshot()
        done.set()

    threading.Thread(target=take, daemon=True).start()
    try:
        assert done.wait(0.5), "Fleet.snapshot blocked behind printer A's upload"
    finally:
        release.set()
        holder.join(1.0)
    assert {row["bambu_id"] for row in box["reports"]} == {"A", "B"}
    assert printer_b.snapshot_calls == 1


def test_hung_publish_on_a_does_not_block_fleet_snapshot():
    """pushall on A blocks in publish. The fleet report must still return."""
    entered = threading.Event()
    release = threading.Event()

    class _BlockingPublish:
        connected = True

        def publish(self, payload):
            entered.set()
            release.wait(30)
            return True

    class _Quiet:
        connected = True

        def publish(self, payload):
            return True

    def factory(cfg, stale_after_seconds=None):
        return BambuPrinter(cfg, monotonic=lambda: 0.0, sleep=lambda _seconds: None)

    fleet = Fleet(
        [_cfg("A", "10.0.0.5"), _cfg("B", "10.0.0.6")],
        printer_factory=factory,
        discover_fn=lambda _timeout, probe_ips=None: [],
    )
    printer_a = fleet.by_id("A")
    printer_a._session = _BlockingPublish()
    # No AMS unit list, so the live snapshot defers pushall onto A's worker.
    printer_a._on_mqtt_report({"print": {"gcode_state": "IDLE"}})
    printer_b = fleet.by_id("B")
    printer_b._session = _Quiet()
    printer_b._on_mqtt_report({"print": {"gcode_state": "IDLE", "ams": {"ams": []}}})

    done = threading.Event()
    box = {}

    def take():
        box["reports"] = fleet.snapshot()
        done.set()

    threading.Thread(target=take, daemon=True).start()
    try:
        assert done.wait(0.5), "Fleet.snapshot blocked behind printer A's publish"
    finally:
        release.set()
    assert {row["bambu_id"] for row in box["reports"]} == {"A", "B"}


def test_remove_during_upload_cancels_and_does_not_deadlock(tmp_path):
    """remove_printer sets the worker cancel and returns while the upload stops."""
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    second_ran = threading.Event()

    def upload(file_path, remote_name=None, cancel=None):
        from bridge.transfer import UploadCancelled

        entered.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                cancelled.set()
                raise UploadCancelled("printer removed")
            if release.is_set():
                return "a.3mf"
            threading.Event().wait(0.01)
        raise AssertionError("upload was not cancelled")

    printer_a = _Member(_cfg("A"), upload=upload)
    fleet = _fleet([printer_a])
    path = str(tmp_path / "a.3mf")
    first = fleet.submit("A", lambda: fleet.upload("A", path))
    assert entered.wait(1.0)
    second = fleet.submit("A", lambda: second_ran.set())

    finished = threading.Event()

    def remove():
        fleet.remove_printer("A")
        finished.set()

    threading.Thread(target=remove, daemon=True).start()
    try:
        assert finished.wait(1.0), "remove_printer blocked behind the upload"
    finally:
        release.set()
    assert cancelled.wait(1.0)
    assert first is not None
    assert _wait_for(lambda: first.done())
    assert second is not None and second.cancelled()
    assert not second_ran.is_set()
    assert fleet.by_id("A") is None
    assert fleet.submit("A", lambda: None) is None


def test_worker_queue_is_bounded_and_rejects_without_blocking():
    from bridge.printer_worker import PrinterWorker, WorkerBusy

    release = threading.Event()
    entered = threading.Event()
    worker = PrinterWorker("A", queue_size=2)
    try:
        worker.submit(lambda: entered.set() or release.wait(5))
        assert entered.wait(1.0)
        assert worker.submit(lambda: None).done() is False
        assert worker.submit(lambda: None).done() is False
        before = time.monotonic()
        rejected = worker.submit(lambda: None)
        assert time.monotonic() - before < 0.2
        assert rejected.done()
        with pytest.raises(WorkerBusy):
            rejected.result(timeout=0)
        assert worker.busy
    finally:
        release.set()
        worker.stop()


def test_jobs_run_in_submission_order_one_at_a_time():
    from bridge.printer_worker import PrinterWorker

    worker = PrinterWorker("A")
    release_first = threading.Event()
    first_entered = threading.Event()
    active = {"n": 0, "max": 0}
    order = []
    lock = threading.Lock()

    def job(n, gate=None):
        if gate is not None:
            gate.set()
            release_first.wait(2)
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            order.append(n)
        with lock:
            active["n"] -= 1

    try:
        first = worker.submit(lambda: job(1, first_entered))
        assert first_entered.wait(1.0)
        second = worker.submit(lambda: job(2))
        third = worker.submit(lambda: job(3))
        release_first.set()
        assert third.result(timeout=2) is None
        assert second.result(timeout=0) is None
        assert first.result(timeout=0) is None
        assert order == [1, 2, 3]
        assert active["max"] == 1
    finally:
        release_first.set()
        worker.stop()


def test_job_exception_is_captured_and_the_worker_stays_alive():
    from bridge.printer_worker import PrinterWorker

    worker = PrinterWorker("A")
    try:
        def boom():
            raise RuntimeError("nope")

        failed = worker.submit(boom)
        with pytest.raises(RuntimeError, match="nope"):
            failed.result(timeout=1)
        assert worker.submit(lambda: "ok").result(timeout=1) == "ok"
        assert not worker.busy
    finally:
        worker.stop()


def test_running_cloud_send_is_not_enqueued_twice(tmp_path, monkeypatch):
    from bridge.app import _apply_desired

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking(desired, fleet, dpf, spool_dir, started_sends=None, **kwargs):
        calls.append(desired[0]["bambu_id"])
        entered.set()
        release.wait(5)

    monkeypatch.setattr("bridge.app._handle_cloud_sends", blocking)
    fleet = _fleet([_Member(_cfg("A"))])
    desired = [{"bambu_id": "A", "desired_status": "IDLE", "send": {"batch_id": "B1"}}]
    jobs = {}
    readiness = {}

    def apply():
        _apply_desired(
            desired, fleet, object(), str(tmp_path), set(), set(),
            cloud_send_jobs=jobs, legacy_marker_readiness=readiness,
        )

    first = threading.Thread(target=apply, daemon=True)
    first.start()
    assert entered.wait(1.0)
    second_done = threading.Event()

    def apply_again():
        apply()
        second_done.set()

    threading.Thread(target=apply_again, daemon=True).start()
    try:
        assert second_done.wait(0.5), "next loop pass enqueued the cloud send again"
        assert calls == ["A"]
    finally:
        release.set()
        first.join(1.0)
    assert _wait_for(lambda: jobs["A"].done())
    apply()
    assert _wait_for(lambda: calls == ["A", "A"])


def test_each_printer_keeps_its_own_legacy_marker_readiness(tmp_path, monkeypatch):
    from bridge.app import _LegacyMarkerReadiness, _apply_desired

    seen = {}
    both = threading.Event()
    release = threading.Event()

    def record(desired, fleet, dpf, spool_dir, started_sends=None, legacy_marker_readiness=None, **kwargs):
        seen[desired[0]["bambu_id"]] = legacy_marker_readiness
        if len(seen) >= 2:
            both.set()
        release.wait(2)

    monkeypatch.setattr("bridge.app._handle_cloud_sends", record)
    fleet = _fleet([_Member(_cfg("A")), _Member(_cfg("B"))])
    desired = [
        {"bambu_id": "A", "desired_status": "IDLE"},
        {"bambu_id": "B", "desired_status": "IDLE"},
    ]
    readiness = {}
    threading.Thread(
        target=lambda: _apply_desired(
            desired, fleet, object(), str(tmp_path), set(), set(),
            cloud_send_jobs={}, legacy_marker_readiness=readiness,
        ),
        daemon=True,
    ).start()
    try:
        assert both.wait(1.0)
    finally:
        release.set()
    assert isinstance(seen["A"], _LegacyMarkerReadiness)
    assert isinstance(seen["B"], _LegacyMarkerReadiness)
    assert seen["A"] is not seen["B"]


def test_in_flight_worker_counts_as_busy_for_update_restart():
    from bridge.app import _printers_busy

    class _Fleet:
        def __init__(self, busy):
            self._busy = busy

        def worker_busy(self, serial):
            return serial in self._busy

    idle = [{"bambu_id": "A", "status": "IDLE"}, {"bambu_id": "B", "status": "IDLE"}]
    assert _printers_busy(idle, _Fleet({"A"})) is True
    assert _printers_busy(idle, _Fleet(set())) is False
    printing = [{"bambu_id": "A", "status": "PRINTING"}]
    assert _printers_busy(printing, _Fleet(set())) is True


def test_worker_survives_printer_swap_and_resolves_the_current_member():
    seen = []
    release = threading.Event()
    entered = threading.Event()
    printer_a = _Member(_cfg("A", "10.0.0.5"))
    fleet = _fleet([printer_a])
    replacement = _Member(_cfg("A", "10.0.0.9"))

    def job():
        entered.set()
        release.wait(2)
        seen.append(fleet.by_id("A"))

    fut = fleet.submit("A", job)
    assert entered.wait(1.0)
    with fleet._lock:
        fleet._printers[0] = replacement
    release.set()
    assert fut.result(timeout=1) is None
    assert seen == [replacement]
    assert fleet.worker_busy("A") is False


class _QuietSession:
    connected = True
    had_session = True
    state = "live"
    down_reason = None

    def __init__(self):
        self.published = []

    def start(self):
        return None

    def disconnect(self):
        return None

    def publish(self, payload):
        self.published.append(payload)
        return True


def _printer_waiting_on_ams():
    session = _QuietSession()
    printer = BambuPrinter(
        _cfg("S1"),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        session_factory=lambda *_args: session,
    )
    printer.connect()
    # A report with no AMS unit list: every snapshot wants a pushall.
    printer._on_mqtt_report({"print": {"gcode_state": "IDLE", "nozzle_temper": 25.0}})
    return printer


def test_snapshot_defers_one_ams_refresh_at_a_time():
    from concurrent.futures import Future

    printer = _printer_waiting_on_ams()
    queued = []

    def defer(fn):
        queued.append(fn)
        return Future()

    printer.set_defer(defer)
    printer.snapshot()
    printer.snapshot()
    printer.snapshot()
    assert len(queued) == 1

    queued[0]()
    printer.snapshot()
    assert len(queued) == 2


def test_a_rejected_ams_refresh_does_not_block_the_next_one():
    from concurrent.futures import Future

    from bridge.printer_worker import WorkerBusy

    printer = _printer_waiting_on_ams()
    attempts = []

    def defer(fn):
        attempts.append(fn)
        future = Future()
        future.set_exception(WorkerBusy("full"))
        return future

    printer.set_defer(defer)
    printer.snapshot()
    printer.snapshot()
    assert len(attempts) == 2
