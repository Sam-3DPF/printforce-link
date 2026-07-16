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
# For the first few minutes after the bridge starts — right after install, when the
# operator is watching the wizard fill in — scan far more often so the whole fleet
# appears in a minute or two instead of trickling in over ten. Bambu printers announce
# themselves (SSDP) on their own cadence, so a passive listen only catches whoever
# broadcast in the window; bursting scans early catches the fleet much faster. After the
# ramp we settle to the steady interval so discovery stops stealing loop time from state
# reporting.
_DEFAULT_FAST_INTERVAL_SECONDS = 20.0
_DEFAULT_RAMP_SECONDS = 180.0
# A longer listen catches more announcements per scan (was 5s).
_DEFAULT_DISCOVER_TIMEOUT_SECONDS = 8.0


def _default_discover(timeout: float):
    return discover(timeout=timeout)


class DiscoveryReporter:
    def __init__(self, dpf, discover_fn=None,
                 interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 fast_interval_seconds: float = _DEFAULT_FAST_INTERVAL_SECONDS,
                 ramp_seconds: float = _DEFAULT_RAMP_SECONDS,
                 discover_timeout_seconds: float = _DEFAULT_DISCOVER_TIMEOUT_SECONDS,
                 monotonic=time.monotonic):
        self._dpf = dpf
        self._discover = discover_fn if discover_fn is not None else _default_discover
        self._interval = interval_seconds
        self._fast_interval = fast_interval_seconds
        self._ramp_seconds = ramp_seconds
        self._timeout = discover_timeout_seconds
        self._monotonic = monotonic
        self._last = None
        self._start = None                      # first-tick time; drives the startup ramp

    def _current_interval(self, now: float) -> float:
        """The fast interval during the post-startup ramp, then the steady interval."""
        if self._start is not None and now - self._start < self._ramp_seconds:
            return self._fast_interval
        return self._interval

    def tick(self) -> None:
        """Throttled LAN scan + report. Never raises."""
        now = self._monotonic()
        if self._start is None:
            self._start = now
        if self._last is not None and now - self._last < self._current_interval(now):
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
