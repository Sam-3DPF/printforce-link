"""First-run bridge pairing (U6): exchange a pair token for a durable cloud token."""
import httpx

import bridge.pairing as pairing_mod
from bridge.pairing import ensure_paired, exchange_pair_token


class FakeResp:
    def __init__(self, status_code=200, json_body=None, raise_json=False):
        self.status_code = status_code
        self._json = json_body
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._json


class FakeStore:
    def __init__(self, token=None):
        self._token = token
        self.saved = []

    def get_cloud_token(self):
        return self._token

    def set_cloud_token(self, token):
        self._token = token
        self.saved.append(token)


# ---- exchange_pair_token -----------------------------------------------------------

def test_exchange_returns_cloud_token_and_unwraps(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(url=url, json=json)
        return FakeResp(200, {"data": {"cloud_token": "CLOUD-XYZ"}})

    monkeypatch.setattr(pairing_mod.httpx, "post", fake_post)
    assert exchange_pair_token("https://app.3dprintforce.com/", "PAIR-1") == "CLOUD-XYZ"
    assert captured["url"] == "https://app.3dprintforce.com/api/bridge/pair/exchange"
    assert captured["json"] == {"pair_token": "PAIR-1"}


def test_exchange_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(pairing_mod.httpx, "post", lambda url, json=None, timeout=None: FakeResp(401))
    assert exchange_pair_token("https://x", "expired-or-used") is None


def test_exchange_network_error_returns_none(monkeypatch):
    def boom(url, json=None, timeout=None):
        raise httpx.RequestError("down")

    monkeypatch.setattr(pairing_mod.httpx, "post", boom)
    assert exchange_pair_token("https://x", "PAIR-1") is None


def test_exchange_non_json_returns_none(monkeypatch):
    monkeypatch.setattr(pairing_mod.httpx, "post",
                        lambda url, json=None, timeout=None: FakeResp(200, raise_json=True))
    assert exchange_pair_token("https://x", "PAIR-1") is None


def test_exchange_missing_cloud_token_returns_none(monkeypatch):
    monkeypatch.setattr(pairing_mod.httpx, "post",
                        lambda url, json=None, timeout=None: FakeResp(200, {"data": {}}))
    assert exchange_pair_token("https://x", "PAIR-1") is None


# ---- ensure_paired -----------------------------------------------------------------

def test_ensure_paired_returns_existing_token_without_exchanging(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(pairing_mod, "exchange_pair_token",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    store = FakeStore(token="ALREADY-PAIRED")
    assert ensure_paired(store, "https://x", "PAIR-1") == "ALREADY-PAIRED"
    assert calls["n"] == 0                       # already paired -> no exchange


def test_ensure_paired_exchanges_and_persists_on_first_run(monkeypatch):
    monkeypatch.setattr(pairing_mod, "exchange_pair_token", lambda base, tok, **k: "CLOUD-NEW")
    store = FakeStore(token=None)
    assert ensure_paired(store, "https://x", "PAIR-1") == "CLOUD-NEW"
    assert store.saved == ["CLOUD-NEW"]          # written to the store


def test_ensure_paired_no_stored_token_and_no_pair_token_returns_none():
    assert ensure_paired(FakeStore(token=None), "https://x", None) is None


def test_ensure_paired_does_not_store_on_exchange_failure(monkeypatch):
    monkeypatch.setattr(pairing_mod, "exchange_pair_token", lambda *a, **k: None)
    store = FakeStore(token=None)
    assert ensure_paired(store, "https://x", "PAIR-1") is None
    assert store.saved == []
