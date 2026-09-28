"""Durable lifecycle event outbox.

A print starting, pausing, resuming, finishing, failing, or being cancelled is a
fact that must reach 3D PrintForce exactly once, even if Link restarts or the
cloud is down for a while. Every event is written to disk before it is sent,
carries a ``seq`` that only goes up for this Link install, and leaves the disk
only when the cloud says it has applied it.

File: ``events.json`` next to ``config.toml``::

    {"next_seq": 42, "events": [{"id", "seq", "bambu_id", "type", ...}, ...]}

Written atomically (temp file, fsync, rename). A corrupt file starts empty so
Link still starts; that is logged loudly because unsent events are lost.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# A long cloud outage must not fill the disk. Far more than a farm produces
# in a day of prints; the oldest events drop first, with a warning.
MAX_EVENTS = 2000


class EventOutbox:
    def __init__(self, path: Optional[str]):
        self._path = path
        self._lock = threading.Lock()
        self._events: List[Dict] = []
        self._next_seq = 1
        self._load()

    # --- write side -------------------------------------------------------

    def append(self, bambu_id: str, event: Dict) -> Dict:
        """Store one event for ``bambu_id``. Returns the stored copy with ``seq``."""
        with self._lock:
            stored = dict(event)
            stored["bambu_id"] = bambu_id
            stored["seq"] = self._next_seq
            self._next_seq += 1
            self._events.append(stored)
            while len(self._events) > MAX_EVENTS:
                dropped = self._events.pop(0)
                logger.warning(
                    "event outbox full; dropping oldest unsent event %s (%s on %s)",
                    dropped.get("id"), dropped.get("type"), dropped.get("bambu_id"),
                )
            self._save()
            return copy.deepcopy(stored)

    def ack(self, ids: Iterable[str]) -> int:
        """Drop exactly these event ids. Returns how many were dropped."""
        wanted = {item for item in (ids or []) if isinstance(item, str) and item}
        if not wanted:
            return 0
        with self._lock:
            before = len(self._events)
            self._events = [e for e in self._events if e.get("id") not in wanted]
            dropped = before - len(self._events)
            if dropped:
                self._save()
            return dropped

    # --- read side --------------------------------------------------------

    def pending(self, bambu_id: Optional[str] = None) -> List[Dict]:
        """Unacked events, oldest first, optionally for one printer."""
        with self._lock:
            events = [
                e for e in self._events
                if bambu_id is None or e.get("bambu_id") == bambu_id
            ]
            return copy.deepcopy(events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    # --- disk -------------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.error("event outbox %s is unreadable (%s); starting empty — "
                         "unsent print events in it are lost", self._path, type(exc).__name__)
            return
        events = data.get("events") if isinstance(data, dict) else None
        if isinstance(events, list):
            self._events = [
                e for e in events
                if isinstance(e, dict) and isinstance(e.get("id"), str)
                and isinstance(e.get("seq"), int) and isinstance(e.get("bambu_id"), str)
            ]
        highest = max((e["seq"] for e in self._events), default=0)
        stored_next = data.get("next_seq") if isinstance(data, dict) else None
        self._next_seq = max(highest + 1, stored_next if isinstance(stored_next, int) else 1)
        if self._events:
            logger.info("event outbox: %d unsent print event(s) restored", len(self._events))

    def _save(self) -> None:
        """Caller holds the lock."""
        if not self._path:
            return
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        body = {"next_seq": self._next_seq, "events": self._events}
        try:
            fd, tmp = tempfile.mkstemp(prefix=".events-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(body, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.exception("event outbox: could not write %s", self._path)
