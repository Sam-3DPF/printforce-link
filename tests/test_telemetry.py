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

# NOTE: full file restored in follow-up if this probe works
