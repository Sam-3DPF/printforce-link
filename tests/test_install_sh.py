"""Shop Mac installer: stop the running agent before replacing its binary."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
ASSET = "printforce-link-macos-arm64.tar.gz"
LABEL = "com.3dprintforce.printforce-link"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _build_release(tmp: Path) -> tuple[Path, str]:
    payload = tmp / "payload"
    inner = payload / "printforce-link"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "printforce-link").write_text("fake-agent\n")
    archive = tmp / ASSET
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(inner, arcname="printforce-link")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def _harness(
    tmp: Path,
    *,
    launchctl_mode: str = "bootstrap_ok",
    uid: str = "501",
    pair_token: str | None = "PAIR-CODE",
) -> dict:
    archive, digest = _build_release(tmp)
    log = tmp / "order.log"
    log.write_text("")
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)

    _write_executable(
        bin_dir / "uname",
        "#!/bin/sh\n"
        'if [ "$1" = "-s" ]; then echo Darwin; exit 0; fi\n'
        'if [ "$1" = "-m" ]; then echo arm64; exit 0; fi\n'
        "exit 1\n",
    )
    _write_executable(
        bin_dir / "id",
        "#!/bin/sh\n"
        f'if [ "$1" = "-u" ]; then echo {uid}; exit 0; fi\n'
        "exit 1\n",
    )
    _write_executable(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    _write_executable(bin_dir / "xattr", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "shasum",
        "#!/bin/sh\n"
        'if [ "$1" = "-a" ] && [ "$2" = "256" ] && [ "$3" = "-c" ]; then\n'
        "  read -r line\n"
        '  file="${line##* }"\n'
        '  test -f "$file" || exit 1\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    sums = tmp / "SHA256SUMS"
    sums.write_text(f"{digest}  {ASSET}\n")
    _write_executable(
        bin_dir / "curl",
        "#!/bin/sh\n"
        f'log="{log}"\n'
        f'archive="{archive}"\n'
        f'sums="{sums}"\n'
        "out=''\n"
        "url=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    -o) out=$2; shift 2 ;;\n"
        "    -fSL|-fsSL|-f|-s|-S|-L) shift ;;\n"
        "    *) url=$1; shift ;;\n"
        "  esac\n"
        "done\n"
        f'if echo "$url" | grep -q "{ASSET}"; then cp "$archive" "$out"; exit 0; fi\n'
        'if echo "$url" | grep -q SHA256SUMS; then cp "$sums" "$out"; exit 0; fi\n'
        "exit 1\n",
    )
    _write_executable(
        bin_dir / "tar",
        "#!/bin/sh\n"
        f'echo tar "$@" >> "{log}"\n'
        f'PATH="{os.defpath}" exec /usr/bin/tar "$@"\n',
    )
    _write_executable(
        bin_dir / "launchctl",
        "#!/bin/sh\n"
        f'echo launchctl "$@" >> "{log}"\n'
        f'mode="{launchctl_mode}"\n'
        'cmd=$1\n'
        'case "$cmd" in\n'
        "  bootout|unload|enable) exit 0 ;;\n"
        "esac\n"
        'if [ "$mode" = "bootstrap_ok" ]; then\n'
        '  [ "$cmd" = "bootstrap" ] && exit 0\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$mode" = "kickstart_ok" ]; then\n'
        '  [ "$cmd" = "bootstrap" ] && exit 1\n'
        '  [ "$cmd" = "kickstart" ] && exit 0\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$mode" = "load_ok" ]; then\n'
        '  [ "$cmd" = "bootstrap" ] && exit 1\n'
        '  [ "$cmd" = "kickstart" ] && exit 1\n'
        '  [ "$cmd" = "load" ] && exit 0\n'
        "  exit 1\n"
        "fi\n"
        "exit 1\n",
    )

    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(home)
    argv = ["bash", str(INSTALL_SH)]
    if pair_token is not None:
        argv.extend([pair_token, "https://dev.3dprintforce.com"])
    result = subprocess.run(
        argv,
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    return {
        "code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "log": log.read_text() if log.exists() else "",
        "home": home,
    }


def test_reinstall_stops_the_running_agent_before_replacing_files(tmp_path):
    result = _harness(tmp_path)
    assert result["code"] == 0, result["stderr"] or result["stdout"]
    lines = [line for line in result["log"].splitlines() if line]
    stop_at = next(i for i, line in enumerate(lines) if line.startswith("launchctl bootout "))
    extract_at = next(i for i, line in enumerate(lines) if line.startswith("tar "))
    start_at = next(i for i, line in enumerate(lines) if line.startswith("launchctl bootstrap "))
    assert stop_at < extract_at
    assert extract_at < start_at
    installed = result["home"] / ".printforce-link" / "printforce-link" / "printforce-link"
    assert installed.read_text() == "fake-agent\n"
    config = (result["home"] / ".printforce-link" / "config.toml").read_text()
    assert config == 'dpf_base_url = "https://dev.3dprintforce.com"\n'
    plist = result["home"] / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert "PAIR-CODE" in plist.read_text()
    assert "Done! PrintForce Link is installed and connecting." in result["stdout"]


def test_reinstall_restarts_in_place_when_bootstrap_hits_error_5(tmp_path):
    result = _harness(tmp_path, launchctl_mode="kickstart_ok")
    assert result["code"] == 0, result["stderr"] or result["stdout"]
    assert "launchctl kickstart -k gui/501/" + LABEL in result["log"]
    assert "Could not start the agent" not in result["stderr"]


def test_reinstall_falls_back_to_load_when_kickstart_fails(tmp_path):
    result = _harness(tmp_path, launchctl_mode="load_ok")
    assert result["code"] == 0, result["stderr"] or result["stdout"]
    assert "launchctl load " in result["log"]


def test_reinstall_fails_closed_when_the_agent_cannot_start(tmp_path):
    result = _harness(tmp_path, launchctl_mode="all_fail")
    assert result["code"] != 0
    assert "Could not start the agent" in result["stderr"]
    assert "Do not run this as root" in result["stderr"]


def test_installer_refuses_root(tmp_path):
    result = _harness(tmp_path, uid="0")
    assert result["code"] != 0
    assert "Do not run this as root" in result["stderr"]
    assert "tar " not in result["log"]


def test_reinstall_keeps_existing_config(tmp_path):
    config = tmp_path / "home" / ".printforce-link" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('dpf_base_url = "https://dev.3dprintforce.com"\n# keep-me\n')
    result = _harness(tmp_path)
    assert result["code"] == 0, result["stderr"] or result["stdout"]
    assert config.read_text() == 'dpf_base_url = "https://dev.3dprintforce.com"\n# keep-me\n'


def test_missing_pair_code_fails_before_download(tmp_path):
    result = _harness(tmp_path, pair_token=None)
    assert result["code"] != 0
    assert "No pairing code" in result["stderr"]
    assert "tar " not in result["log"]
