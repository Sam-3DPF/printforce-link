"""Report fields that sit beside status: connection, message age, connect error,
and the session's CONNACK time.

`status` stays the value today's silence and disconnect paths already produce
(OFFLINE) so an older ingest still refuses to dispatch. `connection` is what
says whether the telemetry in the same report is live, last-known, or absent.
"""

from datetime import datetime, timezone

from bridge.bambu.session import LinkSession
from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter, _DEFAULT_STALE_AFTER_SECONDS
from tests.test_liveness import Broker, Clock, _report
from tests.test_telemetry import FakeClock

_SERIAL = "01P00A123456789"
_SLOT = {"slot_number": 1, "color_hex": "FF6A13FF", "filament_type": "PLA"}
_PRINTING = {"print": {
    "gcode_state": "RUNNING",
    "mc_percent": 47,
    "nozzle_temper": 220.0,
    "ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"}]},
    ]},
}}


class _Session:
    """A session whose state the test sets. It does not deliver payloads."""

    def __init__(self, *, connected=True, state="live", down_reason=None,
                 connack_at=None, session_started_at=None):
        self.connected = connected
        self.state = state
        self.down_reason = down_reason
        self.connack_at = connack_at
        self.session_started_at = session_started_at
        self.published = []

    def publish(self, payload):
        self.published.append(payload)
        return True


def _printer(clock, session, *, stale_after_seconds=_DEFAULT_STALE_AFTER_SECONDS):
    printer = BambuPrinter(
        PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret", name="P1S-1"),
        monotonic=clock.now,
        stale_after_seconds=stale_after_seconds,
        sleep=lambda _seconds: None,
    )
    printer._session = session
    return printer


def _iso(epoch):
    text = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def test_live_report_says_connection_live_and_carries_the_session_stamp():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, session_started_at="2026-09-22T12:00:00Z")
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "live"
    assert snapshot["status"] == "PRINTING"
    assert snapshot["gcode_state"] == "RUNNING"
    assert snapshot["progress_percent"] == 47
    assert snapshot["nozzle_temper"] == 220.0
    assert snapshot["slots"] == [_SLOT]
    assert snapshot["last_message_age_seconds"] == 0.0
    assert snapshot["connect_error"] is None
    assert snapshot["session_started_at"] == "2026-09-22T12:00:00Z"


def test_connect_error_is_none_while_the_report_is_live():
    """A stale down-reason left on the session must not mark a live report."""
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, down_reason="silent_session")
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "live"
    assert snapshot["connect_error"] is None


def test_last_message_age_seconds_is_none_until_a_report_then_seconds():
    clock = FakeClock(2000.0)
    session = _Session(connack_at=2000.0)
    printer = _printer(clock, session)
    before = printer.snapshot()
    assert before["last_message_age_seconds"] is None
    assert before["connection"] == "offline"

    printer._on_mqtt_report({"print": {"gcode_state": "IDLE"}})
    assert printer.snapshot()["last_message_age_seconds"] == 0.0

    clock.advance(12.34)
    aged = printer.snapshot()
    assert aged["connection"] == "live"
    assert aged["last_message_age_seconds"] == 12.3


def test_stale_session_keeps_last_telemetry_and_slots_with_status_offline():
    """Silence past the window is not a live print. Status stays OFFLINE — that
    is what blocks dispatch — and the report still carries the last payload
    under connection stale, including the last AMS slots."""
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, state="live")
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    assert printer.snapshot()["connection"] == "live"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    session.state = "stale"
    session.down_reason = "silent_session"
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "stale"
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["gcode_state"] == "RUNNING"
    assert snapshot["progress_percent"] == 47
    assert snapshot["nozzle_temper"] == 220.0
    assert snapshot["slots"] == [_SLOT]
    assert snapshot["connect_error"] == "silent_session"
    assert snapshot["last_message_age_seconds"] == _DEFAULT_STALE_AFTER_SECONDS + 1


def test_connected_silence_past_the_window_is_stale_not_a_blank_report():
    """The session can still say live for its own 60s net while the report
    window (stale_after_seconds) has already closed. That report is stale,
    not a claim that the AMS was emptied."""
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, state="live", down_reason=None)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "stale"
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["slots"] == [_SLOT]
    assert snapshot["progress_percent"] == 47
    assert snapshot["connect_error"] is None


def test_offline_report_sends_slots_none_and_null_telemetry_but_keeps_state():
    """Session offline (socket down past 60s / unreachable) reports nothing it
    cannot currently see. `slots: []` would delete the cloud's slot rows;
    None leaves them. The merged payload stays for the next report."""
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    printer.snapshot()

    session.connected = False
    session.state = "offline"
    session.down_reason = "unreachable"
    clock.advance(61)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "offline"
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["slots"] is None
    assert snapshot["progress_percent"] is None
    assert snapshot["nozzle_temper"] is None
    assert snapshot["gcode_state"] is None
    assert snapshot["connect_error"] == "unreachable"
    assert snapshot["last_message_age_seconds"] == 61.0
    kept = printer.state.view()
    assert kept["payload"]["print"]["mc_percent"] == 47
    assert kept["payload"]["print"]["nozzle_temper"] == 220.0


def test_auth_rejected_reports_offline_even_when_last_state_exists():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)

    session.connected = False
    session.state = "connecting"
    session.down_reason = "auth_rejected"
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "offline"
    assert snapshot["slots"] is None
    assert snapshot["nozzle_temper"] is None
    assert snapshot["connect_error"] == "auth_rejected"
    assert snapshot["status"] == "OFFLINE"


def test_refused_with_no_recent_message_is_offline_and_a_recent_one_is_stale():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)

    session.connected = False
    session.state = "connecting"
    session.down_reason = "refused"
    recent = printer.snapshot()
    assert recent["connection"] == "stale"
    assert recent["slots"] == [_SLOT]
    assert recent["connect_error"] == "refused"

    clock.advance(_DEFAULT_STALE_AFTER_SECONDS + 1)
    stale_refused = printer.snapshot()
    assert stale_refused["connection"] == "offline"
    assert stale_refused["slots"] is None
    assert stale_refused["progress_percent"] is None


def test_socket_down_at_most_60s_stays_stale_with_last_slots():
    """The session has not called the socket unreachable yet. Last telemetry
    is still the report, labelled stale, and status stays OFFLINE."""
    clock = FakeClock(3000.0)
    session = _Session(connack_at=3000.0, state="live")
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    printer.snapshot()

    session.connected = False
    clock.advance(1)
    snapshot = printer.snapshot()

    assert snapshot["connection"] == "stale"
    assert snapshot["status"] == "OFFLINE"
    assert snapshot["slots"] == [_SLOT]
    assert snapshot["progress_percent"] == 47
    assert snapshot["connect_error"] is None


def test_recovery_is_live_only_after_a_message_in_the_new_session():
    """A CONNACK alone must not present the previous payload as live. The
    next report merges onto the retained payload instead of starting over."""
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0, session_started_at="2026-09-22T12:00:00Z")
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    assert printer.snapshot()["connection"] == "live"

    session.connected = False
    session.state = "offline"
    session.down_reason = "unreachable"
    session.session_started_at = None
    clock.advance(70)
    offline = printer.snapshot()
    assert offline["connection"] == "offline"
    assert offline["slots"] is None
    assert offline["nozzle_temper"] is None

    clock.advance(1)
    session.connected = True
    session.state = "connecting"
    session.down_reason = None
    session.connack_at = clock.now()
    session.session_started_at = "2026-09-22T12:05:00Z"
    before_message = printer.snapshot()
    assert before_message["connection"] == "stale"
    assert before_message["connection"] != "live"
    assert before_message["status"] == "OFFLINE"
    assert before_message["slots"] == [_SLOT]
    assert before_message["progress_percent"] == 47
    assert before_message["session_started_at"] == "2026-09-22T12:05:00Z"

    printer._on_mqtt_report({
        "print": {"gcode_state": "IDLE", "nozzle_temper": 24.0, "mc_percent": 0},
    })
    recovered = printer.snapshot()
    assert recovered["connection"] == "live"
    assert recovered["status"] == "IDLE"
    assert recovered["nozzle_temper"] == 24.0
    assert recovered["progress_percent"] == 0
    assert recovered["slots"] == [_SLOT]
    assert recovered["connect_error"] is None


def test_offline_then_a_partial_report_merges_onto_the_retained_payload():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)

    session.connected = False
    session.state = "offline"
    session.down_reason = "unreachable"
    assert printer.snapshot()["slots"] is None

    session.connected = True
    session.state = "live"
    session.down_reason = None
    session.connack_at = clock.now()
    printer._on_mqtt_report({"print": {"nozzle_temper": 24.0}})
    merged = printer.state.view()["payload"]["print"]
    assert merged["mc_percent"] == 47
    assert merged["nozzle_temper"] == 24.0
    assert merged["gcode_state"] == "RUNNING"


def test_replay_into_state_feeds_inbound_messages_through_ingest():
    from tests.replay import load_fixture, replay_into_state
    from bridge.bambu.state import PrinterState

    state = PrinterState(bambu_id="01P00C4A0000001", monotonic=lambda: 0.0)
    replay_into_state(state, load_fixture(
        "tests/fixtures/replay/p1s-synthetic.json"))
    payload = state.view()["payload"]["print"]
    assert payload["gcode_state"] == "RUNNING"
    assert payload["mc_percent"] == 43
    assert payload["nozzle_temper"] == 220.0
    assert payload["subtask_name"] == "benchy"


def test_reconnect_clear_drops_the_merged_payload_on_the_state_object():
    clock = FakeClock(1000.0)
    session = _Session(connack_at=1000.0)
    printer = _printer(clock, session)
    printer._on_mqtt_report(_PRINTING)
    assert printer.state.view()["payload"] is not None
    assert printer.state.view()["last_fresh_monotonic"] is not None

    printer.state.clear()
    cleared = printer.state.view()
    assert cleared["payload"] is None
    assert cleared["last_raw"] is None
    assert cleared["last_fresh_monotonic"] is None
    assert cleared["last_message_monotonic"] is None


def test_session_started_at_is_iso_utc_of_the_successful_connack():
    """The stamp is the wall clock at CONNACK, not the monotonic liveness clock.
    A replacement client has none until its own handshake succeeds."""
    clock = Clock(5000.0)
    wall = {"now": 1_700_000_000.0}
    broker = Broker()
    session = LinkSession(
        "10.0.0.5",
        "secret-code",
        _SERIAL,
        client_factory=broker.factory,
        monotonic=clock,
        wall_clock=lambda: wall["now"],
        watchdog_interval=None,
    )
    assert session.session_started_at is None
    session.start()
    assert session.session_started_at is None
    broker.current.fire_connack(134)
    assert session.session_started_at is None
    assert session.connack_at is None

    broker.current.fire_connack(0)
    assert session.connack_at == 5000.0
    assert session.session_started_at == _iso(1_700_000_000.0)

    wall["now"] = 1_700_000_100.0
    session.hard_reset()
    assert session.session_started_at is None
    broker.current.fire_connack(0)
    assert session.session_started_at == _iso(1_700_000_100.0)


def test_real_session_down_under_60s_is_stale_and_past_it_is_offline():
    """LinkSession keeps its previous state until the socket has been down 60s.
    The report follows that: stale with the last slots, then offline with
    slots None. A later CONNACK is not live until a new report arrives, and
    that report merges onto the payload offline kept."""
    clock = Clock(8000.0)
    wall = {"now": 1_700_000_000.0}
    broker = Broker()

    def factory(ip, access_code, serial, on_report):
        return LinkSession(
            ip, access_code, serial, on_report=on_report,
            client_factory=broker.factory, monotonic=clock,
            wall_clock=lambda: wall["now"], watchdog_interval=None,
        )

    printer = BambuPrinter(
        PrinterConfig(bambu_id=_SERIAL, ip="10.0.0.5", access_code="secret", name="P1S"),
        monotonic=clock, sleep=lambda _seconds: None, session_factory=factory,
    )
    printer.connect()
    broker.current.fire_connack(0)
    _report(broker.current, _PRINTING)
    live = printer.snapshot()
    assert live["connection"] == "live"
    assert live["status"] == "PRINTING"
    assert live["session_started_at"] == _iso(1_700_000_000.0)
    assert live["slots"] == [_SLOT]

    broker.current.fire_disconnect(128)
    clock.now += 1
    quiet = printer.snapshot()
    assert quiet["connection"] == "stale"
    assert quiet["status"] == "OFFLINE"
    assert quiet["progress_percent"] == 47
    assert quiet["slots"] == [_SLOT]

    clock.now += 61
    printer._session.tick()
    gone = printer.snapshot()
    assert printer.connection_state == "offline"
    assert gone["connection"] == "offline"
    assert gone["connect_error"] == "unreachable"
    assert gone["slots"] is None
    assert gone["progress_percent"] is None
    assert printer.state.view()["payload"]["print"]["mc_percent"] == 47

    wall["now"] = 1_700_000_090.0
    clock.now += 1
    broker.current.fire_connack(0)
    waiting = printer.snapshot()
    assert waiting["connection"] == "stale"
    assert waiting["status"] == "OFFLINE"
    assert waiting["session_started_at"] == _iso(1_700_000_090.0)
    assert waiting["slots"] == [_SLOT]

    _report(broker.current, {
        "print": {"gcode_state": "IDLE", "nozzle_temper": 24.0, "mc_percent": 0},
    })
    back = printer.snapshot()
    assert back["connection"] == "live"
    assert back["status"] == "IDLE"
    assert back["nozzle_temper"] == 24.0
    assert back["progress_percent"] == 0
    assert back["slots"] == [_SLOT]


def test_the_report_loop_helper_cancels_the_previous_dump_when_it_rearms(monkeypatch):
    import bridge.app as app_module

    calls = []
    monkeypatch.setattr(
        app_module.faulthandler, "cancel_dump_traceback_later",
        lambda: calls.append("cancel"),
    )
    monkeypatch.setattr(
        app_module.faulthandler, "dump_traceback_later",
        lambda timeout, exit=False: calls.append(("dump", timeout, exit)),
    )
    app_module.arm_report_loop_dump()
    app_module.arm_report_loop_dump()
    assert calls == [
        "cancel", ("dump", 30.0, False),
        "cancel", ("dump", 30.0, False),
    ]
