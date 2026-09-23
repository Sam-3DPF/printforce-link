"""Per-printer report state, plus `print.net.info` address parsing.

``PrinterState`` is the object the MQTT thread updates. The report loop reads
``view()``, a copy, and does not share the merged dict with that thread.

``net_info_ips`` decodes interface addresses. Each one is a little-endian
uint32. ``0`` means that interface has no address. These are candidates only:
the fleet still proves the serial before it dials one.
"""
import copy
import ipaddress
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..ams import load_remembered_ams, merge_ams
from ..coerce import clean_str

logger = logging.getLogger(__name__)

# User-cancel on a P1S often lands as FAILED plus one of these, not IDLE.
# 50348044 is print.print_error. The code is gone again in about two seconds,
# so the rising edge has to be latched on the merged payload.
_CANCEL_PRINT_ERRORS = frozenset({"50348044", "0300400C"})
# Raw firmware labels/codes cross the bridge boundary only in this bounded form.
_MAX_FIRMWARE_TEXT = 64
# Unacked lifecycle events and recent submission ids.
# Older ones drop so a printer that never gets a POST ack cannot grow forever.
_MAX_LIFECYCLE_EVENTS = 50
_MAX_REMEMBERED_SUBMISSIONS = 50
_PREPARE_FAIL_STATES = frozenset({"PREPARE", "SLICING"})


def _norm_error_code(value) -> str:
    if value is None:
        return ""
    return (
        str(value).strip().upper().replace("0X", "").replace("_", "").replace("-", "")
    )[:_MAX_FIRMWARE_TEXT]


def merge_status_payload(cached: Optional[dict], incoming: Optional[dict]) -> Dict:
    """Merge a (possibly partial) Bambu MQTT payload into the last-known one.

    Most Bambu reports are partial deltas — only a `pushall` carries the whole object —
    so without this, a poll that lands between deltas blanks the temperatures and the
    ETA.

    The merge is deliberately **shallow at the `print` level**:

      * scalars merge key-by-key, so a delta that omits `nozzle_temper` keeps the last
        known value rather than blanking it;
      * `ams` is merged by `merge_ams`: a P1 print delta that only details the
        active tray must not blank RFID colours on trays `tray_exist_bits` still
        marks loaded. A real unload (bit cleared, or no bits and an id-only tray)
        still replaces.

    Nothing from `incoming` is ever stored by reference. The report callback runs
    on the MQTT thread, which can keep the dict it just handed us, so caching it
    without copying would alias it and "last known" would silently become
    "current". `cached` needs only a shallow copy: it is a previous return value of
    this function, so everything reachable from it is already a bridge-owned copy
    that nothing mutates in place.
    """
    merged = dict(cached) if isinstance(cached, dict) else {}
    if not isinstance(incoming, dict):
        return merged

    for key, value in incoming.items():
        if key == "print" and isinstance(value, dict):
            previous = merged.get("print")
            print_obj = dict(previous) if isinstance(previous, dict) else {}
            incoming_print = copy.deepcopy(value)
            if "ams" in incoming_print:
                previous_ams = previous.get("ams") if isinstance(previous, dict) else None
                incoming_print["ams"] = merge_ams(previous_ams, incoming_print.get("ams"))
            print_obj.update(incoming_print)
            merged["print"] = print_obj             # rebuilt, so cached["print"] is untouched
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _iso_utc(epoch: float) -> str:
    text = datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _id_token(value) -> Optional[str]:
    """String form of a Bambu id. Zero and blank are the empty sentinel."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value == 0:
            return None
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text == "0":
            return None
        return text
    return None


def _gcode_state(print_obj: dict) -> str:
    state = print_obj.get("gcode_state")
    if not isinstance(state, str):
        return ""
    return state.strip().upper()


def _has_file(print_obj: dict) -> bool:
    return bool(clean_str(print_obj.get("gcode_file")) or clean_str(print_obj.get("subtask_name")))


def _print_identity(print_obj: dict) -> Optional[str]:
    """One print's key. A non-zero subtask or task id wins over the file name.

    With no such id, the file (then the subtask name) is the print. A change
    of that key while RUNNING is a new print. The same id with a different
    file string is still the same print: Link cannot watch the P1S request
    topic, so the id is the only stable ownership key.
    """
    sub = _id_token(print_obj.get("subtask_id"))
    if sub:
        return "sub:" + sub
    task = _id_token(print_obj.get("task_id"))
    if task:
        return "task:" + task
    gcode = clean_str(print_obj.get("gcode_file"))
    if gcode:
        return "file:" + gcode
    name = clean_str(print_obj.get("subtask_name"))
    if name:
        return "name:" + name
    return None


class LifecycleTracker:
    """Print edges for one printer, updated under ``PrinterState``'s lock.

    One print is one cycle. It opens on ``print_started`` — RUNNING with a
    file after a known non-RUNNING state in this session — or silently on the
    first RUNNING of a session, which is a print already under way. A
    different print identity while RUNNING opens a new cycle with its own
    start. PAUSE and back is the same cycle. The cycle closes on exactly one
    terminal: FINISH or FAILED after RUNNING was seen this session, FAILED
    straight from PREPARE or SLICING, or IDLE straight from RUNNING
    (``print_cancelled``; a FAILED that carries the user-cancel latch is a
    cancel too). A first push has no previous state, so a plate already
    standing at FINISH does not emit, and a closed cycle cannot close twice.

    Queued events survive ``new_session``: a new CONNACK is not an ack.
    ``origin`` is ``link`` only when ``subtask_id`` or ``task_id`` equals a
    submission id ``register_submission`` was given.
    """

    def __init__(self, bambu_id: str, wall_clock=None):
        self._bambu_id = bambu_id
        self._wall_clock = wall_clock or time.time
        self._events: List[dict] = []
        self._submissions: Dict[str, None] = {}
        self._prev_state: Optional[str] = None
        self._active_identity: Optional[str] = None
        self._seen_running = False
        self._print_origin: Optional[str] = None

    def new_session(self) -> None:
        """Forget this session's previous state. Keep queued events and the open cycle.

        The next report is a first push: a standing FINISH is not an edge,
        and a standing RUNNING of the open print is not a new start.
        """
        self._prev_state = None
        self._seen_running = False
        self._print_origin = None

    def register_submission(self, submission_id) -> None:
        token = _id_token(submission_id)
        if token is None:
            return
        self._submissions.pop(token, None)
        self._submissions[token] = None
        while len(self._submissions) > _MAX_REMEMBERED_SUBMISSIONS:
            self._submissions.pop(next(iter(self._submissions)))

    def ack(self, ids) -> None:
        """Drop exactly these event ids. Unknown ids are ignored."""
        if not ids:
            return
        wanted = {item for item in ids if isinstance(item, str) and item}
        if not wanted:
            return
        self._events = [event for event in self._events if event.get("id") not in wanted]

    def copy_events(self) -> List[dict]:
        return copy.deepcopy(self._events)

    @property
    def print_origin(self) -> Optional[str]:
        return self._print_origin

    def observe(self, payload, *, user_cancelled: bool = False) -> None:
        """One merged payload. Caller holds the state lock."""
        print_obj = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(print_obj, dict):
            return
        state = _gcode_state(print_obj)
        if not state:
            return
        identity = _print_identity(print_obj)
        origin, submission_id = self._classify(print_obj)
        self._print_origin = None if state == "IDLE" else origin
        prev = self._prev_state

        if state == "RUNNING" and _has_file(print_obj) and identity:
            if self._active_identity is None:
                if prev is not None and prev != "RUNNING":
                    self._enqueue("print_started", origin, submission_id, print_obj)
                self._active_identity = identity
            elif identity != self._active_identity:
                self._enqueue("print_started", origin, submission_id, print_obj)
                self._active_identity = identity

        terminal = None
        if state == "FINISH" and (prev == "RUNNING" or self._seen_running):
            terminal = "print_finished"
        elif state == "FAILED" and (
            prev in _PREPARE_FAIL_STATES or prev == "RUNNING" or self._seen_running
        ):
            terminal = "print_cancelled" if user_cancelled else "print_failed"
        elif state == "IDLE" and prev == "RUNNING":
            terminal = "print_cancelled"
        if terminal is not None:
            self._enqueue(terminal, origin, submission_id, print_obj)
            self._active_identity = None
            self._seen_running = False
        elif state == "RUNNING":
            self._seen_running = True
        elif state in ("IDLE", "FINISH", "FAILED"):
            # A standing terminal or idle machine has no print open.
            self._active_identity = None

        self._prev_state = state

    def _classify(self, print_obj: dict):
        for key in ("subtask_id", "task_id"):
            token = _id_token(print_obj.get(key))
            if token and token in self._submissions:
                return "link", token
        return "external", None

    def _enqueue(self, kind, origin, submission_id, print_obj) -> None:
        if len(self._events) >= _MAX_LIFECYCLE_EVENTS:
            dropped = self._events.pop(0)
            logger.warning(
                "printer %s: lifecycle queue full; dropping oldest event %s",
                self._bambu_id, dropped.get("id"),
            )
        self._events.append({
            "id": uuid.uuid4().hex,
            "type": kind,
            "submission_id": submission_id,
            "origin": origin,
            "gcode_file": clean_str(print_obj.get("gcode_file")),
            "subtask_name": clean_str(print_obj.get("subtask_name")),
            "at": _iso_utc(self._wall_clock()),
            "observed": True,
        })


class PrinterState:
    """Merged report for one printer. ``ingest`` runs on the paho thread.

    The lock is this object's. ``view`` hands the report loop a deep copy so it
    can build a snapshot without holding the MQTT thread's dict. ``clear`` is
    ``reconnect``: an address change must not keep the previous printer's
    payload. An offline *report* does not call ``clear`` — the next message
    still merges onto what the printer already said.
    """

    def __init__(self, bambu_id: str, ams_cache_path: Optional[str] = None,
                 monotonic=None, wall_clock=None):
        self._bambu_id = bambu_id
        self._ams_cache_path = ams_cache_path
        self._monotonic = monotonic or time.monotonic
        self._lifecycle = LifecycleTracker(bambu_id, wall_clock=wall_clock)
        self._lock = threading.Lock()
        self._payload: Optional[Dict] = None
        self._pending_fresh = False
        # ``_last_raw`` / ``_last_fresh_monotonic`` are the freshness baseline.
        # They survive an offline report. A dead printer keeps handing back the
        # same dict; resetting the baseline would make that dict look new.
        self._last_raw: Optional[Dict] = None
        self._last_fresh_monotonic: Optional[float] = None
        # Any report, even one whose merged dump did not change. A finished
        # plate often republishes the same temperatures; that is still a heartbeat.
        self._last_message_monotonic: Optional[float] = None
        self._user_cancelled = False
        self._last_print_error = ""
        self._net_info_ips: List[str] = []

    def ingest(self, doc, now=None) -> bool:
        """Merge one MQTT document. Returns whether the raw payload changed.

        ``now`` is the caller's monotonic clock. The report age and the
        freshness baseline have to share it, or a test clock and the default
        clock disagree about how long the printer has been quiet.
        """
        if not isinstance(doc, dict):
            return False
        if now is None:
            now = self._monotonic()
        with self._lock:
            self._remember_net_info(doc)
            self._last_message_monotonic = now
            if not doc:
                return False
            if self._payload is None:
                self._seed_remembered_ams()
            self._payload = merge_status_payload(self._payload, doc)
            self._note_cancel_edge(self._payload)
            self._lifecycle.observe(self._payload, user_cancelled=self._user_cancelled)
            fresh = self._note_freshness(doc, now)
            if fresh:
                self._pending_fresh = True
            return fresh

    def view(self) -> Dict:
        """Deep-copied payload plus the stamps the report loop is allowed to read."""
        with self._lock:
            return {
                "payload": copy.deepcopy(self._payload) if self._payload is not None else None,
                "last_raw": copy.deepcopy(self._last_raw) if self._last_raw is not None else None,
                "last_fresh_monotonic": self._last_fresh_monotonic,
                "last_message_monotonic": self._last_message_monotonic,
                "pending_fresh": self._pending_fresh,
                "user_cancelled": self._user_cancelled,
                "net_info_ips": list(self._net_info_ips),
                "events": self._lifecycle.copy_events(),
                "print_origin": self._lifecycle.print_origin,
            }

    def clear(self) -> None:
        """Drop the merged payload and its freshness baseline.

        The cancel latch, the lifecycle queue, and the last ``net.info`` list
        stay. ``reconnect`` did not clear the latch: it is about the print,
        and a missing net block is not evidence the interfaces are gone.
        Dropping the payload is not an ack of events the report POST has not
        accepted. ``new_session`` is what forgets the previous gcode state.
        """
        with self._lock:
            self._payload = None
            self._pending_fresh = False
            self._last_raw = None
            self._last_fresh_monotonic = None
            self._last_message_monotonic = None

    def take_fresh(self) -> bool:
        """Consume the 'payload changed' edge. Only a live snapshot counts it."""
        with self._lock:
            fresh = self._pending_fresh
            self._pending_fresh = False
            return fresh

    def discard_fresh(self) -> None:
        """A non-live report must not leave the edge for the next live one.

        Otherwise the first snapshot after a gap would treat the pre-gap
        change as a new observation.
        """
        with self._lock:
            self._pending_fresh = False

    def address_candidates(self) -> List[str]:
        with self._lock:
            return list(self._net_info_ips)

    def new_session(self) -> None:
        """A new CONNACK. Previous-state knowledge resets; queued events stay."""
        with self._lock:
            self._lifecycle.new_session()

    def register_submission(self, submission_id) -> None:
        """Remember a submission id Link sent to this printer.

        A later report whose ``subtask_id`` or ``task_id`` equals it is
        ``origin: link``. Nothing in the send path calls this until that
        path exists; startup registers ids already stored on assignments.
        """
        with self._lock:
            self._lifecycle.register_submission(submission_id)

    def ack_events(self, ids) -> None:
        """Drop lifecycle events whose report POST was accepted."""
        with self._lock:
            self._lifecycle.ack(ids)

    def pending_events(self) -> List[dict]:
        with self._lock:
            return self._lifecycle.copy_events()

    def stopwatch_sample(self):
        """Merged ``(gcode_state, gcode_start_time)`` for the print stopwatch.

        None when the merged payload has no ``gcode_state``. Observing a
        missing state would clear a clock the printer did not actually leave.
        """
        with self._lock:
            payload = self._payload if isinstance(self._payload, dict) else None
            print_obj = payload.get("print") if isinstance(payload, dict) else None
            if not isinstance(print_obj, dict) or "gcode_state" not in print_obj:
                return None
            return print_obj.get("gcode_state"), print_obj.get("gcode_start_time")

    def _remember_net_info(self, doc) -> None:
        """Keep the last explicit interface list. Absence is not an empty list.

        Caller holds ``self._lock``. Only a real list replaces the last
        interfaces. A missing or broken block is not evidence that the printer
        has no address.
        """
        print_obj = doc.get("print")
        if not isinstance(print_obj, dict):
            return
        net = print_obj.get("net")
        info = net.get("info") if isinstance(net, dict) else None
        if not isinstance(info, list):
            return
        self._net_info_ips = net_info_ips(doc)

    def _seed_remembered_ams(self) -> None:
        """Caller holds ``self._lock``. Only when nothing has been merged yet."""
        if self._payload is not None:
            return
        remembered = load_remembered_ams(self._ams_cache_path, self._bambu_id)
        if remembered:
            self._payload = {"print": {"ams": remembered}}

    def _note_cancel_edge(self, payload) -> None:
        """Latch user-cancel when print_error rises to 50348044.

        The code lasts about two seconds. A later FAILED dump can already have
        print_error 0. Without the latch that looks like a real fail. A
        running print clears it: a leftover code must not hide a live job.
        Caller holds ``self._lock``.
        """
        print_obj = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(print_obj, dict):
            return
        state = print_obj.get("gcode_state")
        gcode = state.strip().upper() if isinstance(state, str) else ""
        if gcode in {"PREPARE", "SLICING", "RUNNING"}:
            self._user_cancelled = False
            self._last_print_error = ""
        current = _norm_error_code(print_obj.get("print_error"))
        previous = self._last_print_error
        if current in _CANCEL_PRINT_ERRORS and previous not in _CANCEL_PRINT_ERRORS:
            self._user_cancelled = True
        self._last_print_error = current

    def _note_freshness(self, raw, now: float) -> bool:
        """Stamp ``now`` when the printer says something new.

        Keyed on the raw payload changing. The comparison stores a copy: the
        MQTT thread can mutate the dict it handed us, and a stored reference
        would compare equal to itself forever. Caller holds ``self._lock``.
        """
        if not isinstance(raw, dict) or not raw:
            return False
        if raw == self._last_raw:
            return False
        self._last_raw = copy.deepcopy(raw)
        self._last_fresh_monotonic = now
        return True


def net_info_ips(doc) -> List[str]:
    """Every usable IPv4 in ``doc["print"]["net"]["info"]``, in order.

    Missing, zero, and non-uint32 entries are ignored. Loopback, multicast,
    and reserved addresses are ignored too: they are not a LAN interface the
    bridge can dial. The same address twice is returned once.
    """
    info = _info_list(doc)
    if info is None:
        return []
    found: List[str] = []
    for entry in info:
        ip = _entry_ip(entry)
        if ip is None or ip in found:
            continue
        found.append(ip)
    return found


def _info_list(doc):
    if not isinstance(doc, dict):
        return None
    print_obj = doc.get("print")
    if not isinstance(print_obj, dict):
        return None
    net = print_obj.get("net")
    if not isinstance(net, dict):
        return None
    info = net.get("info")
    if not isinstance(info, list):
        return None
    return info


def _entry_ip(entry) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    return _ipv4_from_le(entry.get("ip"))


def _ipv4_from_le(value) -> Optional[str]:
    # bool is an int. True would decode as 1.0.0.0, which the printer did not send.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > 0xFFFFFFFF:
        return None
    try:
        addr = ipaddress.IPv4Address(value.to_bytes(4, "little"))
    except (ipaddress.AddressValueError, OverflowError):
        return None
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return None
    return str(addr)
