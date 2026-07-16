import pytest

from bridge.config import parse_config, Config, ConfigError


def _valid_raw():
    # Distinctive secret values (not substrings of the repr's field names) so the
    # redaction test can't false-positive on e.g. "tok" inside "cloud_token".
    return {
        "dpf_base_url": "https://app.3dprintforce.com/",
        "cloud_token": "SUPERSECRETTOKENVALUE",
        "printers": [{"bambu_id": "S1", "ip": "1.2.3.4", "access_code": "SECRETACCESSCODE", "name": "P1"}],
    }


def test_parse_config_valid():
    cfg = parse_config(_valid_raw())
    assert isinstance(cfg, Config)
    assert cfg.dpf_base_url == "https://app.3dprintforce.com"  # trailing slash stripped
    assert len(cfg.printers) == 1 and cfg.printers[0].bambu_id == "S1"
    assert cfg.state_interval_seconds == 15


def test_parse_config_requires_dpf_base_url():
    raw = _valid_raw()
    del raw["dpf_base_url"]
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_parse_config_allows_no_cloud_token():
    # A paired consumer install (U6) gets its cloud token from the store, not config.toml.
    raw = _valid_raw()
    del raw["cloud_token"]
    cfg = parse_config(raw)
    assert cfg.cloud_token is None


def test_parse_config_allows_no_printers():
    # A consumer install (U4) ships config.toml with only dpf_base_url + cloud_token;
    # printers arrive via the courier + local store, not a [[printers]] block.
    raw = _valid_raw()
    raw["printers"] = []
    cfg = parse_config(raw)
    assert cfg.printers == []


def test_parse_config_printer_missing_field():
    raw = _valid_raw()
    del raw["printers"][0]["access_code"]
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_repr_redacts_secrets():
    cfg = parse_config(_valid_raw())
    assert "SUPERSECRETTOKENVALUE" not in repr(cfg)
    assert "***" in repr(cfg)
    assert "SECRETACCESSCODE" not in repr(cfg.printers[0])


def test_parse_config_rejects_nonpositive_interval():
    # 0/negative would make the loop hammer the API with no delay.
    for field in ("state_interval_seconds", "heartbeat_interval_seconds"):
        raw = _valid_raw()
        raw[field] = 0
        with pytest.raises(ConfigError):
            parse_config(raw)


def test_the_staleness_window_is_derived_from_the_poll_interval():
    """How long a printer may say nothing before the bridge stops believing its last
    payload (`BambuPrinter.snapshot`). "Three polls of silence" only means something
    relative to how often we poll, so the window is a product, not a constant."""
    cfg = parse_config(_valid_raw())
    assert cfg.offline_after_stale_polls == 3
    assert cfg.stale_after_seconds == 45          # 3 x the default 15s poll

    raw = _valid_raw()
    raw["state_interval_seconds"] = 10
    raw["offline_after_stale_polls"] = 6
    assert parse_config(raw).stale_after_seconds == 60


def test_parse_config_rejects_a_nonpositive_staleness_window():
    """0 or negative makes the window 0s: every printer is stale the moment it is read,
    and the whole farm reports OFFLINE forever."""
    for value in (0, -1):
        raw = _valid_raw()
        raw["offline_after_stale_polls"] = value
        with pytest.raises(ConfigError):
            parse_config(raw)


# --- [printhost] parsing (U7) ------------------------------------------------

def _valid_printhost():
    return {
        "upload_key": "UPLOADSECRETKEYVALUE",
        "cert_file": "certs/printhost.crt",
        "key_file": "certs/printhost.key",
    }


def test_printhost_absent_is_none():
    cfg = parse_config(_valid_raw())
    assert cfg.printhost is None  # observability-only, no inbound port


def test_printhost_valid_defaults():
    raw = _valid_raw()
    raw["printhost"] = _valid_printhost()
    ph = parse_config(raw).printhost
    assert ph.upload_key == "UPLOADSECRETKEYVALUE"
    assert ph.host == "127.0.0.1" and ph.port == 8899
    assert ph.spool_dir == "spool" and ph.queue_path == "queue.json"
    assert ph.max_upload_bytes == 200 * 1024 * 1024


def test_printhost_repr_redacts_upload_key():
    raw = _valid_raw()
    raw["printhost"] = _valid_printhost()
    text = repr(parse_config(raw).printhost)
    assert "UPLOADSECRETKEYVALUE" not in text
    assert "upload_key=***" in text


@pytest.mark.parametrize("missing", ["upload_key", "cert_file", "key_file"])
def test_printhost_missing_required_raises(missing):
    raw = _valid_raw()
    ph = _valid_printhost()
    del ph[missing]
    raw["printhost"] = ph
    with pytest.raises(ConfigError):
        parse_config(raw)


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_printhost_bad_port_raises(port):
    raw = _valid_raw()
    raw["printhost"] = {**_valid_printhost(), "port": port}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_printhost_bad_max_upload_raises():
    raw = _valid_raw()
    raw["printhost"] = {**_valid_printhost(), "max_upload_mb": 0}
    with pytest.raises(ConfigError):
        parse_config(raw)
