import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "packaging" / "stamp_version.py"
SPEC = importlib.util.spec_from_file_location("stamp_version", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
normalized_version = MODULE.normalized_version
stamp = MODULE.stamp


def test_normalized_version_requires_plain_semver():
    assert normalized_version("v1.2.3") == "1.2.3"
    with pytest.raises(ValueError):
        normalized_version("latest")
    with pytest.raises(ValueError):
        normalized_version("v1.2")


def test_stamp_writes_the_frozen_version_module(tmp_path: Path):
    (tmp_path / "bridge").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "bambu-bridge"\nversion = "0.1.5"\n'
    )

    assert stamp("v0.1.6", tmp_path) == "0.1.6"
    assert '__version__ = "0.1.6"' in (
        tmp_path / "bridge" / "_release_version.py"
    ).read_text()
    assert 'version = "0.1.6"' in (tmp_path / "pyproject.toml").read_text()
