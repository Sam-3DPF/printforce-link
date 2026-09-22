"""Link-owned MQTT session: one client per printer, report topic only, no camera."""
import json
import socket
import time

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bridge.bambu.session import LinkSession, build_paho_client
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


def test_clean_disconnect_within_10s_of_a_report_is_ignored_and_an_error_is_not():
    clock = Clock()
    fake = FakePaho()
    session = _session(fake, monotonic=clock)
    session.start()
    fake.fire_connack(0)

    fake.fire_disconnect(0)
    assert session.connected is False  # no report yet — a clean drop counts

    fake.fire_connack(0)
    fake.fire_message(f"device/{_SERIAL}/report", b'{"print": {"gcode_state": "IDLE"}}')
    assert session.last_message_at == clock.now
    fake.fire_disconnect(0)
    assert session.connected is True
    fake.fire_disconnect(ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection"))
    assert session.connected is True

    fake.fire_disconnect(128)
    assert session.connected is False

    other = FakePaho()
    quiet = _session(other, monotonic=clock)
    quiet.start()
    other.fire_connack(0)
    other.fire_message(f"device/{_SERIAL}/report", b'{"print": {"gcode_state": "IDLE"}}')
    clock.now += 11
    other.fire_disconnect(0)
    assert quiet.connected is False


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
    assert json.loads(payload) == {"print": {"command": "pause"}}
