"""One-shot pause / resume / stop on desired-state (U3)."""
import logging
import threading
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


def test_collect_log_uploads_inline_when_the_fleet_has_no_worker(tmp_path):
    seen = []

    class _Printer(_FakePrinter):
        def collect_log(self):
            return {"serial": "P1", "messages": [], "events": [{"kind": "connect"}]}

    class _Dpf:
        def upload_printer_log(self, bambu_id, log, control_id=None):
            seen.append((bambu_id, log, control_id))
            return {"stored": 1}

    printer = _Printer()
    dpf = _Dpf()
    applied = set()
    _handle_desired(
        _desired("collect_log"), _ControlFleet(printer), applied, str(tmp_path), dpf=dpf,
    )
    _handle_desired(
        _desired("collect_log"), _ControlFleet(printer), applied, str(tmp_path), dpf=dpf,
    )
    assert seen == [("P1", {"serial": "P1", "messages": [], "events": [{"kind": "connect"}]}, "c1")]
    assert applied == {"c1"}
    assert (tmp_path / "control-c1.applied").exists()


def test_collect_log_uploads_off_the_report_loop(tmp_path):
    started = threading.Event()
    release = threading.Event()
    uploads = []
    result = {}

    class _Printer(_FakePrinter):
        def collect_log(self):
            return {"serial": "P1", "messages": [{"direction": "in"}], "events": []}

    class _Dpf:
        def upload_printer_log(self, bambu_id, log, control_id=None):
            uploads.append((threading.get_ident(), bambu_id, log, control_id))
            started.set()
            release.wait(2.0)
            return {"ok": True}

    printer = _Printer()
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    fleet = Fleet(
        [cfg],
        printer_factory=lambda _cfg, stale_after_seconds=None: printer,
        discover_fn=lambda _timeout: [],
    )

    def run():
        result["loop"] = threading.get_ident()
        _handle_desired(
            _desired("collect_log"),
            fleet,
            result.setdefault("applied", set()),
            str(tmp_path),
            dpf=_Dpf(),
        )
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
        assert (tmp_path / "control-c1.applied").exists()
    finally:
        release.set()
        thread.join(2.0)
    assert not thread.is_alive()
    assert uploads
    assert uploads[0][0] != result["loop"]
    assert uploads[0][1:] == (
        "P1",
        {"serial": "P1", "messages": [{"direction": "in"}], "events": []},
        "c1",
    )


def test_collect_log_is_retried_when_the_upload_returns_nothing(tmp_path):
    calls = {"n": 0}

    class _Printer(_FakePrinter):
        def collect_log(self):
            return {"serial": "P1", "messages": [], "events": []}

    class _Dpf:
        def upload_printer_log(self, bambu_id, log, control_id=None):
            calls["n"] += 1
            return {}

    printer = _Printer()
    applied = set()
    desired = _desired("collect_log")
    _handle_desired(desired, _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf())
    _handle_desired(desired, _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf())
    assert calls["n"] == 2
    assert applied == set()
    assert not (tmp_path / "control-c1.applied").exists()


def test_collect_log_without_the_method_warns_and_is_consumed(tmp_path, caplog):
    printer = _FakePrinter()
    applied = set()
    with caplog.at_level(logging.WARNING, logger="bridge.app"):
        _handle_desired(
            _desired("collect_log"), _ControlFleet(printer), applied, str(tmp_path), dpf=object(),
        )
    assert printer.calls == []
    assert "collect_log" in caplog.text
    assert applied == {"c1"}
    assert (tmp_path / "control-c1.applied").exists()


def test_collect_log_without_an_uploader_is_not_marked(tmp_path, caplog):
    class _Printer(_FakePrinter):
        def collect_log(self):
            return {"serial": "P1", "messages": [], "events": []}

    printer = _Printer()
    applied = set()
    with caplog.at_level(logging.WARNING, logger="bridge.app"):
        _handle_desired(
            _desired("collect_log"), _ControlFleet(printer), applied, str(tmp_path),
        )
    assert applied == set()
    assert not (tmp_path / "control-c1.applied").exists()
    assert "collect_log" in caplog.text


def test_diagnose_posts_once_when_the_fleet_has_no_worker(tmp_path, monkeypatch):
    seen = []
    secret = "access-code-SHOULD-NOT-LEAK-9f3a"

    class _Printer(_FakePrinter):
        def __init__(self):
            super().__init__()
            self.current_ip = "10.0.0.9"

        def diagnose(self, trigger="operator"):
            return fake_run(self.current_ip, self.bambu_id, secret, trigger=trigger)

    class _Dpf:
        def report_diagnostic(self, bambu_id, diagnostic, control_id=None):
            seen.append((bambu_id, diagnostic, control_id))
            return {"stored": 1}

    def fake_run(ip, serial, access_code, **kwargs):
        return {
            "bambu_id": serial,
            "ip": ip,
            "ran_at": "2026-01-01T00:00:00.000000Z",
            "trigger": kwargs.get("trigger"),
            "overall": "ok",
            "checks": [],
        }

    printer = _Printer()
    applied = set()
    _handle_desired(
        _desired("diagnose"), _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf(),
    )
    _handle_desired(
        _desired("diagnose"), _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf(),
    )
    assert len(seen) == 1
    assert seen[0][0] == "P1"
    assert seen[0][1]["trigger"] == "operator"
    assert seen[0][1]["overall"] == "ok"
    assert seen[0][2] == "c1"
    assert secret not in str(seen)
    assert applied == {"c1"}
    assert (tmp_path / "control-c1.applied").exists()


def test_diagnose_runs_off_the_report_loop(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    posts = []
    result = {}
    secret = "access-code-SHOULD-NOT-LEAK-9f3a"

    class _Printer(_FakePrinter):
        def __init__(self):
            super().__init__()
            self.current_ip = "10.0.0.9"

        def diagnose(self, trigger="operator"):
            return fake_run(self.current_ip, self.bambu_id, secret, trigger=trigger)

    class _Dpf:
        def report_diagnostic(self, bambu_id, diagnostic, control_id=None):
            posts.append((threading.get_ident(), bambu_id, diagnostic, control_id))
            return {"ok": True}

    def fake_run(ip, serial, access_code, **kwargs):
        result["run_thread"] = threading.get_ident()
        started.set()
        release.wait(2.0)
        return {
            "bambu_id": serial,
            "ip": ip,
            "ran_at": "2026-01-01T00:00:00.000000Z",
            "trigger": "operator",
            "overall": "ok",
            "checks": [],
        }

    printer = _Printer()
    cfg = PrinterConfig(bambu_id="P1", ip="10.0.0.5", access_code="x", name="P1S")
    fleet = Fleet(
        [cfg],
        printer_factory=lambda _cfg, stale_after_seconds=None: printer,
        discover_fn=lambda _timeout: [],
    )

    def run():
        result["loop"] = threading.get_ident()
        _handle_desired(
            _desired("diagnose"),
            fleet,
            result.setdefault("applied", set()),
            str(tmp_path),
            dpf=_Dpf(),
        )
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
        assert (tmp_path / "control-c1.applied").exists()
    finally:
        release.set()
        thread.join(2.0)
    assert not thread.is_alive()
    assert result["run_thread"] != result["loop"]
    assert posts == [] or posts[0][0] != result["loop"]
    deadline = time.monotonic() + 1.0
    while not posts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert posts[0][0] != result["loop"]
    assert posts[0][1] == "P1"
    assert posts[0][3] == "c1"
    assert secret not in str(posts)


def test_diagnose_is_retried_when_the_report_returns_nothing(tmp_path, monkeypatch):
    calls = {"n": 0}

    class _Printer(_FakePrinter):
        def __init__(self):
            super().__init__()
            self.current_ip = "10.0.0.9"

        def diagnose(self, trigger="operator"):
            return fake_run(trigger=trigger)

    class _Dpf:
        def report_diagnostic(self, bambu_id, diagnostic, control_id=None):
            calls["n"] += 1
            return {}

    def fake_run(**kwargs):
        return {"overall": "ok", "checks": [], "trigger": "operator"}

    printer = _Printer()
    applied = set()
    desired = _desired("diagnose")
    _handle_desired(desired, _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf())
    _handle_desired(desired, _ControlFleet(printer), applied, str(tmp_path), dpf=_Dpf())
    assert calls["n"] == 2
    assert applied == set()
    assert not (tmp_path / "control-c1.applied").exists()


def test_diagnose_without_a_reporter_is_not_marked(tmp_path, caplog, monkeypatch):
    class _Printer(_FakePrinter):
        def __init__(self):
            super().__init__()
            self.current_ip = "10.0.0.9"

        def diagnose(self, trigger="operator"):
            return fake_run(trigger=trigger)

    def fake_run(**kwargs):
        return {"overall": "ok"}

    printer = _Printer()
    applied = set()
    with caplog.at_level(logging.WARNING, logger="bridge.app"):
        _handle_desired(
            _desired("diagnose"), _ControlFleet(printer), applied, str(tmp_path),
        )
    assert applied == set()
    assert not (tmp_path / "control-c1.applied").exists()
    assert "diagnose" in caplog.text


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
