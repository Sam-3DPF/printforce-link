"""Per-printer report state, plus `print.net.info` address parsing.

``PrinterState`` is the object the MQTT thread updates. The report loop reads
``view()``, a copy, and does not share the merged dict with that thread.

``net_info_ips`` decodes interface addresses. Each one is a little-endian
uint32. ``0`` means that interface has no address. These are candidates only:
the fleet still proves the serial before it dials one.
"""
import copy
import ipaddress
import threading
import time
from typing import Dict, List, Optional

from ..ams import load_remembered_ams, merge_ams

# User-cancel on a P1S often lands as FAILED plus one of these, not IDLE.
# 50348044 is print.print_error. The code is gone again in about two seconds,
# so the rising edge has to be latched on the merged payload.
_CANCEL_PRINT_ERRORS = frozenset({"50348044", "0300400C"})
# Raw firmware labels/codes cross the bridge boundary only in this bounded form.
_MAX_FIRMWARE_TEXT = 64


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


class PrinterState:
    """Merged report for one printer. ``ingest`` runs on the paho thread.

    The lock is this object's. ``view`` hands the report loop a deep copy so it
    can build a snapshot without holding the MQTT thread's dict. ``clear`` is
    ``reconnect``: an address change must not keep the previous printer's
    payload. An offline *report* does not call ``clear`` — the next message
    still merges onto what the printer already said.
    """

    def __init__(self, bambu_id: str, ams_cache_path: Optional[str] = None,
                 monotonic=None):
        self._bambu_id = bambu_id
        self._ams_cache_path = ams_cache_path
        self._monotonic = monotonic or time.monotonic
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
            }

    def clear(self) -> None:
        """Drop the merged payload and its freshness baseline.

        The cancel latch and the last ``net.info`` list stay. ``reconnect``
        did not clear those: the latch is about the print, and a missing net
        block is not evidence the interfaces are gone.
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
