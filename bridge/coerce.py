"""Coercers for Bambu MQTT payload fields, which are routinely the wrong type.

The printer sends numbers as strings (`nozzle_diameter` arrives as "0.4"), sends
"Unknown" where an int is documented, and omits keys entirely from delta pushes.
Every read of the payload goes through one of these, so a malformed field degrades
to its default instead of raising into the poll loop.
"""

from typing import Optional


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_str(value) -> Optional[str]:
    """A stripped string, or None for anything blank/absent/non-string."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
