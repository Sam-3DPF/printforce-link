"""One send per printer, confirmed by a two-phase watchdog.

Phase A lasts 90 seconds. An active printer state confirms the send. An echo
of this send's submission id, with no active state yet, moves to phase B.
Phase B lasts 180 seconds and only confirms on an active state.

A phase A timeout hard-resets the session and waits until the session is
connected before publishing again. A phase B timeout publishes again without
a reset: resetting while the printer is still parsing the file is what
produces 0500_4003. Three attempts and the send fails with the last reason.
The file is uploaded once.
"""

import json
import os

from .bambu.commands import project_file_refused

PHASE_A_SECONDS = 90.0
PHASE_B_SECONDS = 180.0
MAX_ATTEMPTS = 3

_READY_GCODE = frozenset({"IDLE", "FINISH", "FAILED"})
_BUSY_STATUS = frozenset({"PRINTING", "PAUSED", "OFFLINE"})
_ATTEMPT_FIELDS = (
    "submission_id", "attempts", "phase", "phase_started_at",
    "last_failure", "uploaded", "pending_republish", "gcode_file",
)


def snapshot_is_active(snapshot) -> bool:
    """True when the printer has accepted a job, by status or raw gcode_state."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("status") in ("PRINTING", "PAUSED"):
        return True
    return project_file_refused(_gcode(snapshot))


def snapshot_echoed(snapshot, submission_id) -> bool:
    """True when this report carries the submission id Link just sent."""
    if not isinstance(snapshot, dict) or submission_id is None:
        return False
    token = str(submission_id)
    if not token or token == "0":
        return False
    for key in ("print_submission_id", "subtask_id", "task_id"):
        value = snapshot.get(key)
        if value is not None and str(value) == token:
            return True
    return False


def snapshot_commands_rejected(snapshot) -> bool:
    return isinstance(snapshot, dict) and snapshot.get("commands_rejected") is True


def ready_for_upload(snapshot) -> bool:
    """True when a file may be uploaded.

    No snapshot means this caller has no printer state (the older fakes). A
    snapshot must be IDLE, FINISH, or FAILED, and ``connection: live`` when
    that field is present. PRINTING, PAUSED, and OFFLINE never upload.
    """
    if not isinstance(snapshot, dict):
        return True
    if snapshot.get("status") in _BUSY_STATUS:
        return False
    if snapshot_commands_rejected(snapshot):
        return False
    gcode = _gcode(snapshot)
    if gcode:
        if gcode not in _READY_GCODE:
            return False
    if "connection" in snapshot and snapshot.get("connection") != "live":
        return False
    return True


def printer_is_held(started_keys, bambu_id, key, live) -> bool:
    """True when this printer already has a send the watchdog has not finished.

    A send that is no longer in the desired state does not hold the printer:
    that record is being dropped, and the next plate may start.
    """
    serial = str(bambu_id)
    for other in started_keys:
        if other == key or other not in live:
            continue
        if str(other[1]) == serial:
            return True
    return False


def decide(record, snapshot, now, *, phase_a=PHASE_A_SECONDS,
           phase_b=PHASE_B_SECONDS, max_attempts=MAX_ATTEMPTS) -> str:
    """What the watchdog should do on this pass.

    ``confirm`` the printer is active. ``enter_b`` the printer echoed our id.
    ``reset_retry`` phase A ran out. ``republish`` a reset is waiting on a
    connected session. ``retry`` phase B ran out. ``fail`` the attempt budget
    is spent or the printer is refusing commands. ``wait`` otherwise.
    """
    if snapshot_commands_rejected(snapshot):
        return "fail"
    if snapshot_is_active(snapshot):
        return "confirm"
    phase = record.get("phase") or "A"
    try:
        started = float(record.get("phase_started_at"))
    except (TypeError, ValueError):
        started = float(now)
    age = float(now) - started
    try:
        attempts = int(record.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if record.get("pending_republish"):
        if attempts >= max_attempts:
            return "fail"
        if age >= phase_a:
            return "reset_retry"
        return "republish"
    if phase == "B":
        if age >= phase_b:
            return "fail" if attempts >= max_attempts else "retry"
        return "wait"
    if snapshot_echoed(snapshot, record.get("submission_id")):
        return "enter_b"
    if age >= phase_a:
        if _gcode_file_changed(record, snapshot):
            return "enter_b"
        return "fail" if attempts >= max_attempts else "reset_retry"
    return "wait"


def failure_reason(record, snapshot) -> str:
    """The named reason stored on a send that is about to be reported failed.

    A drying unit (``dry_time`` greater than 0) is named. The cycle is not stopped.
    """
    if snapshot_commands_rejected(snapshot):
        reason = "commands_rejected"
    elif record.get("last_failure"):
        reason = str(record["last_failure"])
    elif (record.get("phase") or "A") == "B":
        reason = "no_active"
    else:
        reason = "no_echo"
    return _with_drying_unit(reason, snapshot)


def attempt_path(started_path: str) -> str:
    return started_path + ".attempt"


def load_attempt(started_path: str, router, bambu_id: str, now: float) -> dict:
    """The persisted attempt, or phase A attempt 1 anchored at the marker time."""
    data = _read_json(attempt_path(started_path))
    assignment = _assignment(router, bambu_id)
    if isinstance(assignment, dict):
        for field in _ATTEMPT_FIELDS:
            if field not in data and assignment.get(field) is not None:
                data[field] = assignment.get(field)
    data.setdefault("phase", "A")
    data.setdefault("attempts", 1)
    if data.get("phase_started_at") is None:
        data["phase_started_at"] = _marker_mtime(started_path, now)
    data.setdefault("uploaded", os.path.exists(started_path))
    data.setdefault("last_failure", None)
    data.setdefault("submission_id", None)
    data.setdefault("pending_republish", False)
    data.setdefault("gcode_file", None)
    return data


def save_attempt(started_path: str, router, bambu_id: str, record: dict) -> None:
    """Write the attempt next to the start marker and onto the assignment."""
    stored = {field: record.get(field) for field in _ATTEMPT_FIELDS}
    _write_json(attempt_path(started_path), stored)
    update = getattr(router, "update_send_attempt", None)
    if callable(update):
        update(str(bambu_id), **stored)


def uploaded_already(started_path: str) -> bool:
    return _read_json(attempt_path(started_path)).get("uploaded") is True


def mark_uploaded(started_path: str) -> None:
    path = attempt_path(started_path)
    data = _read_json(path)
    data["uploaded"] = True
    _write_json(path, data)


def discard_attempt(started_path: str) -> None:
    try:
        os.unlink(attempt_path(started_path))
    except OSError:
        pass


def failure_latched(started_path: str) -> bool:
    """True after this send already reported failure. A later pass must not upload."""
    return _read_json(attempt_path(started_path)).get("phase") == "failed"


def latch_failure(started_path: str, key, reason: str) -> None:
    """Remember a reported failure until this send leaves the desired state."""
    _write_json(attempt_path(started_path), {
        "phase": "failed",
        "last_failure": reason,
        "uploaded": True,
        "key": [str(key[0]), str(key[1]), int(key[2])],
    })


def release_settled_attempts(spool_dir: str, live) -> None:
    """Drop failure latches for sends the cloud is no longer asking for."""
    try:
        names = os.listdir(spool_dir)
    except OSError:
        return
    live_keys = set(live)
    for name in names:
        if not name.endswith(".started.attempt"):
            continue
        path = os.path.join(spool_dir, name)
        data = _read_json(path)
        if data.get("phase") != "failed":
            continue
        raw = data.get("key")
        if not isinstance(raw, list) or len(raw) != 3:
            continue
        try:
            key = (str(raw[0]), str(raw[1]), int(raw[2]))
        except (TypeError, ValueError):
            continue
        if key in live_keys:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def _gcode_file_changed(record, snapshot) -> bool:
    """True when phase A saw a different file name than the one stored at send time.

    A missing stored name is not a change. The pre-send name is what was
    remembered, so a later republish must not overwrite it before this check.
    """
    if not isinstance(record, dict) or not isinstance(snapshot, dict):
        return False
    stored = record.get("gcode_file")
    if stored is None or stored == "":
        return False
    current = snapshot.get("gcode_file")
    if current is None:
        return False
    return str(current) != str(stored)


def _with_drying_unit(reason: str, snapshot) -> str:
    if not isinstance(snapshot, dict):
        return reason
    dry = snapshot.get("dry_time")
    if isinstance(dry, bool):
        return reason
    try:
        dry_time = int(dry)
    except (TypeError, ValueError):
        return reason
    if dry_time <= 0:
        return reason
    unit = snapshot.get("drying_unit")
    named = "drying unit" if unit is None else f"drying unit {unit}"
    if named in reason:
        return reason
    return f"{reason}; {named}"


def _gcode(snapshot) -> str:
    state = snapshot.get("gcode_state") if isinstance(snapshot, dict) else None
    if not isinstance(state, str):
        return ""
    return state.strip().upper()


def _assignment(router, bambu_id: str):
    snapshot = getattr(router, "assignments_snapshot", None)
    if not callable(snapshot):
        return None
    found = snapshot().get(str(bambu_id))
    return found if isinstance(found, dict) else None


def _marker_mtime(started_path: str, now: float) -> float:
    try:
        return float(os.stat(started_path).st_mtime)
    except (OSError, TypeError, ValueError):
        return float(now)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
