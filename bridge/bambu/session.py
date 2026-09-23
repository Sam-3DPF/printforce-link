"""One MQTT session to one Bambu printer.

The printer is the broker. It speaks MQTT 3.1.1 on 8883 with a self-signed
certificate, username ``bblp``, and the LAN access code as the password.
P1S and A1 brokers drop the TCP connection if a client subscribes to
``device/{serial}/request``, so commands are published there and the only
subscription is ``device/{serial}/report``.

Publishing never waits for a PUBACK. Bambu's broker matches those acks
racy enough that paho's default inflight cap of 20 wedges the session
after a handful of commands; the cap is raised, and the caller thread
must not block on it. ``loop_stop`` joins the network thread with no
timeout, so teardown runs that join on a daemon helper and gives up.
"""

import itertools
import json
import logging
import os
import ssl
import threading
import time

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

_PORT = 8883
_KEEPALIVE_SECONDS = 30
_QOS = 1
_MAX_INFLIGHT = 1000
_RECONNECT_MIN_SECONDS = 1
_RECONNECT_MAX_SECONDS = 30
# paho's loop_stop() joins its network thread forever. Callers get a bound.
_DISCONNECT_TIMEOUT_SECONDS = 1.5
# A clean broker disconnect in the wake of a report is not the printer leaving.
_CLEAN_DISCONNECT_IGNORE_SECONDS = 10.0
_AUTH_REJECTED = frozenset({134, 135})
# Silence on a socket that is still up. The fleet backstop is the longer net.
_STALE_AFTER_SECONDS = 60.0
_RESET_COOLDOWN_SECONDS = 30.0
_PROBE_INTERVAL_SECONDS = 300.0
_PROBE_TIMEOUT_SECONDS = 10.0
_AUTH_RETRY_SECONDS = 300.0
_UNREACHABLE_AFTER_SECONDS = 60.0
_WATCHDOG_INTERVAL_SECONDS = 5.0
# Enable only after a captured shop P1S get_version reply is pinned in a test.
COMMAND_PROBE_ENABLED = False

_client_ids = itertools.count(1)
_client_id_lock = threading.Lock()


def build_paho_client(client_id: str) -> mqtt.Client:
    """A fresh MQTT 3.1.1 client. Each connect gets its own id and a clean session."""
    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )


def _printer_tls_context() -> ssl.SSLContext:
    """TLS 1.2 floor, no certificate check. The printer's cert is self-signed."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _next_client_id(serial: str) -> str:
    with _client_id_lock:
        n = next(_client_ids)
    return f"link-{serial}-{os.getpid()}-{n}"


def _reason_value(reason):
    """Numeric CONNACK / DISCONNECT code. paho 2.x hands back a ReasonCode, not the v3 rc."""
    if isinstance(reason, bool) or reason is None:
        return None
    if isinstance(reason, int):
        return int(reason)
    value = getattr(reason, "value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    try:
        return int(reason)
    except (TypeError, ValueError):
        return None


def _publish_accepted(info) -> bool:
    rc = getattr(info, "rc", None)
    if rc is None and isinstance(info, tuple) and info:
        rc = info[0]
    try:
        return int(rc) == 0
    except (TypeError, ValueError):
        return False


class LinkSession:
    """The MQTT connection for one printer. The paho client is built by a factory
    so tests can stand in for the network without opening a socket.
    """

    def __init__(self, host: str, access_code: str, serial: str, *,
                 client_factory=None, on_report=None, monotonic=None,
                 command_probe=None, watchdog_interval=_WATCHDOG_INTERVAL_SECONDS,
                 log=None):
        self.host = host
        self.access_code = access_code
        self.serial = serial
        self._on_report = on_report
        self._client_factory = client_factory or build_paho_client
        self._monotonic = monotonic or time.monotonic
        self._command_probe = (
            COMMAND_PROBE_ENABLED if command_probe is None else bool(command_probe)
        )
        # None keeps the thread off so a test can call tick() on its own clock.
        self._watchdog_interval = watchdog_interval
        # The printer owns the ring. A reset builds a new client, not a new log.
        self._log = log
        self._lock = threading.Lock()
        self._reset_lock = threading.Lock()
        self._client = None
        self._client_id = None
        self._connected = False
        self._last_message_at = None
        self._last_connect_error = None
        self._state = "offline"
        self._down_reason = None
        self._had_session = False
        self._connack_at = None
        self._socket_down_since = None
        self._last_reset_at = None
        self._probe_seq = 1
        self._outstanding = None
        self._last_probe_seq = None
        self._probe_misses = 0
        self._next_probe_at = None
        self._auth_retry_not_before = None
        self._user_stopped = False
        self._watchdog = None
        self._watchdog_stop = threading.Event()
        self._report_topic = f"device/{serial}/report"
        self._request_topic = f"device/{serial}/request"

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self):
        """Monotonic time of the last parsed report, or None."""
        return self._last_message_at

    @property
    def last_connect_error(self):
        """``auth_rejected``, ``refused``, or None after a successful CONNACK."""
        return self._last_connect_error

    @property
    def client_id(self):
        return self._client_id

    @property
    def state(self) -> str:
        """``connecting``, ``live``, ``stale``, ``commands_ignored``, or ``offline``."""
        return self._state

    @property
    def down_reason(self):
        """Why this session is not live, or None while it is connecting or live.

        One of ``auth_rejected``, ``refused``, ``unreachable``, ``silent_session``,
        ``commands_ignored``.
        """
        return self._down_reason

    @property
    def had_session(self) -> bool:
        """True once any client has received a successful CONNACK. Survives a reset."""
        return self._had_session

    def silent_for(self, now=None):
        """Seconds since the last report, or since CONNACK if no report has arrived.

        None when this session has never completed a handshake. ``now`` is the
        caller's monotonic clock so the fleet backstop can share its own.
        """
        if now is None:
            now = self._monotonic()
        anchor = self._silence_anchor()
        if anchor is None:
            return None
        return now - anchor

    def _silence_anchor(self):
        """The later of the last report and the last CONNACK.

        A reset keeps the old report time. Measuring from it alone would call a
        client that connected a second ago stale.
        """
        stamps = [t for t in (self._last_message_at, self._connack_at) if t is not None]
        return max(stamps) if stamps else None

    def set_log(self, log) -> None:
        """Attach the printer-owned ring. Reconnect must not start a second one."""
        self._log = log

    def _record_event(self, kind, **fields) -> None:
        log = self._log
        if log is None:
            return
        try:
            log.record_event(kind, **fields)
        except Exception:
            logger.debug("printer %s: session event was not recorded", self.serial)

    def _record_message(self, direction, topic, payload, *, accepted=None) -> None:
        log = self._log
        if log is None:
            return
        try:
            if accepted is None:
                log.record_message(direction, topic, payload)
            else:
                log.record_message(direction, topic, payload, accepted=accepted)
        except Exception:
            logger.debug("printer %s: session message was not recorded", self.serial)

    def start(self) -> None:
        """Begin connecting. Returns as soon as the network loop is running."""
        self._user_stopped = False
        self._state = "connecting"
        self._open()
        self._start_watchdog()

    def hard_reset(self, *, keep_reason=None) -> None:
        """Drop this client — and the QoS 1 queue paho is holding for it — and connect another.

        The watchdog keeps running. ``disconnect`` is what stops it. A reset from
        the watchdog must not join that thread.
        """
        if self._user_stopped:
            return
        if not self._reset_lock.acquire(blocking=False):
            return
        try:
            if self._user_stopped:
                return
            preserve_auth = keep_reason == "auth_rejected"
            self._record_event("reset", reason=keep_reason)
            self._stop()
            self._last_reset_at = self._monotonic()
            self._state = "connecting"
            self._outstanding = None
            self._probe_misses = 0
            self._next_probe_at = None
            self._down_reason = keep_reason
            if not preserve_auth:
                self._last_connect_error = None
            self._open()
        finally:
            self._reset_lock.release()

    def disconnect(self) -> None:
        """Best-effort close. Returns within a couple of seconds and never raises."""
        self._user_stopped = True
        self._stop_watchdog()
        try:
            self._stop()
        except Exception as exc:
            logger.debug("printer %s: MQTT disconnect raised (%s)", self.serial, type(exc).__name__)

    def tick(self) -> None:
        """One liveness pass. The watchdog calls this; it must not run on the paho thread.

        A reset joins the network thread, so calling ``tick`` from a report callback
        deadlocks that join.
        """
        if self._user_stopped:
            return
        now = self._monotonic()
        if self._last_connect_error == "auth_rejected":
            self._state = "connecting"
            self._down_reason = "auth_rejected"
            if self._auth_retry_not_before is None:
                self._auth_retry_not_before = now + _AUTH_RETRY_SECONDS
            if now >= self._auth_retry_not_before and self._reset_allowed(now):
                self._auth_retry_not_before = now + _AUTH_RETRY_SECONDS
                self._record_event("auth_retry")
                self.hard_reset(keep_reason="auth_rejected")
            return

        if not self._connected:
            if self._last_connect_error == "refused":
                self._state = "connecting"
                self._down_reason = "refused"
                return
            down_since = self._socket_down_since
            if down_since is not None and now - down_since > _UNREACHABLE_AFTER_SECONDS:
                self._state = "offline"
                self._down_reason = "unreachable"
            return

        self._tick_probe(now)
        if self._state == "commands_ignored":
            # Two misses inside the cooldown still owe a reset once it passes.
            if self._reset_allowed(now):
                self.hard_reset(keep_reason="commands_ignored")
            return
        if not self._connected:
            return

        anchor = self._silence_anchor()
        if anchor is not None and now - anchor > _STALE_AFTER_SECONDS:
            if self._state != "stale":
                self._record_event("stale")
            self._state = "stale"
            self._down_reason = "silent_session"
            if self._reset_allowed(now):
                self.hard_reset(keep_reason="silent_session")

    def probe(self):
        """Send one ``get_version`` probe. The sequence id, or None if it was not published.

        Disabled until ``command_probe`` is on. A later send path can call this
        immediately before a command; the watchdog also sends it on the 5-minute timer.
        """
        if not self._command_probe:
            return None
        seq = str(self._probe_seq)
        self._probe_seq += 1
        sent = self.publish({"info": {"sequence_id": seq, "command": "get_version"}})
        if not sent:
            return None
        now = self._monotonic()
        self._outstanding = (seq, now)
        self._last_probe_seq = seq
        self._next_probe_at = now + _PROBE_INTERVAL_SECONDS
        self._record_event("probe_sent", sequence_id=seq)
        return seq

    def publish(self, payload: dict) -> bool:
        """Publish one command at QoS 1. False when the session is down or paho rejects it.

        Does not wait for the PUBACK. A half-open socket must not stall the caller.
        """
        with self._lock:
            client = self._client
            connected = self._connected
        if not connected or client is None:
            self._record_message("out", self._request_topic, payload, accepted=False)
            return False
        try:
            info = client.publish(self._request_topic, json.dumps(payload), qos=_QOS)
        except Exception:
            self._record_message("out", self._request_topic, payload, accepted=False)
            return False
        accepted = _publish_accepted(info)
        self._record_message("out", self._request_topic, payload, accepted=accepted)
        return accepted

    def _open(self) -> None:
        client_id = _next_client_id(self.serial)
        client = self._client_factory(client_id)
        self._configure(client)
        with self._lock:
            self._client = client
            self._client_id = client_id
            self._connected = False
            self._socket_down_since = self._monotonic()
        self._record_event("connect", host=self.host, client_id=client_id)
        try:
            client.connect_async(self.host, _PORT, keepalive=_KEEPALIVE_SECONDS)
            client.loop_start()
        except Exception:
            with self._lock:
                if self._client is client:
                    self._client = None
            raise

    def _configure(self, client) -> None:
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(_printer_tls_context())
        client.tls_insecure_set(True)
        client.max_inflight_messages_set(_MAX_INFLIGHT)
        client.reconnect_delay_set(_RECONNECT_MIN_SECONDS, _RECONNECT_MAX_SECONDS)

    def _stop(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._connected = False
        if client is None:
            return

        def _join_network_thread():
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass

        worker = threading.Thread(
            target=_join_network_thread,
            name=f"link-mqtt-stop-{self.serial}",
            daemon=True,
        )
        worker.start()
        worker.join(_DISCONNECT_TIMEOUT_SECONDS)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if client is not self._client:
            return
        code = _reason_value(reason_code)
        if code == 0:
            with self._lock:
                self._connected = True
                self._last_connect_error = None
                self._had_session = True
                self._connack_at = self._monotonic()
                self._socket_down_since = None
                self._auth_retry_not_before = None
            self._state = "connecting"
            self._down_reason = None
            self._record_event("connack", result="ok", code=code)
            try:
                client.subscribe(self._report_topic, qos=_QOS)
            except Exception:
                logger.exception("printer %s: subscribe failed", self.serial)
            self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
            self.publish({"info": {"sequence_id": "0", "command": "get_version"}})
            return
        rejected = code in _AUTH_REJECTED
        reason = "auth_rejected" if rejected else "refused"
        with self._lock:
            self._connected = False
            self._last_connect_error = reason
        self._down_reason = self._last_connect_error
        self._record_event("connack", result=reason, code=code)
        self._state = "connecting"
        if rejected:
            # paho would redial in 1–30s. An off printer refuses this way too,
            # so the retry stays slow instead of giving up or hammering.
            self._auth_retry_not_before = self._monotonic() + _AUTH_RETRY_SECONDS
            self._hold_paho_retry(client)
        logger.warning(
            "printer %s: MQTT connection refused (%s, code %s)",
            self.serial, self._last_connect_error, code,
        )

    def _on_message(self, client, userdata, msg):
        if client is not self._client:
            return
        topic = getattr(msg, "topic", None)
        if isinstance(topic, bytes):
            topic = topic.decode("utf-8", "replace")
        if topic and topic != self._report_topic:
            return
        doc = _parse_report(getattr(msg, "payload", None))
        if doc is None:
            return
        self._record_message("in", topic or self._report_topic, doc)
        now = self._monotonic()
        self._last_message_at = now
        self._note_probe_reply(doc)
        self._mark_live(now)
        callback = self._on_report
        if callback is None:
            return
        try:
            callback(doc)
        except Exception:
            logger.exception("printer %s: report callback failed", self.serial)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if client is not self._client:
            return
        code = _reason_value(reason_code)
        ignored = code == 0 and self._report_is_recent()
        self._record_event("disconnect", code=code, ignored=bool(ignored))
        if ignored:
            return
        with self._lock:
            if self._client is client:
                self._connected = False
                if self._socket_down_since is None:
                    self._socket_down_since = self._monotonic()

    def _start_watchdog(self) -> None:
        if self._watchdog_interval is None or self._user_stopped:
            return
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name=f"link-watchdog-{self.serial}",
            daemon=True,
        )
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(0.2)

    def _watchdog_loop(self) -> None:
        interval = self._watchdog_interval
        while not self._watchdog_stop.wait(interval):
            if self._user_stopped:
                return
            try:
                self.tick()
            except Exception:
                logger.exception("printer %s: liveness tick failed", self.serial)

    def _reset_allowed(self, now: float) -> bool:
        last = self._last_reset_at
        return last is None or now - last >= _RESET_COOLDOWN_SECONDS

    def _hold_paho_retry(self, client) -> None:
        """Stop paho's 1–30s backoff without joining the network thread.

        ``on_connect`` runs on that thread, so ``loop_stop`` would deadlock here.
        ``disconnect`` moves the client to disconnecting, and paho's loop exits
        instead of redialing.
        """
        try:
            client.disconnect()
        except Exception:
            pass

    def _tick_probe(self, now: float) -> None:
        if not self._command_probe:
            return
        outstanding = self._outstanding
        if outstanding is not None:
            _seq, sent = outstanding
            if now - sent >= _PROBE_TIMEOUT_SECONDS:
                self._outstanding = None
                self._probe_misses += 1
                self._record_event("probe_miss", count=self._probe_misses)
                if self._probe_misses >= 2:
                    self._state = "commands_ignored"
                    self._down_reason = "commands_ignored"
                    if self._reset_allowed(now):
                        self.hard_reset(keep_reason="commands_ignored")
                    return
        if self._state != "live" or self._outstanding is not None or not self._connected:
            return
        if self._next_probe_at is None:
            self._next_probe_at = now + _PROBE_INTERVAL_SECONDS
            return
        if now >= self._next_probe_at and self.probe():
            self._next_probe_at = now + _PROBE_INTERVAL_SECONDS

    def _mark_live(self, now: float) -> None:
        self._state = "live"
        self._down_reason = None
        if self._command_probe and self._next_probe_at is None:
            self._next_probe_at = now + _PROBE_INTERVAL_SECONDS

    def _note_probe_reply(self, doc) -> None:
        info = doc.get("info") if isinstance(doc, dict) else None
        if not isinstance(info, dict) or info.get("command") != "get_version":
            return
        sequence = info.get("sequence_id") if "sequence_id" in info else None
        if sequence in (None, ""):
            self._outstanding = None
            self._probe_misses = 0
            self._record_event("probe_answered", sequence_id=None)
            return
        seq = str(sequence)
        outstanding = self._outstanding
        if outstanding is not None and seq == outstanding[0]:
            self._outstanding = None
            self._probe_misses = 0
            self._record_event("probe_answered", sequence_id=seq)
            return
        if seq == self._last_probe_seq:
            self._outstanding = None
            self._probe_misses = 0
            self._record_event("probe_answered", sequence_id=seq)

    def _report_is_recent(self) -> bool:
        last = self._last_message_at
        if last is None:
            return False
        return self._monotonic() - last <= _CLEAN_DISCONNECT_IGNORE_SECONDS


def _parse_report(payload):
    try:
        if isinstance(payload, (bytes, bytearray)):
            doc = json.loads(payload.decode("utf-8"))
        elif isinstance(payload, str):
            doc = json.loads(payload)
        elif isinstance(payload, dict):
            doc = payload
        else:
            return None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None
