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
    p = _printer(sessions)
    p.connect()
    p._cached = {"print": {"gcode_state": "RUNNING"}}
    p._last_raw = {"print": {}}
    p._last_fresh_monotonic = 123.0
    p._historical_failed_streak = 1
    p.reconnect(new_ip="192.168.1.55")
    assert p._cached is None            # old address's state must not leak into the new one
    assert p._last_raw is None
    assert p._last_fresh_monotonic is None
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
