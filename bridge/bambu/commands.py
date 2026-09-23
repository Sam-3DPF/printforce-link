"""The one place a printer command is shaped.

``project_file`` is refused while the printer's last ``gcode_state`` is
PREPARE, SLICING, RUNNING, or PAUSE. That check uses the state, not the
session flag: a stale connection can still be a printer that is printing.
IDLE, FINISH, and FAILED are valid, and this module never sends a stop first.

``task_id``, ``subtask_id``, and ``project_id`` are one fresh submission id:
epoch milliseconds modulo 2147483647. P1S firmware clamps a larger id, and a
reused id looks like a continuation of the last failed job. Zero is not used;
the state tracker treats it as "no id".

``-1`` in the AMS map is unresolved. It becomes ``ams_mapping2``
``{255, 255}`` and never the external spool. ``use_ams`` is false only when
every entry is 254 or higher.
"""

import time

_BUSY = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
# P1S firmware 01.10.00.00 clamps a larger id and then treats the start as
# the previous job. The value itself must stay at or under this cap.
_ID_CAP = 2147483647
_EXTERNAL_TRAY = 254


def gcode_state_of(payload) -> str:
    """Upper-case ``gcode_state`` from a merged MQTT document, or ""."""
    if not isinstance(payload, dict):
        return ""
    print_obj = payload.get("print")
    if not isinstance(print_obj, dict):
        return ""
    state = print_obj.get("gcode_state")
    if not isinstance(state, str):
        return ""
    return state.strip().upper()


def project_file_refused(gcode_state: str) -> bool:
    """True when a start must not be published. Unknown is not busy."""
    return (gcode_state or "").strip().upper() in _BUSY


def fresh_submission_id(now=None, previous=None) -> int:
    """A submission id in ``1..2147483647``, different from ``previous``.

    Two starts in the same millisecond would otherwise share the clock value.
    The next integer is still inside the firmware cap.
    """
    if now is None:
        now = time.time()
    value = int(now * 1000) % _ID_CAP
    if value <= 0:
        value = 1
    if previous is not None and int(previous) == value:
        value = 1 if value >= _ID_CAP else value + 1
    return value


def project_file_url(remote_name: str, scheme: str) -> str:
    """URL for a file already on the printer. A name that already has a scheme stays."""
    name = (remote_name or "").strip()
    if not name:
        raise ValueError("remote file name is required")
    if "//" in name:
        return name
    name = name.lstrip("/")
    prefix = scheme or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{name}"


def _tray(value) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"AMS tray {value!r} is not a tray index")
    return int(value)


def ams_fields(mapping):
    """``(ams_mapping, ams_mapping2, use_ams)`` for a ``project_file``.

    ``-1`` stays ``-1`` in ``ams_mapping`` and is ``{255, 255}`` in
    ``ams_mapping2``. A tray of 254 or higher is the external spool
    (``{255, 0}``). ``use_ams`` is false only when every entry is external.
    An empty map is not "all external": the printer keeps AMS on.
    """
    trays = [_tray(item) for item in list(mapping or [])]
    mapped = []
    for tray in trays:
        if tray < 0:
            mapped.append({"ams_id": 255, "slot_id": 255})
        elif tray >= _EXTERNAL_TRAY:
            mapped.append({"ams_id": 255, "slot_id": 0})
        else:
            mapped.append({"ams_id": tray // 4, "slot_id": tray % 4})
    use_ams = not (trays and all(tray >= _EXTERNAL_TRAY for tray in trays))
    return trays, mapped, use_ams


def build_project_file(remote_name, ams_mapping, plate_number, profile, submission_id) -> dict:
    """The MQTT document for one start. The caller has already refused a busy state."""
    trays, mapped, use_ams = ams_fields(ams_mapping)
    url = project_file_url(remote_name, profile.start_url_scheme)
    token = str(int(submission_id))
    return {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": f"Metadata/plate_{int(plate_number)}.gcode",
            "url": url,
            "subtask_name": remote_name,
            "project_id": token,
            "task_id": token,
            "subtask_id": token,
            "use_ams": use_ams,
            "ams_mapping": trays,
            "ams_mapping2": mapped,
            "vibration_cali": bool(profile.vibration_cali),
        },
    }
