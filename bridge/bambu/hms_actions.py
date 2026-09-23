"""HMS button commands and the other multi-field publishes.

``ignore`` carries a decimal ``err``, ``param`` ``reserve``, and ``job_id``
from the merged ``print.subtask_id``. The bridge submission id is not
substituted, and ``job_id`` is not put on the snapshot. ``idle_ignore``
dismisses and does not resume. ``clean_print_error`` is its own command.
Names that only exist for a screen publish nothing.
"""

_UI_ONLY = frozenset({
    "check_assistant",
    "jump_to_liveview",
    "cancle",
})

_CALIBRATION_TABLE = frozenset({"extrusion_cali_get", "extrusion_cali_set"})


def ui_only_action(action) -> bool:
    return isinstance(action, str) and action in _UI_ONLY


def is_calibration_table_reply(doc) -> bool:
    """True when this datagram is a calibration-table read or write.

    Feeding it to the status parser would replace ``nozzle_diameter`` with
    the diameter the table was asked about. ``get_version`` is not in this set.
    """
    if not isinstance(doc, dict):
        return False
    for key in ("print", "info", "system"):
        body = doc.get(key)
        if isinstance(body, dict) and body.get("command") in _CALIBRATION_TABLE:
            return True
    return False


def decimal_err(value) -> str:
    """Firmware compares ``err`` as a decimal integer, not the hex code."""
    if isinstance(value, bool) or value is None:
        return "0"
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.lower().startswith("0x"):
        return str(int(text, 16))
    hex_digits = all(c in "0123456789abcdefABCDEF" for c in text) and bool(text)
    if hex_digits and (any(c in "abcdefABCDEF" for c in text) or len(text) >= 8):
        return str(int(text, 16))
    if text.isdigit():
        return str(int(text))
    return "0"


def build_ignore(*, err, job_id) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "ignore",
            "err": decimal_err(err),
            "param": "reserve",
            "job_id": "" if job_id is None else str(job_id),
        },
    }


def build_idle_ignore(*, err, dismiss_type=0) -> dict:
    """Dismiss a reminder. This command does not resume the print."""
    return {
        "print": {
            "sequence_id": "0",
            "command": "idle_ignore",
            "err": decimal_err(err),
            "type": int(dismiss_type),
        },
    }


def build_clean_print_error() -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "clean_print_error",
        },
    }


def build_chamber_light(on: bool) -> list:
    """Two ``ledctrl`` publishes: ``chamber_light`` and ``chamber_light2``."""
    mode = "on" if on else "off"
    return [
        {
            "system": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": node,
                "led_mode": mode,
            },
        }
        for node in ("chamber_light", "chamber_light2")
    ]
