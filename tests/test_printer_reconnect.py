"""BambuPrinter.reconnect / disconnect (U1) — rebuild the MQTT client, optionally at a
new IP, without the real library or live hardware. `bambulabs_api` is replaced with a
fake that records the IP each client was built at and its connect/disconnect calls, so
the "IP is a cache, serial is identity" rebuild is testable in isolation.
"""
import sys
import types

import pytest

from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


@pytest.fixture
def fake_bl(monkeypatch):
    record = {"constructed": [], "connects": [], "disconnects": []}
    module = types.ModuleType("bambulabs_api")

    class FakeClient:
        def __init__(self, ip, access_code, serial):
            self.ip = ip
            record["constructed"].append(ip)

        def connect(self):
            if self.ip == record.get("fail_ip"):   # opt an address into an unreachable connect
                raise OSError("no route to host")
            record["connects"].append(self.ip)

        def disconnect(self):
            record["disconnects"].append(self.ip)

    module.Printer = FakeClient
    monkeypatch.setitem(sys.modules, "bambulabs_api", module)
    return record


def _printer(ip="192.168.1.10"):
    return BambuPrinter(PrinterConfig(bambu_id="S1", ip=ip, access_code="SECRET", name="P1"))


def test_connect_uses_the_configured_ip(fake_bl):
    p = _printer("192.168.1.10")
    p.connect()
    assert fake_bl["constructed"] == ["192.168.1.10"]
    assert p.current_ip == "192.168.1.10"


def test_reconnect_rebuilds_at_new_ip(fake_bl):
    p = _printer("192.168.1.10")
    p.connect()
    p.reconnect(new_ip="192.168.1.55")
    assert fake_bl["disconnects"] == ["192.168.1.10"]                    # old client closed first
    assert fake_bl["constructed"] == ["192.168.1.10", "192.168.1.55"]   # rebuilt at the new IP
    assert fake_bl["connects"][-1] == "192.168.1.55"
    assert p.current_ip == "192.168.1.55"


def test_reconnect_without_new_ip_keeps_the_ip(fake_bl):
    p = _printer("192.168.1.10")
    p.connect()
    p.reconnect()
    assert p.current_ip == "192.168.1.10"
    assert fake_bl["constructed"] == ["192.168.1.10", "192.168.1.10"]


def test_reconnect_clears_cached_payload_and_freshness(fake_bl):
    p = _printer()
    p.connect()
    p._cached = {"print": {"gcode_state": "RUNNING"}}
    p._last_raw = {"print": {}}
    p._last_fresh_monotonic = 123.0
    p.reconnect(new_ip="192.168.1.55")
    assert p._cached is None            # old address's state must not leak into the new one
    assert p._last_raw is None
    assert p._last_fresh_monotonic is None


def test_disconnect_when_never_connected_is_safe(fake_bl):
    p = _printer()
    p.disconnect()                      # _client is None -> best-effort no-op, never raises
    assert fake_bl["disconnects"] == []


def test_failed_reconnect_keeps_old_ip_so_reconcile_retries(fake_bl):
    # If the new address can't be reached, current_ip must NOT advance — otherwise the
    # fleet's same-IP guard (d.ip == current_ip) would conclude paho is retrying a client
    # that never started and strand the printer OFFLINE forever.
    p = _printer("192.168.1.10")
    p.connect()
    fake_bl["fail_ip"] = "192.168.1.99"
    with pytest.raises(OSError):
        p.reconnect(new_ip="192.168.1.99")
    assert p.current_ip == "192.168.1.10"   # still targeting the old IP -> reconcile retries
