"""The bridge-managed durable printer store (U4)."""
import os
import stat

from bridge.config import PrinterConfig
from bridge.store import PrinterStore


def test_upsert_then_configs(tmp_path):
    s = PrinterStore(str(tmp_path / "printers.json"))
    s.upsert("S1", "CODE1", "192.168.1.5", name="P1S")
    assert s.configs() == [PrinterConfig(bambu_id="S1", ip="192.168.1.5",
                                         access_code="CODE1", name="P1S")]


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "printers.json")
    PrinterStore(path).upsert("S1", "CODE1", "192.168.1.5")
    reloaded = PrinterStore(path)               # a restart re-reads the store
    assert reloaded.has("S1")
    assert reloaded.configs()[0].access_code == "CODE1"


def test_file_is_chmod_600(tmp_path):
    path = str(tmp_path / "printers.json")
    PrinterStore(path).upsert("S1", "CODE1", "192.168.1.5")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600   # holds device-control secrets


def test_missing_file_starts_empty(tmp_path):
    assert PrinterStore(str(tmp_path / "absent.json")).configs() == []


def test_corrupt_file_starts_empty_not_crash(tmp_path):
    path = tmp_path / "printers.json"
    path.write_text("{ this is not json")
    assert PrinterStore(str(path)).configs() == []


def test_code_without_ip_is_retained_but_not_connectable(tmp_path):
    s = PrinterStore(str(tmp_path / "printers.json"))
    s.upsert("S1", "CODE1", None)
    assert s.has("S1")                          # the code is kept...
    assert s.configs() == []                    # ...but it can't be connected without an IP


def test_upsert_updates_existing(tmp_path):
    path = str(tmp_path / "printers.json")
    s = PrinterStore(path)
    s.upsert("S1", "OLD", "192.168.1.5")
    s.upsert("S1", "NEW", "192.168.1.9")
    cfgs = PrinterStore(path).configs()
    assert len(cfgs) == 1
    assert cfgs[0].access_code == "NEW" and cfgs[0].ip == "192.168.1.9"


def test_remove_persists(tmp_path):
    path = str(tmp_path / "printers.json")
    s = PrinterStore(path)
    s.upsert("S1", "CODE1", "192.168.1.5")
    s.remove("S1")
    assert not s.has("S1")
    assert PrinterStore(path).configs() == []   # removal survives a reload


def test_cloud_token_get_set_and_persist(tmp_path):
    path = str(tmp_path / "printers.json")
    s = PrinterStore(path)
    assert s.get_cloud_token() is None          # unpaired to start
    s.set_cloud_token("CLOUD-1")
    assert s.get_cloud_token() == "CLOUD-1"
    assert PrinterStore(path).get_cloud_token() == "CLOUD-1"   # survives a restart


def test_cloud_token_and_printers_coexist(tmp_path):
    path = str(tmp_path / "printers.json")
    s = PrinterStore(path)
    s.set_cloud_token("CLOUD-1")
    s.upsert("S1", "CODE1", "192.168.1.5")
    reloaded = PrinterStore(path)
    assert reloaded.get_cloud_token() == "CLOUD-1"
    assert reloaded.has("S1")
