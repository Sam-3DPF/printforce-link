"""The config reconciler that pulls couriered printer config into the live fleet (U4)."""
from bridge.reconciler import ConfigReconciler


class FakeDpf:
    def __init__(self, config):
        self._config = config
        self.pulls = 0
        self.acked = []
        self.acked_removed = []

    def get_printers_config(self):
        self.pulls += 1
        return self._config

    def ack_printers_config(self, acks, removed=None):
        self.acked.append(acks)
        self.acked_removed.append(removed or [])
        return {"acknowledged": len(acks)}


class FakeFleet:
    def __init__(self, serials=()):
        self._serials = set(serials)
        self.added = []
        self.removed = []

    def by_id(self, bambu_id):
        return object() if bambu_id in self._serials else None

    def add_printer(self, cfg):
        self._serials.add(cfg.bambu_id)
        self.added.append(cfg)

    def remove_printer(self, bambu_id):
        self._serials.discard(bambu_id)
        self.removed.append(bambu_id)


class FakeStore:
    def __init__(self):
        self.upserts = []
        self.removed = []
        self.entries = {}
        self.ip_updates = []

    def upsert(self, bambu_id, access_code, local_ip=None, name=""):
        self.upserts.append((bambu_id, access_code, local_ip))
        entry = self.entries.get(bambu_id, {})
        entry["access_code"] = access_code
        if local_ip:
            entry["local_ip"] = local_ip
        if name:
            entry["name"] = name
        self.entries[bambu_id] = entry

    def has(self, bambu_id):
        return bambu_id in self.entries

    def update_ip(self, bambu_id, local_ip):
        self.ip_updates.append((bambu_id, local_ip))
        if bambu_id in self.entries:
            self.entries[bambu_id]["local_ip"] = local_ip

    def remove(self, bambu_id):
        self.removed.append(bambu_id)
        self.entries.pop(bambu_id, None)

    def configs(self):
        from bridge.config import PrinterConfig
        out = []
        for bambu_id, entry in self.entries.items():
            code = entry.get("access_code")
            ip = entry.get("local_ip")
            if not code or not ip:
                continue
            out.append(PrinterConfig(
                bambu_id=bambu_id, ip=ip, access_code=code,
                name=entry.get("name", ""),
            ))
        return out


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _entry(**over):
    e = {"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.1.5",
         "access_code": "CODE", "config_version": "v1"}
    e.update(over)
    return e


def _reconciler(printers, fleet=None, store=None, clock=None, interval=60.0, remove=None):
    config = {"printers": printers}
    if remove is not None:
        config["remove"] = remove
    dpf = FakeDpf(config)
    fleet = fleet or FakeFleet()
    store = store or FakeStore()
    r = ConfigReconciler(dpf, fleet, store, interval_seconds=interval, monotonic=clock or Clock())
    return r, dpf, fleet, store


def test_delivered_code_is_stored_added_and_acked():
    r, dpf, fleet, store = _reconciler([_entry()])
    r.tick()
    assert ("S1", "CODE", "192.168.1.5") in store.upserts          # 1. stored first
    assert [(c.bambu_id, c.ip) for c in fleet.added] == [("S1", "192.168.1.5")]  # 2. added live
    assert dpf.acked == [[{"printer_id": "p1", "config_version": "v1"}]]         # 3. acked -> cloud deletes


def test_already_in_fleet_is_rebuilt_with_the_new_code():
    # Re-adopting with a corrected access code: the cloud delivers the new code for a
    # printer that is ALREADY in the fleet (it joined with the wrong code and is failing).
    # The reconciler must tear down that member and re-add it with the new credential so it
    # reconnects on its own — not leave it stranded on the old code until a restart (R1).
    r, dpf, fleet, store = _reconciler([_entry(access_code="NEWCODE")], fleet=FakeFleet(serials=["S1"]))
    r.tick()
    assert ("S1", "NEWCODE", "192.168.1.5") in store.upserts
    assert fleet.removed == ["S1"]                                             # old member torn down
    assert [(c.bambu_id, c.ip, c.access_code) for c in fleet.added] == [("S1", "192.168.1.5", "NEWCODE")]
    assert dpf.acked


def test_entry_without_code_is_skipped_when_not_stored():
    # An already-delivered printer that this Link has never stored cannot be
    # rebuilt from an IP-only row — the access code is not in the payload.
    r, dpf, fleet, store = _reconciler([{"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.1.5"}])
    r.tick()
    assert store.upserts == [] and fleet.added == [] and dpf.acked == []


def test_ip_only_refresh_moves_a_stored_printer():
    # 3DPF already sent the code. A later reserved-IP edit still arrives as
    # local_ip with no access_code. v0.1.24 skipped that row, so Link kept
    # dialing 192.168.86.x after the printer moved to 192.168.8.x.
    store = FakeStore()
    store.upsert("S1", "CODE", "192.168.86.28")
    fleet = FakeFleet(serials=["S1"])
    r, dpf, fleet, store = _reconciler(
        [{"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.8.246"}],
        fleet=fleet,
        store=store,
    )
    r.tick()
    assert store.ip_updates == [("S1", "192.168.8.246")]
    assert fleet.removed == ["S1"]
    assert [(c.bambu_id, c.ip, c.access_code) for c in fleet.added] == [
        ("S1", "192.168.8.246", "CODE"),
    ]
    assert dpf.acked == []


def test_unchanged_cloud_pin_does_not_clobber_a_learned_ip():
    store = FakeStore()
    store.upsert("S1", "CODE", "192.168.86.20")
    fleet = FakeFleet(serials=["S1"])
    r, dpf, fleet, store = _reconciler(
        [{"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.86.20"}],
        fleet=fleet,
        store=store,
    )
    r.tick()
    store.update_ip("S1", "192.168.8.188")
    store.ip_updates.clear()
    fleet.removed.clear()
    fleet.added.clear()
    r._last = None
    r.tick()
    assert store.ip_updates == []
    assert fleet.removed == []
    assert fleet.added == []
    assert store.entries["S1"]["local_ip"] == "192.168.8.188"


def test_ip_only_same_address_is_noop():
    store = FakeStore()
    store.upsert("S1", "CODE", "192.168.8.236")
    fleet = FakeFleet(serials=["S1"])
    r, dpf, fleet, store = _reconciler(
        [{"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.8.236"}],
        fleet=fleet,
        store=store,
    )
    r.tick()
    assert store.ip_updates == []
    assert fleet.removed == []
    assert fleet.added == []
    assert dpf.acked == []


def test_code_without_ip_is_stored_but_not_added():
    r, dpf, fleet, store = _reconciler([_entry(local_ip=None)])
    r.tick()
    assert ("S1", "CODE", None) in store.upserts   # code retained for later
    assert fleet.added == []                        # not connectable without an address
    assert dpf.acked                                # still acked — it IS durably stored


def test_pull_is_throttled():
    clock = Clock(1000.0)
    r, dpf, fleet, store = _reconciler([], clock=clock, interval=60.0)
    r.tick()                    # pulls (first tick)
    r.tick()                    # within the interval -> no pull
    assert dpf.pulls == 1
    clock.t = 1000.0 + 61
    r.tick()
    assert dpf.pulls == 2


def test_courier_failure_does_not_raise():
    class Boom(FakeDpf):
        def get_printers_config(self):
            raise OSError("network down")

    dpf = Boom({"printers": []})
    r = ConfigReconciler(dpf, FakeFleet(), FakeStore(), monotonic=Clock())
    r.tick()                    # must not raise; loop keeps running


def test_empty_config_is_noop():
    r, dpf, fleet, store = _reconciler([])
    r.tick()
    assert store.upserts == [] and fleet.added == [] and dpf.acked == []


def test_remove_list_drops_from_fleet_and_store_and_is_acked(): # U5
    r, dpf, fleet, store = _reconciler([], fleet=FakeFleet(serials=["S1", "S2"]), remove=["S1"])
    r.tick()
    assert fleet.removed == ["S1"]
    assert store.removed == ["S1"]
    # Acked even though there were no code acks this tick — the ack call itself is
    # shared, not gated on acks being non-empty.
    assert dpf.acked == [[]]
    assert dpf.acked_removed == [["S1"]]


def test_remove_of_serial_not_in_fleet_is_a_noop_no_raise(): # U5
    r, dpf, fleet, store = _reconciler([], fleet=FakeFleet(serials=[]), remove=["GHOST"])
    r.tick()                                    # must not raise
    assert store.removed == ["GHOST"]            # store.remove is a no-op for an unknown serial
    assert dpf.acked_removed == [["GHOST"]]      # still confirmed — the tombstone is cleared either way


def test_remove_list_alongside_a_delivered_code_acks_both(): # U5
    r, dpf, fleet, store = _reconciler([_entry()], fleet=FakeFleet(serials=["S2"]), remove=["S2"])
    r.tick()
    assert ("S1", "CODE", "192.168.1.5") in store.upserts
    assert fleet.removed == ["S2"]
    assert store.removed == ["S2"]
    assert dpf.acked == [[{"printer_id": "p1", "config_version": "v1"}]]
    assert dpf.acked_removed == [["S2"]]
