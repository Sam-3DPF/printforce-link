"""Periodically report the Bambu printers the bridge sees on the LAN to 3DPF (U11).

Feeds the onboarding wizard's "printers found on your network" list. Reports serial / ip /
model / name only — never an access code (discovery is code-free). Throttled, and never
raises, so it can't disturb the report loop.
"""
import logging
import time

from .discover import discover

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60.0
_DEFAULT_DISCOVER_TIMEOUT_SECONDS = 5.0


def _default_discover(timeout: float):
    return discover(timeout=timeout)


class DiscoveryReporter:
    def __init__(self, dpf, discover_fn=None,
                 interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 discover_timeout_seconds: float = _DEFAULT_DISCOVER_TIMEOUT_SECONDS,
                 monotonic=time.monotonic):
        self._dpf = dpf
        self._discover = discover_fn if discover_fn is not None else _default_discover
        self._interval = interval_seconds
        self._timeout = discover_timeout_seconds
        self._monotonic = monotonic
        self._last = None

    def tick(self) -> None:
        """Throttled LAN scan + report. Never raises."""
        now = self._monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        try:
            found = self._discover(self._timeout)
            self._dpf.report_discovered([
                {"bambu_id": d.serial, "ip": d.ip, "model": d.model, "name": d.name}
                for d in found
            ])
        except Exception as e:
            logger.warning("discovery report failed (%s); will retry next interval",
                           type(e).__name__)
