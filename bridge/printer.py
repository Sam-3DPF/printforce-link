"""One Bambu printer, wrapping `bambulabs_api`.

The library is imported lazily inside `connect()` so the pure logic (`map_status`,
`decode_hms`, `parse_telemetry`, `merge_status_payload`, and `ams.parse_ams`) can be
unit-tested without it installed.
"""
import copy
import logging
import os
import time
from typing import Dict, Optional, Tuple
from .ams import parse_ams, parse_tray_exist_bits
from .coerce import as_float, as_int, clean_str
from .config import PrinterConfig
logger = logging.getLogger(__name__)
