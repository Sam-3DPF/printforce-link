"""Self-updater version logic, cloud controls, and durable operator preference."""
import json
import io
import shutil
import threading
import time

import pytest

from bridge.updater import (
    APPLY_FAILED,
    SelfUpdater,
    STATUS_AVAILABLE,
    STATUS_CURRENT,
    STATUS_ERROR,
    _download_file,
    _macos_swap_script,
    _swap_macos,
    _windows_swap_script,
    is_newer,
    latest_release_tag,
)


def test_is_newer_compares_semver():
    assert is_newer("v0.2.0", "0.1.0")
    assert is_newer("0.1.1", "0.1.0")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("v0.1.0", "0.2.0")


def test_is_newer_tolerates_v_prefix_and_junk():
    assert is_newer("v0.2.0", "v0.1.0")
    assert not is_newer("garbage", "0.1.0")   # malformed sorts low, never crashes


def test_latest_release_tag_returns_tag():
    assert latest_release_tag(fetch=lambda url: {"tag_name": "v0.3.0"}) == "v0.3.0"


def test_latest_release_tag_none_on_failure():
    def boom(url):
        raise OSError("offline")
    assert latest_release_tag(fetch=boom) is None
    assert latest_release_tag(fetch=lambda url: {}) is None   # no releases yet


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_updater_applies_when_newer():
    applied = []
    u = SelfUpdater("0.1.0", latest_tag_fn=lambda: "v0.2.0",
                    apply_fn=applied.append, monotonic=Clock())
    u.tick()
    assert applied == ["v0.2.0"]


def test_updater_noop_when_current_is_latest():
    applied = []
    u = SelfUpdater("0.2.0", latest_tag_fn=lambda: "v0.2.0",
                    apply_fn=applied.append, monotonic=Clock())
    u.tick()
    assert applied == []


def test_updater_is_throttled():
    calls = {"n": 0}

    def latest():
        calls["n"] += 1
        return "0.1.0"

    clock = Clock(1000.0)
    u = SelfUpdater("0.1.0", interval_seconds=3600, latest_tag_fn=latest,
                    apply_fn=lambda tag: None, monotonic=clock)
    u.tick()
    u.tick()                       # within interval -> no second check
    assert calls["n"] == 1
    clock.t = 1000.0 + 3601
    u.tick()
    assert calls["n"] == 2


def test_updater_swallows_check_failure():
    def boom():
        raise OSError("down")
    SelfUpdater("0.1.0", latest_tag_fn=boom, apply_fn=lambda tag: None, monotonic=Clock()).tick()


def test_disabled_updater_still_reports_available_without_installing(tmp_path):
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({"auto_update_enabled": False}))
    applied = []
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: "v0.2.0",
        apply_fn=applied.append,
        monotonic=Clock(),
        state_path=str(state_path),
    )

    updater.tick()

    assert applied == []
    assert updater.metadata()["latest_version"] == "0.2.0"
    assert updater.metadata()["update_status"] == STATUS_AVAILABLE
    assert updater.metadata()["auto_update_enabled"] is False


def test_update_now_forces_check_when_automatic_updates_are_off(tmp_path):
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({"auto_update_enabled": False}))
    applied = []
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: "v0.2.0",
        apply_fn=applied.append,
        monotonic=Clock(),
        state_path=str(state_path),
    )

    force = updater.apply_cloud_command({
        "auto_update_enabled": False,
        "request_id": "request-1",
    })
    updater.tick(force=force)

    assert applied == ["v0.2.0"]
    assert updater.metadata()["update_request_id"] == "request-1"


def test_cloud_auto_update_preference_persists_across_restart(tmp_path):
    state_path = tmp_path / "update-state.json"
    updater = SelfUpdater("0.1.0", state_path=str(state_path))

    updater.apply_cloud_command({"auto_update_enabled": False})
    restarted = SelfUpdater("0.1.0", state_path=str(state_path))

    assert restarted.metadata()["auto_update_enabled"] is False


def test_failed_install_is_reported_without_raising():
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: "v0.2.0",
        apply_fn=lambda _tag: APPLY_FAILED,
        monotonic=Clock(),
    )

    updater.tick()

    assert updater.metadata()["update_status"] == STATUS_ERROR
    assert "Could not install" in updater.metadata()["update_error"]


def test_new_build_turns_persisted_installing_state_current(tmp_path):
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({
        "latest_version": "0.2.0",
        "update_status": "installing",
        "update_request_id": "request-1",
    }))

    updater = SelfUpdater("0.2.0", state_path=str(state_path))

    assert updater.metadata()["update_status"] == STATUS_CURRENT
    assert updater.metadata()["update_request_id"] == "request-1"


def test_macos_swap_keeps_backup_until_new_build_is_healthy(tmp_path):
    current = tmp_path / "printforce-link"
    staged = tmp_path / "staged"
    backup = tmp_path / "printforce-link.old"
    current.mkdir()
    staged.mkdir()
    (current / "build.txt").write_text("old")
    (staged / "build.txt").write_text("new")

    _swap_macos(str(current), str(staged), str(backup))

    assert (current / "build.txt").read_text() == "new"
    assert (backup / "build.txt").read_text() == "old"


def test_macos_swap_rolls_back_if_staged_move_fails(tmp_path, monkeypatch):
    current = tmp_path / "printforce-link"
    staged = tmp_path / "staged"
    backup = tmp_path / "printforce-link.old"
    current.mkdir()
    staged.mkdir()
    (current / "build.txt").write_text("old")
    real_move = shutil.move

    def fail_staged_move(source, destination):
        if str(source) == str(staged):
            raise OSError("disk failure")
        return real_move(source, destination)

    monkeypatch.setattr("bridge.updater.shutil.move", fail_staged_move)

    with pytest.raises(OSError):
        _swap_macos(str(current), str(staged), str(backup))

    assert (current / "build.txt").read_text() == "old"


def test_transient_release_failure_keeps_update_request_pending(tmp_path):
    responses = iter([None, "v0.1.0"])
    clock = Clock()
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: next(responses),
        monotonic=clock,
        state_path=str(tmp_path / "state.json"),
    )
    force = updater.apply_cloud_command({"request_id": "request-1"})

    updater.tick(force=force)

    assert updater.metadata()["update_status"] == STATUS_ERROR
    assert updater.metadata()["update_request_id"] is None
    assert updater.apply_cloud_command({"request_id": "request-1"}) is False

    clock.t += 301
    updater.tick()
    assert updater.metadata()["update_request_id"] == "request-1"
    assert updater.metadata()["update_status"] == STATUS_CURRENT


def test_tick_async_does_not_block_the_printer_loop(monkeypatch):
    updater = SelfUpdater("0.1.0")
    started = threading.Event()
    release = threading.Event()

    def slow_tick(force=False):
        started.set()
        release.wait(timeout=1)

    monkeypatch.setattr(updater, "tick", slow_tick)
    before = time.monotonic()
    updater.tick_async()

    assert time.monotonic() - before < 0.1
    assert started.wait(timeout=1)
    release.set()


def test_windows_helper_uses_move_and_health_rollback(tmp_path):
    script = _windows_swap_script(
        root=str(tmp_path),
        current=r"C:\Link\printforce-link",
        staged=r"C:\Temp\printforce-link",
        backup=r"C:\Link\printforce-link.old",
        temp_dir=r"C:\Temp",
        health_file=r"C:\Link\update-healthy",
        failed_version_file=r"C:\Link\failed-update-version",
        candidate_version="0.2.0",
        pid=123,
    )
    content = open(script, encoding="utf-8").read()

    assert "Move-Item $Current $Backup" in content
    assert "Rename-Item $Current $Backup" not in content
    assert "if ($Healthy)" in content
    assert "Stop-ScheduledTask" in content
    assert "Move-Item $Backup $Current" in content
    assert "Set-Content -Path $FailedVersionFile" in content
    assert "-Encoding ASCII" in content


def test_macos_helper_rolls_back_without_health_marker(tmp_path):
    script = _macos_swap_script(
        root=str(tmp_path),
        current="/Link/printforce-link",
        staged="/tmp/printforce-link",
        backup="/Link/printforce-link.old",
        temp_dir="/tmp/update",
        health_file="/Link/update-healthy",
        failed_version_file="/Link/failed-update-version",
        candidate_version="0.2.0",
        pid=123,
    )
    content = open(script, encoding="utf-8").read()

    assert 'launchctl kill SIGTERM "$LABEL"' in content
    assert 'mv "$BACKUP" "$CURRENT"' in content
    assert 'if [ -f "$HEALTH_FILE" ]' in content
    assert '"$CANDIDATE_VERSION" > "$FAILED_VERSION_FILE"' in content


def test_failed_release_is_quarantined_until_a_newer_version(tmp_path):
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({
        "latest_version": "0.2.0",
        "update_status": "installing",
    }))
    (tmp_path / "failed-update-version").write_bytes(
        b"\xef\xbb\xbf0.2.0\r\n"
    )
    applied = []
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: "v0.2.0",
        apply_fn=applied.append,
        monotonic=Clock(),
        state_path=str(state_path),
    )

    updater.tick()

    assert applied == []
    assert updater.metadata()["update_status"] == STATUS_ERROR
    assert "failed its startup health check" in updater.metadata()["update_error"]


def test_restart_waits_for_main_loop_safe_point(monkeypatch):
    restart_lock = threading.Lock()
    applied = threading.Event()

    def fake_apply(_tag, restart_lock=None):
        with restart_lock:
            applied.set()

    monkeypatch.setattr("bridge.updater._apply_update", fake_apply)
    updater = SelfUpdater(
        "0.1.0",
        latest_tag_fn=lambda: "v0.2.0",
        monotonic=Clock(),
        restart_lock=restart_lock,
    )

    restart_lock.acquire()
    updater.tick_async()
    time.sleep(0.05)
    assert not applied.is_set()
    restart_lock.release()
    assert applied.wait(timeout=1)


def test_download_has_a_bounded_timeout(tmp_path, monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_open(request, timeout):
        seen["timeout"] = timeout
        return Response(b"release")

    monkeypatch.setattr("bridge.updater.urllib.request.urlopen", fake_open)
    destination = tmp_path / "asset"

    _download_file("https://example.invalid/asset", str(destination))

    assert seen["timeout"] == 60
    assert destination.read_bytes() == b"release"
