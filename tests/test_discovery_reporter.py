"""DiscoveryReporter (U11): throttled LAN scan reported to 3DPF for the wizard."""
from bridge.discover import DiscoveredPrinter
from bridge.discovery_reporter import DiscoveryReporter


class FakeDpf:
    def __init__(self):
        self.reported = []

    def report_discovered(self, printers):
        self.reported.append(printers)
        return {"recorded": len(printers)}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_reports_discovered_printers_as_dicts():
    dpf = FakeDpf()
    found = [DiscoveredPrinter(ip="192.168.1.5", serial="S1", name="P1S-1", model="C12")]
    DiscoveryReporter(dpf, discover_fn=lambda t: found, monotonic=Clock()).tick()
    assert dpf.reported == [[{"bambu_id": "S1", "ip": "192.168.1.5", "model": "C12", "name": "P1S-1"}]]


def test_throttled():
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], interval_seconds=60.0, monotonic=clock)
    r.tick()
    r.tick()                       # within interval -> no second report
    assert len(dpf.reported) == 1
    clock.t = 1000.0 + 61
    r.tick()
    assert len(dpf.reported) == 2


def test_empty_scan_reports_empty_list():
    dpf = FakeDpf()
    DiscoveryReporter(dpf, discover_fn=lambda t: [], monotonic=Clock()).tick()
    assert dpf.reported == [[]]


def test_discovery_failure_is_swallowed():
    dpf = FakeDpf()

    def boom(t):
        raise OSError("network down")

    DiscoveryReporter(dpf, discover_fn=boom, monotonic=Clock()).tick()   # must not raise
    assert dpf.reported == []
