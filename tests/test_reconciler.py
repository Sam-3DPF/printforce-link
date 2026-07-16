"""The config reconciler that pulls couriered printer config into the live fleet (U4)."""
from bridge.reconciler import ConfigReconciler


class FakeDpf:
    def __init__(self, config):
        self._config = config
        self.pulls = 0
        self.acked = []

    def get_printers_config(self):
        self.pulls += 1
        return self._config

    def ack_printers_config(self, acks):
        self.acked.append(acks)
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

    def upsert(self, bambu_id, access_code, local_ip=None, name=""):
        self.upserts.append((bambu_id, access_code, local_ip))


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


def _reconciler(printers, fleet=None, store=None, clock=None, interval=60.0):
    dpf = FakeDpf({"printers": printers})
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


def test_entry_without_code_is_skipped():
    # An already-delivered printer comes back with no access_code -> nothing to do.
    r, dpf, fleet, store = _reconciler([{"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.1.5"}])
    r.tick()
    assert store.upserts == [] and fleet.added == [] and dpf.acked == []


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
