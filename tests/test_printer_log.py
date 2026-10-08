"""Per-printer MQTT ring and session timeline (U13)."""
import json
import os
import threading

from bridge.bambu.session import LinkSession
from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


_SERIAL = "01P00A123456789"
_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "replay", "p1s-synthetic.json",
)


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class _Client:
    def __init__(self):
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.publishes = []

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
        pass

    def subscribe(self, topic, qos=0):
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.publishes.append((topic, payload, qos))

        class Info:
            rc = 0

        return Info()

    def fire_connack(self, reason):
        self.on_connect(self, None, None, reason, None)

    def fire_message(self, topic, payload):
        self.on_message(self, None, type("Msg", (), {"topic": topic, "payload": payload})())

    def fire_disconnect(self, reason):
        self.on_disconnect(self, None, None, reason, None)


class _Broker:
    def __init__(self):
        self.clients = []

    def factory(self, client_id):
        client = _Client()
        client.client_id = client_id
        self.clients.append(client)
        return client

    @property
    def current(self):
        return self.clients[-1]


def _report(client, doc):
    client.fire_message(f"device/{_SERIAL}/report", json.dumps(doc).encode())


def _version_reply(sequence_id):
    return {"info": {"command": "get_version", "sequence_id": sequence_id, "module": [{"name": "ota"}]}}


def test_ring_keeps_exactly_the_last_100_messages():
    from bridge.bambu.log import PrinterLog

    log = PrinterLog(
        "S1",
        capacity=100,
        monotonic=lambda: 5.0,
        wall_clock=lambda: 1_700_000_000,
    )
    for i in range(150):
        log.record_message("in", "device/S1/report", {"i": i})
    for i in range(250):
        log.record_event("stale", n=i)

    exported = log.export()
    assert exported["serial"] == "S1"
    assert exported["capacity"] == 100
    assert exported["collected_at"] == "2023-11-14T22:13:20.000000Z"
    assert len(exported["messages"]) == 100
    assert exported["messages"][0]["payload"] == {"i": 50}
    assert exported["messages"][-1]["payload"] == {"i": 149}
    assert exported["messages"][0]["t"] == 5.0
    assert exported["messages"][0]["direction"] == "in"
    assert len(exported["events"]) == 200
    assert exported["events"][0]["n"] == 50
    assert exported["events"][-1]["n"] == 249

    exported["messages"][0]["payload"]["i"] = -1
    assert log.export()["messages"][0]["payload"]["i"] == 50


def test_outbound_commands_are_recorded_with_direction_out():
    from bridge.bambu.log import PrinterLog

    broker = _Broker()
    log = PrinterLog(_SERIAL, monotonic=_Clock(), wall_clock=lambda: 1_700_000_000)
    session = LinkSession(
        "10.0.0.5",
        "secret-code",
        _SERIAL,
        client_factory=broker.factory,
        log=log,
        monotonic=_Clock(),
        watchdog_interval=None,
    )
    session.start()
    assert session.publish({"print": {"command": "pause"}}) is False
    broker.current.fire_connack(0)
    assert session.publish({"print": {"command": "pause"}}) is True

    messages = log.export()["messages"]
    pauses = [m for m in messages if m["payload"] == {"print": {"command": "pause"}}]
    assert [m["direction"] for m in pauses] == ["out", "out"]
    assert pauses[0]["accepted"] is False
    assert pauses[1]["accepted"] is True
    assert pauses[1]["topic"] == f"device/{_SERIAL}/request"
    outbound = [m["payload"] for m in messages if m["direction"] == "out"]
    assert {"pushing": {"sequence_id": "0", "command": "pushall"}} in outbound
    assert {"info": {"sequence_id": "0", "command": "get_version"}} in outbound
    assert all(m["direction"] == "out" for m in messages)


def test_a_payload_containing_the_access_code_is_redacted():
    from bridge.bambu.log import PrinterLog

    code = "abcd1234"
    log = PrinterLog(_SERIAL, secrets=(code, "ab", "", "xyz", "abcdefgh"))
    log.record_message(
        "in",
        f"device/{_SERIAL}/report",
        {"print": {"subtask_name": f"job-{code}-end", "note": "ab xyz"}},
    )
    log.record_event("connect", host=f"10.0.0.{code}", client_id="link")
    exported = log.export()
    blob = json.dumps(exported)
    assert code not in blob
    assert exported["messages"][0]["payload"]["print"]["subtask_name"] == "job-[redacted]-end"
    assert exported["messages"][0]["payload"]["print"]["note"] == "ab xyz"
    assert exported["events"][0]["host"] == "10.0.0.[redacted]"

    longer = PrinterLog("S", secrets=("abcdefgh", "abcd"))
    longer.record_message("in", "t", {"v": "abcdefgh"})
    assert longer.export()["messages"][0]["payload"] == {"v": "[redacted]"}

    broker = _Broker()
    session_log = PrinterLog(_SERIAL, secrets=(code,))
    session = LinkSession(
        "10.0.0.5",
        code,
        _SERIAL,
        client_factory=broker.factory,
        log=session_log,
        monotonic=_Clock(),
        watchdog_interval=None,
    )
    session.start()
    broker.current.fire_connack(0)
    session.publish({"print": {"command": "pause", "param": code}})
    session_blob = json.dumps(session_log.export())
    assert code not in session_blob
    assert "bblp" not in session_blob
    assert "[redacted]" in session_blob


def test_collected_log_includes_session_events_in_time_order():
    """Connect through auth-retry. Kinds follow the order the session hit them."""
    from bridge.bambu.log import PrinterLog

    clock = _Clock(1000.0)
    broker = _Broker()
    log = PrinterLog(_SERIAL, secrets=("secret-code",), monotonic=clock, wall_clock=lambda: 1_700_000_000)
    session = LinkSession(
        "10.0.0.5",
        "secret-code",
        _SERIAL,
        client_factory=broker.factory,
        log=log,
        monotonic=clock,
        command_probe=True,
        watchdog_interval=None,
    )

    session.start()
    broker.current.fire_connack(0)
    _report(broker.current, {"print": {"gcode_state": "IDLE"}})
    # Ignored for the offline clock, but the client is done: a fresh one follows.
    broker.current.fire_disconnect(0)
    clock.now = 1005.0
    session.tick()
    broker.current.fire_connack(0)
    clock.now = 1040.0
    assert session.probe() == "1"
    _report(broker.current, _version_reply("1"))
    assert session.probe() == "2"
    clock.now = 1050.0
    session.tick()
    assert session.probe() == "3"
    clock.now = 1060.0
    session.tick()
    broker.current.fire_connack(0)
    clock.now = 1121.0
    session.tick()
    broker.current.fire_connack(134)
    clock.now = 1421.0
    session.tick()
    broker.current.fire_disconnect(50)

    events = log.export()["events"]
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "connect",
        "connack",
        "disconnect",
        "redial",
        "reset",
        "connect",
        "connack",
        "probe_sent",
        "probe_answered",
        "probe_sent",
        "probe_miss",
        "probe_sent",
        "probe_miss",
        "reset",
        "connect",
        "connack",
        "stale",
        "reset",
        "connect",
        "connack",
        "auth_retry",
        "reset",
        "connect",
        "disconnect",
    ]
    assert [event["t"] for event in events] == sorted(event["t"] for event in events)
    assert events[1]["result"] == "ok"
    assert events[1]["code"] == 0
    assert events[2]["ignored"] is True
    assert events[2]["code"] == 0
    assert events[3]["after"] == 5.0
    assert events[7]["sequence_id"] == "1"
    assert events[8]["sequence_id"] == "1"
    assert events[10]["count"] == 1
    assert events[12]["count"] == 2
    assert events[13]["reason"] == "commands_ignored"
    assert events[16]["kind"] == "stale"
    assert events[17]["reason"] == "silent_session"
    assert events[19]["result"] == "auth_rejected"
    assert events[19]["code"] == 134
    assert events[21]["reason"] == "auth_rejected"
    assert events[-1]["ignored"] is False
    assert events[-1]["code"] == 50
    assert all(event["kind"] != "connect" or event["host"] == "10.0.0.5" for event in events)
    assert len({event["client_id"] for event in events if event["kind"] == "connect"}) == 5
    inbound = [m for m in log.export()["messages"] if m["direction"] == "in"]
    assert inbound
    assert all("accepted" not in message for message in inbound)
    assert "secret-code" not in json.dumps(log.export())


def test_replay_fixture_reproduces_the_recorded_state():
    from tests.replay import load_fixture, replay_into_printer

    fixture = load_fixture(_FIXTURE)
    assert fixture["source"] == "synthetic"
    assert "not a shop capture" in fixture["note"].lower()
    times = [message["t"] for message in fixture["messages"]]
    assert times == sorted(times)
    event_times = [event["t"] for event in fixture["events"]]
    assert event_times == sorted(event_times)

    class _Connected:
        connected = True

        def start(self):
            return None

        def publish(self, payload):
            return True

    printer = BambuPrinter(
        PrinterConfig(
            bambu_id=fixture["serial"],
            ip="10.0.0.51",
            access_code="not-in-the-fixture",
            name="P1S",
        ),
        monotonic=lambda: 1_000_000.0,
        sleep=lambda _seconds: None,
        session_factory=lambda *_args, **_kwargs: _Connected(),
    )
    printer.connect()
    snapshot = replay_into_printer(printer, fixture)
    assert snapshot["bambu_id"] == fixture["serial"]
    assert snapshot["status"] == "PRINTING"
    assert snapshot["progress_percent"] == 43
    assert snapshot["nozzle_temper"] == 220.0
    assert snapshot["bed_temper"] == 60.0
    assert snapshot["remaining_seconds"] == 1800
    assert snapshot["subtask_name"] == "benchy"
    assert snapshot["slots"] is None


def test_printer_log_survives_rebuild_and_reconnect_and_records_commands():
    clock = _Clock(2000.0)
    broker = _Broker()
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret-code", name="P1S")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip,
            access_code,
            serial,
            on_report=on_report,
            client_factory=broker.factory,
            monotonic=clock,
            watchdog_interval=None,
        )

    printer = BambuPrinter(
        cfg,
        monotonic=clock,
        sleep=lambda _seconds: None,
        session_factory=factory,
    )
    printer.connect()
    kept = printer.log
    broker.current.fire_connack(0)
    assert printer.pause_print() is True
    assert printer.start_print("benchy.3mf", [0], 1) is True
    printer.rebuild_session()
    assert printer.log is kept
    printer.reconnect(new_ip="10.0.0.9")
    assert printer.log is kept
    broker.current.fire_connack(0)
    assert printer.pause_print() is True

    exported = printer.collect_log()
    commands = [event for event in exported["events"] if event["kind"] == "command"]
    # connect() no longer asks for a dump before CONNACK: that publish could only
    # be refused. The session asks for one when the broker answers.
    assert [(event["name"], event["accepted"]) for event in commands] == [
        ("pause", True),
        ("project_file", True),
        ("pause", True),
    ]
    assert any(event["kind"] == "reset" for event in exported["events"])
    assert exported["serial"] == _SERIAL
    assert isinstance(exported["findings"], list)
    assert "secret-code" not in json.dumps(exported)


def test_log_file_rotates_and_a_missing_directory_does_not_raise(tmp_path):
    from bridge.bambu.log import PrinterLog

    path = tmp_path / "printer.jsonl"
    log = PrinterLog(
        "S1",
        file_path=str(path),
        max_file_bytes=300,
        backups=3,
        monotonic=lambda: 1.0,
        wall_clock=lambda: 1_700_000_000,
    )
    for i in range(40):
        log.record_message("in", "device/S1/report", {"i": i, "pad": "x" * 40})
    assert path.exists()
    assert (tmp_path / "printer.jsonl.1").exists()
    assert (tmp_path / "printer.jsonl.2").exists()
    assert (tmp_path / "printer.jsonl.3").exists()
    assert not (tmp_path / "printer.jsonl.4").exists()
    assert path.stat().st_size < 800
    current = path.read_text(encoding="utf-8")
    assert '"i": 39' in current or '"i":39' in current
    assert '"i": 0' not in current
    for line in current.splitlines():
        json.loads(line)

    missing = tmp_path / "absent" / "printer.jsonl"
    broken = PrinterLog("S1", file_path=str(missing))
    broken.record_message("in", "t", {"a": 1})
    broken.record_event("connect", host="10.0.0.1")
    assert broken.export()["messages"][0]["payload"] == {"a": 1}
    assert not missing.exists()


def test_concurrent_records_stay_inside_the_rings():
    from bridge.bambu.log import PrinterLog

    log = PrinterLog("S1", capacity=100, event_capacity=200)

    def worker():
        for i in range(80):
            log.record_message("in", "t", {"i": i})
            log.record_event("stale", n=i)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)
    assert not any(thread.is_alive() for thread in threads)
    exported = log.export()
    assert len(exported["messages"]) == 100
    assert len(exported["events"]) == 200


def _log(**kwargs):
    from bridge.bambu.log import PrinterLog

    defaults = {"monotonic": lambda: 5.0, "wall_clock": lambda: 1_700_000_000}
    defaults.update(kwargs)
    return PrinterLog("S1", **defaults)


def test_three_stale_events_are_one_finding_and_one_is_not():
    log = _log()
    log.record_event("stale")
    assert log.export()["findings"] == []

    log.record_event("reset", reason="silent_session")
    log.record_event("stale")
    findings = log.export()["findings"]
    assert len(findings) == 1
    assert findings[0]["id"] == "stale_or_reset"
    assert findings[0]["count"] == 3
    assert findings[0]["text"].startswith("The session went stale or reset repeatedly.")
    assert "silent_session" in findings[0]["text"]

    only_stale = _log()
    for _ in range(3):
        only_stale.record_event("stale")
    assert only_stale.export()["findings"] == [
        {
            "id": "stale_or_reset",
            "count": 3,
            "text": "The session went stale or reset repeatedly.",
        }
    ]


def test_auth_retry_and_probe_miss_use_their_own_finding_ids():
    log = _log()
    for i in range(3):
        log.record_event("auth_retry")
        log.record_event("probe_miss", count=i + 1)
    findings = log.export()["findings"]
    assert [item["id"] for item in findings] == ["auth_retry", "probe_miss"]
    assert [item["count"] for item in findings] == [3, 3]
    assert findings[0]["text"] == "The session retried after the printer rejected the connection."
    assert findings[1]["text"] == "Command probes went unanswered."


def test_an_unrelated_event_kind_produces_no_finding():
    log = _log()
    for _ in range(4):
        log.record_event("connect", host="10.0.0.5")
        log.record_event("command", name="pause")
        log.record_event("probe_sent", sequence_id="1")
    assert log.export()["findings"] == []


def test_finding_text_does_not_restore_a_stripped_access_code():
    code = "abcd1234"
    log = _log(secrets=(code,))
    for _ in range(3):
        log.record_event("stale", detail=f"retry {code} now")
    exported = log.export()
    assert code not in json.dumps(exported["events"])
    assert exported["events"][0]["detail"] == "retry [redacted] now"
    text = " ".join(item["text"] for item in exported["findings"])
    assert code not in text
    assert "[redacted]" in text
    assert "camera" not in text
    assert "captcha" not in text.lower()
    assert "database" not in text.lower()


def test_printer_log_path_sanitizes_the_serial(tmp_path):
    from bridge.app import _printer_log_path

    path = _printer_log_path(str(tmp_path), "01P/../weird serial")
    assert path == os.path.join(str(tmp_path), "printer-01P_.._weird_serial.jsonl")


_SHOP_REPLAY = os.path.join(
    os.path.dirname(__file__), "fixtures", "shop_replay_2026_09_30.jsonl",
)


def _epoch(at):
    import datetime
    return datetime.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()


def test_the_shop_replay_records_each_replayed_start_as_link_earlier(caplog):
    """Shop P1S, 2026-09-30 (serial anonymised). Link started two files at
    14:29 and 15:00. At 18:06:29, after the 18:00 drop, the printer echoed
    both starts again. The 18:07:56 start is Link's own on the new client."""
    import logging

    from bridge.bambu.log import PrinterLog

    with open(_SHOP_REPLAY, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    serial = "01P00TESTSERIAL"
    assert all(serial in r["topic"] for r in records if r["record"] == "message")

    clock = _Clock(_epoch(records[0]["at"]))
    broker = _Broker()
    log = PrinterLog(serial, event_capacity=500, monotonic=clock, wall_clock=clock)
    session = LinkSession(
        "10.0.0.5", "secret-code", serial, client_factory=broker.factory,
        log=log, monotonic=clock, watchdog_interval=None,
    )
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        session.start()
        for record in records:
            clock.now = _epoch(record["at"])
            if record["record"] == "event":
                if record["kind"] == "connack":
                    # U1: the watchdog redials a fresh client after a drop.
                    session.tick()
                    broker.current.fire_connack(record["code"])
                elif record["kind"] == "disconnect":
                    broker.current.fire_disconnect(record["code"])
                continue
            payload = record["payload"]
            if record["direction"] == "out":
                # CONNACK publishes pushall and get_version on its own.
                if payload.get("print", {}).get("command") == "project_file":
                    assert session.publish(payload) is True
                continue
            broker.current.fire_message(record["topic"], json.dumps(payload).encode())

    assert len(broker.clients) == 2
    unexpected = [e for e in log.export()["events"] if e["kind"] == "unexpected_start"]
    assert [(e["task_id"], e["file"], e["origin"]) for e in unexpected] == [
        ("1924711886", "batch-2026-09-28-ke9pacfn-1.3mf", "link_earlier"),
        ("1926569165", "batch-2026-09-28-zeg32WPs-1.3mf", "link_earlier"),
    ]
    assert all(0 < e["seconds_since_connack"] < 1 for e in unexpected)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all(serial in w for w in warnings)


# U7: every print_error edge records the context around it.

class _QuietSession:
    """Stands in for LinkSession without a watchdog: no stale, no reset."""

    connected = True

    def __init__(self, connack_at=None):
        self.connack_at = connack_at

    def start(self):
        return None

    def publish(self, payload):
        return True

    def disconnect(self):
        return None


def _state_report(state, *, error=0, bits="f"):
    return {"print": {
        "gcode_state": state,
        "print_error": error,
        "ams": {"tray_exist_bits": bits},
    }}


def _print_error_events(printer):
    return [e for e in printer.collect_log()["events"] if e["kind"] == "print_error"]


def _linked_printer(clock, broker, serial=_SERIAL):
    def factory(ip, access_code, serial_, on_report):
        return LinkSession(
            ip, access_code, serial_, on_report=on_report,
            client_factory=broker.factory, monotonic=clock, watchdog_interval=None,
        )

    return BambuPrinter(
        PrinterConfig(bambu_id=serial, ip="10.0.0.5", access_code="secret-code", name="P1S"),
        monotonic=clock, sleep=lambda _seconds: None, session_factory=factory,
    )


def test_a_0500_4003_after_a_70s_gap_in_prepare_records_the_gap_and_timeline(
        tmp_path, monkeypatch, caplog):
    import logging

    from bridge import printer as printer_module

    clock = _Clock(5000.0)
    session = _QuietSession(connack_at=4990.0)
    printer = BambuPrinter(
        PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret-code", name="P1S"),
        monotonic=clock, sleep=lambda _seconds: None,
        session_factory=lambda *_args, **_kwargs: session,
    )
    printer.connect()
    printer._on_mqtt_report(_state_report("IDLE"))
    sliced = tmp_path / "job.3mf"
    sliced.write_bytes(b"x" * 1234)
    monkeypatch.setattr(printer_module.ftps, "upload", lambda *args, **kwargs: "job.3mf")
    monkeypatch.setattr(
        printer_module.ftps, "remove_files",
        lambda *args, **kwargs: {"found": 0, "deleted": 0, "failed": 0},
    )
    printer.upload_file(str(sliced))
    assert printer.start_print("job.3mf", [0, 1], 1) is True
    clock.now = 5001.0
    printer._on_mqtt_report(_state_report("PREPARE"))
    clock.now = 5071.0
    with caplog.at_level(logging.WARNING, logger="bridge.printer"):
        printer._on_mqtt_report(_state_report("PREPARE", error=83902467))

    events = _print_error_events(printer)
    assert len(events) == 1
    event = events[0]
    assert event["code"] == "0500_4003"
    assert event["print_error"] == 83902467
    assert event["gcode_state"] == "PREPARE"
    assert event["states"] == [["IDLE", 1.0], ["PREPARE", 70.0]]
    assert event["since_start"] == 71.0
    assert event["longest_gap"] == {"seconds": 70.0, "state": "PREPARE", "ago": 0.0}
    assert event["since_connack"] == 81.0
    assert event["session_events"] == []
    assert event["trays"] == [{"tray": 0, "present": True}, {"tray": 1, "present": True}]
    assert event["tray_exist_bits"] == "f"
    assert event["upload"]["bytes"] == 1234
    assert event["upload"]["result"] == "ok"
    assert isinstance(event["upload"]["seconds"], float)
    assert event["suspect"] == "70s status gap during PREPARE"
    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "bridge.printer" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert _SERIAL in warnings[0]
    assert "0500_4003" in warnings[0]
    assert "70s status gap during PREPARE" in warnings[0]


def test_a_print_error_shortly_after_a_reset_records_the_reset(caplog):
    import logging

    clock = _Clock(8000.0)
    broker = _Broker()
    printer = _linked_printer(clock, broker)
    printer.connect()
    broker.current.fire_connack(0)
    _report(broker.current, _state_report("IDLE", bits="3"))
    assert printer.start_print("job.3mf", [0, 2], 1) is True
    clock.now = 8002.0
    _report(broker.current, _state_report("PREPARE", bits="3"))
    clock.now = 8010.0
    printer.rebuild_session()
    broker.current.fire_connack(0)
    clock.now = 8013.0
    with caplog.at_level(logging.WARNING, logger="bridge.printer"):
        _report(broker.current, _state_report("PREPARE", error=83902467, bits="3"))

    events = _print_error_events(printer)
    assert len(events) == 1
    event = events[0]
    assert [(e["kind"], e["age"]) for e in event["session_events"]] == [("reset", 3.0)]
    assert event["since_connack"] == 3.0
    # Tray 2 is mapped and its bit is clear. The reset still outranks it.
    assert event["trays"] == [{"tray": 0, "present": True}, {"tray": 2, "present": False}]
    assert event["suspect"] == "reset 3s before"
    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "bridge.printer" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "reset 3s before" in warnings[0]


def test_clearing_a_print_error_records_nothing():
    clock = _Clock(100.0)
    session = _QuietSession()
    printer = BambuPrinter(
        PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret-code", name="P1S"),
        monotonic=clock, sleep=lambda _seconds: None,
        session_factory=lambda *_args, **_kwargs: session,
    )
    printer.connect()
    # A code already standing on Link's first report is not an edge Link saw.
    printer._on_mqtt_report(_state_report("FAILED", error=83902467))
    clock.now = 101.0
    printer._on_mqtt_report(_state_report("IDLE", error=0))
    assert _print_error_events(printer) == []
    clock.now = 102.0
    printer._on_mqtt_report(_state_report("FAILED", error=83902467))
    clock.now = 103.0
    printer._on_mqtt_report(_state_report("FAILED", error=83902467))
    clock.now = 104.0
    printer._on_mqtt_report(_state_report("IDLE", error=0))
    clock.now = 105.0
    printer._on_mqtt_report(_state_report("IDLE"))
    # User cancel is not a fault.
    printer._on_mqtt_report(_state_report("FAILED", error=50348044))
    assert len(_print_error_events(printer)) == 1


def test_a_standing_code_after_a_first_report_without_print_error_is_not_an_edge():
    """After a restart the first message can be an info reply or a sparse
    delta. The pushall that follows carries the standing code; that is not a
    new fault."""
    clock = _Clock(200.0)
    session = _QuietSession()
    printer = BambuPrinter(
        PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret-code", name="P1S"),
        monotonic=clock, sleep=lambda _seconds: None,
        session_factory=lambda *_args, **_kwargs: session,
    )
    printer.connect()
    printer._on_mqtt_report({"info": {"command": "get_version", "sequence_id": "0"}})
    clock.now = 200.5
    printer._on_mqtt_report({"print": {"command": "push_status", "gcode_state": "FAILED"}})
    clock.now = 201.0
    printer._on_mqtt_report(_state_report("FAILED", error=83902467))
    assert _print_error_events(printer) == []


def test_the_shop_p1s_5_1500_case_records_one_error_with_no_session_event(caplog):
    """Shop P1S-5, 2026-09-30 (serial anonymised). Link started a file at
    15:00:47Z. The printer sat in IDLE, went to PREPARE, and raised
    0500_4003 (83902467) at 15:01:30Z. No reset, no reconnect, no replay,
    every mapped tray present."""
    import logging

    serial = "01P00TESTSERIAL"
    clock = _Clock(_epoch("2026-09-30T15:00:30Z"))
    broker = _Broker()
    printer = _linked_printer(clock, broker, serial=serial)
    printer.connect()
    broker.current.fire_connack(0)

    def report(doc):
        broker.current.fire_message(f"device/{serial}/report", json.dumps(doc).encode())

    t0 = _epoch("2026-09-30T15:00:47Z")
    clock.now = t0 - 1
    report(_state_report("IDLE", bits="ffff"))
    clock.now = t0
    assert printer.start_print("batch-2026-09-28-zeg32WPs-1.3mf", [5, 9], 1) is True
    for second in range(1, 42):
        clock.now = t0 + second
        state = "IDLE" if second < 2 else "PREPARE"
        report(_state_report(state, bits="ffff"))
    clock.now = t0 + 43
    with caplog.at_level(logging.WARNING, logger="bridge.printer"):
        report(_state_report("PREPARE", error=83902467, bits="ffff"))

    events = _print_error_events(printer)
    assert len(events) == 1
    event = events[0]
    assert event["code"] == "0500_4003"
    assert event["session_events"] == []
    assert event["states"] == [["IDLE", 2.0], ["PREPARE", 41.0]]
    assert event["longest_gap"]["seconds"] == 2.0
    assert all(tray["present"] for tray in event["trays"])
    assert event["suspect"] == "no preceding session event"
    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "bridge.printer" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert serial in warnings[0]
