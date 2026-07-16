"""First-run device pairing for the bridge (U6).

The install command carries a one-time pair token (`... | bash -s -- <PAIR_TOKEN>`). On
first run — when the store holds no cloud token yet — the bridge exchanges that pair
token for a durable cloud credential and writes it to the store; every later run reads
the stored token. So no bearer token is ever pasted into a config file: the bridge writes
its own credential store.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def exchange_pair_token(dpf_base_url: str, pair_token: str, timeout: float = 10.0) -> Optional[str]:
    """Trade a one-time pair token for a durable cloud token via
    POST /api/bridge/pair/exchange. Unauthenticated (the pair token is the credential).
    Returns the cloud token, or None on any failure (expired/used token, network, bad
    body) — the caller surfaces the failure rather than crashing."""
    url = dpf_base_url.rstrip("/") + "/api/bridge/pair/exchange"
    try:
        resp = httpx.post(url, json={"pair_token": pair_token}, timeout=timeout)
    except httpx.RequestError as e:
        logger.warning("pairing: could not reach %s (%s)", url, type(e).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("pairing: exchange rejected (%s) — the pair token may be expired "
                       "or already used; re-issue it in 3DPF", resp.status_code)
        return None
    try:
        body = resp.json()
    except ValueError:
        logger.warning("pairing: exchange returned a non-JSON body")
        return None
    data = body.get("data") if isinstance(body, dict) else None
    token = data.get("cloud_token") if isinstance(data, dict) else None
    if not token:
        logger.warning("pairing: exchange returned no cloud token")
        return None
    return token


def ensure_paired(store, dpf_base_url: str, pair_token: Optional[str]) -> Optional[str]:
    """Return the durable cloud token, pairing first if needed (U6):

      * store already holds a cloud token  -> return it (already paired);
      * else a pair token is available     -> exchange it, persist, and return it;
      * else                                -> None (not paired, nothing to pair with).
    """
    existing = store.get_cloud_token()
    if existing:
        return existing
    if not pair_token:
        return None
    token = exchange_pair_token(dpf_base_url, pair_token)
    if token:
        store.set_cloud_token(token)
        logger.info("bridge paired successfully; cloud credential stored locally")
    return token


def repair(store, dpf_base_url: str, pair_token: str) -> Optional[str]:
    """Re-pair after the stored cloud token was rejected (401) — e.g. the operator hit
    Disconnect in 3DPF (revoking the credential) and re-ran the installer with a fresh
    pair token. Unlike ensure_paired, this does NOT prefer the stored token (that IS the
    rejected one): it exchanges the pair token for a new credential and overwrites the
    store. Returns the new cloud token, or None if the pair token is expired/used/unreachable."""
    token = exchange_pair_token(dpf_base_url, pair_token)
    if token:
        store.set_cloud_token(token)
    return token
