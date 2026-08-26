"""Cloud send commands start a printer once, then only retry the dispatched report."""
import pytest

from bridge.app import _handle_cloud_sends


class _FakeDpf:
    def __init__(self, download_ok=True, desired=None):
        self.download_ok = download_ok
        self.desired = desired
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
        return {"printers": self.desired if self.desired is not None else _desired()}


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
            "ams_mapping_format": "filament-id-v1",
            "required_filaments": [
                {"filament_id": 4, "hex": "#D3B7A7", "family": "PLA"},
                {"filament_id": 9, "hex": "#F99963", "family": "PETG"},
            ],
            "required_filament_hexes": [
                "__3DPF_REQUIRES_FILAMENT_ID_MAPPING_V1__",
            ],
            "ams_mapping": [-1, -1, -1, 0, -1, -1, -1, -1, 2],
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
    assert fleet.calls[0][2] == [-1, -1, -1, 0, -1, -1, -1, -1, 2]
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
            {"slot_number": 1, "color_hex": "#D3B7A7", "filament_type": "PLA"},
            {"slot_number": 2, "color_hex": "#BB3D43", "filament_type": "PLA"},
            {"slot_number": 3, "color_hex": "#F99963", "filament_type": "PETG"},
            {"slot_number": 4, "color_hex": "#9B9EA0", "filament_type": "PLA"},
        ])


def test_cloud_send_computes_ams_mapping_from_live_slots(tmp_path):
    fleet = _LiveFleet()
    desired = _desired()
    desired[0]["send"]["ams_mapping"] = [-1, -1, -1, 1, -1, -1, -1, -1, 3]
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls[0][2] == [-1, -1, -1, 0, -1, -1, -1, -1, 2]


def test_cloud_send_refuses_color_file_without_ams_mapping(tmp_path):
    fleet = _FakeFleet()
    desired = _desired()
    desired[0]["send"]["ams_mapping"] = []
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


@pytest.mark.parametrize("mapping", [
    [-1, -1, -1, True, -1, -1, -1, -1, 2],
    [-1, -1, -1, 0, -2, -1, -1, -1, 2],
    [-1, -1, -1, 0, -1, -1, -1, -1],
    [-1, -1, -1, 0, -1, -1, -1, -1, -1],
    [-1, -1, -1, 0, 1, -1, -1, -1, 2],
    [-1, -1, -1, 16, -1, -1, -1, -1, 2],
    [0, 2],
])
def test_cloud_send_rejects_malformed_or_dense_mapping(tmp_path, mapping):
    fleet = _LiveFleet()
    desired = _desired()
    desired[0]["send"]["ams_mapping"] = mapping
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_rejects_old_server_payload_even_with_live_colors(tmp_path):
    fleet = _LiveFleet()
    desired = _desired()
    send = desired[0]["send"]
    send.pop("ams_mapping_format")
    send.pop("required_filaments")
    send["required_filament_hexes"] = ["#D3B7A7", "#F99963"]
    send["ams_mapping"] = [0, 2]
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_rejects_empty_required_filaments(tmp_path):
    fleet = _LiveFleet()
    desired = _desired()
    desired[0]["send"]["required_filaments"] = []
    desired[0]["send"]["ams_mapping"] = []
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_rejects_missing_required_filament_mapping(tmp_path):
    fleet = _LiveFleet()
    desired = _desired()
    desired[0]["send"]["required_filaments"] = [
        {"filament_id": 4, "hex": "#D3B7A7", "family": "PLA"},
        {"filament_id": 9, "hex": "#F99963", "family": "PETG"},
        {"filament_id": 10, "hex": "#BB3D43", "family": "PLA"},
    ]
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


@pytest.mark.parametrize("filament_id", [True, 0, 257])
def test_cloud_send_rejects_invalid_or_oversize_logical_ids(tmp_path, filament_id):
    fleet = _FakeFleet()
    desired = _desired()
    desired[0]["send"]["required_filaments"][0]["filament_id"] = filament_id
    _handle_cloud_sends(desired, fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_maps_duplicate_color_by_material_family(tmp_path):
    class FamilyFleet(_FakeFleet):
        def by_id(self, bambu_id):
            return _SnapPrinter([
                {"slot_number": 1, "color_hex": "#112233", "filament_type": "PLA"},
                {"slot_number": 2, "color_hex": "#112233", "filament_type": "PETG"},
            ])

    desired = _desired()
    desired[0]["send"]["required_filaments"] = [
        {"filament_id": 1, "hex": "#112233", "family": "PLA"},
        {"filament_id": 2, "hex": "#112233", "family": "PETG"},
    ]
    desired[0]["send"]["ams_mapping"] = [0, 1]
    fleet = FamilyFleet()
    _handle_cloud_sends(desired, fleet, _FakeDpf(desired=desired), str(tmp_path), set())
    assert fleet.calls[0][2] == [0, 1]


def test_cloud_send_reuses_one_unique_tray_for_same_color_and_family(tmp_path):
    class OneTrayFleet(_FakeFleet):
        def by_id(self, bambu_id):
            return _SnapPrinter([
                {"slot_number": 1, "color_hex": "#112233", "filament_type": "PLA"},
            ])

    desired = _desired()
    desired[0]["send"]["required_filaments"] = [
        {"filament_id": 1, "hex": "#112233", "family": "PLA"},
        {"filament_id": 2, "hex": "#112233", "family": "PLA"},
    ]
    desired[0]["send"]["ams_mapping"] = [0, 0]
    fleet = OneTrayFleet()
    _handle_cloud_sends(desired, fleet, _FakeDpf(desired=desired), str(tmp_path), set())
    assert fleet.calls[0][2] == [0, 0]


def test_cloud_send_fails_closed_on_ambiguous_or_unknown_live_family(tmp_path):
    class AmbiguousFleet(_FakeFleet):
        def by_id(self, bambu_id):
            return _SnapPrinter([
                {"slot_number": 1, "color_hex": "#D3B7A7", "filament_type": None},
                {"slot_number": 2, "color_hex": "#D3B7A7", "filament_type": "PLA"},
                {"slot_number": 3, "color_hex": "#D3B7A7", "filament_type": "PLA"},
                {"slot_number": 4, "color_hex": "#F99963", "filament_type": "PETG"},
            ])

    fleet = AmbiguousFleet()
    _handle_cloud_sends(_desired(), fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_does_not_fall_back_when_live_snapshot_has_no_slot_info(tmp_path):
    class NoInfoFleet(_FakeFleet):
        def by_id(self, bambu_id):
            return _SnapPrinter({"not": "a-list"})

    fleet = NoInfoFleet()
    _handle_cloud_sends(_desired(), fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


def test_cloud_send_does_not_fall_back_when_live_snapshot_fails(tmp_path):
    class BrokenPrinter:
        def snapshot(self):
            raise RuntimeError("live snapshot unavailable")

    class BrokenFleet(_FakeFleet):
        def by_id(self, bambu_id):
            return BrokenPrinter()

    fleet = BrokenFleet()
    _handle_cloud_sends(_desired(), fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.calls == []


class _SwapAfterUploadFleet(_FakeFleet):
    def __init__(self):
        super().__init__()
        self.slots = [
            {"slot_number": 1, "color_hex": "#D3B7A7", "filament_type": "PLA"},
            {"slot_number": 3, "color_hex": "#F99963", "filament_type": "PETG"},
        ]

    def by_id(self, bambu_id):
        return _SnapPrinter(self.slots)

    def upload(self, bambu_id, dest, remote_name=None):
        uploaded = super().upload(bambu_id, dest, remote_name=remote_name)
        self.slots = [
            {"slot_number": 2, "color_hex": "#D3B7A7", "filament_type": "PLA"},
            {"slot_number": 4, "color_hex": "#F99963", "filament_type": "PETG"},
        ]
        return uploaded


def test_cloud_send_recomputes_live_mapping_after_upload_before_start(tmp_path):
    fleet = _SwapAfterUploadFleet()
    _handle_cloud_sends(_desired(), fleet, _FakeDpf(), str(tmp_path), set())
    assert fleet.starts[0][2] == [-1, -1, -1, 1, -1, -1, -1, -1, 3]


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


class _MissingDesiredAfterUploadDpf(_FakeDpf):
    def heartbeat(self):
        return {}


def test_cloud_send_fails_closed_when_desired_refresh_fails_after_upload(tmp_path):
    fleet = _FakeFleet()
    _handle_cloud_sends(
        _desired(), fleet, _MissingDesiredAfterUploadDpf(), str(tmp_path), set(),
    )
    assert len(fleet.uploads) == 1
    assert fleet.starts == []
    assert fleet.calls == []
