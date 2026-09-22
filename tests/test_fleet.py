"""Fleet self-healing (U1) and dynamic membership (U2).

Exercises the fleet's connection-management logic in isolation: a fake printer records
connect/disconnect/reconnect calls, and a fake `discover_fn` stands in for the SSDP
scan, so no library and no live hardware are needed.
"""
import threading
import time

from bridge.config import PrinterConfig
from bridge.discover import DiscoveredPrinter
from bridge.fleet import Fleet


class FakePrinter:
    """Stand-in for BambuPrinter with the surface the Fleet uses."""

    def __init__(self, cfg, stale_after_seconds=None, connect_hook=None):
        self.cfg = cfg
        self.bambu_id = cfg.bambu_id
        self.current_ip = cfg.ip
        self.is_offline = False
        self.had_session = False
        self.silent_seconds = None
        self.rebuild_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.reconnects = []            # new_ip passed to each reconnect()
        self.connect_hook = connect_hook

    def connect(self):
        self.connect_calls += 1
        if self.connect_hook is not None:
            self.connect_hook(self)
        if self.cfg.name == "FAIL":     # opt a printer into a connect failure
            raise OSError("no route to host")

    def disconnect(self):
        self.disconnect_calls += 1

    def reconnect(self, new_ip=None):
        self.reconnects.append(new_ip)
        if new_ip:
            self.current_ip = new_ip
        self.is_offline = False

    def silent_for(self, now):
        return self.silent_seconds

    def rebuild_session(self):
        self.rebuild_calls += 1

    def snapshot(self):
        return {"bambu_id": self.bambu_id, "status": "OFFLINE" if self.is_offline else "IDLE"}


class FakePrinterFactory:
    def __init__(self, connect_hook=None):
        self.connect_hook = connect_hook
        self.created = []

    def __call__(self, cfg, stale_after_seconds=None):
        printer = FakePrinter(
            cfg,
            stale_after_seconds=stale_after_seconds,
            connect_hook=self.connect_hook,
        )
        self.created.append(printer)
        return printer


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cfg(serial, ip, name=""):
    return PrinterConfig(bambu_id=serial, ip=ip, access_code="SECRET", name=name)


def _fleet(configs, discovered=None, clock=None, rediscover_interval=60.0,
           printer_factory=None, on_address=None, connect_timeout_seconds=12.0,
           tcp_probe=None):
    calls = {"count": 0, "probe_ips": []}

    def discover_fn(timeout, probe_ips=None):
        calls["count"] += 1
        calls["probe_ips"] = list(probe_ips or [])
        return list(discovered() if callable(discovered) else (discovered or []))

    clock = clock or Clock()
    extra = {}
    if tcp_probe is not None:
        extra["tcp_probe"] = tcp_probe
    fleet = Fleet(configs, printer_factory=printer_factory or FakePrinter,
                  discover_fn=discover_fn,
                  rediscover_interval_seconds=rediscover_interval, monotonic=clock,
                  on_address=on_address,
                  connect_timeout_seconds=connect_timeout_seconds,
                  **extra)
    return fleet, calls, clock


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return bool(predicate())


# ---- U1: self-healing reconnection -------------------------------------------------

def test_no_scan_when_all_online():
    fleet, calls, _ = _fleet([_cfg("S1", "192.168.1.10")])
    fleet.reconcile_connections()
    assert calls["count"] == 0          # nothing offline -> no SSDP scan at all


def test_reconnects_printer_that_moved_ip():
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1", name="P1", model="C12")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    assert _wait_for(lambda: fleet.by_id("S1") is not old)
    replacement = fleet.by_id("S1")
    assert calls["count"] == 1
    assert replacement is factory.created[1]
    assert replacement.current_ip == "192.168.1.55"
    assert replacement.connect_calls == 1
    assert old.reconnects == []
    assert old.disconnect_calls == 1


def _assert_printer_stays(fleet, old):
    """Same-IP recovery must not swap the printer object out from under the fleet."""
    assert not _wait_for(lambda: fleet.by_id(old.bambu_id) is not old, timeout=0.15)
    assert fleet.by_id(old.bambu_id) is old
    assert old.disconnect_calls == 0
    assert old.reconnects == []
    assert old.rebuild_calls == 0


def test_same_ip_offline_is_not_rebuilt():
    # SSDP still names the address we are dialing. A new printer object would drop
    # the stopwatch and the cancel latch; same-IP recovery is not this path.
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.8.236")],
        discovered=[DiscoveredPrinter(ip="192.168.8.236", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    assert calls["count"] == 1
    _assert_printer_stays(fleet, old)
    assert len(factory.created) == 1


def test_same_ip_ssdp_hit_does_not_rebuild():
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.10", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    assert calls["count"] == 1
    _assert_printer_stays(fleet, old)


def test_no_ssdp_hit_does_not_rebuild_stored_ip():
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 1
    _assert_printer_stays(fleet, old)


def test_ssdp_miss_does_not_rebuild_stored_ip():
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    assert calls["count"] == 1
    _assert_printer_stays(fleet, old)


def test_scan_failure_does_not_rebuild_stored_ip():
    factory = FakePrinterFactory()

    def boom(_timeout, probe_ips=None):
        raise OSError("ssdp socket unavailable")

    fleet = Fleet(
        [_cfg("S1", "192.168.8.223")],
        printer_factory=factory,
        discover_fn=boom,
        monotonic=Clock(),
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    _assert_printer_stays(fleet, old)
    assert fleet.by_id("S1").current_ip == "192.168.8.223"


def test_stale_ip_reconnects_when_reserved_address_is_on_a_known_lan():
    # P1S-5 is stored at 192.168.86.28. P1S-8 (or P1P-2) is already on
    # 192.168.8.x. Multicast SSDP is empty — the live farm's
    # bridge_discovered_printers table stayed empty on v0.1.24. Asking
    # discovery for the addresses we already have must be enough for the
    # fake LAN to return the reserved 192.168.8.246.
    factory = FakePrinterFactory()
    seen = {}

    def discover_fn(timeout, probe_ips=None):
        seen["probe_ips"] = list(probe_ips or [])
        if probe_ips and "192.168.8.126" in probe_ips:
            return [DiscoveredPrinter(ip="192.168.8.246", serial="S5", name="P1S-5")]
        return []

    fleet = Fleet(
        [_cfg("S5", "192.168.86.28"), _cfg("S8", "192.168.8.126")],
        printer_factory=factory,
        discover_fn=discover_fn,
        monotonic=Clock(),
    )
    old = fleet.by_id("S5")
    old.is_offline = True
    fleet.reconcile_connections()

    assert "192.168.8.126" in seen.get("probe_ips", [])
    assert _wait_for(lambda: fleet.by_id("S5") is not old)
    assert fleet.by_id("S5").current_ip == "192.168.8.246"


def test_reconnect_to_a_new_ip_remembers_the_address():
    remembered = []
    factory = FakePrinterFactory()
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.86.28")],
        discovered=[DiscoveredPrinter(ip="192.168.8.246", serial="S1")],
        printer_factory=factory,
        on_address=lambda bambu_id, ip: remembered.append((bambu_id, ip)),
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()
    assert _wait_for(lambda: fleet.by_id("S1") is not old)
    assert remembered == [("S1", "192.168.8.246")]


def test_startup_connect_shares_one_budget_across_hung_printers():
    # v0.1.29's health check rolled back because connect_all waited on each
    # printer in turn. One half-open MQTT ack ate the two-minute window before
    # Link could tell 3DPF the new build was up.
    started = threading.Event()

    def hang(_printer):
        started.set()
        time.sleep(30)

    factory = FakePrinterFactory(connect_hook=hang)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.8.223"), _cfg("S2", "192.168.8.224")],
        printer_factory=factory,
        connect_timeout_seconds=0.05,
    )
    began = time.monotonic()
    fleet.connect_all()
    elapsed = time.monotonic() - began

    assert started.wait(1.0)
    assert elapsed < 1.0
    assert fleet.by_id("S1").connect_calls == 1
    assert fleet.by_id("S2").connect_calls == 1


def test_hung_reconnect_releases_the_slot():
    # A connect that never returns used to keep the per-serial worker slot,
    # so P1P-2 / P1S-9 at a correct reserved IP were never tried again.
    started = threading.Event()

    def hang(_printer):
        started.set()
        time.sleep(30)

    factory = FakePrinterFactory(connect_hook=hang)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.8.223")],
        discovered=[DiscoveredPrinter(ip="192.168.8.250", serial="S1")],
        printer_factory=factory,
        connect_timeout_seconds=0.05,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()
    assert started.wait(1.0)
    assert _wait_for(lambda: fleet._reconnects_in_flight == {} and
                     fleet._reconnect_worker_counts == {}, timeout=1.0)
    assert fleet.by_id("S1") is old


def test_scan_is_throttled():
    clock = Clock(1000.0)
    factory = FakePrinterFactory()
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[],
        clock=clock,
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()       # scans at t=1000
    fleet.reconcile_connections()       # within the interval -> no second scan
    assert calls["count"] == 1
    _assert_printer_stays(fleet, old)   # no SSDP hit -> no printer swap
    clock.t = 1000.0 + 61               # past the 60s interval
    fleet.by_id("S1").is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 2
    assert fleet.by_id("S1") is old


def test_two_printers_swap_ips():
    factory = FakePrinterFactory()
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10"), _cfg("S2", "192.168.1.11")],
        discovered=[DiscoveredPrinter(ip="192.168.1.11", serial="S1"),
                    DiscoveredPrinter(ip="192.168.1.10", serial="S2")],
        printer_factory=factory,
    )
    s1, s2 = fleet.by_id("S1"), fleet.by_id("S2")
    s1.is_offline = s2.is_offline = True
    fleet.reconcile_connections()
    assert _wait_for(lambda: fleet.by_id("S1") is not s1)
    assert _wait_for(lambda: fleet.by_id("S2") is not s2)
    assert fleet.by_id("S1").current_ip == "192.168.1.11"
    assert fleet.by_id("S2").current_ip == "192.168.1.10"


def test_discovery_failure_is_swallowed_and_still_throttled():
    calls = {"count": 0}

    def boom(timeout):
        calls["count"] += 1
        raise OSError("network down")

    clock = Clock(1000.0)
    factory = FakePrinterFactory()
    fleet = Fleet([_cfg("S1", "192.168.1.10")], printer_factory=factory,
                  discover_fn=boom, rediscover_interval_seconds=60.0, monotonic=clock)
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()            # scan raises internally -> swallowed, not re-raised
    fleet.reconcile_connections()            # within the interval -> must NOT scan again
    assert calls["count"] == 1               # throttle timestamp advanced despite the failure
    _assert_printer_stays(fleet, old)
    assert fleet.by_id("S1").current_ip == "192.168.1.10"
    clock.t = 1000.0 + 61
    fleet.by_id("S1").is_offline = True
    fleet.reconcile_connections()
    assert calls["count"] == 2


def test_old_object_remains_stable_during_blocked_replacement_connect():
    started = threading.Event()
    release = threading.Event()

    def blocked_connect(printer):
        if printer.current_ip == "192.168.1.55":
            started.set()
            release.wait(2.0)

    factory = FakePrinterFactory(connect_hook=blocked_connect)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True

    def blocked_reconnect(new_ip=None):
        old.disconnect()
        old.is_offline = False
        started.set()
        release.wait(2.0)

    old.reconnect = blocked_reconnect
    try:
        before = time.monotonic()
        fleet.reconcile_connections()
        elapsed = time.monotonic() - before

        assert started.wait(1.0)
        assert elapsed < 0.5
        assert fleet.by_id("S1") is old
        assert old.current_ip == "192.168.1.10"
        assert old.disconnect_calls == 0
        assert old.reconnects == []
        assert fleet.snapshot()[0]["status"] == "OFFLINE"
    finally:
        release.set()
    assert _wait_for(lambda: fleet.by_id("S1") is not old)


def test_successful_reconnect_atomically_replaces_fleet_member():
    factory = FakePrinterFactory()
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()

    assert _wait_for(lambda: fleet.by_id("S1") is not old)
    replacement = fleet.by_id("S1")
    assert replacement is factory.created[1]
    assert replacement.current_ip == "192.168.1.55"
    assert replacement.connect_calls == 1
    assert old.disconnect_calls == 1


def test_removal_during_blocked_connect_cannot_resurrect_printer():
    started = threading.Event()
    release = threading.Event()

    def blocked_connect(printer):
        if printer.current_ip == "192.168.1.55":
            started.set()
            release.wait(2.0)

    factory = FakePrinterFactory(connect_hook=blocked_connect)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True

    def blocked_reconnect(new_ip=None):
        started.set()
        release.wait(2.0)

    old.reconnect = blocked_reconnect
    try:
        fleet.reconcile_connections()
        assert started.wait(1.0)
        fleet.remove_printer("S1")
        assert fleet.by_id("S1") is None
    finally:
        release.set()

    assert _wait_for(lambda: len(factory.created) == 1 or factory.created[1].disconnect_calls == 1)
    assert fleet.by_id("S1") is None
    assert old.disconnect_calls == 1
    if len(factory.created) == 2:
        assert factory.created[1].disconnect_calls == 1


def test_removed_generation_does_not_block_readded_serial_reconnect():
    old_started = threading.Event()
    release_old = threading.Event()
    new_started = threading.Event()
    release_new = threading.Event()
    discovered_ip = ["192.168.1.55"]

    def blocked_connect(printer):
        if printer.current_ip == "192.168.1.55":
            old_started.set()
            release_old.wait(2.0)
        elif printer.current_ip == "192.168.1.66":
            new_started.set()
            release_new.wait(2.0)

    factory = FakePrinterFactory(connect_hook=blocked_connect)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=lambda: [DiscoveredPrinter(ip=discovered_ip[0], serial="S1")],
        rediscover_interval=0.0,
        printer_factory=factory,
    )
    original = fleet.by_id("S1")
    original.is_offline = True

    try:
        fleet.reconcile_connections()
        assert old_started.wait(1.0)

        fleet.remove_printer("S1")
        fleet.add_printer(_cfg("S1", "192.168.1.20"))
        readded = fleet.by_id("S1")
        readded.is_offline = True
        discovered_ip[0] = "192.168.1.66"
        fleet.reconcile_connections()
        assert new_started.wait(1.0)

        release_old.set()
        assert _wait_for(lambda: factory.created[1].disconnect_calls == 1)
        replacement_count = len(factory.created)
        fleet.reconcile_connections()
        assert len(factory.created) == replacement_count
    finally:
        release_old.set()
        release_new.set()

    assert _wait_for(lambda: fleet.by_id("S1") is factory.created[3])
    assert fleet.by_id("S1").current_ip == "192.168.1.66"
    assert original.disconnect_calls == 1


def test_repeated_readd_caps_blocked_workers_per_serial_without_starving_others():
    release_s1 = threading.Event()
    s1_started = [threading.Event() for _ in range(3)]
    s2_connected = threading.Event()
    count_lock = threading.Lock()
    active_s1 = 0
    max_active_s1 = 0
    s1_connects = 0
    discovered_ips = {"S1": "10.0.1.1"}

    def connect_hook(printer):
        nonlocal active_s1, max_active_s1, s1_connects
        if printer.bambu_id == "S1" and printer.current_ip.startswith("10.0.1."):
            with count_lock:
                s1_connects += 1
                active_s1 += 1
                max_active_s1 = max(max_active_s1, active_s1)
                if s1_connects <= len(s1_started):
                    s1_started[s1_connects - 1].set()
            try:
                release_s1.wait(2.0)
            finally:
                with count_lock:
                    active_s1 -= 1
        elif printer.bambu_id == "S2" and printer.current_ip == "10.0.2.2":
            s2_connected.set()

    def discovered():
        return [
            DiscoveredPrinter(ip=ip, serial=serial)
            for serial, ip in discovered_ips.items()
        ]

    def s1_worker_count():
        return sum(
            thread.name == "printer-reconnect-S1"
            for thread in threading.enumerate()
        )

    factory = FakePrinterFactory(connect_hook=connect_hook)
    fleet, _, _ = _fleet(
        [_cfg("S1", "10.0.0.1"), _cfg("S2", "10.0.0.2")],
        discovered=discovered,
        rediscover_interval=0.0,
        printer_factory=factory,
    )
    max_s1_workers = 0
    current_s1 = None

    try:
        fleet.by_id("S1").is_offline = True
        fleet.reconcile_connections()
        assert s1_started[0].wait(1.0)
        max_s1_workers = max(max_s1_workers, s1_worker_count())

        fleet.remove_printer("S1")
        fleet.add_printer(_cfg("S1", "10.0.0.11"))
        discovered_ips["S1"] = "10.0.1.2"
        fleet.by_id("S1").is_offline = True
        fleet.reconcile_connections()
        assert s1_started[1].wait(1.0)
        max_s1_workers = max(max_s1_workers, s1_worker_count())

        fleet.remove_printer("S1")
        fleet.add_printer(_cfg("S1", "10.0.0.12"))
        discovered_ips["S1"] = "10.0.1.3"
        fleet.by_id("S1").is_offline = True
        fleet.reconcile_connections()
        max_s1_workers = max(max_s1_workers, s1_worker_count())
        current_s1 = fleet.by_id("S1")
        assert fleet._reconnect_worker_counts["S1"] == 2
        assert "S1" not in fleet._reconnects_in_flight

        old_s2 = fleet.by_id("S2")
        old_s2.is_offline = True
        discovered_ips["S2"] = "10.0.2.2"
        fleet.reconcile_connections()
        assert s2_connected.wait(1.0)
        assert _wait_for(lambda: fleet.by_id("S2") is not old_s2)

        assert max_s1_workers <= 2
        assert max_active_s1 <= 2
        assert s1_connects <= 2
    finally:
        release_s1.set()

    assert _wait_for(lambda: s1_worker_count() == 0)
    assert "S1" not in fleet._reconnect_worker_counts

    fleet.reconcile_connections()
    assert s1_started[2].wait(1.0)
    assert _wait_for(lambda: fleet.by_id("S1") is not current_s1)
    assert fleet.by_id("S1").current_ip == "10.0.1.3"


def test_repeated_ticks_deduplicate_replacement_worker_per_serial():
    started = threading.Event()
    release = threading.Event()

    def blocked_connect(printer):
        if printer.current_ip == "192.168.1.55":
            started.set()
            release.wait(2.0)

    factory = FakePrinterFactory(connect_hook=blocked_connect)
    fleet, calls, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1")],
        rediscover_interval=0.0,
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    old_reconnect_calls = []

    def blocked_reconnect(new_ip=None):
        old_reconnect_calls.append(new_ip)
        started.set()
        release.wait(2.0)

    old.reconnect = blocked_reconnect
    try:
        fleet.reconcile_connections()
        assert started.wait(1.0)
        for _ in range(5):
            fleet.reconcile_connections()

        assert calls["count"] == 6
        assert len(factory.created) == 2
        assert factory.created[1].connect_calls == 1
        assert old_reconnect_calls == []
    finally:
        release.set()
    assert _wait_for(lambda: fleet.by_id("S1") is not old)


def test_blocked_printers_do_not_starve_unrelated_reconnect():
    release = threading.Event()
    four_blocked = threading.Event()
    unrelated_connected = threading.Event()
    count_lock = threading.Lock()
    blocked_count = 0

    def connect_hook(printer):
        nonlocal blocked_count
        if printer.current_ip.startswith("10.0.1.") and printer.bambu_id != "S5":
            with count_lock:
                blocked_count += 1
                if blocked_count == 4:
                    four_blocked.set()
            release.wait(2.0)
        elif printer.bambu_id == "S5" and printer.current_ip == "10.0.1.5":
            unrelated_connected.set()

    factory = FakePrinterFactory(connect_hook=connect_hook)
    configs = [_cfg(f"S{i}", f"10.0.0.{i}") for i in range(1, 6)]
    discovered = [
        DiscoveredPrinter(ip=f"10.0.1.{i}", serial=f"S{i}")
        for i in range(1, 6)
    ]
    fleet, _, _ = _fleet(configs, discovered=discovered, printer_factory=factory)
    old_by_id = {p.bambu_id: p for p in factory.created}
    for printer in old_by_id.values():
        printer.is_offline = True

    def block_old(printer):
        def reconnect(new_ip=None):
            nonlocal blocked_count
            if printer.bambu_id != "S5":
                with count_lock:
                    blocked_count += 1
                    if blocked_count == 4:
                        four_blocked.set()
                release.wait(2.0)
            else:
                unrelated_connected.set()
        return reconnect

    for printer in old_by_id.values():
        printer.reconnect = block_old(printer)

    try:
        fleet.reconcile_connections()
        assert four_blocked.wait(1.0)
        assert unrelated_connected.wait(0.5)
        assert _wait_for(lambda: fleet.by_id("S5") is not old_by_id["S5"])
    finally:
        release.set()


def test_control_does_not_hold_the_fleet_lock_across_the_command():
    """Pause publishes outside the membership lock.

    A reconnect swap and the next snapshot must not wait for the command to
    return. The command still completes on the printer it looked up.
    """
    replacement_ready = threading.Event()
    allow_replacement_connect = threading.Event()
    replacement_connected = threading.Event()
    control_started = threading.Event()
    release_control = threading.Event()

    class ControlPrinter(FakePrinter):
        def pause_print(self):
            control_started.set()
            release_control.wait(2.0)
            return True

    class ControlPrinterFactory(FakePrinterFactory):
        def __call__(self, cfg, stale_after_seconds=None):
            printer = ControlPrinter(
                cfg,
                stale_after_seconds=stale_after_seconds,
                connect_hook=self.connect_hook,
            )
            self.created.append(printer)
            return printer

    def connect_hook(printer):
        if printer.current_ip == "192.168.1.55":
            replacement_ready.set()
            allow_replacement_connect.wait(2.0)
            replacement_connected.set()

    factory = ControlPrinterFactory(connect_hook=connect_hook)
    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        discovered=[DiscoveredPrinter(ip="192.168.1.55", serial="S1")],
        printer_factory=factory,
    )
    old = fleet.by_id("S1")
    old.is_offline = True
    fleet.reconcile_connections()
    assert replacement_ready.wait(1.0)

    results = []
    control_thread = threading.Thread(
        target=lambda: results.append(fleet.apply_control("S1", "pause")),
    )
    control_thread.start()
    snapshot_done = threading.Event()

    def take_snapshot():
        fleet.snapshot()
        snapshot_done.set()

    try:
        assert control_started.wait(1.0)
        allow_replacement_connect.set()
        assert replacement_connected.wait(1.0)
        assert control_thread.is_alive()
        threading.Thread(target=take_snapshot, daemon=True).start()
        assert snapshot_done.wait(0.5), "snapshot blocked behind the in-flight command"
        assert _wait_for(lambda: fleet.by_id("S1") is not old)
    finally:
        allow_replacement_connect.set()
        release_control.set()
        control_thread.join(1.0)

    assert not control_thread.is_alive()
    assert results == [True]


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


def test_remove_cancels_add_that_is_still_connecting():
    started = threading.Event()
    release = threading.Event()

    def blocked_connect(printer):
        started.set()
        release.wait(2.0)

    factory = FakePrinterFactory(connect_hook=blocked_connect)
    fleet, _, _ = _fleet([], printer_factory=factory)
    add_thread = threading.Thread(
        target=fleet.add_printer,
        args=(_cfg("S1", "192.168.1.10"),),
    )
    add_thread.start()
    try:
        assert started.wait(1.0)
        fleet.remove_printer("S1")
    finally:
        release.set()
        add_thread.join(1.0)

    assert not add_thread.is_alive()
    assert fleet.by_id("S1") is None
    assert factory.created[0].disconnect_calls == 1


# ---- dead-session backstop ---------------------------------------------------------

def test_backstop_skips_a_closed_port_and_rebuilds_an_open_one_once_per_300s():
    clock = Clock(5000.0)
    probed = []

    def tcp_probe(ip):
        probed.append(ip)
        return ip.endswith(".10")

    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10"), _cfg("S2", "192.168.1.11")],
        clock=clock,
        tcp_probe=tcp_probe,
    )
    open_printer = fleet.by_id("S1")
    closed_printer = fleet.by_id("S2")
    open_printer.had_session = True
    closed_printer.had_session = True
    open_printer.silent_seconds = 300
    closed_printer.silent_seconds = 300
    fleet.recover_dead_sessions()
    assert not _wait_for(lambda: len(probed) > 0, timeout=0.15)
    assert open_printer.rebuild_calls == 0

    open_printer.silent_seconds = 301
    closed_printer.silent_seconds = 301
    clock.t = 5000.0 + 60
    fleet.recover_dead_sessions()
    assert _wait_for(lambda: open_printer.rebuild_calls == 1)
    assert closed_printer.rebuild_calls == 0
    assert set(probed) == {"192.168.1.10", "192.168.1.11"}
    assert fleet.by_id("S1") is open_printer

    fleet.recover_dead_sessions()
    assert open_printer.rebuild_calls == 1

    clock.t = 5000.0 + 60 + 299
    fleet.recover_dead_sessions()
    assert open_printer.rebuild_calls == 1

    # That pass consumed the fleet's 60s slot, so one second later is too soon
    # even though the per-printer wait has elapsed.
    clock.t = 5000.0 + 60 + 300
    fleet.recover_dead_sessions()
    assert open_printer.rebuild_calls == 1

    clock.t = 5000.0 + 60 + 299 + 60
    fleet.recover_dead_sessions()
    assert _wait_for(lambda: open_printer.rebuild_calls == 2)
    assert fleet.by_id("S1") is open_printer


def test_backstop_leaves_a_printer_that_never_had_a_session():
    clock = Clock(8000.0)
    probed = []

    def tcp_probe(ip):
        probed.append(ip)
        return True

    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        clock=clock,
        tcp_probe=tcp_probe,
    )
    printer = fleet.by_id("S1")
    printer.had_session = False
    printer.silent_seconds = 9999
    fleet.recover_dead_sessions()
    assert not _wait_for(lambda: len(probed) > 0, timeout=0.15)
    assert printer.rebuild_calls == 0
    assert fleet.by_id("S1") is printer


def test_backstop_returns_without_waiting_on_the_tcp_probe():
    clock = Clock(9000.0)
    started = threading.Event()
    release = threading.Event()

    def tcp_probe(ip):
        started.set()
        release.wait(1.0)
        return False

    fleet, _, _ = _fleet(
        [_cfg("S1", "192.168.1.10")],
        clock=clock,
        tcp_probe=tcp_probe,
    )
    printer = fleet.by_id("S1")
    printer.had_session = True
    printer.silent_seconds = 301
    try:
        began = time.monotonic()
        fleet.recover_dead_sessions()
        elapsed = time.monotonic() - began
        assert elapsed < 0.15
        assert started.wait(1.0)
        assert printer.rebuild_calls == 0
    finally:
        release.set()
