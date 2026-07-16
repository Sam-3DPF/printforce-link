"""Self-update PrintForce Link from GitHub Releases (U9).

On a slow interval the agent checks the latest release; if it is newer than the running
version it downloads the matching artifact, verifies its checksum, atomically swaps the
installed `--onedir` folder, and EXITS so the supervisor (LaunchAgent / Task Scheduler)
relaunches it from the new build. It never overwrites its own running executable in place.

The version comparison + release lookup are pure and unit-tested. The download/swap runs
only from a packaged (frozen) install where the layout is known — from source it is a
no-op — and is validated by the release smoke test, not unit tests.
"""
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_REPO = "Sam-3DPF/printforce-link"
_RELEASES_LATEST_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_DEFAULT_INTERVAL_SECONDS = 6 * 3600   # check a few times a day


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


def _apply_update(tag: str) -> None:
    """Download the release for `tag`, verify, atomically swap the install, and hard-exit so
    the supervisor relaunches the new build. No-op (logged) unless running as a packaged
    (frozen) install, since only then is the swap layout known and safe."""
    if not getattr(sys, "frozen", False):
        logger.info("self-update available (%s) but skipped: not a packaged install "
                    "(update the source with `git pull`)", tag)
        return
    onedir = os.path.dirname(sys.executable)            # <root>/printforce-link
    asset = _release_asset_name()
    base = f"https://github.com/{_REPO}/releases/download/{tag}"
    tmp = tempfile.mkdtemp(prefix="pfl-update-")
    try:
        archive = os.path.join(tmp, asset)
        urllib.request.urlretrieve(f"{base}/{asset}", archive)
        sums = os.path.join(tmp, "SHA256SUMS")
        urllib.request.urlretrieve(f"{base}/SHA256SUMS", sums)
        if not _checksum_ok(archive, asset, sums):
            logger.warning("self-update: checksum mismatch for %s — keeping current version", asset)
            return
        extracted = os.path.join(tmp, "extracted")
        shutil.unpack_archive(archive, extracted)
        new_onedir = os.path.join(extracted, "printforce-link")
        if not os.path.isdir(new_onedir):
            logger.warning("self-update: unexpected archive layout — keeping current version")
            return
        # Move the current build aside, move the new one in, then drop the old copy. On
        # macOS/Linux the running binary's open dir can be renamed; the hard exit + relaunch
        # then starts the new build.
        backup = onedir + ".old"
        shutil.rmtree(backup, ignore_errors=True)
        os.rename(onedir, backup)
        shutil.move(new_onedir, onedir)
        shutil.rmtree(backup, ignore_errors=True)
        logger.info("self-update: installed %s; restarting", tag)
        os._exit(0)   # supervisor (LaunchAgent/Task) relaunches from the swapped-in build
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class SelfUpdater:
    def __init__(self, current_version: str,
                 interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 latest_tag_fn=None, apply_fn=None, monotonic=time.monotonic):
        self._current = current_version
        self._interval = interval_seconds
        self._latest_tag = latest_tag_fn or latest_release_tag
        self._apply = apply_fn or _apply_update
        self._monotonic = monotonic
        self._last = None

    def tick(self) -> None:
        """Throttled update check. Never raises."""
        now = self._monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        try:
            tag = self._latest_tag()
            if tag and is_newer(tag, self._current):
                self._apply(tag)
        except Exception as e:
            logger.warning("self-update check failed (%s); will retry", type(e).__name__)
