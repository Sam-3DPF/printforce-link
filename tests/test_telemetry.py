"""Telemetry, status mapping, the delta merge, and the observed print duration.

Every function under test here is pure or clock-injected, so none of this needs
`bambulabs_api` installed — the same property `test_ams.py` relies on. `BambuPrinter`
only ever touches its client through `mqtt_dump()`, so a fake stands in for it.
"""

import logging
import sys

from bridge.config import PrinterConfig
from bridge.printer import (
    _DEFAULT_STALE_AFTER_SECONDS,
    BambuPrinter,
    PrintStopwatch,
    decode_hms,
    merge_status_payload,
    parse_telemetry,
)

_BAMBU_ID = "01P00A123456789"


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self._now = now

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeClient:
    """Stands in for `bambulabs_api.Printer`. Yields one payload per poll and then
    repeats the last one — which is a printer that has stopped changing, and *also*
    exactly what an unplugged printer looks like: `mqtt_dump()` is a dict read on a cache
    the library already holds, so it keeps answering, cheerfully, long after the machine
    is gone. It cannot raise. That is why the liveness tests below exist.

    It returns each payload **raw**, i.e. as the delta it arrived as — the worst case
    the merge exists to absorb — so the bridge's own merge is what these tests
    exercise.

    `mqtt_client_connected()` mirrors the real accessor (`Printer.mqtt_client_connected`
    -> `PrinterMQTTClient.is_connected` -> paho). Set `connected = False` to pull the
    printer off the LAN.
    """

    def __init__(self, payloads, connected=True):
        self._payloads = list(payloads)
        self._last = {}
        self.connected = connected

    def mqtt_dump(self):
        if self._payloads:
            self._last = self._payloads.pop(0)
        return self._last

    def mqtt_client_connected(self):
        return self.connected

    def push(self, payload):
        """The printer sends a new report."""
        self._payloads.append(payload)


def _stopwatch(monotonic=None, wall_clock=None) -> PrintStopwatch:
    return PrintStopwatch(
        _BAMBU_ID,
        monotonic=monotonic or (lambda: 0.0),
        wall_clock=wall_clock or (lambda: 0.0),
    )


def _printer(payloads, monotonic=None, wall_clock=None, connected=True,
             stale_after_seconds=_DEFAULT_STALE_AFTER_SECONDS) -> BambuPrinter:
    cfg = PrinterConfig(bambu_id=_BAMBU_ID, ip="10.0.0.5",
                        access_code="secret", name="P1S-1")
    printer = BambuPrinter(cfg, stopwatch=_stopwatch(monotonic, wall_clock),
                           stale_after_seconds=stale_after_seconds,
                           monotonic=monotonic or (lambda: 0.0))
    printer._client = FakeClient(payloads, connected=connected)
    return printer
