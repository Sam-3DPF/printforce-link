"""P1 file-push rules: start URL, prepare percent, NLST names."""

from bridge.transfer import (
    lan_start_url,
    listing_has_file,
    prepare_download_complete,
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


def test_nlst_accepts_bare_name_or_path():
    assert listing_has_file(["job.3mf"], "job.3mf")
    assert listing_has_file(["/cache/job.3mf"], "job.3mf")
    assert not listing_has_file(["other.3mf"], "job.3mf")
