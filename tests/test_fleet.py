"""Fleet self-healing (U1) and dynamic membership (U2).

Exercises the fleet's connection-management logic in isolation: a fake printer records
connect/disconnect/reconnect calls, and a fake `discover_fn` stands in for the SSDP
scan, so no library and no live hardware are needed.
"""
from bridge.config import PrinterConfig
from bridge.discover import DiscoveredPrinter
from bridge.fleet import Fleet


class FakePrinter:
    """Stand-in for BambuPrinter with the surface the Fleet uses."""

    def __init__(self, cfg, stale_after_seconds=None):
        self.cfg = cfg
        self.bambu_id = cfg.bambu_id
        self.current_ip = cfg.ip
        self.is_offline = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.reconnects = []            # new_ip passed to each reconnect()

    def connect(self):
        self.connect_calls += 1
        if self.cfg.name == "FAIL":     # opt a printer into a connect failure
            raise OSError("no route to host")

    def disconnect(self):
        self.disconnect_calls += 1

    def reconnect(self, new_ip=None):
        self.reconnects.append(new_ip)
        if new_ip:
            self.current_ip = new_ip
        self.is_offline = False

    def snapshot(self):
        return {"bambu_id": self.bambu_id, "status": "OFFLINE" if self.is_offline else "IDLE"}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cfg(serial, ip, name=""):
    return PrinterConfig(bambu_id=serial, ip=ip, access_code="SECRET", name=name)


def _fleet(configs, discovered=None, clock=None):
    calls = {"count": 0}

    def discover_fn(timeout):
        calls["count"] += 1
        return list(discovered() if callable(discovered) else (discovered or []))

    clock = clock or Clock()
    fleet = Fleet(configs, printer_factory=FakePrinter, discover_fn=discover_fn,
                  rediscover_interval_seconds=60.0, monotonic=clock)
    return fleet, calls, clock


# ---- U1: self-healing reconnection -------------------------------------------------

def test_no_scan_when_all_online():
    fleet, calls, _ = _fleet([_cfg("S1", "192.168.1.10")])
    fleet.reconcile_connections()
    assert calls["count"] == 0          # nothing offline -> no SSDP scan at all


def test_reconnects_printer_that_moved_ip():
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1", name="P1", model="C12")],
    )
    p = fleet.by_id("S1")
    p.is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 1
    assert p.reconnects == ["192.168.1.55"]
    assert p.current_ip == "192.168.1.55"


def test_same_ip_offline_is_not_reconnected():
    # Offline but still at the same address: paho keeps retrying, so rebuilding the
    # client would throw away its in-progress reconnection. Do nothing.
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.10", serial="S1")],
    )
    p = fleet.by_id("S1")
    p.is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 1
    assert p.reconnects == []


def test_absent_printer_is_not_reconnected():
    # Offline and not seen on the LAN this scan -> leave it; the next interval scans again.
    fleet, calls, _ = _fleet([_cfg("S1", "192.168.1.10")], discovered=[])
    p = fleet.by_id("S1")
    p.is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 1
    assert p.reconnects == []


def test_scan_is_throttled():
    clock = Clock(1000.0)
    fleet, calls, _ = _fleet([_cfg("S1", "192.168.1.10")], discovered=[], clock=clock)
    fleet.by_id("S1").is_offline = True
    fleet.reconcile_connections()       # scans at t=1000
    fleet.reconcile_connections()       # within the interval -> no second scan
    assert calls["count"] == 1
    clock.t = 1000.0 + 61               # past the 60s interval
    fleet.reconcile_connections()
    assert calls["count"] == 2


def test_two_printers_swap_ips():
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10"), _cfg("S2", "192.168.1.11")],
        discovered=[DiscoveredPrinter(ip="192.168.1.11", serial="S1"),
                    DiscoveredPrinter(ip="192.168.1.10", serial="S2")],
    )
    s1, s2 = fleet.by_id("S1"), fleet.by_id("S2")
    s1.is_offline = s2.is_offline = True
    fleet.reconcile_connections()
    assert s1.current_ip == "192.168.1.11"   # each rebinds to its OWN serial's new IP
    assert s2.current_ip == "192.168.1.10"


def test_discovery_failure_is_swallowed_and_still_throttled():
    calls = {"count": 0}

    def boom(timeout):
        calls["count"] += 1
        raise OSError("network down")

    clock = Clock(1000.0)
    fleet = Fleet([_cfg("S1", "192.168.1.10")], printer_factory=FakePrinter,
                  discover_fn=boom, rediscover_interval_seconds=60.0, monotonic=clock)
    fleet.by_id("S1").is_offline = True
    fleet.reconcile_connections()            # scan raises internally -> swallowed, not re-raised
    fleet.reconcile_connections()            # within the interval -> must NOT scan again
    assert calls["count"] == 1               # throttle timestamp advanced despite the failure
    assert fleet.by_id("S1").reconnects == []


# ---- U2: dynamic fleet membership --------------------------------------------------

def test_add_printer_connects_and_appears():
    fleet, _, _ = _fleet([])
    fleet.add_printer(_cfg("S1", "192.168.1.10", "New"))
    p = fleet.by_id("S1")
    assert p is not None and p.connect_calls == 1
    assert [r["bambu_id"] for r in fleet.snapshot()] == ["S1"]


def test_add_existing_serial_is_idempotent():
    fleet, _, _ = _fleet([_cfg("S1", "192.168.1.10")])
    first = fleet.by_id("S1")
    fleet.add_printer(_cfg("S1", "192.168.1.99"))
    assert fleet.by_id("S1") is first        # not replaced or duplicated
    assert len(fleet.snapshot()) == 1


def test_add_printer_connect_failure_still_registers():
    # A printer added before it is reachable must still join the fleet — it self-heals
    # via reconcile_connections rather than being dropped.
    fleet, _, _ = _fleet([])
    fleet.add_printer(_cfg("S1", "192.168.1.10", "FAIL"))
    assert fleet.by_id("S1") is not None


def test_remove_printer_disconnects_and_drops():
    fleet, _, _ = _fleet([_cfg("S1", "192.168.1.10"), _cfg("S2", "192.168.1.11")])
    s1 = fleet.by_id("S1")
    fleet.remove_printer("S1")
    assert s1.disconnect_calls == 1
    assert fleet.by_id("S1") is None
    assert [r["bambu_id"] for r in fleet.snapshot()] == ["S2"]


def test_remove_unknown_serial_is_noop():
    fleet, _, _ = _fleet([_cfg("S1", "192.168.1.10")])
    fleet.remove_printer("NOPE")             # must not raise
    assert len(fleet.snapshot()) == 1
