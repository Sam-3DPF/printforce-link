"""Per-printer ring of MQTT traffic and session events.

The rings stay in memory so a collect can upload them without reading disk.
The access code is a configured secret and is stripped before anything is
stored or written; the FTPS login is not an input here. A missing log
directory must not take down the MQTT thread or the watchdog, so file
errors are logged and swallowed.

``export`` counts repeated session trouble onto ``findings``. The catalog
is stale or reset, auth retry, and probe miss. Camera, captcha, and
database signatures are not counted.
"""

import copy
import datetime
import json
import logging
import math
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_REDACTED = "[redacted]"
_MIN_SECRET_LENGTH = 4
_EVENT_TEXT_LIMIT = 256
_FINDING_MIN_COUNT = 2
_FINDING_SKIP_KEYS = frozenset({"t", "at", "kind"})

# One stable id per kind of repeated session trouble. Stale and reset share
# an id because either one is the session dropping and coming back.
_SESSION_FINDINGS = (
    (
        "stale_or_reset",
        frozenset({"stale", "reset"}),
        "The session went stale or reset repeatedly.",
    ),
    (
        "auth_retry",
        frozenset({"auth_retry"}),
        "The session retried after the printer rejected the connection.",
    ),
    (
        "probe_miss",
        frozenset({"probe_miss"}),
        "Command probes went unanswered.",
    ),
)


def _active_secrets(secrets) -> tuple:
    """Drop empty and short secrets. A 1-character code would punch holes in JSON."""
    active = []
    for secret in secrets or ():
        if secret is None:
            continue
        text = secret if isinstance(secret, str) else str(secret)
        if len(text) < _MIN_SECRET_LENGTH:
            continue
        active.append(text)
    # A shorter secret that is a prefix of a longer one must not match first.
    active.sort(key=len, reverse=True)
    return tuple(dict.fromkeys(active))


def _iso(epoch) -> str:
    try:
        stamp = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "1970-01-01T00:00:00.000000Z"
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond:06d}Z"


class PrinterLog:
    """Two rings for one serial: MQTT messages and the session timeline.

    ``capacity`` is the message ring (the last 100). Events use their own
    bound so a noisy watchdog cannot push the traffic out.
    """

    def __init__(self, serial, *, capacity=100, event_capacity=200, secrets=(),
                 monotonic=time.monotonic, wall_clock=time.time, file_path=None,
                 max_file_bytes=1_000_000, backups=3):
        self._serial = serial
        self._capacity = capacity
        self._messages = deque(maxlen=capacity)
        self._events = deque(maxlen=event_capacity)
        self._secrets = _active_secrets(secrets)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._file_path = os.fspath(file_path) if file_path else None
        self._max_file_bytes = max_file_bytes
        self._backups = backups
        self._lock = threading.Lock()

    def record_message(self, direction, topic, payload, *, accepted=None) -> None:
        """Append one MQTT message. ``accepted`` is set on publish attempts only."""
        try:
            safe_topic = self._redact(topic)
            if not isinstance(safe_topic, str):
                safe_topic = str(topic)
            record = {
                "direction": "out" if direction == "out" else "in",
                "topic": safe_topic,
                "payload": self._redact(payload),
            }
            if accepted is not None:
                record["accepted"] = bool(accepted)
            self._append("message", self._messages, record)
        except Exception as exc:
            logger.warning(
                "printer %s: message was not recorded (%s)",
                self._serial, type(exc).__name__,
            )

    def record_event(self, kind, **fields) -> None:
        """Append one timeline event. Fields stay small and JSON-safe."""
        try:
            safe = {}
            for key, value in fields.items():
                name = str(key)
                if name in ("t", "at", "kind"):
                    continue
                safe[name[:64]] = self._bound_field(value)
            self._append("event", self._events, {"kind": str(kind), **safe})
        except Exception as exc:
            logger.warning(
                "printer %s: event was not recorded (%s)",
                self._serial, type(exc).__name__,
            )

    def export(self) -> dict:
        """Deep copy of both rings, oldest first, plus counted findings.

        Findings are computed from the copied events, which are already
        redacted. Safe for the caller to keep.
        """
        with self._lock:
            messages = copy.deepcopy(list(self._messages))
            events = copy.deepcopy(list(self._events))
            serial = self._serial
            capacity = self._capacity
        return {
            "serial": serial,
            "collected_at": _iso(self._wall_clock()),
            "capacity": capacity,
            "messages": messages,
            "events": events,
            "findings": _session_findings(events),
        }

    def _append(self, record_type, ring, record) -> None:
        with self._lock:
            record["t"] = self._monotonic()
            record["at"] = _iso(self._wall_clock())
            # Stamp first so a reader of the deque always sees t and at.
            ordered = {"t": record.pop("t"), "at": record.pop("at")}
            ordered.update(record)
            ring.append(ordered)
            path = self._file_path
            line = None
            if path:
                line = json.dumps(
                    {"record": record_type, **ordered},
                    ensure_ascii=False,
                    default=str,
                )
            if path and line is not None:
                self._write_line(path, line)

    def _redact(self, value):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = json.dumps(str(value), ensure_ascii=False)
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def _bound_field(self, value):
        return _truncate(self._redact(_json_safe(value)))

    def _write_line(self, path, line: str) -> None:
        try:
            self._rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
        except OSError as exc:
            logger.warning(
                "printer %s: log file was not written (%s)",
                self._serial, type(exc).__name__,
            )

    def _rotate_if_needed(self, path: str) -> None:
        """``path.1`` is the newest backup. ``path.N`` is the oldest."""
        if self._backups < 1:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self._max_file_bytes:
            return
        try:
            os.remove(f"{path}.{self._backups}")
        except OSError:
            pass
        for index in range(self._backups - 1, 0, -1):
            try:
                os.replace(f"{path}.{index}", f"{path}.{index + 1}")
            except OSError:
                pass
        try:
            os.replace(path, f"{path}.1")
        except OSError:
            pass


def _session_findings(events) -> list:
    """Count repeated session trouble on an already-exported event list.

    A kind needs ``_FINDING_MIN_COUNT`` hits still on the ring. Text is the
    fixed sentence plus string fields from those events, which were stripped
    before they were stored.
    """
    grouped = {finding_id: [] for finding_id, _kinds, _sentence in _SESSION_FINDINGS}
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        for finding_id, kinds, _sentence in _SESSION_FINDINGS:
            if kind in kinds:
                grouped[finding_id].append(event)
    findings = []
    for finding_id, _kinds, sentence in _SESSION_FINDINGS:
        matched = grouped[finding_id]
        if len(matched) < _FINDING_MIN_COUNT:
            continue
        findings.append({
            "id": finding_id,
            "count": len(matched),
            "text": _finding_text(sentence, matched),
        })
    return findings


def _finding_text(sentence, matched) -> str:
    seen = []
    for event in matched:
        for key, value in event.items():
            if key in _FINDING_SKIP_KEYS or not isinstance(value, str) or not value:
                continue
            if value not in seen:
                seen.append(value)
    if not seen:
        return sentence
    return sentence + " " + " ".join(seen)


def _json_safe(value, depth=0):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= 4:
        return str(value)[:_EVENT_TEXT_LIMIT]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, dict):
        safe = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 20:
                break
            safe[str(key)[:64]] = _json_safe(item, depth + 1)
        return safe
    return str(value)[:_EVENT_TEXT_LIMIT]


def _truncate(value, limit=_EVENT_TEXT_LIMIT):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate(item, limit) for key, item in value.items()}
    return value
