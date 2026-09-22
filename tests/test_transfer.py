"""P1 file-push rules: start URL, prepare percent, FTPS SIZE, NLST names."""
import ftplib

import pytest

from bridge.transfer import (
    lan_start_url,
    listing_has_file,
    prepare_download_complete,
    remote_size_matches,
    store_on_printer,
)


def test_p1_start_url_is_sdcard():
    assert lan_start_url("job.3mf") == "file:///sdcard/job.3mf"
    assert lan_start_url("/job.3mf") == "file:///sdcard/job.3mf"
    assert lan_start_url("file:///sdcard/already.3mf") == "file:///sdcard/already.3mf"
    assert lan_start_url("job.3mf", family="h2") == "ftp:///job.3mf"


def test_prepare_percent_99_is_downloaded():
    assert prepare_download_complete(99) is True
    assert prepare_download_complete("99") is True
    assert prepare_download_complete(100) is True
    assert prepare_download_complete(98) is False
    assert prepare_download_complete(None) is False


class _FakeFtp:
    def __init__(self):
        self.stored = None
        self._size = None
        self.closed = False
        self.raise_426 = False

    def connect(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def login(self, user, password):
        self.user = user
        self.password = password

    def auth(self):
        return True

    def prot_p(self):
        return True

    def storbinary(self, cmd, handle):
        self.stored = handle.read()
        self._size = len(self.stored)
        if self.raise_426:
            raise ftplib.error_temp("426 Failure reading network stream")
        return "226"

    def size(self, name):
        return self._size

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


def test_store_treats_426_with_matching_size_as_success(tmp_path):
    local = tmp_path / "job.3mf"
    local.write_bytes(b"3mf-bytes")
    ftp = _FakeFtp()
    ftp.raise_426 = True
    store_on_printer("10.0.0.5", "code", str(local), "job.3mf", ftp_factory=lambda: ftp)
    assert ftp.stored == b"3mf-bytes"
    assert ftp.user == "bblp"
    assert remote_size_matches(ftp, "job.3mf", 9)


def test_store_fails_when_remote_size_mismatches(tmp_path):
    local = tmp_path / "job.3mf"
    local.write_bytes(b"3mf-bytes")
    ftp = _FakeFtp()

    def size(_name):
        return 1

    ftp.size = size
    with pytest.raises(RuntimeError):
        store_on_printer("10.0.0.5", "code", str(local), "job.3mf", ftp_factory=lambda: ftp)


def test_nlst_accepts_bare_name_or_path():
    assert listing_has_file(["job.3mf"], "job.3mf")
    assert listing_has_file(["/cache/job.3mf"], "job.3mf")
    assert not listing_has_file(["other.3mf"], "job.3mf")
