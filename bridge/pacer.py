"""When the report loop runs its next pass (plan U10).

The loop used to sleep a fixed ``state_interval_seconds`` (15 s) between
passes, so a print finishing or a printer pausing reached 3D PrintForce up to
15 s late. A printer now pokes the pacer when something the card shows
changes (``PrinterState.change_signature``). The next pass then starts about a
second later instead of waiting out the interval.

Two limits keep a noisy printer from flooding the cloud:

* ``min_gap``: passes start at least this far apart, pokes or not.
* ``settle``: after a poke, wait this long so the rest of the same burst
  (a FINISH is often followed by a full status dump) lands in one report.

With no pokes the loop still runs every ``interval``, as before.
"""
from __future__ import annotations

import threading
import time


class ReportPacer:
    def __init__(self, interval: float, *, min_gap: float = 1.0, settle: float = 0.25,
                 monotonic=time.monotonic, sleep=time.sleep):
        self._interval = float(interval)
        self._min_gap = float(min_gap)
        self._settle = float(settle)
        self._monotonic = monotonic
        self._sleep = sleep
        self._event = threading.Event()

    def poke(self) -> None:
        """A printer changed. Safe from any thread, including paho's."""
        self._event.set()

    def wait(self, pass_started: float) -> str:
        """Block until the next pass may start. Returns ``"change"`` or ``"interval"``.

        ``pass_started`` is the monotonic time the pass that just ended began.
        A poke during that pass still counts: it is only cleared here, right
        before the next pass begins.
        """
        remaining = pass_started + self._interval - self._monotonic()
        poked = self._event.wait(timeout=max(0.0, remaining))
        if poked:
            now = self._monotonic()
            start_at = max(pass_started + self._min_gap, now + self._settle)
            if start_at > now:
                self._sleep(start_at - now)
        self._event.clear()
        return "change" if poked else "interval"
