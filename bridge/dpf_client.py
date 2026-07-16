"""Outbound client to the 3DPF bridge API.

Sends the cloud bearer token, retries transient (5xx / network) failures with
backoff, and returns `{}` when a call ultimately fails so the bridge loop keeps
running. Never logs the token or request/response bodies.

Corrected 2026-07-14 (access-code courier / U7): it is NO LONGER true that "bodies
carry only printer state, never access codes". The config-pull RESPONSE body
(GET /api/bridge/printers/config) DELIVERS decrypted printer access codes down to the
bridge during onboarding — which is exactly why bodies must never be logged here.
"""

import logging
import random
import time
from typing import List, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class DpfClient:
    def __init__(self, base_url: str, cloud_token: str, timeout: float = 10.0, retries: int = 3):
        # rstrip defensively so DpfClient is self-contained when built with a raw
        # base URL directly (tests / other callers), not only via normalized config.
        self._base = base_url.rstrip("/")
        if not self._base.startswith("https://"):
            logger.warning("dpf_base_url is not https:// — the bearer token would be sent in cleartext")
        self._headers = {"Authorization": f"Bearer {cloud_token}"}
        self._retries = retries
        # Set when the cloud rejects our token (401/403) — i.e. the credential was
        # revoked (the operator hit Disconnect). The main loop watches this and re-pairs;
        # set_token clears it. It is a flag, not an exception, so the forever loop's
        # per-iteration try/except never has to special-case auth.
        self.unauthorized = False
        # Persistent client reuses the keep-alive connection across the forever
        # report/heartbeat loop instead of a fresh TCP+TLS handshake per call.
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def set_token(self, cloud_token: str) -> None:
        """Swap in a freshly re-paired cloud token after the old one was revoked, and
        clear the unauthorized flag so the loop resumes normal reporting."""
        self._headers["Authorization"] = f"Bearer {cloud_token}"
        self.unauthorized = False

    def report_state(self, reports: List[Dict]) -> Dict:
        """POST a batch of printer-state reports; returns the desired-state body."""
        return self._post("/api/bridge/printers/state", {"printers": reports})

    def heartbeat(self) -> Dict:
        """Liveness ping; returns desired-state for all bridge-managed printers."""
        return self._post("/api/bridge/heartbeat", {})

    def resolve_batch(self, correlation_key: str) -> Dict:
        """Resolve an upload's correlation key → {batch_id, required_colors, ...} (U8).

        Returns {} when the cloud can't resolve it yet (404): the batch may not exist
        yet, or the key is unroutable. The router treats an empty/`batch_id`-less result
        as "still unresolved" and leaves the job queued to retry — a 404 here is an
        expected, quiet outcome, not an error, so it is polled without log spam.
        """
        return self._send("GET", "/api/bridge/batches/resolve",
                           params={"correlation_key": correlation_key}, quiet_404=True)

    def report_dispatched(self, batch_id: str, bambu_id: str) -> Dict:
        """Tell 3DPF a batch started on `bambu_id` (U10). Best-effort: the print is
        already physically running, so a transient failure is retried by _send and an
        ultimate failure returns {} without unwinding the local dispatch."""
        return self._post(f"/api/bridge/batches/{batch_id}/dispatched",
                          {"bambu_id": bambu_id})

    def report_complete(self, batch_id: str, plate_number: Optional[int] = None) -> Dict:
        """Tell 3DPF a batch's print finished (U12). Idempotent cloud-side, so the caller
        retries until the returned dict carries a batch_id (the ack)."""
        return self._post(f"/api/bridge/batches/{batch_id}/complete",
                          {"plate_number": plate_number})

    def report_failed(self, batch_id: str, plate_number: Optional[int] = None,
                      reason: Optional[str] = None) -> Dict:
        """Tell 3DPF a batch's print failed (U12). Idempotent; retried until acked."""
        return self._post(f"/api/bridge/batches/{batch_id}/failed",
                          {"plate_number": plate_number, "reason": reason})

    def get_printers_config(self) -> Dict:
        """Pull this farm's couriered printer config (U4):

            {"printers": [{"printer_id", "bambu_id", "local_ip",
                           "access_code"?, "config_version"?}, ...]}

        access_code/config_version are present ONLY for a printer whose code has not yet
        been delivered. Returns {} on failure — the reconciler retries next tick. The
        response body carries plaintext access codes, so (like all of DpfClient) it is
        never logged."""
        return self._send("GET", "/api/bridge/printers/config")

    def ack_printers_config(self, acks: List[Dict]) -> Dict:
        """Confirm the delivered codes are durably stored so the cloud deletes them (U4).

        `acks`: [{"printer_id", "config_version"}, ...] echoed from get_printers_config.
        Returns {"acknowledged": N, "acknowledged_ids": [...]}."""
        return self._post("/api/bridge/printers/config/ack", {"acks": acks})

    def report_discovered(self, printers: List[Dict]) -> Dict:
        """Report the LAN printers the bridge currently sees, for the onboarding wizard (U11).
        `printers`: [{"bambu_id", "ip", "model", "name"}, ...] — no access code. Best-effort:
        a failure returns {} and is retried next interval."""
        return self._post("/api/bridge/printers/discovered", {"printers": printers})

    @staticmethod
    def _unwrap(payload) -> Dict:
        """Unwrap 3DPF's standard `{"data": ...}` success envelope.

        Every 3DPF endpoint returns its body through create_json_response, which
        wraps it as `{"data": <body>}`. The bridge cares only about the body, so
        callers (report_state/heartbeat -> desired-state, and U9's resolve) read
        the unwrapped dict. Tolerates a missing/None envelope by returning {}, so
        an old server or an empty body degrades to "no desired-state", never a
        KeyError in the forever loop.
        """
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"] or {}
        return payload or {}

    def _post(self, path: str, body: Dict) -> Dict:
        return self._send("POST", path, body=body)

    def _send(self, method: str, path: str, body: Optional[Dict] = None,
              params: Optional[Dict] = None, quiet_404: bool = False) -> Dict:
        """One retrying request. 5xx / network errors retry with jittered backoff; a 4xx
        is surfaced (our bug, not transient) and returns {}. `quiet_404` demotes a 404 to
        an expected empty result (the resolve poll), so a not-found batch isn't logged as
        an error every loop. Always returns a dict — the forever loop never sees a raise.
        """
        url = self._base + path
        delay = 1.0
        for attempt in range(1, self._retries + 1):
            try:
                if method == "GET":
                    resp = self._client.get(url, params=params, headers=self._headers)
                else:
                    resp = self._client.post(url, json=body, headers=self._headers)
                if resp.status_code >= 500:
                    logger.warning("3DPF %s -> %s (attempt %d)", path, resp.status_code, attempt)
                else:
                    if quiet_404 and resp.status_code == 404:
                        return {}  # expected "not resolved yet", not an error
                    resp.raise_for_status()  # 4xx is our bug, not transient — surface it
                    try:
                        return self._unwrap(resp.json())
                    except ValueError:
                        # Non-JSON 2xx body (an HTML page from a proxy/CDN, or a
                        # redirect body). json.JSONDecodeError subclasses ValueError
                        # and is caught by neither httpx except above — so guard it
                        # here, or it escapes and crashes the forever loop.
                        logger.warning("3DPF %s returned a non-JSON body", path)
                        return {}
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    # Credential revoked (Disconnect) — flag it so the loop re-pairs
                    # instead of knocking forever with a dead token.
                    self.unauthorized = True
                logger.error("3DPF %s rejected: %s", path, e.response.status_code)
                return {}
            except httpx.RequestError as e:
                logger.warning("3DPF %s network error: %s (attempt %d)", path, type(e).__name__, attempt)
            if attempt < self._retries:
                time.sleep(delay * (1 + random.random()))  # jitter avoids synchronized retry waves
                delay *= 2
        logger.error("3DPF %s failed after %d attempts", path, self._retries)
        return {}
