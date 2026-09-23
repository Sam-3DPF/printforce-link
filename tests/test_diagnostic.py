"""Connection diagnostic: fixed-order checks, no FTP login, no live-session reset."""
import json
import ssl
import socket

import pytest

from bridge.bambu.diagnostic import run_connection_diagnostic
from bridge.bambu.session import LinkSession
from bridge.config import PrinterConfig


_SECRET = "access-code-SHOULD-NOT-LEAK-9f3a"
_IDS = ("port_mqtt", "port_ftps", "mqtt_auth", "reports", "commands", "subnet")


class Clock:
    """Millisecond clock. ``sleep`` advances it so a 10s wait does not block."""

    def __init__(self):
        self._ms = 0
        self.pump = None

    @property
    def now(self):
        return self._ms / 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self._ms += int(round(float(seconds) * 1000))
        if self.pump is not None:
            self.pump()


class _Sock:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def sendall(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True


class _Log:
    def __init__(self):
        self.events = []

    def record_event(self, kind, **fields):
        self.events.append((kind, dict(fields)))


class _ProdSession:
    def __init__(self):
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1


class FakeSession:
    """CONNACK, reports, and probe replies follow the injected clock."""

    def __init__(self, clock, *, connack_at=0.0, error=None, report_at=0.0,
                 probe_reply_after=None):
        self.clock = clock
        self.connack_at = connack_at
        self.error = error
        self.report_at = report_at
        self.probe_reply_after = probe_reply_after
        self.on_report = None
        self.disconnected = False
        self.probe_calls = []
        self._started = False
        self._reported = False
        self._probe_at = None
        self._probe_replied = False

    def start(self):
        self._started = True

    def disconnect(self):
        self.disconnected = True

    def pump(self):
        if not self._started or self.disconnected:
            return
        now = self.clock.now
        if (
            self.error is None
            and self.report_at is not None
            and not self._reported
            and now >= self.report_at
            and now >= self.connack_at
        ):
            self._reported = True
            if self.on_report is not None:
                self.on_report({"print": {"gcode_state": "IDLE"}})
        if (
            self._probe_at is not None
            and not self._probe_replied
            and self.probe_reply_after is not None
            and now >= self._probe_at + self.probe_reply_after
        ):
            self._probe_replied = True
            if self.on_report is not None:
                self.on_report({
                    "info": {"command": "get_version", "sequence_id": self.probe_calls[-1]},
                })

    @property
    def connected(self):
        return self._started and self.error is None and self.clock.now >= self.connack_at

    @property
    def last_connect_error(self):
        if self._started and self.error and self.clock.now >= self.connack_at:
            return self.error
        return None

    def probe(self):
        seq = str(len(self.probe_calls) + 1)
        self.probe_calls.append(seq)
        self._probe_at = self.clock.now
        return seq


def _printer(secret=_SECRET):
    printer = type("Printer", (), {})()
    printer.bambu_id = "P1"
    printer.current_ip = "10.0.0.9"
    printer._cfg = PrinterConfig("P1", "10.0.0.9", secret, "P")
    printer.log = _Log()
    printer._session = _ProdSession()

    def diagnose(trigger="operator"):
        import bridge.printer as printer_module

        return printer_module.run_connection_diagnostic(
            printer.current_ip, printer.bambu_id, secret, printer=printer, trigger=trigger,
        )

    printer.diagnose = diagnose
    return printer


def _diagnose(*, printer_ip="10.0.0.9", local_ip="10.0.0.2", mqtt="open",
              ftps="tls", session=None, session_factory=None, printer=None,
              command_probe=None, trigger="operator", secret=_SECRET,
              clock=None, local_raises=False):
    clock = clock or Clock()
    tcp_calls = []
    tls_calls = []
    sockets = []
    sessions = []

    def tcp_connect(ip, port, timeout):
        tcp_calls.append((ip, port, timeout))
        if port == 8883 and mqtt == "refused":
            raise ConnectionRefusedError("closed")
        if port == 8883 and mqtt == "timeout":
            raise TimeoutError("timed out")
        if port == 990 and ftps == "closed":
            raise ConnectionRefusedError("closed")
        if port == 990 and ftps == "timeout":
            raise TimeoutError("timed out")
        sock = _Sock()
        sockets.append(sock)
        return sock

    def tls_handshake(sock, timeout):
        tls_calls.append(timeout)
        if ftps == "no_tls":
            raise ssl.SSLError("plaintext")
        return sock

    def factory(host, access_code, serial, *, on_report, command_probe, log):
        if session is None:
            made = FakeSession(clock)
        else:
            made = session
            made.clock = clock
        made.on_report = on_report
        clock.pump = made.pump
        sessions.append((made, access_code, command_probe, log))
        return made

    def local_ip_for(ip):
        if local_raises:
            raise OSError("no route")
        return local_ip

    result = run_connection_diagnostic(
        printer_ip,
        "P1",
        secret,
        printer=printer,
        tcp_connect=tcp_connect,
        tls_handshake=tls_handshake,
        session_factory=session_factory or factory,
        local_ip_for=local_ip_for,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        command_probe=command_probe,
        trigger=trigger,
    )
    return result, clock, {
        "tcp": tcp_calls,
        "tls": tls_calls,
        "sockets": sockets,
        "sessions": sessions,
    }


def _by_id(result):
    assert [item["id"] for item in result["checks"]] == list(_IDS)
    found = {}
    for item in result["checks"]:
        assert item["result"] in {"pass", "fail", "warn", "skip"}
        assert isinstance(item["cause"], str) and item["cause"]
        assert isinstance(item["detail"], dict)
        found[item["id"]] = item
    assert _SECRET not in json.dumps(result)
    return found


def test_closed_mqtt_port_fails_and_skips_the_mqtt_checks():
    printer = _printer()
    result, clock, extra = _diagnose(mqtt="refused", printer=printer)
    checks = _by_id(result)
    assert checks["port_mqtt"]["result"] == "fail"
    assert checks["port_mqtt"]["detail"]["reason"] == "closed"
    for check_id in ("mqtt_auth", "reports", "commands"):
        assert checks[check_id]["result"] == "skip"
        assert "8883" in checks[check_id]["cause"]
    assert extra["sessions"] == []
    assert clock.now < 1
    assert result["overall"] == "problems"
    assert ("diagnostic_start", {"trigger": "operator"}) in printer.log.events
    result_event = printer.log.events[-1]
    assert result_event[0] == "diagnostic_result"
    assert result_event[1]["overall"] == "problems"
    assert "port_mqtt" in result_event[1]["failing"]
    assert printer._session.disconnects == 0


def test_mqtt_port_timeout_is_the_same_closed_failure():
    result, _clock, extra = _diagnose(mqtt="timeout")
    checks = _by_id(result)
    assert checks["port_mqtt"]["result"] == "fail"
    assert checks["port_mqtt"]["detail"]["reason"] == "closed"
    assert extra["sessions"] == []
    assert extra["tcp"][0][1:] == (8883, 3)


def test_ftps_plaintext_warns_no_tls():
    result, _clock, extra = _diagnose(ftps="no_tls")
    checks = _by_id(result)
    assert checks["port_ftps"]["result"] == "warn"
    assert checks["port_ftps"]["detail"]["reason"] == "no_tls"
    assert "TLS" in checks["port_ftps"]["cause"] or "tls" in checks["port_ftps"]["cause"].lower()
    assert extra["tls"] == [3]
    assert result["overall"] == "warnings"
    assert checks["port_mqtt"]["result"] == "pass"
    assert checks["mqtt_auth"]["result"] == "pass"


@pytest.mark.parametrize("ftps", ["closed", "timeout"])
def test_ftps_refused_or_timeout_warns_closed(ftps):
    result, _clock, extra = _diagnose(ftps=ftps)
    checks = _by_id(result)
    assert checks["port_ftps"]["result"] == "warn"
    assert checks["port_ftps"]["detail"]["reason"] == "closed"
    assert extra["tls"] == []
    assert result["overall"] == "warnings"


class _Paho:
    def __init__(self):
        self.on_connect = None
        self.disconnects = 0

    def username_pw_set(self, username, password=None):
        pass

    def tls_set_context(self, context):
        pass

    def tls_insecure_set(self, value):
        pass

    def max_inflight_messages_set(self, inflight):
        pass

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def connect_async(self, host, port=1883, keepalive=60, **kwargs):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.disconnects += 1

    def subscribe(self, topic, qos=0):
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        class Info:
            rc = 0
        return Info()


def test_connack_135_fails_mqtt_auth_as_auth_rejected():
    closed = []

    def factory(host, access_code, serial, *, on_report, command_probe, log):
        fake = _Paho()
        session = LinkSession(
            host, access_code, serial,
            client_factory=lambda _cid: fake,
            on_report=on_report,
            command_probe=command_probe,
            watchdog_interval=None,
            log=log,
        )
        start = session.start
        disconnect = session.disconnect

        def wrapped_start():
            start()
            fake.fire_connack = None
            session._on_connect(fake, None, None, 135, None)

        def wrapped_disconnect():
            closed.append(True)
            disconnect()

        session.start = wrapped_start
        session.disconnect = wrapped_disconnect
        return session

    # _on_connect is the CONNACK path. Drive it with code 135 directly so the
    # test does not depend on a paho helper that this file would have to copy.
    result, clock, _extra = _diagnose(session_factory=factory)
    checks = _by_id(result)
    assert checks["mqtt_auth"]["result"] == "fail"
    assert checks["mqtt_auth"]["detail"]["reason"] == "auth_rejected"
    assert checks["reports"]["result"] == "skip"
    assert clock.now < 1
    assert closed == [True]
    assert result["overall"] == "problems"


def test_other_connack_refusal_fails_as_refused():
    session = FakeSession(Clock(), error="refused")
    result, clock, _extra = _diagnose(session=session)
    checks = _by_id(result)
    assert checks["mqtt_auth"]["result"] == "fail"
    assert checks["mqtt_auth"]["detail"]["reason"] == "refused"
    assert checks["reports"]["result"] == "skip"
    assert clock.now < 1
    assert session.disconnected is True


def test_no_connack_within_10s_fails_mqtt_auth():
    session = FakeSession(Clock(), connack_at=10**9, report_at=None)
    result, clock, _extra = _diagnose(session=session)
    checks = _by_id(result)
    assert checks["mqtt_auth"]["result"] == "fail"
    assert checks["mqtt_auth"]["detail"]["reason"] == "no_connack"
    assert checks["reports"]["result"] == "skip"
    assert clock.now >= 10
    assert clock.now < 11
    assert session.disconnected is True


def test_zero_reports_after_10s_fails_as_a_likely_wrong_serial():
    session = FakeSession(Clock(), report_at=None)
    result, clock, _extra = _diagnose(session=session)
    checks = _by_id(result)
    assert checks["mqtt_auth"]["result"] == "pass"
    assert checks["reports"]["result"] == "fail"
    assert checks["reports"]["detail"]["reason"] == "no_reports"
    assert "serial" in checks["reports"]["cause"].lower()
    assert clock.now >= 10
    assert clock.now < 11
    assert result["overall"] == "problems"
    assert session.disconnected is True


def test_a_young_session_is_not_reported_as_a_wrong_serial():
    """The 10s report window starts at CONNACK, not at the start of the run.

    A handshake that lands at 9s and a report at 18s is still inside the window.
    Measuring 10s from the start of the run would call this a wrong serial.
    """
    session = FakeSession(Clock(), connack_at=9.0, report_at=18.0)
    result, clock, _extra = _diagnose(session=session)
    checks = _by_id(result)
    assert checks["reports"]["result"] == "pass"
    assert "serial" not in checks["reports"]["cause"].lower()
    assert clock.now >= 18
    assert clock.now < 19


def test_commands_rejected_fails_a_clean_flag_passes_and_none_skips():
    rejected = _printer()
    rejected.commands_rejected = True
    failed, _clock, _extra = _diagnose(printer=rejected)
    failed_checks = _by_id(failed)
    assert failed_checks["commands"]["result"] == "fail"
    assert failed_checks["commands"]["detail"]["reason"] == "commands_rejected"
    assert failed["overall"] == "problems"

    clean = _printer()
    clean.commands_rejected = False
    passed, _clock, _extra = _diagnose(printer=clean)
    passed_checks = _by_id(passed)
    assert passed_checks["commands"]["result"] == "pass"
    assert passed["overall"] == "ok"

    missing, _clock, extra = _diagnose(printer=_printer())
    missing_checks = _by_id(missing)
    assert missing_checks["commands"]["result"] == "skip"
    assert missing_checks["commands"]["cause"] == "command check not available yet"
    assert extra["sessions"][0][0].probe_calls == []


def test_an_unanswered_probe_fails_commands_as_commands_ignored():
    session = FakeSession(Clock(), probe_reply_after=None)
    result, clock, extra = _diagnose(session=session, command_probe=True)
    checks = _by_id(result)
    assert checks["reports"]["result"] == "pass"
    assert checks["commands"]["result"] == "fail"
    assert checks["commands"]["detail"]["reason"] == "commands_ignored"
    assert extra["sessions"][0][0].probe_calls == ["1"]
    assert extra["sessions"][0][2] is True
    assert clock.now >= 10
    assert result["overall"] == "problems"


def test_a_probe_reply_passes_the_command_check():
    session = FakeSession(Clock(), probe_reply_after=0.0)
    result, clock, extra = _diagnose(session=session, command_probe=True)
    checks = _by_id(result)
    assert checks["commands"]["result"] == "pass"
    assert extra["sessions"][0][0].probe_calls == ["1"]
    assert clock.now < 5


def test_a_different_subnet_warns_and_an_unknown_one_skips():
    warned, _clock, _extra = _diagnose(printer_ip="10.0.1.5", local_ip="10.0.0.4")
    checks = _by_id(warned)
    assert checks["subnet"]["result"] == "warn"
    assert checks["subnet"]["detail"]["reason"] == "subnet"
    assert checks["subnet"]["detail"]["printer"] == "10.0.1.5"
    assert checks["subnet"]["detail"]["local"] == "10.0.0.4"
    assert warned["overall"] == "warnings"

    skipped, _clock, _extra = _diagnose(local_ip=None)
    skipped_checks = _by_id(skipped)
    assert skipped_checks["subnet"]["result"] == "skip"

    raised, _clock, _extra = _diagnose(local_raises=True)
    assert _by_id(raised)["subnet"]["result"] == "skip"


def test_overall_rolls_up_ok_warnings_or_problems():
    clean = _printer()
    clean.commands_rejected = False
    ok, _clock, _extra = _diagnose(printer=clean)
    assert _by_id(ok)
    assert ok["overall"] == "ok"
    assert ok["trigger"] == "operator"
    assert ok["bambu_id"] == "P1"
    assert ok["ip"] == "10.0.0.9"
    assert ok["ran_at"].endswith("Z")

    warned, _clock, _extra = _diagnose(ftps="closed", printer=clean)
    assert warned["overall"] == "warnings"

    both = _printer()
    both.commands_rejected = True
    problems, _clock, _extra = _diagnose(ftps="no_tls", printer=both)
    assert problems["overall"] == "problems"


def test_the_diagnostic_never_sends_ftp_user_or_pass(monkeypatch):
    writes = []
    handshakes = []

    class Raw(_Sock):
        def send(self, data):
            writes.append(bytes(data))
            return len(data)

        def sendall(self, data):
            writes.append(bytes(data))

    class Wrapped:
        def __init__(self):
            self.handshook = False
            self.closed = False

        def do_handshake(self):
            self.handshook = True
            handshakes.append(True)

        def send(self, data):
            writes.append(bytes(data))
            return len(data)

        def sendall(self, data):
            writes.append(bytes(data))

        def close(self):
            self.closed = True

    def wrap(self, sock, server_side=False, do_handshake_on_connect=True,
             suppress_ragged_eofs=True, server_hostname=None, session=None):
        assert do_handshake_on_connect is False
        assert self.verify_mode == ssl.CERT_NONE
        assert self.check_hostname is False
        return Wrapped()

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)

    def tcp_connect(ip, port, timeout):
        assert timeout == 3
        return Raw()

    result, _clock, _extra = _diagnose_with_tcp(tcp_connect)
    checks = _by_id(result)
    assert checks["port_ftps"]["result"] == "pass"
    assert handshakes == [True]
    blob = b"".join(writes)
    assert b"USER" not in blob
    assert b"PASS" not in blob
    assert writes == []


def _diagnose_with_tcp(tcp_connect):
    """FTPS uses the real handshake helper. Everything else stays injected."""
    clock = Clock()
    session = FakeSession(clock)

    def factory(host, access_code, serial, *, on_report, command_probe, log):
        session.on_report = on_report
        clock.pump = session.pump
        return session

    result = run_connection_diagnostic(
        "10.0.0.9",
        "P1",
        _SECRET,
        tcp_connect=tcp_connect,
        session_factory=factory,
        local_ip_for=lambda ip: "10.0.0.2",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        command_probe=False,
    )
    return result, clock, session


def test_local_ip_for_uses_udp_and_sends_nothing(monkeypatch):
    from bridge.bambu.diagnostic import _local_ip_for

    sent = []

    class Sock:
        def __init__(self, *args, **kwargs):
            self.addr = None

        def connect(self, addr):
            self.addr = addr

        def getsockname(self):
            return ("10.0.0.2", 55555)

        def send(self, data):
            sent.append(bytes(data))

        def sendto(self, data, addr):
            sent.append(bytes(data))

        def close(self):
            pass

    created = []

    def _socket(*args, **kwargs):
        sock = Sock()
        created.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", _socket)
    assert _local_ip_for("10.0.0.9") == "10.0.0.2"
    assert sent == []
    assert created[0].addr == ("10.0.0.9", 8883)


def test_the_access_code_never_appears_in_the_result():
    printer = _printer()
    result, _clock, extra = _diagnose(printer=printer)
    blob = json.dumps({"result": result, "events": printer.log.events})
    assert _SECRET not in blob
    assert extra["sessions"][0][1] == _SECRET
    assert extra["sessions"][0][3] is printer.log
    assert result["trigger"] == "operator"
    assert printer._session.disconnects == 0
    assert extra["sessions"][0][0].disconnected is True


def test_temporary_session_does_not_start_a_watchdog(monkeypatch):
    captured = {}

    class Capture:
        def __init__(self, host, access_code, serial, **kwargs):
            captured["args"] = (host, access_code, serial)
            captured["kwargs"] = kwargs
            self.connected = False
            self.last_connect_error = None

        def start(self):
            self.connected = True
            callback = captured["kwargs"].get("on_report")
            if callback is not None:
                callback({"print": {"gcode_state": "IDLE"}})

        def disconnect(self):
            captured["disconnected"] = True

    monkeypatch.setattr("bridge.bambu.diagnostic.LinkSession", Capture)
    clock = Clock()
    result = run_connection_diagnostic(
        "10.0.0.9",
        "P1",
        _SECRET,
        tcp_connect=lambda ip, port, timeout: _Sock(),
        tls_handshake=lambda sock, timeout: sock,
        local_ip_for=lambda ip: "10.0.0.2",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        printer=_printer(),
    )
    assert _by_id(result)["mqtt_auth"]["result"] == "pass"
    assert captured["kwargs"]["watchdog_interval"] is None
    assert captured["kwargs"]["log"] is not None
    assert captured["disconnected"] is True
    assert _SECRET not in json.dumps(result)


class _TriggerClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _auto_world():
    from bridge.app import _OfflineDiagnosticTrigger

    printer = _printer()
    runs = []
    posts = []
    queued = []

    class _Fleet:
        def __init__(self):
            self.busy = False

        def worker_busy(self, serial):
            return self.busy

        def by_id(self, serial):
            return printer if serial == printer.bambu_id else None

        def submit(self, serial, fn, *args, **kwargs):
            from concurrent.futures import Future

            queued.append((serial, fn))
            future = Future()
            return future

    fleet = _Fleet()
    clock = _TriggerClock()
    trigger = _OfflineDiagnosticTrigger(monotonic=clock)
    return trigger, clock, fleet, printer, runs, posts, queued


def _patch_run(monkeypatch, runs):
    def fake_run(ip, serial, access_code, **kwargs):
        runs.append({
            "ip": ip,
            "serial": serial,
            "trigger": kwargs.get("trigger"),
            "access_code": access_code,
        })
        return {
            "bambu_id": serial,
            "ip": ip,
            "ran_at": "2026-01-01T00:00:00.000000Z",
            "trigger": kwargs.get("trigger"),
            "overall": "ok",
            "checks": [],
        }

    monkeypatch.setattr("bridge.printer.run_connection_diagnostic", fake_run)


def test_auto_offline_runs_once_per_spell(monkeypatch):
    trigger, clock, fleet, printer, runs, posts, queued = _auto_world()
    _patch_run(monkeypatch, runs)

    def tick(status, at):
        clock.now = at
        trigger.consider(
            [{"bambu_id": printer.bambu_id, "status": status}], fleet, _Dpf_from(posts),
        )

    tick("OFFLINE", 0)
    tick("OFFLINE", 300)
    assert queued == []
    tick("OFFLINE", 301)
    assert len(queued) == 1
    assert runs == []
    queued[0][1]()
    assert runs[0]["trigger"] == "auto_offline"
    assert posts[-1][2] is None
    assert _SECRET not in str(posts)
    tick("OFFLINE", 10000)
    assert len(queued) == 1
    tick("IDLE", 10001)
    tick("OFFLINE", 10001)
    assert len(queued) == 1
    tick("OFFLINE", 10001 + 301)
    assert len(queued) == 2
    queued[1][1]()
    assert [item["trigger"] for item in runs] == ["auto_offline", "auto_offline"]


def _Dpf_from(posts):
    class _Dpf:
        def report_diagnostic(self, bambu_id, diagnostic, control_id=None):
            posts.append((bambu_id, diagnostic, control_id))
            return {"ok": True}

    return _Dpf()


def test_auto_offline_skips_a_busy_worker_for_this_pass(monkeypatch):
    trigger, clock, fleet, printer, runs, posts, queued = _auto_world()
    _patch_run(monkeypatch, runs)
    clock.now = 0
    trigger.consider([{"bambu_id": "P1", "status": "OFFLINE"}], fleet, _Dpf_from(posts))
    fleet.busy = True
    clock.now = 301
    trigger.consider([{"bambu_id": "P1", "status": "OFFLINE"}], fleet, _Dpf_from(posts))
    assert queued == []
    fleet.busy = False
    clock.now = 302
    trigger.consider([{"bambu_id": "P1", "status": "OFFLINE"}], fleet, _Dpf_from(posts))
    assert len(queued) == 1
    clock.now = 5000
    trigger.consider([{"bambu_id": "P1", "status": "OFFLINE"}], fleet, _Dpf_from(posts))
    assert len(queued) == 1


def test_printer_diagnose_checks_the_address_it_is_dialing(monkeypatch):
    from bridge.config import PrinterConfig
    from bridge.printer import BambuPrinter

    calls = []

    def fake_run(ip, serial, access_code, **kwargs):
        calls.append((ip, serial, access_code, kwargs))
        return {"overall": "ok"}

    monkeypatch.setattr("bridge.printer.run_connection_diagnostic", fake_run)
    printer = BambuPrinter(
        PrinterConfig(bambu_id="S9", ip="10.0.0.42", access_code="code-1234", name="P"),
    )
    assert printer.diagnose(trigger="auto_offline") == {"overall": "ok"}
    ip, serial, access_code, kwargs = calls[0]
    assert (ip, serial, access_code) == ("10.0.0.42", "S9", "code-1234")
    assert kwargs["printer"] is printer
    assert kwargs["trigger"] == "auto_offline"
