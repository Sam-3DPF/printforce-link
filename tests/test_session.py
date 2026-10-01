"""Link-owned MQTT session: one client per printer, report topic only, no camera."""
import json
import logging
import socket
import time

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bridge.bambu.session import COMMAND_PROBE_ENABLED, LinkSession, build_paho_client
from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


_SERIAL = "01P00A123456789"


class FakePaho:
    """Records the calls LinkSession makes and plays CONNACK / reports back."""

    def __init__(self):
        self.subscriptions = []
        self.publishes = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.username = None
        self.password = None
        self.tls_context = None
        self.tls_insecure = None
        self.inflight = None
        self.reconnect_delay = None
        self.connect_args = None
        self.loop_started = 0
        self.publish_calls = 0
        self.block_stop = False
        self.publish_rc = 0

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set_context(self, context):
        self.tls_context = context

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def max_inflight_messages_set(self, inflight):
        self.inflight = inflight

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delay = (min_delay, max_delay)

    def connect_async(self, host, port=1883, keepalive=60, **kwargs):
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_started += 1

    def loop_stop(self):
        if self.block_stop:
            time.sleep(30)

    def disconnect(self):
        if self.block_stop:
            time.sleep(30)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.publish_calls += 1
        self.publishes.append((topic, payload, qos))

        class Info:
            pass

        info = Info()
        info.rc = self.publish_rc
        return info

    def fire_connack(self, reason):
        self.on_connect(self, None, None, reason, None)

    def fire_message(self, topic, payload):
        self.on_message(self, None, type("Msg", (), {"topic": topic, "payload": payload})())

    def fire_disconnect(self, reason):
        self.on_disconnect(self, None, None, reason, None)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _session(fake, serial=_SERIAL, **kwargs):
    return LinkSession(
        "10.0.0.5",
        "secret-code",
        serial,
        client_factory=lambda client_id: fake,
        **kwargs,
    )


def _bodies(fake):
    return [json.loads(payload) for _topic, payload, _qos in fake.publishes]


def test_connect_subscribes_to_the_report_topic_only_and_asks_pushall_and_version():
    fake = FakePaho()
    session = _session(fake)
    session.start()

    assert fake.username == "bblp"
    assert fake.password == "secret-code"
    assert fake.tls_context is not None
    assert fake.tls_context.minimum_version == __import__("ssl").TLSVersion.TLSv1_2
    assert fake.tls_context.check_hostname is False
    assert fake.tls_context.verify_mode == __import__("ssl").CERT_NONE
    assert fake.tls_insecure is True
    assert fake.inflight == 1000
    assert fake.reconnect_delay == (1, 30)
    assert fake.connect_args == ("10.0.0.5", 8883, 30)
    assert fake.loop_started == 1
    assert fake.subscriptions == []

    fake.fire_connack(0)

    assert fake.subscriptions == [(f"device/{_SERIAL}/report", 1)]
    assert all(topic == f"device/{_SERIAL}/request" for topic, _payload, qos in fake.publishes)
    assert all(qos == 1 for _topic, _payload, qos in fake.publishes)
    bodies = _bodies(fake)
    assert {"pushing": {"sequence_id": "0", "command": "pushall"}} in bodies
    assert {"info": {"sequence_id": "0", "command": "get_version"}} in bodies
    assert session.connected is True
    assert session.last_connect_error is None
    assert not any(topic.endswith("/request") for topic, _qos in fake.subscriptions)


def test_each_connect_uses_a_new_client_id():
    seen = []

    def factory(client_id):
        seen.append(client_id)
        return FakePaho()

    session = LinkSession("10.0.0.5", "code", _SERIAL, client_factory=factory)
    session.start()
    session.hard_reset()

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert seen[0].startswith(f"link-{_SERIAL}-")
    assert seen[1].startswith(f"link-{_SERIAL}-")
    pid = str(__import__("os").getpid())
    assert pid in seen[0].split("-")
    assert pid in seen[1].split("-")


@pytest.mark.parametrize(
    "reason, expected",
    [
        (134, "auth_rejected"),
        (ReasonCode(PacketTypes.CONNACK, identifier=134), "auth_rejected"),
        (135, "auth_rejected"),
        (ReasonCode(PacketTypes.CONNACK, identifier=135), "auth_rejected"),
        (136, "refused"),
        (4, "refused"),
        (ReasonCode(PacketTypes.CONNACK, identifier=132), "refused"),
    ],
)
def test_connack_failure_records_auth_rejected_or_refused(reason, expected):
    fake = FakePaho()
    session = _session(fake)
    session.start()
    fake.fire_connack(reason)
    assert session.connected is False
    assert session.last_connect_error == expected
    assert fake.subscriptions == []


def test_publish_while_disconnected_returns_false_without_blocking():
    fake = FakePaho()
    fake.block_stop = True  # publish must not reach the client, so this must not matter
    session = _session(fake)
    session.start()
    started = time.monotonic()
    assert session.publish({"print": {"command": "pause"}}) is False
    assert time.monotonic() - started < 0.5
    assert fake.publish_calls == 0


def test_publish_failure_rc_returns_false_and_does_not_wait():
    fake = FakePaho()
    session = _session(fake)
    session.start()
    fake.fire_connack(0)
    fake.publish_rc = 4
    fake.publishes.clear()
    assert session.publish({"print": {"command": "pause"}}) is False
    assert fake.publishes[-1][2] == 1


def test_disconnect_returns_within_two_seconds_when_the_network_thread_is_stuck():
    fake = FakePaho()
    fake.block_stop = True
    session = _session(fake)
    session.start()
    fake.fire_connack(0)
    started = time.monotonic()
    session.disconnect()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert session.connected is False


def test_clean_disconnect_within_10s_of_a_report_ends_the_client_but_not_the_printer():
    """KTD2: the ignore window only skips the offline clock. The client is done."""
    clock = Clock()
    broker = _ReplayBroker()
    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    broker.accept(broker.current)
    first = broker.current
    first.fire_message(f"device/{_SERIAL}/report", b'{"print": {"gcode_state": "IDLE"}}')
    assert session.last_message_at == clock.now

    first.fire_disconnect(ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection"))
    assert session.connected is False
    assert session.publish({"print": {"command": "pause"}}) is False
    assert session.state == "live"
    assert session.down_reason is None

    clock.now += 5
    session.tick()
    assert len(broker.clients) == 2
    assert first.loop_stop_calls == 1
    # The unreachable clock starts at the redial, not at the ignored drop.
    clock.now += 57
    session.tick()
    assert session.down_reason is None
    clock.now += 4
    session.tick()
    assert session.down_reason == "unreachable"


@pytest.mark.parametrize("reason, report_age", [(0, None), (0, 11), (128, 0)])
def test_a_drop_outside_the_ignore_window_starts_the_offline_clock(reason, report_age):
    clock = Clock()
    broker = _ReplayBroker()
    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    broker.accept(broker.current)
    if report_age is not None:
        broker.current.fire_message(
            f"device/{_SERIAL}/report", b'{"print": {"gcode_state": "IDLE"}}',
        )
        clock.now += report_age
    broker.current.fire_disconnect(reason)
    assert session.connected is False
    for _ in range(12):
        clock.now += 5
        session.tick()
    clock.now += 1
    session.tick()
    assert session.down_reason == "unreachable"
    assert len(broker.clients) >= 2


def test_connecting_a_printer_does_not_open_port_6000(monkeypatch):
    def port_of(address):
        if isinstance(address, tuple) and len(address) >= 2:
            return address[1]
        return None

    def guarded_connect(self, address):
        if port_of(address) == 6000:
            raise AssertionError("camera socket on port 6000")

    def guarded_create_connection(address, *args, **kwargs):
        if port_of(address) == 6000:
            raise AssertionError("camera socket on port 6000")
        raise OSError("no network in this test")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)

    import bridge.bambu.session as session_mod
    source = open(session_mod.__file__, encoding="utf-8").read().lower()
    assert "camera" not in source
    assert "6000" not in source
    assert "bambulabs" not in source

    fake = FakePaho()
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="x", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip, access_code, serial, on_report=on_report, client_factory=lambda _cid: fake,
        )

    printer = BambuPrinter(cfg, session_factory=factory, sleep=lambda _seconds: None)
    printer.connect()
    fake.fire_connack(0)
    assert fake.subscriptions == [(f"device/{_SERIAL}/report", 1)]


def test_default_client_is_mqtt311_with_a_clean_session():
    import paho.mqtt.client as mqtt

    client = build_paho_client("link-abc")
    try:
        assert client._protocol == mqtt.MQTTv311
        assert client._clean_session is True
        assert client._client_id == b"link-abc"
        # A redial of a used client replays its unacked QoS 1 queue.
        assert client._reconnect_on_failure is False
    finally:
        client.loop_stop()


def test_printer_snapshot_reads_a_session_report_and_pause_publishes_qos1():
    """CONNACK, then one report, then pause — through the real LinkSession."""
    fake = FakePaho()
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="x", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip,
            access_code,
            serial,
            on_report=on_report,
            client_factory=lambda _cid: fake,
            monotonic=lambda: 0.0,
        )

    printer = BambuPrinter(
        cfg,
        session_factory=factory,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    printer.connect()
    fake.fire_connack(0)
    fake.fire_message(
        f"device/{_SERIAL}/report",
        json.dumps({"print": {"gcode_state": "RUNNING", "nozzle_temper": 220.0, "mc_percent": 47}}).encode(),
    )

    snapshot = printer.snapshot()
    assert snapshot["status"] == "PRINTING"
    assert snapshot["nozzle_temper"] == 220.0
    assert snapshot["progress_percent"] == 47

    assert printer.pause_print() is True
    topic, payload, qos = fake.publishes[-1]
    assert topic == f"device/{_SERIAL}/request"
    assert qos == 1
    assert json.loads(payload) == {"print": {"sequence_id": "0", "command": "pause"}}


_REJECT = {"attr": 0x05000500, "code": 0x00010007}


def test_command_probe_stays_off():
    assert COMMAND_PROBE_ENABLED is False


def test_a_non_utf8_report_parses_and_marks_the_session_live():
    fake = FakePaho()
    clock = Clock()
    session = _session(fake, monotonic=clock, watchdog_interval=None)
    session.start()
    fake.fire_connack(0)
    body = b'{"print": {"gcode_state": "IDLE", "note": "' + bytes([0xFF]) + b'"}}'
    fake.fire_message(f"device/{_SERIAL}/report", body)
    assert session.state == "live"
    assert session.last_message_at == clock.now


def test_non_json_after_replacement_does_not_mark_the_session_live():
    fake = FakePaho()
    clock = Clock()
    session = _session(fake, monotonic=clock, watchdog_interval=None)
    session.start()
    fake.fire_connack(0)
    fake.fire_message(f"device/{_SERIAL}/report", b"\xff\xfe not-json")
    assert session.last_message_at is None
    assert session.state != "live"


def test_connack_134_logs_the_toggle_sentence_and_holds_the_client(caplog):
    fake = FakePaho()
    clock = Clock(1000.0)
    session = _session(fake, monotonic=clock, watchdog_interval=None)
    session.start()
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        fake.fire_connack(134)
    assert "The access code changes when LAN Only or Developer Mode is toggled." in caplog.text
    assert "secret-code" not in caplog.text
    opened = fake.loop_started
    clock.now += 30
    session.tick()
    assert fake.loop_started == opened
    assert session.last_connect_error == "auth_rejected"


def test_an_empty_report_topic_is_logged_once_for_the_client(caplog):
    fake = FakePaho()
    clock = Clock()
    session = _session(fake, monotonic=clock, watchdog_interval=None)
    session.start()
    fake.fire_connack(0)
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        session.tick()
        session.tick()
    assert caplog.text.count("report topic is empty") == 1


def test_command_rejection_does_not_open_a_client_in_any_gcode_state(caplog):
    fake = FakePaho()
    clock = Clock(5000.0)
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="x", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip, access_code, serial, on_report=on_report,
            client_factory=lambda _cid: fake, monotonic=clock,
            watchdog_interval=None,
        )

    printer = BambuPrinter(
        cfg, session_factory=factory, monotonic=lambda: clock.now, sleep=lambda _seconds: None,
    )
    printer.connect()
    fake.fire_connack(0)
    opened = fake.loop_started
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        for state in ("IDLE", "FINISH", "FAILED", "RUNNING", "PREPARE", "SLICING", "PAUSE"):
            fake.fire_message(
                f"device/{_SERIAL}/report",
                json.dumps({"print": {"gcode_state": state, "hms": [_REJECT]}}).encode(),
            )
            clock.now += 70
            printer._session.tick()
    assert fake.loop_started == opened
    assert caplog.text.count("Enable Developer Mode and restart the printer.") == 1
    assert "secret-code" not in caplog.text


def test_the_message_callback_does_not_join_the_network_thread_for_that_fault():
    fake = FakePaho()
    clock = Clock()
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="x", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip, access_code, serial, on_report=on_report,
            client_factory=lambda _cid: fake, monotonic=clock,
            watchdog_interval=None,
        )

    printer = BambuPrinter(
        cfg, session_factory=factory, monotonic=lambda: clock.now, sleep=lambda _seconds: None,
    )
    printer.connect()
    fake.fire_connack(0)
    fake.block_stop = True
    started = time.monotonic()
    fake.fire_message(
        f"device/{_SERIAL}/report",
        json.dumps({"print": {"gcode_state": "IDLE", "hms": [_REJECT]}}).encode(),
    )
    assert time.monotonic() - started < 2
    assert printer.commands_rejected is True
    assert fake.loop_started == 1


# paho 2.1 redials a used client in place and re-sends every QoS 1 message the
# broker never PUBACKed (Client._messages_reconnect_reset_out). Bambu's broker
# rarely PUBACKs, so each in-place redial replayed old project_file starts
# (shop logs 2026-09-30). The model below redials in place exactly when the
# client build_paho_client makes would.
def _paho_redials_in_place() -> bool:
    client = build_paho_client("link-probe")
    try:
        return bool(client._reconnect_on_failure)
    finally:
        client.loop_stop()


_CONN_LOST = 7
_PUSHALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}
_GET_VERSION = {"info": {"sequence_id": "0", "command": "get_version"}}


class _ReplayPaho(FakePaho):
    """A client whose QoS 1 publishes are never PUBACKed, like a busy P1S."""

    def __init__(self, broker, client_id):
        super().__init__()
        self.broker = broker
        self.client_id = client_id
        self.redials_in_place = _paho_redials_in_place()
        self.unacked = []
        self.up = False
        self.loop_stop_calls = 0

    def publish(self, topic, payload=None, qos=0, retain=False):
        info = super().publish(topic, payload, qos, retain)
        if qos == 1:
            self.unacked.append(payload)
        if self.up:
            self.broker.wire.append((self.client_id, payload))
        return info

    def loop_stop(self):
        self.loop_stop_calls += 1


class _ReplayBroker:
    """The printer's broker. ``wire`` is every command it received, in order."""

    def __init__(self):
        self.clients = []
        self.wire = []

    def factory(self, client_id):
        client = _ReplayPaho(self, client_id)
        self.clients.append(client)
        return client

    @property
    def current(self):
        return self.clients[-1]

    def accept(self, client):
        client.up = True
        client.fire_connack(0)

    def drop(self):
        client = self.current
        client.up = False
        client.fire_disconnect(_CONN_LOST)

    def come_back(self):
        """The printer answers again. A client paho still runs redials in place."""
        for client in self.clients:
            if client.up or not client.redials_in_place or client.loop_stop_calls:
                continue
            replay = list(client.unacked)
            self.accept(client)  # on_connect runs before paho's resend, as in paho
            for payload in replay:
                self.wire.append((client.client_id, payload))


def _start_body(seq):
    return {"print": {"sequence_id": seq, "command": "project_file",
                      "param": "Metadata/plate_1.gcode"}}


def test_a_reconnect_never_replays_starts_published_before_the_drop():
    clock = Clock(1000.0)
    broker = _ReplayBroker()
    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    broker.accept(broker.current)
    assert session.publish(_start_body("20000")) is True
    assert session.publish(_start_body("20001")) is True

    broker.drop()
    since_drop = len(broker.wire)
    # The printer answers again at once. paho's own redial would win this race.
    broker.come_back()
    clock.now += 5
    session.tick()
    if not broker.current.up:
        broker.accept(broker.current)

    after = [json.loads(payload) for _cid, payload in broker.wire[since_drop:]]
    assert after == [_PUSHALL, _GET_VERSION]
    assert session.connected is True


def test_each_post_connack_drop_gets_a_new_client_and_stops_the_old_one_once():
    clock = Clock(2000.0)
    broker = _ReplayBroker()
    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    for _ in range(3):
        broker.accept(broker.current)
        broker.drop()
        clock.now += 5
        session.tick()
        session.tick()
    ids = [client.client_id for client in broker.clients]
    assert len(ids) == 4
    assert len(set(ids)) == 4
    assert [client.loop_stop_calls for client in broker.clients[:-1]] == [1, 1, 1]
    assert broker.current.loop_stop_calls == 0
    broker.come_back()
    assert [client.up for client in broker.clients[:-1]] == [False, False, False]


def test_a_redial_waits_5_then_10_20_30s_and_a_connack_resets_it():
    clock = Clock(3000.0)
    broker = _ReplayBroker()
    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    broker.accept(broker.current)
    dropped_at = clock.now
    broker.drop()

    opened = []
    for _ in range(100):
        clock.now += 1
        before = len(broker.clients)
        session.tick()
        if len(broker.clients) > before:
            opened.append(clock.now - dropped_at)
            # The printer takes the socket and closes it before CONNACK.
            broker.current.fire_disconnect(_CONN_LOST)
    assert opened == [5, 15, 35, 65, 95]

    clock.now = dropped_at + 125
    session.tick()
    assert len(broker.clients) == 7
    broker.accept(broker.current)
    broker.drop()
    clock.now += 4
    session.tick()
    before = len(broker.clients)
    clock.now += 1
    session.tick()
    assert len(broker.clients) == before + 1


def test_the_send_watchdog_reset_then_republish_sends_exactly_one_start():
    """Phase A timeout: the app resets the session, then republishes once."""
    clock = Clock(4000.0)
    broker = _ReplayBroker()
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="x", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip, access_code, serial, on_report=on_report,
            client_factory=broker.factory, monotonic=clock, watchdog_interval=None,
        )

    printer = BambuPrinter(
        cfg, session_factory=factory, monotonic=clock, sleep=lambda _seconds: None,
    )
    printer.connect()
    broker.accept(broker.current)
    broker.current.fire_message(
        f"device/{_SERIAL}/report", b'{"print": {"gcode_state": "IDLE"}}',
    )
    assert printer.start_print("benchy.3mf", [0], 1) is True

    clock.now += 90
    since_reset = len(broker.wire)
    printer._session.hard_reset()
    broker.come_back()
    broker.accept(broker.current)
    assert printer.start_print("benchy.3mf", [0], 1) is True

    commands = [
        json.loads(payload).get("print", {}).get("command")
        for _cid, payload in broker.wire[since_reset:]
    ]
    assert commands.count("project_file") == 1
    assert len(broker.clients) == 2
    assert broker.clients[0].loop_stop_calls == 1


# U2: a start the printer echoes that Link did not publish on this client is
# recorded as unexpected_start. The printer echoes every project_file it takes
# on the report topic.
class _EventLog:
    def __init__(self):
        self.events = []

    def record_event(self, kind, **fields):
        self.events.append((kind, fields))

    def record_message(self, direction, topic, payload, *, accepted=None):
        pass


def _start_with_id(task_id, file="plate.3mf", *, subtask_only=False):
    body = {"sequence_id": "20000", "command": "project_file",
            "param": "Metadata/plate_1.gcode", "file": file,
            "subtask_id": task_id}
    if not subtask_only:
        body["task_id"] = task_id
    return {"print": body}


def _echo(start):
    return {"print": {**start["print"], "result": "success", "reason": "success"}}


def _unexpected(log):
    return [fields for kind, fields in log.events if kind == "unexpected_start"]


def _logged_session(clock, broker, log, serial=_SERIAL):
    return LinkSession(
        "10.0.0.5", "secret-code", serial, client_factory=broker.factory,
        monotonic=clock, watchdog_interval=None, log=log,
    )


def _fire_report(client, serial, doc):
    client.fire_message(f"device/{serial}/report", json.dumps(doc).encode())


def test_an_echo_of_the_start_link_just_published_records_nothing(caplog):
    clock = Clock(5000.0)
    broker = _ReplayBroker()
    log = _EventLog()
    session = _logged_session(clock, broker, log)
    session.start()
    broker.accept(broker.current)
    start = _start_with_id("5000000001")
    assert session.publish(start) is True

    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        _fire_report(broker.current, _SERIAL, _echo(start))

    assert _unexpected(log) == []
    assert not [r for r in caplog.records if "start" in r.getMessage()]


def test_an_echo_of_a_start_from_the_previous_client_is_link_earlier(caplog):
    clock = Clock(6000.0)
    broker = _ReplayBroker()
    log = _EventLog()
    session = _logged_session(clock, broker, log)
    session.start()
    broker.accept(broker.current)
    start = _start_with_id("5000000002", "batch-old-1.3mf")
    assert session.publish(start) is True

    broker.drop()
    clock.now += 5
    session.tick()
    broker.accept(broker.current)
    clock.now += 2
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        _fire_report(broker.current, _SERIAL, _echo(start))

    assert _unexpected(log) == [{
        "file": "batch-old-1.3mf",
        "task_id": "5000000002",
        "seconds_since_connack": 2.0,
        "origin": "link_earlier",
    }]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert _SERIAL in warnings[0] and "batch-old-1.3mf" in warnings[0]


def test_an_echo_with_an_id_link_never_published_is_other_sender(caplog):
    clock = Clock(7000.0)
    broker = _ReplayBroker()
    log = _EventLog()
    session = _logged_session(clock, broker, log)
    session.start()
    broker.accept(broker.current)
    clock.now += 1.5
    # No task_id: the subtask_id is the fallback.
    stranger = _start_with_id("7000000009", "studio-send.3mf", subtask_only=True)
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        _fire_report(broker.current, _SERIAL, _echo(stranger))

    assert _unexpected(log) == [{
        "file": "studio-send.3mf",
        "task_id": "7000000009",
        "seconds_since_connack": 1.5,
        "origin": "other_sender",
    }]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert _SERIAL in warnings[0] and "studio-send.3mf" in warnings[0]


def test_connack_and_disconnect_carry_the_attempt_and_the_reason():
    clock = Clock(8000.0)
    broker = _ReplayBroker()
    log = _EventLog()
    session = _logged_session(clock, broker, log)
    session.start()
    # The first client never reaches CONNACK.
    broker.current.fire_disconnect(_CONN_LOST)
    clock.now += 5
    session.tick()
    broker.accept(broker.current)
    broker.current.up = False
    broker.current.fire_disconnect(ReasonCode(PacketTypes.DISCONNECT, identifier=141))
    clock.now += 5
    session.tick()
    broker.accept(broker.current)

    connacks = [f for kind, f in log.events if kind == "connack"]
    disconnects = [f for kind, f in log.events if kind == "disconnect"]
    assert [f["attempt"] for f in connacks] == [2, 1]
    assert [f["attempt"] for f in disconnects] == [1, 2]
    assert disconnects[1]["reason"] == "Keep alive timeout"
    assert disconnects[1]["code"] == 141


def test_a_redial_whose_client_fails_to_start_is_redialled_again(caplog):
    """A failed ``_open`` (loop_start could not start its thread) must not
    strand the session: the next backoff builds another client."""
    clock = Clock(4000.0)
    broker = _ReplayBroker()
    real_factory = broker.factory

    def factory(client_id):
        client = real_factory(client_id)
        if len(broker.clients) == 2:
            def refuse():
                raise RuntimeError("can't start new thread")
            client.loop_start = refuse
        return client

    session = LinkSession(
        "10.0.0.5", "secret-code", _SERIAL, client_factory=factory,
        monotonic=clock, watchdog_interval=None,
    )
    session.start()
    broker.accept(broker.current)
    broker.drop()

    escaped = []
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.session"):
        for _ in range(30):
            clock.now += 1
            try:
                session.tick()
            except Exception as exc:  # the watchdog loop would log and go on
                escaped.append(exc)
    assert len(broker.clients) == 3
    assert broker.clients[2].loop_started == 1
    assert escaped == []
    assert "redial" in caplog.text
    broker.accept(broker.current)
    assert session.connected is True
