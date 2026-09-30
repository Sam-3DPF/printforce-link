"""The context around a printer's ``print_error`` (plan U7, R13).

Shop P1S-5, P1S-6 and P1S-8 have raised 0500_4003 ("unable to parse the
file") since 2026-09-28. The cause is still unknown: every instance logged on
2026-09-30 had no replay, no reconnect and every mapped tray present. Bambuddy
#1150/#1678 tie it to MQTT disruption while a P1 unpacks a file. So when the
code rises, the printer records what led up to it: the ``gcode_state`` steps
since the last start, the longest silence, recent session trouble, the mapped
trays and the last upload.

Bookkeeping runs on every report, so it stays bounded and does no copying.
The context itself is only built on the rising edge.
"""

import threading
from collections import deque

from ..ams import TRAYS_PER_AMS, mapped_tray_presence
from ..coerce import as_int
from .commands import gcode_state_of

# How far back the gap and session-event checks look.
WINDOW_SECONDS = 180.0
# Session events worth naming beside an error. ``connect`` and ``connack``
# are routine; seconds since the CONNACK stands in for them.
SESSION_EVENT_KINDS = frozenset({
    "reset", "stale", "reset_held", "redial", "unexpected_start", "disconnect",
})
_SESSION_EVENTS_KEPT = 20
# A P1S reports about once a second. A shorter gap is not worth keeping.
_GAP_FLOOR_SECONDS = 1.0
_GAPS_KEPT = 256
_STATES_KEPT = 16
_MAPPING_KEPT = 20
# Link's report loop calls a printer stale at 45s. A longer silence while the
# printer unpacks a file is the Bambuddy lead.
_SUSPECT_GAP_SECONDS = 45.0
# A P1 unpacking or preparing a file raises 0500_4003 if its MQTT session is
# reset (Bambuddy #1150/#1678), so the printer leaves a silent session alone.
UNPACKING_STATES = frozenset({"PREPARE", "SLICING"})
# ``tray_exist_bits`` covers four regular AMS units, bit N == global tray N.
_BIT_TRAYS = 4 * TRAYS_PER_AMS
_UNSEEN = object()


def _seconds(value):
    return round(float(value), 1)


class PrintErrorWatch:
    """Per-printer record of what came before a ``print_error``. Thread-safe.

    Reports arrive on the MQTT thread; starts and uploads on the printer's
    worker.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_report_at = None
        self._state = ""
        # (ended_at, seconds, gcode_state during the silence)
        self._gaps = deque(maxlen=_GAPS_KEPT)
        # (gcode_state, entered_at), reset by each start
        self._states = deque(maxlen=_STATES_KEPT)
        self._start_at = None
        self._mapping = None
        self._upload = None
        # The fault code on the last report. Unseen until the first report,
        # so a code already standing when Link connects is not an edge.
        self._code = _UNSEEN

    def note_report(self, now) -> None:
        """Any printer message. Call before it is merged."""
        with self._lock:
            last = self._last_report_at
            self._last_report_at = now
            if last is not None and now - last >= _GAP_FLOOR_SECONDS:
                self._gaps.append((now, now - last, self._state))
            while self._gaps and now - self._gaps[0][0] > WINDOW_SECONDS:
                self._gaps.popleft()

    def note_start(self, mapping, now) -> None:
        """A ``project_file`` was published. The state history starts over."""
        with self._lock:
            self._start_at = now
            self._mapping = list(mapping or [])[:_MAPPING_KEPT]
            self._states.clear()
            if self._state:
                self._states.append((self._state, now))

    def note_upload(self, *, size, seconds, result, now) -> None:
        with self._lock:
            self._upload = {"bytes": size, "seconds": seconds, "result": result, "at": now}

    def observe(self, gcode_state, code, now) -> bool:
        """Track the merged state. True when ``code`` is a new non-zero fault.

        ``code`` is the fault ``print_error`` or None. A clear, a repeat, and
        the first report Link sees are not edges.
        """
        state = gcode_state_of({"print": {"gcode_state": gcode_state}})
        with self._lock:
            if state and state != self._state:
                self._state = state
                self._states.append((state, now))
            previous = self._code
            self._code = code
        return previous is not _UNSEEN and code is not None and code != previous

    def context(self, now, *, session_events=(), connack_at=None,
                tray_exist_bits=None) -> dict:
        """Fields for the ``print_error`` event, ``origin`` included.

        ``session_events`` are PrinterLog events from the last
        ``WINDOW_SECONDS``, stamped on the same monotonic clock as ``now``.
        """
        with self._lock:
            states = list(self._states)
            gaps = [gap for gap in self._gaps if now - gap[0] <= WINDOW_SECONDS]
            start_at = self._start_at
            mapping = list(self._mapping) if self._mapping is not None else None
            upload = dict(self._upload) if self._upload is not None else None

        timeline = []
        for index, (state, entered) in enumerate(states):
            until = states[index + 1][1] if index + 1 < len(states) else now
            timeline.append([state, _seconds(until - entered)])

        longest = max(gaps, key=lambda gap: gap[1], default=None)
        recent = [_session_event(event, now) for event in list(session_events)[-_SESSION_EVENTS_KEPT:]]
        trays = _tray_presence(mapping, tray_exist_bits)
        if upload is not None:
            upload["age"] = _seconds(now - upload.pop("at"))

        return {
            "states": timeline,
            "since_start": _seconds(now - start_at) if start_at is not None else None,
            "longest_gap": None if longest is None else {
                "seconds": _seconds(longest[1]),
                "state": longest[2] or None,
                "ago": _seconds(now - longest[0]),
            },
            "since_connack": _seconds(now - connack_at) if connack_at is not None else None,
            "session_events": recent,
            "mapping": mapping,
            "trays": trays,
            "tray_exist_bits": tray_exist_bits,
            "upload": upload,
            "origin": _most_suspicious(recent, gaps, trays),
        }


def _session_event(event, now) -> dict:
    out = {"kind": event.get("kind"), "age": _seconds(now - event.get("t", now))}
    for key in ("reason", "origin"):
        if event.get(key) is not None:
            out[key] = event[key]
    return out


def _tray_presence(mapping, bits) -> list:
    """Mapped regular-AMS trays and whether their bit is set.

    ``present`` is None when the bits are unknown. External and AMS HT
    entries have no bit and are left out.
    """
    if not mapping:
        return []
    trays = []
    seen = set()
    for item in mapping:
        tray = as_int(item, default=None)
        if tray is None or not 0 <= tray < _BIT_TRAYS or tray in seen:
            continue
        seen.add(tray)
        trays.append({"tray": tray, "present": mapped_tray_presence(tray, bits)})
    return trays


def _most_suspicious(events, gaps, trays) -> str:
    """In order: a reset or an unexpected start in the window, a long silence
    while unpacking, an absent mapped tray. Else nothing Link can see."""
    for event in reversed(events):
        if event["kind"] in ("unexpected_start", "reset"):
            return f"{event['kind']} {event['age']:.0f}s before"
    unpacking = [
        gap for gap in gaps
        if gap[2] in UNPACKING_STATES and gap[1] > _SUSPECT_GAP_SECONDS
    ]
    if unpacking:
        gap = max(unpacking, key=lambda item: item[1])
        return f"{gap[1]:.0f}s status gap during {gap[2]}"
    absent = [str(tray["tray"]) for tray in trays if tray["present"] is False]
    if absent:
        noun = "tray" if len(absent) == 1 else "trays"
        return f"mapped {noun} {', '.join(absent)} absent"
    return "no preceding session event"
