"""One-shot pause / resume / stop on desired-state (U3)."""
import time

from bridge.app import _handle_desired
from bridge.fleet import Fleet
from bridge.printer import BambuPrinter
from bridge.config import PrinterConfig


class _FakePrinter:
    def __init__(self, bambu_id="P1", stage=None):
        self.bambu_id = bambu_id
        self.stage = stage
        self.calls = []

    def snapshot(self):
        return {"bambu_id": self.bambu_id, "status": "PAUSED", "stage": self.stage}

    def pause_print(self):
        self.calls.append("pause_print")
        return True

    def resume_print(self):
        self.calls.append("resume_print")
        return True

    def stop_print(self):
        self.calls.append("stop_print")
        return True

    def retry_filament_action(self):
        self.calls.append("retry_filament_action")
        return True

    def request_full_status(self):
        self.calls.append("request_full_status")
        return True

    def resume_from_stage(self, stage=None):
        if stage is None:
            stage = self.snapshot().get("stage")
        if stage in {6, 17, 20, 21, 24, 35}:
            self.retry_filament_action()
        return self.resume_print()


class _ControlFleet:
    def __init__(self, printer):
        self._printer = printer

    def by_id(self, bambu_id):
        return self._printer if bambu_id == self._printer.bambu_id else None


class _FakeRouter:
    def __init__(self):
        self.cleared = []

    def clear_assignment(self, bambu_id):
        self.cleared.append(bambu_id)


def _desired(action, control_id="c1", extra=None):
    row = {"bambu_id": "P1", "control": {"id": control_id, "action": action}}
    if extra:
        row.update(extra)
    return [row]


def test_pause_control_publishes_once(tmp_path):
    printer = _FakePrinter()
    fleet = _ControlFleet(printer)
    applied = set()
    desired = _desired("pause")
    _handle_desired(desired, fleet, applied, str(tmp_path))
    _handle_desired(desired, fleet, applied, str(tmp_path))
    assert printer.calls == ["pause_print"]
    assert (tmp_path / "control-c1.applied").exists()


def test_resume_with_runout_retries_filament_then_resumes(tmp_path):
    printer = _FakePrinter(stage=6)
    _handle_desired(_desired("resume"), _ControlFleet(printer), set(), str(tmp_path))
    assert printer.calls == ["retry_filament_action", "resume_print"]


def test_resume_user_pause_skips_filament_retry(tmp_path):
    printer = _FakePrinter(stage=16)
    _handle_desired(_desired("resume"), _ControlFleet(printer), set(), str(tmp_path))
    assert printer.calls == ["resume_print"]


def test_refresh_requests_full_status(tmp_path):
    printer = _FakePrinter()
    _handle_desired(_desired("refresh"), _ControlFleet(printer), set(), str(tmp_path))
    assert printer.calls == ["request_full_status"]


def test_fleet_refresh_asks_the_printer_for_a_full_status(tmp_path):
    printer = _FakePrinter()
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    fleet = Fleet(
        [cfg],
        printer_factory=lambda _cfg, stale_after_seconds=None: printer,
        discover_fn=lambda _timeout: [],
    )
    _handle_desired(_desired("refresh"), fleet, set(), str(tmp_path))
    deadline = time.monotonic() + 1.0
    while printer.calls != ["request_full_status"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert printer.calls == ["request_full_status"]


def test_fleet_refresh_returns_before_the_status_request_finishes(tmp_path):
    """Refresh is queued. The report loop must not sit inside request_full_status."""
    import threading

    started = threading.Event()
    release = threading.Event()

    class _BlockingRefresh(_FakePrinter):
        def request_full_status(self):
            started.set()
            release.wait(2.0)
            self.calls.append("request_full_status")
            return True

    printer = _BlockingRefresh()
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    fleet = Fleet(
        [cfg],
        printer_factory=lambda _cfg, stale_after_seconds=None: printer,
        discover_fn=lambda _timeout: [],
    )
    result = {}

    def run():
        _handle_desired(_desired("refresh"), fleet, result.setdefault("applied", set()), str(tmp_path))
        result["done"] = True

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(1.0)
        deadline = time.monotonic() + 1.0
        while result.get("done") is not True and time.monotonic() < deadline:
            time.sleep(0.01)
        assert result.get("done") is True
        assert not release.is_set()
    finally:
        release.set()
        thread.join(1.0)
    assert not thread.is_alive()
    deadline = time.monotonic() + 1.0
    while printer.calls != ["request_full_status"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert printer.calls == ["request_full_status"]


def test_stop_clears_assignment(tmp_path):
    printer = _FakePrinter()
    router = _FakeRouter()
    _handle_desired(
        _desired("stop"), _ControlFleet(printer), set(), str(tmp_path), router=router,
    )
    assert printer.calls == ["stop_print"]
    assert router.cleared == ["P1"]


def test_unknown_desired_keys_do_not_crash(tmp_path):
    printer = _FakePrinter()
    _handle_desired(
        _desired("pause", extra={"future_key": True, "also": {"nested": 1}}),
        _ControlFleet(printer), set(), str(tmp_path),
    )
    assert printer.calls == ["pause_print"]


def test_false_control_result_is_retried_without_applied_state(tmp_path):
    class _FalsePrinter(_FakePrinter):
        def pause_print(self):
            self.calls.append("pause_print")
            return False

    printer = _FalsePrinter()
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    fleet = Fleet(
        [cfg],
        printer_factory=lambda _cfg, stale_after_seconds=None: printer,
        discover_fn=lambda _timeout: [],
    )
    applied = set()

    _handle_desired(_desired("pause"), fleet, applied, str(tmp_path))

    assert printer.calls == ["pause_print"]
    assert applied == set()
    assert not (tmp_path / "control-c1.applied").exists()


def test_request_full_status_publishes_pushall():
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    printer = BambuPrinter(cfg)

    class _Session:
        def __init__(self):
            self.published = []

        def publish(self, payload):
            self.published.append(payload)
            return True

    printer._session = _Session()
    printer._sleep = lambda _seconds: None
    assert printer.request_full_status() is True
    assert printer._session.published[0]["pushing"]["command"] == "pushall"


def test_start_print_uses_p1_sdcard_url():
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    printer = BambuPrinter(cfg)

    class _Session:
        def __init__(self):
            self.payloads = []

        def publish(self, payload):
            self.payloads.append(payload)
            return True

    printer._session = _Session()
    assert printer.start_print("batch-a.3mf", [0], 1) is True
    payload = printer._session.payloads[0]["print"]
    assert payload["command"] == "project_file"
    assert payload["url"] == "file:///sdcard/batch-a.3mf"


def test_resume_from_stage_on_real_printer_wrapper():
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    printer = BambuPrinter(cfg)

    class _Session:
        def __init__(self):
            self.published = []

        def publish(self, payload):
            self.published.append(payload)
            return True

    printer._session = _Session()
    printer.resume_from_stage(6)
    assert [item["print"]["command"] for item in printer._session.published] == [
        "ams_control", "resume",
    ]
    assert printer._session.published[0]["print"]["param"] == "resume"
    printer._session.published.clear()
    printer.resume_from_stage(16)
    assert [item["print"]["command"] for item in printer._session.published] == ["resume"]
