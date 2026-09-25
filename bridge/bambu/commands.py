"""The one place a printer command is shaped.

``project_file`` is refused while the printer's last ``gcode_state`` is
PREPARE, SLICING, RUNNING, or PAUSE. That check uses the state, not the
session flag: a stale connection can still be a printer that is printing.
IDLE, FINISH, and FAILED are valid, and this module never sends a stop first.

``task_id``, ``subtask_id``, and ``project_id`` are one fresh submission id:
epoch milliseconds modulo 2147483647. P1S firmware clamps a larger id, and a
reused id looks like a continuation of the last failed job. Zero is not used;
the state tracker treats it as "no id".

Tray integers and the flat ``ams_mapping`` are not the same number for every
unit. ``-1`` stays unresolved (``ams_mapping2`` ``{255, 255}``). ``254`` and
``255`` are the external spool (flat ``-1``, ``{255, 0}``). AMS-HT keeps
128–135. A2L 24–27 is sent as local slot 0–3 on ``ams_id`` 16. ``use_ams``
is false only when every entry is external.
"""

import time

_BUSY = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
# P1S firmware 01.10.00.00 clamps a larger id and then treats the start as
# the previous job. The value itself must stay at or under this cap.
_ID_CAP = 2147483647
_EXTERNAL = frozenset({254, 255})
_FAN_PARTS = frozenset({1, 2, 3, 10})
_SKIP_STATES = frozenset({"RUNNING", "PAUSE"})


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


def tray_index_allowed(value) -> bool:
    """True for a cloud map integer this encoder knows how to send.

    The old cap was 15. Raising that cap to 255 would also accept the gaps
    between regular, A2L, AMS-HT, and the external spool.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if value == -1 or value in _EXTERNAL:
        return True
    if 0 <= value <= 15 or 24 <= value <= 27 or 128 <= value <= 135:
        return True
    return False


def live_slot_number_allowed(slot_number) -> bool:
    """True for a report ``slot_number`` the live remap can turn into a tray."""
    if isinstance(slot_number, bool) or not isinstance(slot_number, int):
        return False
    return 1 <= slot_number <= 28


def live_slot_to_tray(slot_number: int) -> int:
    """Wire tray index for a ``slots`` row.

    Regular 1–16 become 0–15. AMS-HT 17–24 become 128–135. A2L 25–28 become
    24–27. HT and A2L are not ``slot_number - 1``.
    """
    number = _tray(slot_number)
    if 1 <= number <= 16:
        return number - 1
    if 17 <= number <= 24:
        return 128 + (number - 17)
    if 25 <= number <= 28:
        return 24 + (number - 25)
    raise ValueError(f"slot_number {slot_number!r} is not a known tray")


def ams_fields(mapping):
    """``(ams_mapping, ams_mapping2, use_ams)`` for a ``project_file``.

    ``-1`` stays ``-1`` in ``ams_mapping`` and is ``{255, 255}`` in
    ``ams_mapping2``. ``254`` and ``255`` become flat ``-1`` and
    ``{255, 0}``. ``use_ams`` is false only when every entry is external.
    An empty map is not "all external": the printer keeps AMS on.
    """
    trays = [_tray(item) for item in list(mapping or [])]
    flat = []
    mapped = []
    for tray in trays:
        if tray < 0:
            flat.append(-1)
            mapped.append({"ams_id": 255, "slot_id": 255})
        elif tray in _EXTERNAL:
            flat.append(-1)
            mapped.append({"ams_id": 255, "slot_id": 0})
        elif 128 <= tray <= 135:
            flat.append(tray)
            mapped.append({"ams_id": tray, "slot_id": 0})
        elif 24 <= tray <= 27:
            local = tray - 24
            flat.append(local)
            mapped.append({"ams_id": 16, "slot_id": local})
        else:
            flat.append(tray)
            mapped.append({"ams_id": tray // 4, "slot_id": tray % 4})
    use_ams = not (trays and all(tray in _EXTERNAL for tray in trays))
    return flat, mapped, use_ams


def _bare_file_name(remote_name: str) -> str:
    name = (remote_name or "").strip()
    if "//" in name:
        name = name.split("//", 1)[-1]
    name = name.rstrip("/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def _subtask_name(remote_name: str) -> str:
    """Drop one trailing ``.3mf`` or ``.gcode``, case-insensitive."""
    name = _bare_file_name(remote_name)
    lower = name.lower()
    for suffix in (".3mf", ".gcode"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def bed_leveling_fields(bed_leveling=None):
    """``bed_leveling`` and ``auto_bed_leveling`` for one start.

    ``auto_bed_leveling`` is 0 off, 1 on, 2 let the printer decide. The
    operator's choice from 3DPF (True or False) sets both. None, when 3DPF
    sent no choice, keeps the printer's own decision.
    """
    if bed_leveling is True:
        return True, 1
    if bed_leveling is False:
        return False, 0
    return False, 2


def build_project_file(remote_name, ams_mapping, plate_number, profile, submission_id,
                       bed_leveling=None) -> dict:
    """The MQTT document for one start. The caller has already refused a busy state."""
    trays, mapped, use_ams = ams_fields(ams_mapping)
    leveling, auto_leveling = bed_leveling_fields(bed_leveling)
    url = project_file_url(remote_name, profile.start_url_scheme)
    token = str(int(submission_id))
    bare = _bare_file_name(remote_name)
    return {
        "print": {
            "sequence_id": "20000",
            "command": "project_file",
            "param": f"Metadata/plate_{int(plate_number)}.gcode",
            "url": url,
            "subtask_name": _subtask_name(remote_name),
            "file": bare,
            "md5": "",
            "bed_type": "auto",
            "bed_leveling": leveling,
            "auto_bed_leveling": auto_leveling,
            "flow_cali": False,
            "extrude_cali_flag": 2,
            "extrude_cali_manual_mode": 0,
            "layer_inspect": False,
            "timelapse": False,
            "cfg": "0",
            "profile_id": "0",
            "nozzle_offset_cali": 0,
            "project_id": token,
            "task_id": token,
            "subtask_id": token,
            "use_ams": use_ams,
            "ams_mapping": trays,
            "ams_mapping2": mapped,
            "vibration_cali": bool(profile.vibration_cali),
        },
    }


def build_gcode_line(text, sequence_id) -> dict:
    """One ``gcode_line``. ``text`` is sent unchanged, newlines included."""
    return {
        "print": {
            "sequence_id": str(sequence_id),
            "command": "gcode_line",
            "param": "" if text is None else str(text),
        },
    }


def build_print_speed(level) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "print_speed",
            "param": str(level),
        },
    }


def build_airduct(mode_id) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "set_airduct",
            "modeId": int(mode_id),
            "submode": -1,
        },
    }


def build_skip_objects(obj_list) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "skip_objects",
            "obj_list": list(obj_list),
        },
    }


def build_select_extruder(index) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "select_extruder",
            "extruder_index": int(index),
        },
    }


def build_calibration(option) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "calibration",
            "option": int(option),
        },
    }


def resolve_drying_filament(filament, loaded_type) -> str:
    """The printer rejects an empty filament type. A loaded tray wins, else PLA."""
    text = filament.strip() if isinstance(filament, str) else ""
    if text:
        return text
    loaded = loaded_type.strip() if isinstance(loaded_type, str) else ""
    return loaded or "PLA"


def build_drying(*, mode, temp, duration, filament="", loaded_type=None,
                 ams_id=0, rotate_tray=False, sequence_id="0") -> dict:
    """``ams_filament_drying``. Mode 1 cools at 20. Mode 0 cools at 0.

    The printer method refuses this document on a P1 profile. Tests of the
    temperatures call this builder directly.
    """
    cooling = 20 if int(mode) == 1 else 0
    return {
        "print": {
            "sequence_id": str(sequence_id),
            "command": "ams_filament_drying",
            "ams_id": int(ams_id),
            "temp": int(temp),
            "cooling_temp": cooling,
            "duration": int(duration),
            "humidity": 0,
            "mode": int(mode),
            "rotate_tray": bool(rotate_tray),
            "filament": resolve_drying_filament(filament, loaded_type),
            "close_power_conflict": False,
        },
    }


def filament_load_target(tray) -> tuple:
    """``(ams_id, slot_id, target)`` for ``ams_change_filament``.

    Tray 5 is unit 1 slot 1, target 5. External 254 is ``ams_id`` 255 and
    ``slot_id`` 254. Unload is not this function.
    """
    index = _tray(tray)
    if index == 254:
        return 255, 254, 254
    return index // 4, index % 4, index


def build_change_filament(*, ams_id, slot_id, target) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "ams_change_filament",
            "ams_id": int(ams_id),
            "slot_id": int(slot_id),
            "target": int(target),
            "curr_temp": -1,
            "tar_temp": -1,
        },
    }


def build_ams_control(param) -> dict:
    return {
        "print": {
            "sequence_id": "0",
            "command": "ams_control",
            "param": param,
        },
    }


def _upper_hex(color) -> str:
    text = "" if color is None else str(color).strip()
    if text.startswith("#"):
        text = text[1:]
    return text.upper()


def build_filament_setting(*, ams_id, tray_id, tray_info_idx="", tray_color="",
                           tray_type="", nozzle_temp_min="", nozzle_temp_max="") -> dict:
    """A slot write. ``tray_color`` is bare hex, forced to uppercase."""
    return {
        "print": {
            "sequence_id": "0",
            "command": "ams_filament_setting",
            "ams_id": int(ams_id),
            "tray_id": int(tray_id),
            "tray_info_idx": "" if tray_info_idx is None else str(tray_info_idx),
            "tray_color": _upper_hex(tray_color),
            "tray_type": "" if tray_type is None else str(tray_type),
            "nozzle_temp_min": "" if nozzle_temp_min is None else nozzle_temp_min,
            "nozzle_temp_max": "" if nozzle_temp_max is None else nozzle_temp_max,
        },
    }


def build_filament_reset(*, ams_id, tray_id) -> dict:
    return build_filament_setting(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx="",
        tray_color="00000000",
        tray_type="",
        nozzle_temp_min="",
        nozzle_temp_max="",
    )


def build_extrusion_cali_sel(*, tray_id, **fields) -> dict:
    """Bind a K profile. ``setting_id`` is omitted: firmware mislinks it."""
    body = {
        "sequence_id": "0",
        "command": "extrusion_cali_sel",
        "tray_id": int(tray_id),
    }
    for key, value in fields.items():
        if key == "setting_id":
            continue
        body[key] = value
    return {"print": body}


def skip_objects_allowed(gcode_state: str) -> bool:
    return (gcode_state or "").strip().upper() in _SKIP_STATES


def fan_part_allowed(part) -> bool:
    return isinstance(part, int) and not isinstance(part, bool) and part in _FAN_PARTS


def fan_speed_allowed(speed) -> bool:
    return isinstance(speed, int) and not isinstance(speed, bool) and 0 <= speed <= 255
