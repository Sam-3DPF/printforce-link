"""Match a printer's reply to the command Link sent (plan U13, KTD12).

A Bambu printer answers many commands on its report topic with the same
``command`` and ``sequence_id`` plus ``result`` ("success" / "fail") and
``reason``. Link used ``sequence_id: "0"`` for all of them, so a reply could
not be tied to a click. A mailbox command now gets its own id while it is
published:

    with replies.watch(command_id, on_reply):
        fleet.apply_control(...)          # publishes on this thread

``ReplyBook.stamp`` gives each publish inside the watch a unique id and
remembers it; ``ReplyBook.resolve`` reads each report and calls
``on_reply(command_id, "applied" | "rejected", reason, body)`` once. Nothing
happens for a printer that does not reply: the command stays ``published``.

``project_file`` keeps its own id rules (``20000`` / submission ids) and is
never stamped here.
"""
from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Optional, Tuple

_local = threading.local()

APPLIED = "applied"
REJECTED = "rejected"
_SUCCESS = frozenset({"success", "ok"})
_FAILURE = frozenset({"fail", "failed", "failure", "error"})
# Stamped ids start here, clear of "0", "20000" and the get_version probe.
_FIRST_SEQUENCE = 40000


@contextmanager
def watch(command_id: str, on_reply: Callable):
    """Publishes on this thread inside the block belong to ``command_id``."""
    previous = getattr(_local, "watch", None)
    _local.watch = (str(command_id), on_reply)
    try:
        yield
    finally:
        _local.watch = previous


def current() -> Optional[Tuple[str, Callable]]:
    return getattr(_local, "watch", None)


class ReplyBook:
    """One printer's commands waiting for a reply."""

    def __init__(self, monotonic=time.monotonic, ttl_seconds: float = 60.0):
        self._monotonic = monotonic
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._pending: Dict[Tuple[str, str], Tuple[str, Callable, float]] = {}
        self._next = _FIRST_SEQUENCE

    def stamp(self, payload):
        """The payload to publish. Unchanged unless a watch is active."""
        active = current()
        if active is None or not isinstance(payload, dict):
            return payload
        command_id, on_reply = active
        stamped = copy.deepcopy(payload)
        now = self._monotonic()
        with self._lock:
            self._expire(now)
            for body in stamped.values():
                if not isinstance(body, dict):
                    continue
                name = body.get("command")
                if not isinstance(name, str) or name == "project_file":
                    continue
                if str(body.get("sequence_id", "0")) != "0":
                    continue
                sequence = str(self._next)
                self._next += 1
                body["sequence_id"] = sequence
                self._pending[(name, sequence)] = (command_id, on_reply, now)
        return stamped

    def resolve(self, doc) -> None:
        """Read one report. Calls the matching command's ``on_reply`` once."""
        if not isinstance(doc, dict):
            return
        for body in doc.values():
            if not isinstance(body, dict):
                continue
            name = body.get("command")
            sequence = body.get("sequence_id")
            if not isinstance(name, str) or sequence is None:
                continue
            result = str(body.get("result") or "").strip().lower()
            if result in _SUCCESS:
                state = APPLIED
            elif result in _FAILURE:
                state = REJECTED
            else:
                continue
            with self._lock:
                entry = self._pending.pop((name, str(sequence)), None)
                if entry is None:
                    continue
                command_id, on_reply, _at = entry
                # A command published as several messages settles on its first reply.
                for key in [k for k, v in self._pending.items() if v[0] == command_id]:
                    self._pending.pop(key, None)
            reason = body.get("reason")
            reason = str(reason).strip()[:200] if reason not in (None, "") else None
            if state == APPLIED and reason and reason.lower() in _SUCCESS:
                reason = None
            try:
                on_reply(command_id, state, reason, body)
            except Exception:
                pass

    def _expire(self, now: float) -> None:
        stale = [key for key, value in self._pending.items() if now - value[2] > self._ttl]
        for key in stale:
            self._pending.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)
