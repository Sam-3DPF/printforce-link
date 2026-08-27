"""Cloud send commands start a printer once, then only retry the dispatched report."""
import pytest

from bridge.app import (
    LEGACY_MARKER_MIN_AGE_SECONDS,
    LEGACY_READY_OBSERVATION_LIMIT,
    LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS,
    _LegacyMarkerReadiness,
    _handle_cloud_sends,
)
from bridge.router import Dispatcher, Router


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
            "item_id": "B1:2",
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


def _desired_plate(plate_index):
    desired = _desired()
    desired[0]["send"]["item_id"] = f"B1:{plate_index}"
    desired[0]["send"]["plate_index"] = plate_index
    desired[0]["send"]["filename"] = (
        f"batch-2026-08-25-2eK6CY5I-{plate_index}.gcode.3mf"
    )
    return desired


def _legacy_ready_snapshot(**overrides):
    snapshot = {
        "status": "IDLE",
        "historical_failed_ready": False,
        "has_active_file": False,
        "has_active_task": False,
        "has_active_project": False,
        "progress_percent": 0,
        "nozzle_target_temper": 0.0,
        "bed_target_temper": 0.0,
        "hms_empty": True,
        "slots": [
            {"slot_number": 1, "color_hex": "#D3B7A7", "filament_type": "PLA"},
            {"slot_number": 3, "color_hex": "#F99963", "filament_type": "PETG"},
        ],
    }
    snapshot.update(overrides)
    return snapshot


class _LegacySnapshotPrinter:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return dict(self._snapshot)


class _LegacySnapshotFleet(_FakeFleet):
    def __init__(self, snapshot):
        super().__init__()
        self._printer = _LegacySnapshotPrinter(snapshot)

    def by_id(self, bambu_id):
        return self._printer if bambu_id == "P1" else None


class _FakeClock:
    def __init__(self, now=1_000.0):
        self.now = now

    def advance(self, seconds):
        self.now += seconds


def _age_marker(marker, clock, age=LEGACY_MARKER_MIN_AGE_SECONDS):
    marker.touch()
    marker_time = clock.now - age
    marker.chmod(0o600)
    import os
    os.utime(marker, (marker_time, marker_time))


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
    assert started == {("B1", "P1", 2)}

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


def test_plate_one_clear_to_plate_two_immediate_desired_starts_each_plate_once(tmp_path):
    fleet = _FakeFleet()
    router = Router(str(tmp_path / "queue.json"))
    started = set()
    plate_one = _desired_plate(1)
    plate_two = _desired_plate(2)
    dpf = _FakeDpf(desired=plate_one)

    _handle_cloud_sends(
        plate_one, fleet, dpf, str(tmp_path), started, router=router,
    )
    dpf.desired = plate_two
    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), started, router=router,
    )
    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), started, router=router,
    )

    assert [call[3] for call in fleet.calls] == [1, 2]
    assert len(fleet.uploads) == 2
    assert started == {("B1", "P1", 2)}
    assert router.assignments_snapshot()["P1"]["plate_number"] == 2


def test_cloud_send_marker_is_restart_idempotent_per_plate(tmp_path):
    fleet = _FakeFleet()
    plate_two = _desired_plate(2)
    dpf = _FakeDpf(desired=plate_two)

    _handle_cloud_sends(plate_two, fleet, dpf, str(tmp_path), set())
    _handle_cloud_sends(plate_two, fleet, dpf, str(tmp_path), set())

    assert [call[3] for call in fleet.calls] == [2]
    assert (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()


def test_no_send_gap_cleans_only_that_plate_marker(tmp_path):
    fleet = _FakeFleet()
    started = set()
    plate_one = _desired_plate(1)
    dpf = _FakeDpf(desired=plate_one)

    _handle_cloud_sends(plate_one, fleet, dpf, str(tmp_path), started)
    marker = tmp_path / "cloud-send-B1-P1-plate-1.started"
    assert marker.exists()

    _handle_cloud_sends(
        [{"bambu_id": "P1", "desired_status": "IDLE"}],
        fleet, dpf, str(tmp_path), started,
    )

    assert started == set()
    assert not marker.exists()


def test_idle_no_file_legacy_marker_requires_age_and_two_separated_observations(
    tmp_path,
):
    legacy_marker = tmp_path / "B1.3mf.started"
    clock = _FakeClock()
    _age_marker(legacy_marker, clock)
    fleet = _LegacySnapshotFleet(_legacy_ready_snapshot())
    plate_two = _desired_plate(2)
    dpf = _FakeDpf(desired=plate_two)
    router = Router(str(tmp_path / "queue.json"))
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), set(), router=router,
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    assert fleet.calls == []
    assert legacy_marker.exists()

    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), set(), router=router,
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    assert fleet.calls == []
    assert legacy_marker.exists()

    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), set(), router=router,
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    _handle_cloud_sends(
        plate_two, fleet, dpf, str(tmp_path), set(), router=router,
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )

    assert [call[3] for call in fleet.calls] == [2]
    assert not legacy_marker.exists()
    assert (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()
    assert dpf.dispatched == [("B1", "P1"), ("B1", "P1")]


def test_historical_failed_ready_legacy_marker_allows_next_plate(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    clock = _FakeClock()
    _age_marker(legacy_marker, clock)
    fleet = _LegacySnapshotFleet(_legacy_ready_snapshot(
        status="ERROR",
        historical_failed_ready=True,
    ))
    plate_two = _desired_plate(2)
    router = Router(str(tmp_path / "queue.json"))
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        router=router, legacy_marker_readiness=readiness,
        wall_time=lambda: clock.now,
    )
    assert fleet.calls == []

    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        router=router, legacy_marker_readiness=readiness,
        wall_time=lambda: clock.now,
    )

    assert [call[3] for call in fleet.calls] == [2]
    assert not legacy_marker.exists()
    assert (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()


def test_fresh_legacy_marker_cannot_accumulate_ready_observations(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    clock = _FakeClock()
    _age_marker(legacy_marker, clock, age=0)
    fleet = _LegacySnapshotFleet(_legacy_ready_snapshot())
    plate_two = _desired_plate(2)
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )

    assert fleet.calls == []
    assert legacy_marker.exists()
    assert len(readiness) == 0


def test_unsafe_snapshot_resets_legacy_marker_ready_observation(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    clock = _FakeClock()
    _age_marker(legacy_marker, clock)
    ready = _legacy_ready_snapshot()
    fleet = _LegacySnapshotFleet(ready)
    plate_two = _desired_plate(2)
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    def poll():
        _handle_cloud_sends(
            plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
            legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
        )

    poll()
    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    fleet._printer._snapshot = _legacy_ready_snapshot(status="PRINTING")
    poll()
    fleet._printer._snapshot = ready
    poll()
    assert fleet.calls == []

    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    poll()
    assert [call[3] for call in fleet.calls] == [2]


def test_missing_desired_resets_legacy_marker_ready_observation(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    clock = _FakeClock()
    _age_marker(legacy_marker, clock)
    fleet = _LegacySnapshotFleet(_legacy_ready_snapshot())
    plate_two = _desired_plate(2)
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    _handle_cloud_sends(
        [], fleet, _FakeDpf(desired=[]), str(tmp_path), set(),
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )
    clock.advance(LEGACY_READY_OBSERVATION_MIN_GAP_SECONDS)
    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        legacy_marker_readiness=readiness, wall_time=lambda: clock.now,
    )

    assert fleet.calls == []
    assert legacy_marker.exists()


def test_legacy_marker_readiness_state_is_bounded():
    clock = _FakeClock()
    readiness = _LegacyMarkerReadiness(monotonic=lambda: clock.now)

    for index in range(LEGACY_READY_OBSERVATION_LIMIT + 1):
        readiness.observe(
            ("batch", f"printer-{index}", 1),
            marker_identity=(index, 0),
            ready=True,
        )

    assert len(readiness) == LEGACY_READY_OBSERVATION_LIMIT


@pytest.mark.parametrize("unsafe_snapshot", [
    _legacy_ready_snapshot(status="PRINTING"),
    _legacy_ready_snapshot(has_active_file=True),
    _legacy_ready_snapshot(status="OFFLINE"),
])
def test_unsafe_live_snapshot_preserves_legacy_marker_and_refuses_start(
    tmp_path, unsafe_snapshot,
):
    legacy_marker = tmp_path / "B1.3mf.started"
    legacy_marker.touch()
    fleet = _LegacySnapshotFleet(unsafe_snapshot)
    plate_two = _desired_plate(2)
    router = Router(str(tmp_path / "queue.json"))

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        router=router,
    )

    assert fleet.calls == []
    assert legacy_marker.exists()
    assert not (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()


def test_missing_live_snapshot_preserves_legacy_marker_and_refuses_start(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    legacy_marker.touch()
    fleet = _FakeFleet()
    plate_two = _desired_plate(2)
    router = Router(str(tmp_path / "queue.json"))

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
        router=router,
    )

    assert fleet.calls == []
    assert legacy_marker.exists()
    assert not (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()


def test_legacy_marker_with_matching_assignment_migrates_without_duplicate_start(tmp_path):
    router = Router(str(tmp_path / "queue.json"))
    router.record_assignment("P1", "B1", 2)
    legacy_marker = tmp_path / "B1.3mf.started"
    legacy_marker.touch()
    fleet = _FakeFleet()
    plate_two = _desired_plate(2)

    _handle_cloud_sends(
        plate_two,
        fleet,
        _FakeDpf(desired=plate_two),
        str(tmp_path),
        set(),
        router=router,
    )

    assert fleet.calls == []
    assert not legacy_marker.exists()
    assert (tmp_path / "cloud-send-B1-P1-plate-2.started").exists()


def test_ambiguous_legacy_marker_is_not_discarded_as_redundant(tmp_path):
    legacy_marker = tmp_path / "B1.3mf.started"
    plate_marker = tmp_path / "cloud-send-B1-P1-plate-2.started"
    legacy_marker.touch()
    plate_marker.touch()
    fleet = _FakeFleet()
    plate_two = _desired_plate(2)

    _handle_cloud_sends(
        plate_two, fleet, _FakeDpf(desired=plate_two), str(tmp_path), set(),
    )

    assert fleet.calls == []
    assert legacy_marker.exists()
    assert plate_marker.exists()


def test_restart_cleans_disk_marker_orphaned_by_authoritative_printer_state(tmp_path):
    marker = tmp_path / "cloud-send-B1-P1-plate-2.started"
    marker.touch()

    _handle_cloud_sends(
        [{"bambu_id": "P1", "desired_status": "IDLE"}],
        _FakeFleet(),
        _FakeDpf(),
        str(tmp_path),
        set(),
    )

    assert not marker.exists()


def test_restart_preserves_disk_marker_when_printer_state_is_absent(tmp_path):
    marker = tmp_path / "cloud-send-B1-P1-plate-2.started"
    marker.touch()

    _handle_cloud_sends([], _FakeFleet(), _FakeDpf(), str(tmp_path), set())

    assert marker.exists()


def test_fresh_desired_for_different_plate_does_not_authorize_start(tmp_path):
    fleet = _FakeFleet()
    plate_one = _desired_plate(1)
    plate_two = _desired_plate(2)

    _handle_cloud_sends(
        plate_one,
        fleet,
        _FakeDpf(desired=plate_two),
        str(tmp_path),
        set(),
    )

    assert len(fleet.uploads) == 1
    assert fleet.starts == []


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


def test_cloud_start_then_same_pass_old_error_does_not_fail_new_assignment(tmp_path):
    class CompletionDpf(_FakeDpf):
        def __init__(self):
            super().__init__()
            self.failed = []

        def report_failed(self, batch_id, plate_number=None):
            self.failed.append((batch_id, plate_number))
            return {"batch_id": batch_id}

    router = Router(str(tmp_path / "queue.json"))
    dpf = CompletionDpf()
    _handle_cloud_sends(
        _desired(), _FakeFleet(), dpf, str(tmp_path), set(), router=router,
    )
    started_at = router.assignments_snapshot()["P1"]["started_at"]

    Dispatcher(router, _FakeFleet(), dpf, now_fn=lambda: started_at).drain([{
        "bambu_id": "P1",
        "status": "ERROR",
        "slots": [],
    }])

    assert dpf.failed == []
    assert router.assignments_snapshot()["P1"]["observed_active"] is False


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
