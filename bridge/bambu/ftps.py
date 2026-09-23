"""Implicit FTPS upload onto a Bambu printer.

Port 990 is implicit TLS: the server wraps the socket before it sends 220.
``ftplib.FTP_TLS`` does the opposite (plaintext banner, then AUTH TLS), so a
stock client never finishes the handshake. P1 vsftpd also refuses a data
connection that does not resume the control connection's TLS session.

One transfer per printer at a time. The remote file is deleted before STOR
because an existing name comes back as 553. A trailing 426, or a 226 that
never arrives, is success only when SIZE equals the local file — a short
copy must not be reported as uploaded.

The deadline is::

    max(base_seconds, size_bytes / (25 * 1024)) + overhead_seconds

with a 30s base and 5s of overhead. 25 KiB/s is the slowest rate that still
gets the full transfer window. The overhead is the commands around STOR
(DELE, PASV, SIZE, the closing reply), not the bytes themselves.

DELE, STOR, and SIZE name the file at the session root (``/job.3mf``).
``upload`` still returns the bare name a start publishes. A cancel or a
short SIZE deletes that remote name. A 550 from the cleanup delete still
finishes the attempt. A matching SIZE is left in place.

``list_files``, ``download``, and ``delete`` use this client and the same
per-host lock. An empty directory is an empty list. A refused connection is
a failure. A short download removes the local partial. A remote 550 is not
a missing local file.

``cleartext=True`` sends PROT C and does not wrap the data socket. The
default stays PROT P, and session reuse stays on unless the profile says
otherwise.

After a TLS handshake failure, new TLS connects to that host wait 300
seconds. The wait is a timestamp, so it does not hold the transfer lock.
One cleartext banner is read for the log. The access code is the FTP
password and is not put in an exception or a log line.
"""

from __future__ import annotations

import contextlib
import ftplib
import logging
import os
import select
import socket
import ssl
import threading
import time

logger = logging.getLogger(__name__)

_FTPS_PORT = 990
_FTPS_USER = "bblp"
_BLOCK_BYTES = 8192
_DEADLINE_BASE_SECONDS = 30.0
_DEADLINE_OVERHEAD_SECONDS = 5.0
# 25 KiB/s. Below this the deadline grows with the file instead of the base.
_DEADLINE_FLOOR_BPS = 25 * 1024
# A P1 sends 226 as soon as the data connection closes. Waiting out the
# size-based deadline for a reply that is not coming would hold the printer
# lock after the bytes are already on disk.
_CLOSING_REPLY_SECONDS = 2.0
_HANDSHAKE_COOLOFF_SECONDS = 300.0

_KINDS = frozenset({"handshake", "auth", "timeout", "storage", "not_found", "network"})

_locks_guard = threading.Lock()
_host_locks = {}
_cooloff_guard = threading.Lock()
_cooloff_until = {}


class FtpsError(Exception):
    """A failed upload with a stable ``kind``.

    ``kind`` is one of handshake, auth, timeout, storage, not_found, network.
    The message is safe to log: it does not contain the access code.
    """

    def __init__(self, kind, message):
        if kind not in _KINDS:
            raise ValueError(kind)
        self.kind = kind
        super().__init__(message)


class UploadCancelled(Exception):
    """The printer was removed while STOR was still sending blocks."""


def transfer_deadline_seconds(size_bytes, *, base_seconds=_DEADLINE_BASE_SECONDS,
                              overhead_seconds=_DEADLINE_OVERHEAD_SECONDS) -> float:
    """Seconds allowed for one upload, including the commands around STOR.

    ``max(base_seconds, size_bytes / (25 * 1024)) + overhead_seconds``.
    """
    transfer = float(size_bytes) / _DEADLINE_FLOOR_BPS
    return max(float(base_seconds), transfer) + float(overhead_seconds)


def upload(host, access_code, local_path, remote_name, *, profile=None, port=_FTPS_PORT,
           cancel=None, connect_timeout=10.0, monotonic=None, tls_context=None,
           base_seconds=_DEADLINE_BASE_SECONDS,
           overhead_seconds=_DEADLINE_OVERHEAD_SECONDS, cleartext=False) -> str:
    """Store ``local_path`` on the printer as ``remote_name``.

    Returns the bare ``remote_name`` when SIZE matches the local file. The
    stored object is ``/`` plus that name. ``cancel`` is a ``threading.Event``;
    the next block raises ``UploadCancelled`` and deletes the partial.
    ``cleartext=True`` sends PROT C. The default is protected data.
    ``base_seconds`` and ``overhead_seconds`` are the two terms of
    ``transfer_deadline_seconds``.
    """
    clock = monotonic or time.monotonic
    secret = access_code if isinstance(access_code, str) else ""
    started = time.monotonic()
    size = None
    kind = "network"
    try:
        if not host or not str(host).strip():
            raise FtpsError("network", "printer address is required")
        if not isinstance(remote_name, str) or not remote_name.strip():
            raise FtpsError("storage", "remote file name is required")
        if not os.path.isfile(local_path):
            raise FtpsError("not_found", "local file not found")
        size = os.path.getsize(local_path)

        def action():
            nonlocal started
            started = time.monotonic()
            _perform(
                str(host), secret, local_path, remote_name, size,
                profile=profile, port=port, cancel=cancel,
                connect_timeout=connect_timeout, clock=clock,
                tls_context=tls_context, base_seconds=base_seconds,
                overhead_seconds=overhead_seconds, cleartext=cleartext,
            )

        _guarded(str(host), port, connect_timeout, action)
        kind = "ok"
        return remote_name
    except UploadCancelled:
        kind = "cancelled"
        raise
    except FtpsError as exc:
        kind = exc.kind
        raise
    finally:
        elapsed = time.monotonic() - started
        logger.info(
            "ftps upload host=%s file=%s bytes=%s seconds=%.3f kind=%s",
            host, remote_name, "-" if size is None else size, elapsed, kind,
        )


def list_files(host, access_code, *, profile=None, port=_FTPS_PORT,
               connect_timeout=10.0, monotonic=None, tls_context=None,
               cleartext=False) -> list:
    """Names at the file-session root.

    An empty directory returns ``[]``. A refused connection raises
    ``FtpsError`` and is not an empty list.
    """
    clock = monotonic or time.monotonic
    secret = access_code if isinstance(access_code, str) else ""
    started = time.monotonic()
    kind = "network"
    names = []
    try:
        if not host or not str(host).strip():
            raise FtpsError("network", "printer address is required")

        def action():
            nonlocal started
            started = time.monotonic()
            with _connected(
                str(host), secret, profile=profile, port=port,
                connect_timeout=connect_timeout, clock=clock,
                tls_context=tls_context, cleartext=cleartext,
            ) as ftp:
                deadline_at = clock() + transfer_deadline_seconds(0)
                names.extend(_read_list(ftp, deadline_at, clock))

        _guarded(str(host), port, connect_timeout, action)
        kind = "ok"
        return list(names)
    except FtpsError as exc:
        kind = exc.kind
        raise
    finally:
        logger.info(
            "ftps list host=%s seconds=%.3f kind=%s",
            host, time.monotonic() - started, kind,
        )


def download(host, access_code, remote_name, local_path, *, profile=None, port=_FTPS_PORT,
             cancel=None, connect_timeout=10.0, monotonic=None, tls_context=None,
             cleartext=False, base_seconds=_DEADLINE_BASE_SECONDS,
             overhead_seconds=_DEADLINE_OVERHEAD_SECONDS) -> str:
    """Copy the remote name onto ``local_path``.

    A short body fails and the local partial is removed. A remote 550 is
    ``storage`` / ``remote file not found``, not a missing local file.
    """
    clock = monotonic or time.monotonic
    secret = access_code if isinstance(access_code, str) else ""
    started = time.monotonic()
    kind = "network"
    try:
        if not host or not str(host).strip():
            raise FtpsError("network", "printer address is required")
        remote_path = _root_path(remote_name)

        def action():
            nonlocal started
            started = time.monotonic()
            with _connected(
                str(host), secret, profile=profile, port=port,
                connect_timeout=connect_timeout, clock=clock,
                tls_context=tls_context, cleartext=cleartext,
            ) as ftp:
                _retr_file(
                    ftp, remote_path, local_path, cancel, clock,
                    base_seconds, overhead_seconds,
                )

        _guarded(str(host), port, connect_timeout, action)
        kind = "ok"
        return local_path
    except FtpsError as exc:
        kind = exc.kind
        raise
    finally:
        logger.info(
            "ftps download host=%s file=%s seconds=%.3f kind=%s",
            host, remote_name, time.monotonic() - started, kind,
        )


def delete(host, access_code, remote_name, *, profile=None, port=_FTPS_PORT,
           connect_timeout=10.0, monotonic=None, tls_context=None,
           cleartext=False) -> None:
    """Delete one name at the file-session root.

    A remote 550 is not the missing-local-file error.
    """
    clock = monotonic or time.monotonic
    secret = access_code if isinstance(access_code, str) else ""
    started = time.monotonic()
    kind = "network"
    try:
        if not host or not str(host).strip():
            raise FtpsError("network", "printer address is required")
        remote_path = _root_path(remote_name)

        def action():
            nonlocal started
            started = time.monotonic()
            with _connected(
                str(host), secret, profile=profile, port=port,
                connect_timeout=connect_timeout, clock=clock,
                tls_context=tls_context, cleartext=cleartext,
            ) as ftp:
                deadline_at = clock() + transfer_deadline_seconds(0)
                _arm(ftp, deadline_at, clock)
                try:
                    ftp.voidcmd(f"DELE {remote_path}")
                except ftplib.error_perm as exc:
                    raise _remote_perm(exc) from None

        _guarded(str(host), port, connect_timeout, action)
        kind = "ok"
    except FtpsError as exc:
        kind = exc.kind
        raise
    finally:
        logger.info(
            "ftps delete host=%s file=%s seconds=%.3f kind=%s",
            host, remote_name, time.monotonic() - started, kind,
        )


class _ImplicitFtps(ftplib.FTP_TLS):
    """Control channel is TLS before the banner. Data channel may resume it.

    The data socket is closed without ``unwrap``. Some P1 firmware hangs on
    the SSL shutdown after STOR, and by then the bytes are already on disk.
    """

    def __init__(self, context, *, reuse_session):
        super().__init__(context=context)
        self._reuse_session = reuse_session

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if self.timeout is not None and not self.timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        if source_address is not None:
            self.source_address = source_address
        raw = socket.create_connection(
            (self.host, self.port), self.timeout, source_address=self.source_address,
        )
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            _close_quiet(raw)
            raise
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if not self._prot_p:
            return conn, size
        session = self.sock.session if self._reuse_session else None
        try:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=session,
            )
        except Exception:
            _close_quiet(conn)
            raise
        return conn, size


def _perform(host, secret, local_path, remote_name, size, *, profile, port, cancel,
             connect_timeout, clock, tls_context, base_seconds, overhead_seconds,
             cleartext=False):
    remote_path = _root_path(remote_name)
    deadline_at = clock() + transfer_deadline_seconds(
        size, base_seconds=base_seconds, overhead_seconds=overhead_seconds,
    )
    ftp = _ImplicitFtps(_client_context(profile, tls_context), reuse_session=_reuse(profile))
    try:
        _connect(ftp, host, port, connect_timeout)
        _login(ftp, secret)
        _arm(ftp, deadline_at, clock)
        _protect(ftp, cleartext=cleartext)
        _delete_existing(ftp, remote_path)
        with open(local_path, "rb") as handle:
            _stor(ftp, remote_path, handle, cancel, deadline_at, clock)
        _require_size(ftp, remote_path, size, deadline_at, clock)
    except UploadCancelled:
        _cleanup_partial(ftp, remote_path, deadline_at, clock)
        raise
    except FtpsError:
        raise
    except FileNotFoundError:
        raise FtpsError("not_found", "local file not found") from None
    except TimeoutError:
        raise FtpsError("timeout", "transfer deadline exceeded") from None
    except ftplib.error_perm as exc:
        raise _perm(exc) from None
    except (ssl.SSLError, EOFError, OSError):
        raise FtpsError("network", "connection dropped") from None
    finally:
        _quit(ftp)


def _connect(ftp, host, port, timeout):
    try:
        ftp.connect(host, port, timeout=timeout)
    except FtpsError:
        raise
    except TimeoutError:
        raise FtpsError("timeout", "timed out connecting") from None
    except ConnectionRefusedError:
        raise FtpsError("network", "connection refused") from None
    except (ssl.SSLError, EOFError, ConnectionResetError):
        raise FtpsError("handshake", "TLS handshake failed") from None
    except OSError:
        raise FtpsError("network", "could not connect") from None
    except ftplib.all_errors:
        raise FtpsError("handshake", "TLS handshake failed") from None


def _login(ftp, secret):
    try:
        ftp.login(_FTPS_USER, secret)
    except ftplib.error_perm:
        raise FtpsError("auth", "printer rejected the login") from None
    except TimeoutError:
        raise FtpsError("timeout", "transfer deadline exceeded") from None


def _protect(ftp, *, cleartext=False):
    try:
        if cleartext:
            # PROT C does not send PBSZ. RFC 4217 still requires PBSZ 0 first.
            ftp.voidcmd("PBSZ 0")
            ftp.prot_c()
            return
        ftp.prot_p()
    except ftplib.error_perm as exc:
        raise _perm(exc) from None


def _delete_existing(ftp, remote_path):
    """550 means the name is not there yet, which is what STOR wants."""
    try:
        ftp.voidcmd(f"DELE {remote_path}")
    except ftplib.error_perm as exc:
        if str(exc).lstrip().startswith("550"):
            return
        raise _perm(exc) from None


def _cleanup_partial(ftp, remote_path, deadline_at, clock):
    """Drop a cancelled upload. A 550 still counts as a finished cancel."""
    try:
        _await_closing(ftp, deadline_at, clock)
    except FtpsError:
        pass
    try:
        _delete_name(ftp, remote_path, deadline_at, clock)
    except ftplib.error_perm as exc:
        if str(exc).lstrip().startswith("550"):
            return
        raise _perm(exc) from None
    except (FtpsError, TimeoutError, ssl.SSLError, EOFError, OSError, ftplib.Error):
        return


def _delete_name(ftp, remote_path, deadline_at, clock):
    """DELE one root path. 550 means it is already gone."""
    try:
        _arm(ftp, deadline_at, clock)
    except FtpsError:
        return
    try:
        ftp.voidcmd(f"DELE {remote_path}")
    except ftplib.error_perm as exc:
        if str(exc).lstrip().startswith("550"):
            return
        raise


def _stor(ftp, remote_path, handle, cancel, deadline_at, clock):
    _arm(ftp, deadline_at, clock)
    try:
        ftp.voidcmd("TYPE I")
        conn, _ignored = ftp.ntransfercmd(f"STOR {remote_path}")
    except ftplib.error_perm as exc:
        raise _perm(exc) from None
    try:
        while True:
            # Once per block, before the read, so cancel stops the next chunk
            # and a deadline that has already passed does not send it.
            if cancel is not None and cancel.is_set():
                raise UploadCancelled("upload cancelled")
            _arm_socket(conn, deadline_at, clock)
            buf = handle.read(_BLOCK_BYTES)
            if not buf:
                break
            conn.sendall(buf)
    except UploadCancelled:
        raise
    except FtpsError:
        raise
    except TimeoutError:
        raise FtpsError("timeout", "transfer deadline exceeded") from None
    except (ssl.SSLError, EOFError, OSError):
        raise FtpsError("network", "connection dropped") from None
    finally:
        _close_quiet(conn)
    _await_closing(ftp, deadline_at, clock)


def _await_closing(ftp, deadline_at, clock):
    """Read 226 or 426. A silent control connection is a missing 226.

    A timed-out ``readline`` on the TLS socket leaves OpenSSL unwilling to
    send the following SIZE. Wait with ``select`` and read only when the
    reply is already there.
    """
    left = deadline_at - clock()
    if left <= 0:
        raise FtpsError("timeout", "transfer deadline exceeded")
    if not _control_readable(ftp.sock, min(_CLOSING_REPLY_SECONDS, left)):
        return
    try:
        ftp.voidresp()
    except ftplib.error_temp as exc:
        if str(exc).lstrip().startswith("426"):
            return
        raise FtpsError("network", "connection dropped") from None
    except EOFError:
        return
    except (ssl.SSLError, OSError):
        raise FtpsError("network", "connection dropped") from None


def _control_readable(sock, timeout):
    pending = getattr(sock, "pending", None)
    if callable(pending) and pending():
        return True
    try:
        ready, _, _ = select.select([sock], [], [], max(0.0, timeout))
    except (OSError, ValueError):
        return False
    return bool(ready)


def _require_size(ftp, remote_path, expected, deadline_at, clock):
    _arm(ftp, deadline_at, clock)
    try:
        got = ftp.size(remote_path)
    except TimeoutError:
        raise FtpsError("timeout", "transfer deadline exceeded") from None
    except ftplib.error_perm as exc:
        if str(exc).lstrip().startswith("530"):
            raise FtpsError("auth", "printer rejected the login") from None
        raise FtpsError("storage", "remote size does not match") from None
    except (ssl.SSLError, EOFError, OSError, ftplib.Error):
        raise FtpsError("network", "connection dropped") from None
    if got != expected:
        try:
            _delete_name(ftp, remote_path, deadline_at, clock)
        except ftplib.error_perm as exc:
            if not str(exc).lstrip().startswith("550"):
                raise _perm(exc) from None
        except (FtpsError, TimeoutError, ssl.SSLError, EOFError, OSError, ftplib.Error):
            pass
        raise FtpsError("storage", "remote size does not match")


def _perm(exc):
    if str(exc).lstrip().startswith("530"):
        return FtpsError("auth", "printer rejected the login")
    return FtpsError("storage", "printer rejected the upload")


def _remote_perm(exc):
    """A remote 550 is a different error from a missing local file."""
    text = str(exc).lstrip()
    if text.startswith("530"):
        return FtpsError("auth", "printer rejected the login")
    if text.startswith("550"):
        return FtpsError("storage", "remote file not found")
    return _perm(exc)


def _root_path(remote_name):
    if not isinstance(remote_name, str) or not remote_name.strip():
        raise FtpsError("storage", "remote file name is required")
    name = remote_name.strip()
    if name.startswith("/"):
        return name
    return "/" + name


def _guarded(host, port, connect_timeout, action):
    """Run ``action`` under the per-host lock.

    A handshake failure arms a 300s cool-off and reads one cleartext banner
    after the lock is released. A later call inside that window does not
    open a TLS socket and does not hold the lock across the wait.
    """
    handshake_failed = False
    try:
        with _printer_lock(host):
            if _cooloff_active(host):
                exc = FtpsError("handshake", "waiting after a failed handshake")
                exc.cooling = True
                raise exc
            try:
                return action()
            except FtpsError as exc:
                if exc.kind == "handshake" and not getattr(exc, "cooling", False):
                    _mark_cooloff(host)
                    handshake_failed = True
                raise
    finally:
        if handshake_failed:
            _log_cleartext_banner(host, port, connect_timeout)


def _cooloff_active(host):
    now = time.monotonic()
    with _cooloff_guard:
        until = _cooloff_until.get(host)
    return until is not None and now < until


def _mark_cooloff(host):
    with _cooloff_guard:
        _cooloff_until[host] = time.monotonic() + _HANDSHAKE_COOLOFF_SECONDS


def _log_cleartext_banner(host, port, timeout):
    """Read one plaintext reply. The access code is never sent or logged."""
    raw = None
    line = ""
    try:
        raw = socket.create_connection((host, int(port)), timeout)
        raw.settimeout(max(0.1, float(timeout)))
        data = b""
        while b"\n" not in data and len(data) < 512:
            chunk = raw.recv(256)
            if not chunk:
                break
            data += chunk
        line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        line = "".join(ch for ch in line if ch.isprintable())
        if len(line) > 200:
            line = line[:200]
    except OSError:
        line = ""
    finally:
        _close_quiet(raw)
    logger.info("ftps cleartext banner host=%s line=%s", host, line)


@contextlib.contextmanager
def _connected(host, secret, *, profile, port, connect_timeout, clock, tls_context,
               cleartext):
    ftp = _ImplicitFtps(_client_context(profile, tls_context), reuse_session=_reuse(profile))
    try:
        _connect(ftp, host, port, connect_timeout)
        _login(ftp, secret)
        deadline_at = clock() + transfer_deadline_seconds(0)
        _arm(ftp, deadline_at, clock)
        _protect(ftp, cleartext=cleartext)
        yield ftp
    finally:
        _quit(ftp)


def _read_list(ftp, deadline_at, clock):
    """LIST without ``unwrap``. A P1 data socket hangs on SSL shutdown."""
    _arm(ftp, deadline_at, clock)
    try:
        ftp.voidcmd("TYPE A")
        conn, _ignored = ftp.ntransfercmd("LIST")
    except ftplib.error_perm as exc:
        raise _perm(exc) from None
    names = []
    pending = b""
    try:
        while True:
            while b"\n" not in pending:
                _arm_socket(conn, deadline_at, clock)
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    raise FtpsError("timeout", "transfer deadline exceeded") from None
                except (ssl.SSLError, EOFError, OSError):
                    raise FtpsError("network", "connection dropped") from None
                if not chunk:
                    break
                pending += chunk
            if not pending:
                break
            if b"\n" not in pending:
                name = _list_name(pending.decode("utf-8", "replace"))
                if name:
                    names.append(name)
                break
            raw, _, pending = pending.partition(b"\n")
            name = _list_name(raw.rstrip(b"\r").decode("utf-8", "replace"))
            if name:
                names.append(name)
    finally:
        _close_quiet(conn)
    _await_closing(ftp, deadline_at, clock)
    return names


def _list_name(line):
    text = line.strip()
    if not text or text.lower().startswith("total "):
        return None
    parts = text.split(None, 8)
    if len(parts) >= 9 and parts[0][:1] in "-dl":
        name = parts[8]
    else:
        name = text
    if name.startswith("/"):
        name = name[1:]
    if not name or name in (".", ".."):
        return None
    return name


def _retr_file(ftp, remote_path, local_path, cancel, clock, base_seconds, overhead_seconds):
    deadline_at = clock() + transfer_deadline_seconds(
        0, base_seconds=base_seconds, overhead_seconds=overhead_seconds,
    )
    _arm(ftp, deadline_at, clock)
    try:
        expected = ftp.size(remote_path)
    except ftplib.error_perm as exc:
        raise _remote_perm(exc) from None
    except TimeoutError:
        raise FtpsError("timeout", "transfer deadline exceeded") from None
    if not isinstance(expected, int):
        raise FtpsError("storage", "remote size does not match")
    deadline_at = clock() + transfer_deadline_seconds(
        expected, base_seconds=base_seconds, overhead_seconds=overhead_seconds,
    )
    conn = None
    created = False
    received = 0
    try:
        _arm(ftp, deadline_at, clock)
        try:
            ftp.voidcmd("TYPE I")
            conn, _ignored = ftp.ntransfercmd(f"RETR {remote_path}")
        except ftplib.error_perm as exc:
            raise _remote_perm(exc) from None
        with open(local_path, "wb") as handle:
            created = True
            while True:
                if cancel is not None and cancel.is_set():
                    raise UploadCancelled("download cancelled")
                _arm_socket(conn, deadline_at, clock)
                buf = conn.recv(_BLOCK_BYTES)
                if not buf:
                    break
                handle.write(buf)
                received += len(buf)
        _await_closing(ftp, deadline_at, clock)
        if received != expected:
            raise FtpsError("storage", "remote size does not match")
    except Exception:
        if created:
            _remove_quiet(local_path)
        raise
    finally:
        _close_quiet(conn)


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _reuse(profile) -> bool:
    if profile is None:
        return True
    return bool(getattr(profile, "ftps_session_reuse", True))


def _client_context(profile, override):
    if override is not None:
        return override
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The printer certificate is self-signed. Check the handshake, not the name.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_max = None if profile is None else getattr(profile, "ftps_tls_max", None)
    if tls_max is not None:
        ctx.maximum_version = tls_max
    return ctx


def _arm(ftp, deadline_at, clock):
    left = deadline_at - clock()
    if left <= 0:
        raise FtpsError("timeout", "transfer deadline exceeded")
    ftp.timeout = left
    if getattr(ftp, "sock", None) is not None:
        ftp.sock.settimeout(left)
    return left


def _arm_socket(sock, deadline_at, clock):
    left = deadline_at - clock()
    if left <= 0:
        raise FtpsError("timeout", "transfer deadline exceeded")
    sock.settimeout(left)
    return left


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def _close_quiet(sock):
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _lock_for(host):
    with _locks_guard:
        lock = _host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _host_locks[host] = lock
        return lock


@contextlib.contextmanager
def _printer_lock(host):
    lock = _lock_for(host)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
