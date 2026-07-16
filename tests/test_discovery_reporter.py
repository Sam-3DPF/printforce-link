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


def test_scans_often_during_the_startup_ramp():
    # Right after startup, scan on the fast interval so printers appear quickly.
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], interval_seconds=60.0,
                          fast_interval_seconds=20.0, ramp_seconds=180.0, monotonic=clock)
    r.tick()                       # first scan (t=1000, start of the ramp)
    r.tick()                       # same instant, within the fast interval -> no scan
    assert len(dpf.reported) == 1
    clock.t = 1000.0 + 21          # 21s later: past the 20s fast interval, still in the ramp
    r.tick()
    assert len(dpf.reported) == 2  # scanned again on the fast cadence, not the 60s one


def test_settles_to_the_slow_interval_after_the_ramp():
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], interval_seconds=60.0,
                          fast_interval_seconds=20.0, ramp_seconds=180.0, monotonic=clock)
    r.tick()                       # first scan at t=1000
    clock.t = 1000.0 + 200         # past the 180s ramp window
    r.tick()
    assert len(dpf.reported) == 2
    clock.t = 1000.0 + 230         # 30s later: past fast(20) but not slow(60) -> no scan
    r.tick()
    assert len(dpf.reported) == 2  # steady state uses the 60s interval
    clock.t = 1000.0 + 261         # 61s after the last scan -> due again
    r.tick()
    assert len(dpf.reported) == 3


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
