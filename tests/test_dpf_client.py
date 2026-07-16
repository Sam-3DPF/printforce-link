import bridge.dpf_client as dpf_mod
from bridge.dpf_client import DpfClient


class _FakeResp:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def raise_for_status(self):
        # Only 4xx would raise here; the tests exercise 200 and 5xx paths.
        pass

    def json(self):
        return self._json


def test_report_state_posts_expected_shape(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None):  # method on the persistent Client
        captured.update(url=url, json=json, headers=headers)
        return _FakeResp(200, {"printers": [{"bambu_id": "S1", "desired_status": "IDLE"}]})

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)

    client = DpfClient("https://app.3dprintforce.com", "tok")
    out = client.report_state([{"bambu_id": "S1", "status": "IDLE", "slots": []}])

    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/printers/state"
    assert captured["json"] == {"printers": [{"bambu_id": "S1", "status": "IDLE", "slots": []}]}
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert out == {"printers": [{"bambu_id": "S1", "desired_status": "IDLE"}]}


def test_post_retries_on_5xx_then_returns_empty(monkeypatch):
    calls = {"n": 0}

    def fake_post(self, url, json=None, headers=None):  # method on the persistent Client
        calls["n"] += 1
        return _FakeResp(503)

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    monkeypatch.setattr(dpf_mod.time, "sleep", lambda *_a, **_k: None)  # no real backoff

    client = DpfClient("https://x", "tok", retries=3)
    assert client.heartbeat() == {}
    assert calls["n"] == 3  # retried up to the cap, then gave up


def test_non_json_2xx_body_returns_empty_not_crash(monkeypatch):
    # A 2xx with a non-JSON body (e.g. an HTML page from a proxy/CDN, or the
    # Vercel edge proxy mangling /api/*) must NOT escape _post and crash the
    # forever loop — json.JSONDecodeError (a ValueError) is caught -> {}.
    class _NonJsonResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def fake_post(self, url, json=None, headers=None):
        return _NonJsonResp()

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    client = DpfClient("https://x", "tok")
    assert client.report_state([]) == {}  # guarded, returns empty instead of raising


def test_resolve_batch_gets_with_key_and_unwraps_envelope(monkeypatch):
    captured = {}

    def fake_get(self, url, params=None, headers=None):
        captured.update(url=url, params=params, headers=headers)
        return _FakeResp(200, {"data": {"batch_id": "B1", "required_colors": ["#FF6A13"]}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "get", fake_get)
    client = DpfClient("https://app.3dprintforce.com", "tok")
    out = client.resolve_batch("batch-2026-07-15-abcd1234-{plate_num}.3mf")

    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/batches/resolve"
    assert captured["params"] == {"correlation_key": "batch-2026-07-15-abcd1234-{plate_num}.3mf"}
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert out == {"batch_id": "B1", "required_colors": ["#FF6A13"]}


def test_resolve_batch_404_is_quiet_empty_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_get(self, url, params=None, headers=None):
        calls["n"] += 1
        return _FakeResp(404)  # batch not known yet — expected, not an error

    monkeypatch.setattr(dpf_mod.httpx.Client, "get", fake_get)
    monkeypatch.setattr(dpf_mod.time, "sleep", lambda *_a, **_k: None)
    client = DpfClient("https://x", "tok", retries=3)

    assert client.resolve_batch("batch-x") == {}
    assert calls["n"] == 1  # a 404 returns immediately; it is not a transient 5xx to retry


def test_report_dispatched_posts_to_batch_path(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None):
        captured.update(url=url, json=json)
        return _FakeResp(200, {"data": {"batch_id": "B1", "status": "PRINTING"}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    client = DpfClient("https://app.3dprintforce.com", "tok")
    out = client.report_dispatched("B1", "SERIAL-A")

    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/batches/B1/dispatched"
    assert captured["json"] == {"bambu_id": "SERIAL-A"}
    assert out == {"batch_id": "B1", "status": "PRINTING"}


def test_report_complete_and_failed_post_to_batch_paths(monkeypatch):
    seen = []

    def fake_post(self, url, json=None, headers=None):
        seen.append((url, json))
        return _FakeResp(200, {"data": {"batch_id": "B1"}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    client = DpfClient("https://app.3dprintforce.com", "tok")

    client.report_complete("B1", plate_number=1)
    client.report_failed("B1", plate_number=1, reason="clog")

    assert seen[0] == ("https://app.3dprintforce.com/api/bridge/batches/B1/complete",
                       {"plate_number": 1})
    assert seen[1] == ("https://app.3dprintforce.com/api/bridge/batches/B1/failed",
                       {"plate_number": 1, "reason": "clog"})


def test_get_printers_config_unwraps_envelope(monkeypatch):
    entry = {"printer_id": "p1", "bambu_id": "S1", "local_ip": "192.168.1.5",
             "access_code": "CODE", "config_version": "abcd"}

    def fake_get(self, url, params=None, headers=None):
        assert url == "https://x/api/bridge/printers/config"
        return _FakeResp(200, {"data": {"printers": [entry]}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "get", fake_get)
    client = DpfClient("https://x", "tok")
    assert client.get_printers_config() == {"printers": [entry]}


def test_ack_printers_config_posts_acks(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None):
        captured.update(url=url, json=json)
        return _FakeResp(200, {"data": {"acknowledged": 1, "acknowledged_ids": ["p1"]}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    client = DpfClient("https://app.3dprintforce.com", "tok")
    out = client.ack_printers_config([{"printer_id": "p1", "config_version": "abcd"}])

    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/printers/config/ack"
    assert captured["json"] == {"acks": [{"printer_id": "p1", "config_version": "abcd"}]}
    assert out == {"acknowledged": 1, "acknowledged_ids": ["p1"]}


def test_report_discovered_posts_printers(monkeypatch):
    captured = {}
    entry = {"bambu_id": "S1", "ip": "192.168.1.5", "model": "C12", "name": "P1S-1"}

    def fake_post(self, url, json=None, headers=None):
        captured.update(url=url, json=json)
        return _FakeResp(200, {"data": {"recorded": 1}})

    monkeypatch.setattr(dpf_mod.httpx.Client, "post", fake_post)
    client = DpfClient("https://app.3dprintforce.com", "tok")
    out = client.report_discovered([entry])

    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/printers/discovered"
    assert captured["json"] == {"printers": [entry]}
    assert out == {"recorded": 1}
