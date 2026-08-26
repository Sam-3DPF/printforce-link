"""Self-update PrintForce Link from signed-off GitHub Releases.

The updater is deliberately boring: check a stable release channel, verify SHA-256,
stage beside the running build, swap, and let the OS supervisor restart Link. Its small
JSON state file contains no credentials; it only preserves the operator's auto-update
preference and enough status for the 3DPF Integrations page.
"""
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_REPO = "Sam-3DPF/printforce-link"
_RELEASES_LATEST_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_DEFAULT_INTERVAL_SECONDS = 6 * 3600   # check a few times a day
_STATE_FILE = "update-state.json"

STATUS_CHECKING = "checking"
STATUS_CURRENT = "current"
STATUS_AVAILABLE = "update_available"
STATUS_INSTALLING = "installing"
STATUS_ERROR = "error"

APPLY_SCHEDULED = "scheduled"
APPLY_SKIPPED = "skipped"
APPLY_FAILED = "failed"


def _parse_version(tag: str) -> Tuple[int, ...]:
    """Parse a 'vMAJOR.MINOR.PATCH' (or unprefixed) tag into a comparable tuple. Non-numeric
    parts become 0 so a malformed tag sorts low rather than crashing the check."""
    cleaned = (tag or "").strip().lstrip("vV")
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """True if release tag `candidate` is a newer version than `current`."""
    return _parse_version(candidate) > _parse_version(current)


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "printforce-link-updater",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def latest_release_tag(fetch=None) -> Optional[str]:
    """Return the latest release's tag, or None on any failure (offline, rate-limited, no
    releases yet)."""
    fetch = fetch or _http_get_json
    try:
        data = fetch(_RELEASES_LATEST_API)
    except Exception as e:
        logger.debug("update check: could not reach GitHub (%s)", type(e).__name__)
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag or None


def _release_asset_name() -> str:
    if sys.platform not in ("darwin", "win32"):
        raise RuntimeError(f"unsupported update platform: {sys.platform}")
    system = "macos" if sys.platform == "darwin" else "windows"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    ext = "tar.gz" if system == "macos" else "zip"
    return f"printforce-link-{system}-{arch}.{ext}"


def _checksum_ok(path: str, name: str, sums_path: str) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    with open(sums_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*").endswith(name):
                return parts[0] == actual
    return False


def _windows_swap_script(
    *,
    root: str,
    current: str,
    staged: str,
    backup: str,
    temp_dir: str,
    pid: int,
) -> str:
    """Write the detached Windows helper that swaps after this process releases its files."""
    script = os.path.join(root, "complete-update.ps1")
    content = f"""\
$ErrorActionPreference = "Stop"
$Current = '{current.replace("'", "''")}'
$Staged = '{staged.replace("'", "''")}'
$Backup = '{backup.replace("'", "''")}'
$TempDir = '{temp_dir.replace("'", "''")}'
try {{
  Wait-Process -Id {pid} -ErrorAction SilentlyContinue
  if (Test-Path $Backup) {{ Remove-Item $Backup -Recurse -Force }}
  Rename-Item $Current $Backup
  try {{
    Move-Item $Staged $Current
  }} catch {{
    if (Test-Path $Current) {{ Remove-Item $Current -Recurse -Force }}
    Rename-Item $Backup $Current
    throw
  }}
  Start-ScheduledTask -TaskName "PrintForceLink"
}} catch {{
  try {{ Start-ScheduledTask -TaskName "PrintForceLink" }} catch {{}}
}} finally {{
  if (Test-Path $TempDir) {{ Remove-Item $TempDir -Recurse -Force }}
  Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
}}
"""
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(content)
    return script


def _swap_macos(current: str, staged: str, backup: str) -> None:
    """Swap a staged build while preserving/restoring the last known-good build."""
    shutil.rmtree(backup, ignore_errors=True)
    os.rename(current, backup)
    try:
        shutil.move(staged, current)
    except Exception:
        shutil.rmtree(current, ignore_errors=True)
        os.rename(backup, current)
        raise


def _apply_update(tag: str) -> str:
    """Download, verify, stage, and install ``tag``.

    macOS can rename the running ``--onedir`` tree, so it swaps in-process and exits for
    LaunchAgent KeepAlive to restart it. Windows keeps executable files locked; a detached
    PowerShell helper waits for this PID to exit, swaps with rollback, then explicitly
    starts the scheduled task.
    """
    if not getattr(sys, "frozen", False):
        logger.info("self-update available (%s) but skipped: not a packaged install "
                    "(update the source with `git pull`)", tag)
        return APPLY_SKIPPED
    onedir = os.path.dirname(sys.executable)            # <root>/printforce-link
    root = os.path.dirname(onedir)
    asset = _release_asset_name()
    base = f"https://github.com/{_REPO}/releases/download/{tag}"
    tmp = tempfile.mkdtemp(prefix="pfl-update-")
    handed_off = False
    try:
        archive = os.path.join(tmp, asset)
        urllib.request.urlretrieve(f"{base}/{asset}", archive)
        sums = os.path.join(tmp, "SHA256SUMS")
        urllib.request.urlretrieve(f"{base}/SHA256SUMS", sums)
        if not _checksum_ok(archive, asset, sums):
            logger.warning("self-update: checksum mismatch for %s — keeping current version", asset)
            return APPLY_FAILED
        extracted = os.path.join(tmp, "extracted")
        shutil.unpack_archive(archive, extracted)
        new_onedir = os.path.join(extracted, "printforce-link")
        if not os.path.isdir(new_onedir):
            logger.warning("self-update: unexpected archive layout — keeping current version")
            return APPLY_FAILED
        executable = os.path.join(
            new_onedir,
            "printforce-link.exe" if sys.platform == "win32" else "printforce-link",
        )
        if not os.path.isfile(executable):
            logger.warning("self-update: release has no executable — keeping current version")
            return APPLY_FAILED

        backup = onedir + ".old"
        if sys.platform == "win32":
            script = _windows_swap_script(
                root=root,
                current=onedir,
                staged=new_onedir,
                backup=backup,
                temp_dir=tmp,
                pid=os.getpid(),
            )
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", script,
                ],
                close_fds=True,
                creationflags=flags,
            )
            handed_off = True
            logger.info("self-update: staged %s; handing off to Windows updater", tag)
            os._exit(75)

        _swap_macos(onedir, new_onedir, backup)
        # Keep .old until the new build has successfully talked to 3DPF. That is the
        # health checkpoint; SelfUpdater.confirm_running() removes it.
        logger.info("self-update: installed %s; restarting", tag)
        os._exit(75)
    except Exception:
        logger.exception("self-update: install failed; keeping current version")
        return APPLY_FAILED
    finally:
        if not handed_off:
            shutil.rmtree(tmp, ignore_errors=True)


def default_state_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), _STATE_FILE)


def _platform_name() -> str:
    system = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else sys.platform
    return f"{system}-{platform.machine().lower() or 'unknown'}"


class SelfUpdater:
    def __init__(self, current_version: str,
                 interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 latest_tag_fn=None, apply_fn=None, monotonic=time.monotonic,
                 state_path: Optional[str] = None):
        self._current = current_version
        self._interval = interval_seconds
        self._latest_tag = latest_tag_fn or latest_release_tag
        self._apply = apply_fn or _apply_update
        self._monotonic = monotonic
        self._last = None
        self._state_path = state_path
        state = self._read_state()
        self._enabled = state.get("auto_update_enabled", True) is not False
        self._latest = state.get("latest_version")
        self._status = state.get("update_status") or STATUS_CHECKING
        self._error = state.get("update_error")
        self._request_id = state.get("update_request_id")
        if self._status == STATUS_INSTALLING and self._latest and not is_newer(
            self._latest, self._current
        ):
            self._status = STATUS_CURRENT
            self._error = None
            self._persist()

    def _read_state(self) -> dict:
        if not self._state_path:
            return {}
        try:
            with open(self._state_path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _persist(self) -> None:
        if not self._state_path:
            return
        payload = {
            "auto_update_enabled": self._enabled,
            "latest_version": self._latest,
            "update_status": self._status,
            "update_error": self._error,
            "update_request_id": self._request_id,
        }
        os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, self._state_path)

    def metadata(self) -> dict:
        return {
            "version": self._current,
            "platform": _platform_name(),
            "auto_update_enabled": self._enabled,
            "latest_version": self._latest,
            "update_status": self._status,
            "update_error": self._error,
            "update_request_id": self._request_id,
        }

    def apply_cloud_command(self, command) -> None:
        """Apply the server-owned preference and one-shot "check now" request."""
        if not isinstance(command, dict):
            return
        enabled = command.get("auto_update_enabled")
        if isinstance(enabled, bool) and enabled != self._enabled:
            self._enabled = enabled
            self._persist()
        request_id = command.get("request_id")
        if request_id and str(request_id) != self._request_id:
            self._request_id = str(request_id)
            self._persist()
            self.tick(force=True)

    def tick(self, force: bool = False) -> None:
        """Throttled update check. Never raises."""
        now = self._monotonic()
        if not force and self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        try:
            tag = self._latest_tag()
            if not tag:
                return
            self._latest = tag.lstrip("vV")
            if not is_newer(tag, self._current):
                self._status = STATUS_CURRENT
                self._error = None
                self._persist()
                return
            if not self._enabled and not force:
                self._status = STATUS_AVAILABLE
                self._error = None
                self._persist()
                return
            self._status = STATUS_INSTALLING
            self._error = None
            self._persist()
            result = self._apply(tag)
            if result == APPLY_FAILED:
                self._status = STATUS_ERROR
                self._error = f"Could not install {tag}. Link will retry later."
            elif result == APPLY_SKIPPED:
                self._status = STATUS_AVAILABLE
            self._persist()
        except Exception as e:
            self._status = STATUS_ERROR
            self._error = f"Update check failed ({type(e).__name__})."
            self._persist()
            logger.warning("self-update check failed (%s); will retry", type(e).__name__)

    def confirm_running(self) -> None:
        """Remove the previous build only after this build has reached 3DPF successfully."""
        if not getattr(sys, "frozen", False):
            return
        backup = os.path.dirname(sys.executable) + ".old"
        if os.path.isdir(backup):
            shutil.rmtree(backup, ignore_errors=True)
