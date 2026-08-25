"""Cloud send commands start a printer once, then only retry the dispatched report."""
from bridge.app import _handle_cloud_sends


class _FakeDpf:
    def __init__(self, download_ok=True):
        self.download_ok = download_ok
        self.downloads = []
        self.dispatched = []

    def download_url(self, url, dest):
        self.downloads.append((url, dest))
        if self.download_ok:
            with open(dest, "wb") as handle:
                handle.write(b"3mf")
        return self.download_ok

    def report_dispatched(self, batch_id, bambu_id):
        self.dispatched.append((batch_id, bambu_id))
        return {"batch_id": batch_id}

    def heartbeat(self):
        return {"printers": []}


class _FakeFleet:
    def __init__(self):
        self.calls = []
        self.uploads = []
        self.starts = []
        self._last_dest = None

    def upload(self, bambu_id, dest, remote_name=None):
        self.uploads.append((bambu_id, dest, remote_name))
        self._last_dest = dest
        return remote_name or "file.3mf"

    def start_print(self, bambu_id, remote_name, mapping, plate_index=1):
        self.starts.append((bambu_id, remote_name, list(mapping), plate_index))
        self.calls.append((bambu_id, self._last_dest, list(mapping), plate_index, remote_name))
        return True

    def dispatch(self, bambu_id, dest, mapping, plate_index=1, remote_name=None):
        self.calls.append((bambu_id, dest, list(mapping), plate_index, remote_name))
        return True


def _desired():
    return [{
        "bambu_id": "P1",
        "desired_status": "IDLE",
        "send": {
            "batch_id": "B1",
            "file_url": "https://example/signed.3mf",
            "plate_index": 2,
            "filename": "batch-2026-08-25-2eK6CY5I-1.gcode.3mf",
            "required_filament_hexes": ["#D3B7A7", "#F99963"],
            "ams_mapping": [0, 2],
        },
    }]


def test_cloud_send_starts_once_then_only_re_reports(tmp_path):
    fleet = _FakeFleet()
    dpf = _FakeDpf()
    started = set()
    desired = _desired()

    _handle_cloud_sends(desired, fleet, dpf, str(tmp_path), started)
    _handle_cloud_sends(desired, fleet, dpf, str(tmp_path), started)

    assert len(fleet.calls) == 1
    assert fleet.calls[0][0] == "P1"
    assert fleet.calls[0][2] == [0, 2]
    assert fleet.calls[0][3] == 2
    assert fleet.calls[0][4] == "batch-2026-08-25-2eK6CY5I-1.gcode.3mf"
    assert dpf.dispatched == [("B1", "P1"), ("B1", "P1")]
    assert started == {("B1", "P1")}

    _handle_cloud_sends(
        [{"bambu_id": "P1", "desired_status": "PRINTING"}],
        fleet, dpf, str(tmp_path), started,
    )
    assert started == set()
    assert len(fleet.calls) == 1


def test_failed_download_does_not_start(tmp_path):
    fleet = _FakeFleet()
    dpf = _FakeDpf(download_ok=False)
    started = set()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), started)
    assert fleet.calls == []
    assert dpf.dispatched == []
    assert started == set()
    leftover = list(tmp_path.iterdir())
    assert leftover == []


def test_cloud_send_skips_dispatch_when_printer_not_idle(tmp_path):
    fleet = _FakeFleet()
    dpf = _FakeDpf()
    started = set()
    desired = _desired()
    desired[0]["desired_status"] = "PRINTING"
    _handle_cloud_sends(desired, fleet, dpf, str(tmp_path), started)
    assert fleet.calls == []
    assert started == set()
    assert dpf.dispatched == []


def test_restart_with_started_marker_only_re_reports(tmp_path):
    fleet = _FakeFleet()
    dpf = _FakeDpf()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set())
    assert len(fleet.calls) == 1
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set())
    assert len(fleet.calls) == 1
    assert dpf.dispatched == [("B1", "P1"), ("B1", "P1")]


class _FakeRouter:
    def __init__(self):
        self.assignments = []

    def record_assignment(self, bambu_id, batch_id, plate_number=None):
        self.assignments.append((bambu_id, batch_id, plate_number))


def test_cloud_send_records_completion_assignment(tmp_path):
    fleet = _FakeFleet()
    dpf = _FakeDpf()
    router = _FakeRouter()
    _handle_cloud_sends(_desired(), fleet, dpf, str(tmp_path), set(), router=router)
    assert router.assignments == [("P1", "B1", 2)]


class _SnapPrinter:
    def __init__(self, slots):
        self._slots = slots

    def snapshot(self):
        return {"slots": self._slots}


class _LiveFleet(_FakeFleet):
    def by_id(self, bambu_id):
        return _SnapPrinter([
            {"slot_number": 1, "color_hex": "#D3B7A7"},
            {"slot_number": 2, "color_hex": "#BB3D43"},
            {"slot_number": 3, "color_hex": "#F99963"},
            {"slot_number": 4, "color_hex": "#9B9EA0"},
        ])


def test_cloud_send_computes_ams_mapping_from_live_slots(tmp_path):
    fleet = _LiveFleet()
    desired = _desired()
    desired[0]["send"]["ams_mapping"] = []
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls[0][2] == [0, 2]


def test_cloud_send_refuses_color_file_without_ams_mapping(tmp_path):
    fleet = _FakeFleet()
    desired = _desired()
    desired[0]["send"]["ams_mapping"] = []
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_skips_when_stop_is_live(tmp_path):
    fleet = _FakeFleet()
    desired = _desired()
    desired[0]["control"] = {"id": "c-stop", "action": "stop"}
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.uploads == []
    assert fleet.starts == []
    assert fleet.calls == []


class _StopAfterUploadDpf(_FakeDpf):
    def heartbeat(self):
        return {"printers": [{
            "bambu_id": "P1",
            "desired_status": "IDLE",
            "control": {"id": "c-stop", "action": "stop"},
        }]}


def test_cloud_send_skips_start_when_stop_arrives_after_upload(tmp_path):
    fleet = _FakeFleet()
    _handle_cloud_sends(_desired(), fleet, _StopAfterUploadDpf(), str(tmp_path), set())
    assert len(fleet.uploads) == 1
    assert fleet.starts == []
    assert fleet.calls == []
