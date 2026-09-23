"""One connection check for one printer.

The checks run in a fixed order. A closed port 8883 skips the MQTT checks;
FTPS and the subnet check still run. FTPS is a TLS handshake on 990 and
nothing after it — an FTP login here would be a second client beside the
uploader. MQTT uses a fresh temporary session so the printer's live client
keeps its subscription and its unacked commands.

The access code is the MQTT password. It is not copied into the result.
"""

import datetime
import logging
import socket
import ssl
import time

from .session import COMMAND_PROBE_ENABLED, LinkSession

logger = logging.getLogger(__name__)

_MQTT_PORT = 8883
_FTPS_PORT = 990
_CONNECT_TIMEOUT_SECONDS = 3.0
_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.05
# 10s / 0.05s is 200 polls. The cap is only for a clock that never moves:
# a stuck wait must not pin the printer worker.
_MAX_POLLS = 400

_MQTT_SKIPPED = (
    "Skipped because port 8883 did not accept a connection, so MQTT was not checked."
)
_AUTH_SKIPPED = "Skipped because the MQTT login did not succeed."


def _check(check_id, result, cause, **detail):
    return {"id": check_id, "result": result, "cause": cause, "detail": detail}


def _iso_now() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond:06d}Z"


def _record(printer, kind, **fields) -> None:
    if printer is None:
        return
    log = getattr(printer, "log", None)
    record = getattr(log, "record_event", None) if log is not None else None
    if not callable(record):
        return
    try:
        record(kind, **fields)
    except Exception:
        logger.debug("diagnostic event %s was not recorded", kind)


def _overall(checks) -> str:
    results = [item["result"] for item in checks]
    if "fail" in results:
        return "problems"
    if "warn" in results:
        return "warnings"
    return "ok"


def _wait(predicate, timeout, monotonic, sleep) -> bool:
    """Poll until ``predicate`` or ``timeout`` seconds on ``monotonic``.

    ``sleep`` is injected so tests can advance a fake clock. The poll cap
    bounds a clock that does not move.
    """
    deadline = monotonic() + timeout
    for _poll in range(_MAX_POLLS):
        if predicate():
            return True
        now = monotonic()
        if now >= deadline:
            return False
        sleep(min(_POLL_SECONDS, deadline - now))
    return False


def _close(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        pass


def _tcp_connect(ip, port, timeout):
    return socket.create_connection((ip, port), timeout=timeout)


def _tls_handshake(sock, timeout):
    """TLS client handshake only. No certificate check, and no FTP commands.

    Port 990 is implicit FTPS: the first bytes are TLS. Stopping after the
    handshake is what keeps USER and PASS off the wire.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock.settimeout(timeout)
    wrapped = ctx.wrap_socket(sock, do_handshake_on_connect=False)
    try:
        wrapped.do_handshake()
    except Exception:
        _close(wrapped)
        raise
    return wrapped


def _local_ip_for(ip):
    """The address this Mac would use to reach ``ip``.

    A UDP connect chooses the route and does not send a packet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((ip, _MQTT_PORT))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _default_session_factory(host, access_code, serial, *, on_report, command_probe, log):
    """A session that does not start the liveness watchdog.

    The diagnostic polls on its own clock. The watchdog would reset this
    temporary client out from under the check, and it would share the
    printer's log with resets that are not this run.
    """
    return LinkSession(
        host,
        access_code,
        serial,
        on_report=on_report,
        command_probe=command_probe,
        watchdog_interval=None,
        log=log,
    )


def _prefix24(value):
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 4:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 or number > 255 for number in numbers):
        return None
    return tuple(numbers[:3])


def _port_mqtt(ip, tcp_connect):
    sock = None
    try:
        sock = tcp_connect(ip, _MQTT_PORT, _CONNECT_TIMEOUT_SECONDS)
    except Exception:
        sock = None
    if sock is None:
        return False, _check(
            "port_mqtt",
            "fail",
            "Port 8883 did not accept a TCP connection within 3 seconds. "
            "Check that the printer is on and that this address is current.",
            reason="closed",
            port=_MQTT_PORT,
        )
    _close(sock)
    return True, _check(
        "port_mqtt",
        "pass",
        "Port 8883 accepted a TCP connection.",
        port=_MQTT_PORT,
    )


def _port_ftps(ip, tcp_connect, tls_handshake):
    sock = None
    try:
        sock = tcp_connect(ip, _FTPS_PORT, _CONNECT_TIMEOUT_SECONDS)
    except Exception:
        sock = None
    if sock is None:
        return _check(
            "port_ftps",
            "warn",
            "Port 990 is closed, so Link cannot send print files to this printer. "
            "Status and controls do not use this port.",
            reason="closed",
            port=_FTPS_PORT,
        )
    wrapped = None
    ok = False
    try:
        wrapped = tls_handshake(sock, _CONNECT_TIMEOUT_SECONDS)
        ok = True
    except Exception:
        ok = False
    finally:
        _close(wrapped if wrapped is not None else sock)
        if wrapped is not None and wrapped is not sock:
            _close(sock)
    if not ok:
        return _check(
            "port_ftps",
            "warn",
            "Port 990 accepted a connection but did not speak TLS, so print "
            "file uploads will fail. Nothing was logged in.",
            reason="no_tls",
            port=_FTPS_PORT,
        )
    return _check(
        "port_ftps",
        "pass",
        "Port 990 completed a TLS handshake.",
        port=_FTPS_PORT,
    )


def _skipped(check_id, cause, because):
    return _check(check_id, "skip", cause, reason="skipped", because=because)


def _rejected_flag(printer):
    """U6 owns this flag. Until then it is absent, and absent is not a failure."""
    if printer is None:
        return None
    value = getattr(printer, "commands_rejected", None)
    if value is True or value is False:
        return value
    return None


def _commands_from_flag(printer):
    rejected = _rejected_flag(printer)
    if rejected is True:
        return _check(
            "commands",
            "fail",
            "The printer is rejecting commands.",
            reason="commands_rejected",
        )
    if rejected is False:
        return _check(
            "commands",
            "pass",
            "The printer is accepting commands.",
            reason="ok",
        )
    return None


def _is_probe_reply(doc) -> bool:
    info = doc.get("info") if isinstance(doc, dict) else None
    return isinstance(info, dict) and info.get("command") == "get_version"


def _await_connack(session, monotonic, sleep):
    def ready():
        if getattr(session, "connected", False):
            return True
        return getattr(session, "last_connect_error", None) in ("auth_rejected", "refused")

    _wait(ready, _WAIT_SECONDS, monotonic, sleep)
    if getattr(session, "connected", False):
        return _check(
            "mqtt_auth",
            "pass",
            "The printer accepted the MQTT login.",
            reason="ok",
        )
    error = getattr(session, "last_connect_error", None)
    if error == "auth_rejected":
        return _check(
            "mqtt_auth",
            "fail",
            "The printer rejected the access code (MQTT authentication failed).",
            reason="auth_rejected",
        )
    if error == "refused":
        return _check(
            "mqtt_auth",
            "fail",
            "The printer refused the MQTT connection.",
            reason="refused",
        )
    return _check(
        "mqtt_auth",
        "fail",
        "The printer did not finish the MQTT handshake within 10 seconds.",
        reason="no_connack",
    )


def _await_reports(reports, monotonic, sleep):
    """The 10s starts when this is called, which is when CONNACK was observed.

    Time spent waiting for the handshake does not count. A late CONNACK still
    gets the full window; a young session is not a wrong serial.
    """
    if _wait(lambda: len(reports) > 0, _WAIT_SECONDS, monotonic, sleep):
        return _check(
            "reports",
            "pass",
            "A status report arrived within 10 seconds of the login.",
            reason="ok",
        )
    return _check(
        "reports",
        "fail",
        "No status report arrived within 10 seconds of connecting. "
        "The serial number is probably wrong.",
        reason="no_reports",
    )


def _await_probe(session, probe_replies, monotonic, sleep):
    before = len(probe_replies)
    probe = getattr(session, "probe", None)
    if callable(probe):
        try:
            probe()
        except Exception:
            logger.debug("diagnostic command probe was not sent")
    if _wait(lambda: len(probe_replies) > before, _WAIT_SECONDS, monotonic, sleep):
        return _check(
            "commands",
            "pass",
            "The command probe got a reply.",
            reason="ok",
        )
    return _check(
        "commands",
        "fail",
        "A command probe got no reply within 10 seconds. "
        "The printer is ignoring commands.",
        reason="commands_ignored",
    )


def _commands_check(printer, session, probe_enabled, probe_replies, monotonic, sleep):
    flagged = _commands_from_flag(printer)
    if flagged is not None:
        return flagged
    if not probe_enabled:
        return _check(
            "commands",
            "skip",
            "command check not available yet",
            reason="unavailable",
        )
    return _await_probe(session, probe_replies, monotonic, sleep)


def _mqtt_checks(ip, serial, access_code, *, printer, port_open, session_factory,
                 monotonic, sleep, probe_enabled, log):
    if not port_open:
        return (
            _skipped("mqtt_auth", _MQTT_SKIPPED, "port_mqtt"),
            _skipped("reports", _MQTT_SKIPPED, "port_mqtt"),
            _skipped("commands", _MQTT_SKIPPED, "port_mqtt"),
        )

    reports = []
    probe_replies = []

    def on_report(doc):
        reports.append(doc)
        if _is_probe_reply(doc):
            probe_replies.append(doc)

    session = None
    try:
        try:
            session = session_factory(
                ip,
                access_code,
                serial,
                on_report=on_report,
                command_probe=probe_enabled,
                log=log,
            )
            session.start()
        except Exception:
            logger.debug("printer %s: diagnostic session did not start", serial)
            flagged = _commands_from_flag(printer)
            return (
                _check(
                    "mqtt_auth",
                    "fail",
                    "The MQTT session could not be started.",
                    reason="no_connack",
                ),
                _skipped("reports", _AUTH_SKIPPED, "mqtt_auth"),
                flagged if flagged is not None else _skipped(
                    "commands", _AUTH_SKIPPED, "mqtt_auth",
                ),
            )

        auth = _await_connack(session, monotonic, sleep)
        if auth["result"] != "pass":
            flagged = _commands_from_flag(printer)
            return (
                auth,
                _skipped("reports", _AUTH_SKIPPED, "mqtt_auth"),
                flagged if flagged is not None else _skipped(
                    "commands", _AUTH_SKIPPED, "mqtt_auth",
                ),
            )
        report = _await_reports(reports, monotonic, sleep)
        commands = _commands_check(
            printer, session, probe_enabled, probe_replies, monotonic, sleep,
        )
        return auth, report, commands
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                logger.debug("printer %s: diagnostic session did not close", serial)


def _subnet(ip, local_ip_for):
    try:
        local = local_ip_for(ip)
    except Exception:
        local = None
    printer_prefix = _prefix24(ip)
    local_prefix = _prefix24(local) if isinstance(local, str) else None
    if printer_prefix is None or local_prefix is None:
        return _check(
            "subnet",
            "skip",
            "The Mac's address on the way to this printer is unknown, "
            "so the subnet was not checked.",
            reason="unknown",
        )
    if printer_prefix != local_prefix:
        printer_net = ".".join(str(part) for part in printer_prefix)
        local_net = ".".join(str(part) for part in local_prefix)
        return _check(
            "subnet",
            "warn",
            f"This printer is on {printer_net}.x and this Mac is on {local_net}.x. "
            "They need to be on the same network.",
            reason="subnet",
            printer=ip,
            local=local,
        )
    return _check(
        "subnet",
        "pass",
        "This printer is on the same /24 network as this Mac.",
        reason="ok",
    )


def run_connection_diagnostic(ip, serial, access_code, *, printer=None,
                              tcp_connect=None, tls_handshake=None,
                              session_factory=None, local_ip_for=None,
                              monotonic=None, sleep=None, command_probe=None,
                              trigger="operator") -> dict:
    """Run the checks and return the roll-up for one printer.

    ``overall`` is ``problems`` when any check fails, ``warnings`` when any
    warns, and ``ok`` otherwise. A skip is neither. The live session on
    ``printer`` is left running; this opens its own and always closes it.
    """
    tcp_connect = tcp_connect or _tcp_connect
    tls_handshake = tls_handshake or _tls_handshake
    session_factory = session_factory or _default_session_factory
    local_ip_for = local_ip_for or _local_ip_for
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    probe_enabled = COMMAND_PROBE_ENABLED if command_probe is None else bool(command_probe)
    log = getattr(printer, "log", None) if printer is not None else None

    _record(printer, "diagnostic_start", trigger=str(trigger))
    port_open, mqtt_port = _port_mqtt(ip, tcp_connect)
    ftps = _port_ftps(ip, tcp_connect, tls_handshake)
    auth, reports, commands = _mqtt_checks(
        ip,
        serial,
        access_code,
        printer=printer,
        port_open=port_open,
        session_factory=session_factory,
        monotonic=monotonic,
        sleep=sleep,
        probe_enabled=probe_enabled,
        log=log,
    )
    subnet = _subnet(ip, local_ip_for)
    checks = [mqtt_port, ftps, auth, reports, commands, subnet]
    overall = _overall(checks)
    failing = [item["id"] for item in checks if item["result"] == "fail"]
    _record(printer, "diagnostic_result", overall=overall, failing=failing)
    return {
        "bambu_id": serial,
        "ip": ip,
        "ran_at": _iso_now(),
        "trigger": trigger,
        "overall": overall,
        "checks": checks,
    }
