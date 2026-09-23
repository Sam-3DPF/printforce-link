"""Session liveness, the command probe, and same-object recovery.

Clocks are injected. The watchdog thread is off so each test calls ``tick``
itself; nothing here waits on a real second.
"""
import json
import threading

from bridge.bambu.session import LinkSession
from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


_SERIAL = "01P00A123456789"

# Shape a get_version reply must have for the probe to treat it as the answer.
# This is the contract the matcher pins, not a capture from a shop printer.
EXPECTED_GET_VERSION_REPLY = {
    "info": {
        "command": "get_version",
        "sequence_id": "<ours>",
        "module": [{"name": "ota"}],
    },
}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.publishes = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.disconnect_calls = 0
        self.loop_stop_calls = 0

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
        self.loop_stop_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
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


class Broker:
    def __init__(self):
        self.clients = []

    def factory(self, client_id):
        client = FakeClient()
        client.client_id = client_id
        self.clients.append(client)
        return client

    @property
    def current(self):
        return self.clients[-1]


def _session(broker, clock, **kwargs):
    return LinkSession(
        "10.0.0.5",
        "secret-code",
        _SERIAL,
        client_factory=broker.factory,
        monotonic=clock,
        watchdog_interval=None,
        **kwargs,
    )


def _bodies(client):
    return [json.loads(payload) for _topic, payload, _qos in client.publishes]


def _report(client, doc):
    client.fire_message(f"device/{_SERIAL}/report", json.dumps(doc).encode())


def _version_reply(sequence_id, *, echo_sequence=True):
    info = {
        "command": EXPECTED_GET_VERSION_REPLY["info"]["command"],
        "module": list(EXPECTED_GET_VERSION_REPLY["info"]["module"]),
    }
    if echo_sequence:
        info["sequence_id"] = sequence_id
    return {"info": info}


def _go_live(session, broker):
    session.start()
    broker.current.fire_connack(0)
    _report(broker.current, {"print": {"gcode_state": "IDLE"}})


def _linked_printer(clock, broker, **session_kwargs):
    cfg = PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret", name="P1")

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip,
            access_code,
            serial,
            on_report=on_report,
            client_factory=broker.factory,
            monotonic=clock,
            watchdog_interval=None,
            **session_kwargs,
        )

    return BambuPrinter(
        cfg,
        session_factory=factory,
        monotonic=clock,
        sleep=lambda _seconds: None,
    )


def test_silence_stays_live_at_59s_and_resets_once_then_honors_the_30s_cooldown():
    clock = Clock(1000.0)
    broker = Broker()
    session = _session(broker, clock)
    _go_live(session, broker)
    assert session.state == "live"
    assert session.down_reason is None

    clock.now = 1059.0
    session.tick()
    assert session.state == "live"
    assert len(broker.clients) == 1

    clock.now = 1061.0
    session.tick()
    assert len(broker.clients) == 2
    assert session.down_reason == "silent_session"
    assert broker.clients[0] is not broker.clients[1]
    assert broker.clients[0].loop_stop_calls >= 1

    broker.current.fire_connack(0)
    session.tick()
    assert len(broker.clients) == 2

    # The new CONNACK restarts the silence clock, so the old report time does
    # not make a client that just connected look stale.
    clock.now = 1061.0 + 59
    session.tick()
    assert len(broker.clients) == 2

    clock.now = 1061.0 + 61
    session.tick()
    assert len(broker.clients) == 3


def test_a_second_reset_waits_out_the_30s_cooldown():
    clock = Clock(1000.0)
    broker = Broker()
    session = _session(broker, clock, command_probe=True)
    _go_live(session, broker)

    clock.now = 1061.0
    session.tick()
    assert len(broker.clients) == 2
    broker.current.fire_connack(0)
    _report(broker.current, {"print": {"gcode_state": "IDLE"}})

    session.probe()
    clock.now = 1071.0
    session.tick()
    session.probe()
    clock.now = 1081.0
    session.tick()
    assert session.down_reason == "commands_ignored"
    assert len(broker.clients) == 2

    clock.now = 1091.0
    session.tick()
    assert len(broker.clients) == 3


def test_printer_state_survives_an_in_place_session_rebuild():
    clock = Clock(1000.0)
    broker = Broker()
    printer = _linked_printer(clock, broker)
    printer.connect()
    broker.current.fire_connack(0)
    _report(broker.current, {"print": {"gcode_state": "RUNNING", "mc_percent": 10}})
    # PAUSE does not clear the cancel latch the way RUNNING does. The latch
    # and the merged percent both have to outlive a same-object client rebuild.
    _report(broker.current, {
        "print": {"gcode_state": "PAUSE", "print_error": 50348044, "mc_percent": 10},
    })
    assert printer.state.view()["user_cancelled"] is True
    printer._stopwatch._duration_seconds = 90
    printer._stopwatch._source = "bridge"
    stopwatch = printer._stopwatch

    printer.rebuild_session()

    kept = printer.state.view()
    assert kept["user_cancelled"] is True
    assert kept["payload"]["print"]["mc_percent"] == 10
    assert kept["payload"]["print"]["gcode_state"] == "PAUSE"
    assert printer._stopwatch is stopwatch
    assert printer._stopwatch.duration_seconds == 90
    assert printer._stopwatch.source == "bridge"
    assert len(broker.clients) == 2


def test_one_missed_probe_does_not_reset_and_two_do():
    clock = Clock(2000.0)
    broker = Broker()
    session = _session(broker, clock, command_probe=True)
    _go_live(session, broker)

    assert session.probe() == "1"
    clock.now += 10
    session.tick()
    assert len(broker.clients) == 1
    assert session.down_reason != "commands_ignored"
    assert session.state == "live"

    assert session.probe() == "2"
    clock.now += 10
    session.tick()
    assert len(broker.clients) == 2
    assert session.down_reason == "commands_ignored"


def test_a_reply_clears_one_miss_including_a_reply_with_no_sequence_id():
    clock = Clock(3000.0)
    broker = Broker()
    session = _session(broker, clock, command_probe=True)
    _go_live(session, broker)

    seq = session.probe()
    _report(broker.current, _version_reply(seq))
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 1

    missed = session.probe()
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 1
    assert session.state == "live"

    _report(broker.current, _version_reply(missed))
    assert session.probe() == "3"
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 1
    _report(broker.current, _version_reply("3", echo_sequence=False))

    assert session.probe() == "4"
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 1
    assert session.down_reason != "commands_ignored"


def test_a_get_version_reply_for_someone_else_does_not_clear_the_miss():
    clock = Clock(3500.0)
    broker = Broker()
    session = _session(broker, clock, command_probe=True)
    _go_live(session, broker)

    seq = session.probe()
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 1
    _report(broker.current, _version_reply("not-" + seq))
    assert session.probe() is not None
    clock.now += 11
    session.tick()
    assert len(broker.clients) == 2
    assert session.down_reason == "commands_ignored"


def test_a_live_session_probes_every_300s_when_enabled():
    clock = Clock(4000.0)
    broker = Broker()
    session = _session(broker, clock, command_probe=True)
    _go_live(session, broker)
    connack_bodies = _bodies(broker.current)

    clock.now = 4250.0
    _report(broker.current, {"print": {"gcode_state": "IDLE", "sequence_id": "keep-alive"}})
    clock.now = 4299.0
    session.tick()
    assert _bodies(broker.current) == connack_bodies

    clock.now = 4300.0
    session.tick()
    probe_bodies = _bodies(broker.current)[len(connack_bodies):]
    assert probe_bodies == [
        {"info": {"sequence_id": "1", "command": "get_version"}},
    ]


def test_probe_stays_off_until_a_shop_reply_is_pinned():
    import bridge.bambu.session as session_mod

    assert getattr(session_mod, "COMMAND_PROBE_ENABLED", True) is False
    clock = Clock(5000.0)
    broker = Broker()
    session = _session(broker, clock)
    _go_live(session, broker)
    assert session.probe() is None
    connack_bodies = _bodies(broker.current)
    clock.now = 5250.0
    _report(broker.current, {"print": {"gcode_state": "IDLE"}})
    clock.now = 5301.0
    session.tick()
    assert _bodies(broker.current) == connack_bodies


def test_reset_drops_unacked_qos1_commands():
    clock = Clock(6000.0)
    broker = Broker()
    session = _session(broker, clock)
    _go_live(session, broker)
    assert session.publish({
        "print": {
            "sequence_id": "9",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
        },
    }) is True
    old = broker.current
    assert any(body.get("print", {}).get("command") == "project_file" for body in _bodies(old))

    clock.now += 61
    session.tick()
    new = broker.current
    assert new is not old
    new.fire_connack(0)
    assert _bodies(new) == [
        {"pushing": {"sequence_id": "0", "command": "pushall"}},
        {"info": {"sequence_id": "0", "command": "get_version"}},
    ]


def test_auth_rejected_stops_fast_retry_and_redials_after_300s():
    clock = Clock(7000.0)
    broker = Broker()
    session = _session(broker, clock)
    session.start()
    broker.current.fire_connack(134)
    assert session.last_connect_error == "auth_rejected"
    assert session.down_reason == "auth_rejected"
    assert broker.current.disconnect_calls >= 1

    clock.now = 7000.0 + 30
    session.tick()
    assert len(broker.clients) == 1
    assert session.down_reason == "auth_rejected"

    clock.now = 7000.0 + 299
    session.tick()
    assert len(broker.clients) == 1

    clock.now = 7000.0 + 300
    session.tick()
    assert len(broker.clients) == 2
    assert session.down_reason == "auth_rejected"
    assert session.last_connect_error == "auth_rejected"

    broker.current.fire_connack(135)
    assert session.down_reason == "auth_rejected"
    assert len(broker.clients) == 2
    clock.now = 7300.0 + 300
    session.tick()
    assert len(broker.clients) == 3
    broker.current.fire_connack(0)
    assert session.last_connect_error is None
    assert session.down_reason is None
    assert session.had_session is True


def test_refused_is_not_relabelled_unreachable():
    clock = Clock(8000.0)
    broker = Broker()
    session = _session(broker, clock)
    session.start()
    broker.current.fire_connack(4)
    clock.now += 61
    session.tick()
    assert session.down_reason == "refused"
    assert session.state != "offline"
    assert len(broker.clients) == 1


def test_socket_down_past_60s_is_unreachable_without_a_reset():
    clock = Clock(9000.0)
    broker = Broker()
    printer = _linked_printer(clock, broker)
    printer.connect()
    clock.now = 9059.0
    printer._session.tick()
    assert printer.connection_state == "connecting"
    assert printer.down_reason is None

    clock.now = 9061.0
    printer._session.tick()
    assert printer.connection_state == "offline"
    assert printer.down_reason == "unreachable"
    assert len(broker.clients) == 1
    assert printer._session.had_session is False


def test_a_live_socket_that_drops_is_unreachable_and_not_reset_by_the_stale_timer():
    clock = Clock(10000.0)
    broker = Broker()
    session = _session(broker, clock)
    _go_live(session, broker)
    assert session.had_session is True
    broker.current.fire_disconnect(128)
    assert session.connected is False
    clock.now += 61
    session.tick()
    assert session.state == "offline"
    assert session.down_reason == "unreachable"
    assert len(broker.clients) == 1
    assert session.silent_for(clock.now) > 300 or session.silent_for(clock.now) >= 61


def test_had_session_and_silent_for_follow_the_last_report():
    clock = Clock(11000.0)
    broker = Broker()
    session = _session(broker, clock)
    session.start()
    assert session.had_session is False
    assert session.silent_for(clock.now) is None
    broker.current.fire_connack(0)
    assert session.had_session is True
    assert session.silent_for(clock.now) == 0
    clock.now += 10
    assert session.silent_for(clock.now) == 10
    _report(broker.current, {"print": {"gcode_state": "IDLE"}})
    clock.now += 4
    assert session.silent_for(clock.now) == 4
    assert session.state == "live"


def test_watchdog_ticks_until_disconnect():
    clock = Clock(12000.0)
    broker = Broker()
    seen = threading.Event()
    session = LinkSession(
        "10.0.0.5",
        "secret-code",
        _SERIAL,
        client_factory=broker.factory,
        monotonic=clock,
        watchdog_interval=0.01,
    )

    def _mark():
        seen.set()

    session.tick = _mark
    session.start()
    assert seen.wait(0.5)
    session.disconnect()
    seen.clear()
    assert not seen.wait(0.05)
    assert session.connected is False
