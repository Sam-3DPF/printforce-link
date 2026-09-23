"""BambuPrinter.reconnect / disconnect (U1) — rebuild the MQTT session, optionally at a
new IP, without a live broker. The session factory records the IP each session was
built at and its connect/disconnect calls, so the "IP is a cache, serial is identity"
rebuild is testable in isolation.
"""
import pytest

from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


class _RecordingSession:
    def __init__(self, ip, record):
        self.ip = ip
        self._record = record

    def start(self):
        if self.ip == self._record.get("fail_ip"):
            raise OSError("no route to host")
        self._record["connects"].append(self.ip)

    def disconnect(self):
        self._record["disconnects"].append(self.ip)


@pytest.fixture
def sessions():
    record = {"constructed": [], "connects": [], "disconnects": []}

    def factory(ip, access_code, serial, on_report):
        record["constructed"].append(ip)
        return _RecordingSession(ip, record)

    record["factory"] = factory
    return record


def _printer(sessions, ip="192.168.1.10"):
    return BambuPrinter(
        PrinterConfig(bambu_id="S1", ip=ip, access_code="SECRET", name="P1"),
        session_factory=sessions["factory"],
    )


def test_connect_uses_the_configured_ip(sessions):
    p = _printer(sessions, "192.168.1.10")
    p.connect()
    assert sessions["constructed"] == ["192.168.1.10"]
    assert p.current_ip == "192.168.1.10"


def test_reconnect_rebuilds_at_new_ip(sessions):
    p = _printer(sessions, "192.168.1.10")
    p.connect()
    p.reconnect(new_ip="192.168.1.55")
    assert sessions["disconnects"] == ["192.168.1.10"]                    # old session closed first
    assert sessions["constructed"] == ["192.168.1.10", "192.168.1.55"]   # rebuilt at the new IP
    assert sessions["connects"][-1] == "192.168.1.55"
    assert p.current_ip == "192.168.1.55"


def test_reconnect_without_new_ip_keeps_the_ip(sessions):
    p = _printer(sessions, "192.168.1.10")
    p.connect()
    p.reconnect()
    assert p.current_ip == "192.168.1.10"
    assert sessions["constructed"] == ["192.168.1.10", "192.168.1.10"]


def test_reconnect_clears_cached_payload_and_freshness(sessions):
    """An address change drops the merged payload. It described the old
    address and must not be merged into the new one."""
    p = _printer(sessions)
    p.connect()
    p._on_mqtt_report({"print": {"gcode_state": "RUNNING"}})
    assert p.state.view()["payload"]["print"]["gcode_state"] == "RUNNING"
    assert p.state.view()["last_fresh_monotonic"] is not None
    assert p.state.view()["last_raw"] is not None
    p._historical_failed_streak = 1
    p.reconnect(new_ip="192.168.1.55")
    cleared = p.state.view()
    assert cleared["payload"] is None
    assert cleared["last_raw"] is None
    assert cleared["last_fresh_monotonic"] is None
    assert p._historical_failed_streak == 0


def test_disconnect_when_never_connected_is_safe(sessions):
    p = _printer(sessions)
    p.disconnect()                      # no session yet -> best-effort no-op, never raises
    assert sessions["disconnects"] == []


def test_failed_reconnect_keeps_old_ip_so_reconcile_retries(sessions):
    # If the new address can't be reached, current_ip must NOT advance — otherwise the
    # fleet's same-IP guard (d.ip == current_ip) would conclude paho is retrying a client
    # that never started and strand the printer OFFLINE forever.
    p = _printer(sessions, "192.168.1.10")
    p.connect()
    sessions["fail_ip"] = "192.168.1.99"
    with pytest.raises(OSError):
        p.reconnect(new_ip="192.168.1.99")
    assert p.current_ip == "192.168.1.10"   # still targeting the old IP -> reconcile retries


def test_net_info_from_a_report_is_remembered_until_a_later_net_block(sessions):
    p = _printer(sessions)
    p._on_mqtt_report({"print": {"net": {"info": [
        {"ip": 0x0A08A8C0},
        {"ip": 0},
    ]}}})
    assert p.address_candidates() == ["192.168.8.10"]
    # A status delta with no net block must not wipe the last interfaces.
    p._on_mqtt_report({"print": {"gcode_state": "IDLE"}})
    assert p.address_candidates() == ["192.168.8.10"]
    # A broken net block is not "no interfaces."
    p._on_mqtt_report({"print": {"net": {"info": "nope"}}})
    assert p.address_candidates() == ["192.168.8.10"]
    p._on_mqtt_report({"print": {"net": {"info": [{"ip": 0}]}}})
    assert p.address_candidates() == []


def test_proves_serial_at_keeps_the_access_code_inside_the_printer(sessions, monkeypatch):
    seen = {}

    def fake(ip, serial, access_code, **kwargs):
        seen["args"] = (ip, serial, access_code)
        seen["log"] = kwargs.get("log")
        return True

    monkeypatch.setattr("bridge.printer.proves_serial", fake)
    p = _printer(sessions)
    assert p.proves_serial_at("192.168.8.10") is True
    assert seen["args"] == ("192.168.8.10", "S1", "SECRET")
    assert seen["log"] is p.log
    assert not hasattr(p, "access_code")
