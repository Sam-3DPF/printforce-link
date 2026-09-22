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
                 client_factory=None, on_report=None, monotonic=None):
        self.host = host
        self.access_code = access_code
        self.serial = serial
        self._on_report = on_report
        self._client_factory = client_factory or build_paho_client
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._client = None
        self._client_id = None
        self._connected = False
        self._last_message_at = None
        self._last_connect_error = None
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

    def start(self) -> None:
        """Begin connecting. Returns as soon as the network loop is running."""
        self._open()

    def hard_reset(self) -> None:
        """Drop this client — and the QoS 1 queue paho is holding for it — and connect another."""
        self.disconnect()
        self._open()

    def disconnect(self) -> None:
        """Best-effort close. Returns within a couple of seconds and never raises."""
        try:
            self._stop()
        except Exception as exc:
            logger.debug("printer %s: MQTT disconnect raised (%s)", self.serial, type(exc).__name__)

    def publish(self, payload: dict) -> bool:
        """Publish one command at QoS 1. False when the session is down or paho rejects it.

        Does not wait for the PUBACK. A half-open socket must not stall the caller.
        """
        with self._lock:
            client = self._client
            connected = self._connected
        if not connected or client is None:
            return False
        try:
            info = client.publish(self._request_topic, json.dumps(payload), qos=_QOS)
        except Exception:
            return False
        return _publish_accepted(info)

    def _open(self) -> None:
        client_id = _next_client_id(self.serial)
        client = self._client_factory(client_id)
        self._configure(client)
        with self._lock:
            self._client = client
            self._client_id = client_id
            self._connected = False
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
            try:
                client.subscribe(self._report_topic, qos=_QOS)
            except Exception:
                logger.exception("printer %s: subscribe failed", self.serial)
            self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
            self.publish({"info": {"sequence_id": "0", "command": "get_version"}})
            return
        with self._lock:
            self._connected = False
            self._last_connect_error = (
                "auth_rejected" if code in _AUTH_REJECTED else "refused"
            )
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
        self._last_message_at = self._monotonic()
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
        if code == 0 and self._report_is_recent():
            return
        with self._lock:
            if self._client is client:
                self._connected = False

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
