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


def test_no_autonomous_scan_after_the_ramp_without_a_scan_request():
    # U7: no more perpetual steady-interval scanning. Once the ramp ends, tick() is a
    # no-op unless a scan is explicitly requested — even long after the ramp, even
    # though plenty of time has passed on the old "steady interval" clock.
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], fast_interval_seconds=20.0,
                          ramp_seconds=180.0, monotonic=clock)
    r.tick()                       # first scan at t=1000 (ramp start)
    assert len(dpf.reported) == 1
    clock.t = 1000.0 + 200         # past the 180s ramp window
    r.tick(scan_requested=False)
    assert len(dpf.reported) == 1  # still quiet
    clock.t = 1000.0 + 400         # well past — still nothing without a request
    r.tick(scan_requested=False)
    assert len(dpf.reported) == 1


def test_scan_requested_after_the_ramp_opens_a_bounded_burst():
    # A scan_requested=True after the ramp opens a burst: scans immediately, keeps
    # scanning on the fast interval while the burst is open, then goes quiet again
    # once burst_seconds elapses.
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], fast_interval_seconds=20.0,
                          ramp_seconds=180.0, burst_seconds=45.0, monotonic=clock)
    r.tick()                            # ramp-start scan, t=1000
    clock.t = 1000.0 + 200               # past the ramp; quiet
    r.tick(scan_requested=False)
    assert len(dpf.reported) == 1

    # Operator clicks "Add Printer" -> the next state poll carries scan_requested=True.
    r.tick(scan_requested=True)          # opens a burst until t=1245
    assert len(dpf.reported) == 2

    clock.t = 1000.0 + 221               # 21s later: still inside the burst, past the fast interval
    r.tick()                             # scan_requested defaults False, burst still open
    assert len(dpf.reported) == 3

    clock.t = 1000.0 + 250               # past the burst deadline (1200 + 45 = 1245)
    r.tick()
    assert len(dpf.reported) == 3        # quiet again — the burst closed


def test_fresh_scan_request_reopens_an_already_open_burst():
    dpf = FakeDpf()
    clock = Clock(1000.0)
    r = DiscoveryReporter(dpf, discover_fn=lambda t: [], fast_interval_seconds=20.0,
                          ramp_seconds=180.0, burst_seconds=45.0, monotonic=clock)
    r.tick()                             # ramp-start scan
    clock.t = 1000.0 + 200
    r.tick(scan_requested=True)          # opens a burst, due to close at t=1245
    assert len(dpf.reported) == 2

    clock.t = 1000.0 + 240                # still inside the first burst
    r.tick(scan_requested=True)           # a fresh request re-extends it to t=1285
    assert len(dpf.reported) == 3

    clock.t = 1000.0 + 262                # past the ORIGINAL 1245 deadline, but the reopen
                                           # pushed it to 1285 -> still active
    r.tick()
    assert len(dpf.reported) == 4


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
