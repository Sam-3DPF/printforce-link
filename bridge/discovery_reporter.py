"""Report the Bambu printers the bridge sees on the LAN to 3DPF: an initial scan burst
at startup, then quiet, then an on-demand bounded burst on request (U7/U11).

Feeds the onboarding wizard's "printers found on your network" list. Reports serial / ip /
model / name only — never an access code (discovery is code-free). Throttled, and never
raises, so it can't disturb the report loop.

SCANNING WINDOWS. There are exactly two, and outside both this is a no-op:

  1. THE STARTUP RAMP — right after the bridge starts, for `ramp_seconds`, scan on the
     fast interval so the whole fleet appears in a minute or two instead of trickling in
     over ten. Bambu printers announce themselves (SSDP) on their own cadence, so a
     passive listen only catches whoever broadcast in the window; bursting scans early
     catches the fleet much faster.

  2. AN ON-DEMAND BURST — opened only when the caller passes `scan_requested=True` to
     `tick()` (wired from the 3DPF state-poll response's `scan_requested` flag, itself
     set by the operator's "Add Printer" click, U8). While the burst is open (for
     `burst_seconds` from the request), scan on the fast interval, same as the ramp. A
     fresh `scan_requested=True` while a burst is already open EXTENDS the window rather
     than stacking a second one.

Before U7 the reporter fell back to a perpetual STEADY interval after the ramp — this
module used to scan forever, quietly, on every bridge in the fleet. That's gone: past the
ramp, with no active burst, `tick()` does nothing at all. `interval_seconds` /
`_DEFAULT_INTERVAL_SECONDS` are kept only so an existing constructor call/test that still
passes it doesn't break; nothing in `tick()` reads it any more.
"""
import logging
import time

from .discover import discover

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60.0  # vestigial — no longer drives scanning; see module docstring
_DEFAULT_FAST_INTERVAL_SECONDS = 20.0
_DEFAULT_RAMP_SECONDS = 180.0
# How long an on-demand burst (triggered by scan_requested=True) stays open once opened.
_DEFAULT_BURST_SECONDS = 45.0
# A longer listen catches more announcements per scan (was 5s).
_DEFAULT_DISCOVER_TIMEOUT_SECONDS = 8.0


def _default_discover(timeout: float):
    return discover(timeout=timeout)


class DiscoveryReporter:
    def __init__(self, dpf, discover_fn=None,
                 interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 fast_interval_seconds: float = _DEFAULT_FAST_INTERVAL_SECONDS,
                 ramp_seconds: float = _DEFAULT_RAMP_SECONDS,
                 burst_seconds: float = _DEFAULT_BURST_SECONDS,
                 discover_timeout_seconds: float = _DEFAULT_DISCOVER_TIMEOUT_SECONDS,
                 monotonic=time.monotonic):
        self._dpf = dpf
        self._discover = discover_fn if discover_fn is not None else _default_discover
        self._interval = interval_seconds  # unused by tick() post-U7; kept for compatibility
        self._fast_interval = fast_interval_seconds
        self._ramp_seconds = ramp_seconds
        self._burst_seconds = burst_seconds
        self._timeout = discover_timeout_seconds
        self._monotonic = monotonic
        self._last = None
        self._start = None                      # first-tick time; drives the startup ramp
        self._burst_until = None                 # monotonic deadline of an open on-demand burst

    def _in_ramp(self, now: float) -> bool:
        return self._start is not None and now - self._start < self._ramp_seconds

    def _in_burst(self, now: float) -> bool:
        return self._burst_until is not None and now < self._burst_until

    def tick(self, scan_requested: bool = False) -> None:
        """Throttled LAN scan + report. Never raises.

        `scan_requested=True` opens a bounded on-demand burst (or extends one already
        open) — see the module docstring for the two scanning windows this respects.
        """
        now = self._monotonic()
        if self._start is None:
            self._start = now

        if scan_requested:
            self._burst_until = now + self._burst_seconds

        if not (self._in_ramp(now) or self._in_burst(now)):
            return  # quiet: past the startup ramp, and no on-demand burst is open

        if self._last is not None and now - self._last < self._fast_interval:
            return  # throttle: fast interval between scans, even inside a window

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
