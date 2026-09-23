"""Implicit-TLS FTP server for upload tests.

Port 990 wraps the control socket before the 220. pyftpdlib only does explicit
TLS (AUTH TLS after a plaintext banner), so it cannot stand in for a P1.
TLS is pinned to 1.2 so session resumption is the session id vsftpd checks
with SSL_session_reused. The data socket is wrapped only after the 150:
ftplib connects, sends STOR, reads the 150, and only then starts the data
handshake. Wrapping earlier deadlocks that client.
"""

import socket
import ssl
import threading
import time


class ImplicitFtpsServer:
    """One control connection at a time, bound to 127.0.0.1 on an ephemeral port."""

    def __init__(self, certfile, keyfile, *, require_session_reuse=False,
                 dele_reply="550 No such file.", stor_final_reply="226 Transfer complete.",
                 size_override=None, plaintext=False, stall_seconds=0.0,
                 password="access-code", hold_before_reply=None):
        self._certfile = certfile
        self._keyfile = keyfile
        self.require_session_reuse = require_session_reuse
        self.dele_reply = dele_reply
        self.stor_final_reply = stor_final_reply
        self.size_override = size_override
        self.plaintext = plaintext
        self.stall_seconds = stall_seconds
        self.password = password
        self.hold_before_reply = hold_before_reply
        self.stor_blocked = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._commands = []
        self._replies = []
        self._files = {}
        self._accepted_at = []
        self._closed_at = []
        self._reused = []
        self._listen = None
        self._thread = None
        self.port = None

    def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self._certfile, self._keyfile)
        self._ctx = ctx
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(0.2)
        self._listen = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="implicit-ftps", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self.hold_before_reply is not None:
            self.hold_before_reply.set()
        listen = self._listen
        self._listen = None
        if listen is not None:
            try:
                listen.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def snapshot(self):
        with self._lock:
            return {
                "commands": list(self._commands),
                "replies": list(self._replies),
                "files": dict(self._files),
                "accepted_at": list(self._accepted_at),
                "closed_at": list(self._closed_at),
                "reused": list(self._reused),
            }

    def _accept_loop(self):
        while not self._stop.is_set():
            listen = self._listen
            if listen is None:
                break
            try:
                conn, _addr = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:
                _close(conn)

    def _handle(self, conn):
        wrapped = None
        try:
            conn.settimeout(30)
            _nodelay(conn)
            if self.plaintext:
                control = conn
            else:
                wrapped = self._ctx.wrap_socket(conn, server_side=True)
                control = wrapped
            with self._lock:
                self._accepted_at.append(time.monotonic())
            self._session(control)
        finally:
            with self._lock:
                self._closed_at.append(time.monotonic())
            _close(wrapped if wrapped is not None else conn)
            if wrapped is not None and wrapped is not conn:
                _close(conn)

    def _session(self, control):
        self._reply(control, "220 implicit ftps")
        user = ""
        pasv = None
        pending = b""
        try:
            while not self._stop.is_set():
                line, pending = _readline(control, pending)
                if line is None:
                    break
                with self._lock:
                    self._commands.append(line)
                verb, _, arg = line.partition(" ")
                verb = verb.upper()
                if verb == "USER":
                    user = arg
                    self._reply(control, "331 Password required")
                elif verb == "PASS":
                    if user == "bblp" and arg == self.password:
                        self._reply(control, "230 Logged in")
                    else:
                        self._reply(control, "530 Login incorrect")
                elif verb == "PBSZ":
                    self._reply(control, "200 PBSZ=0")
                elif verb == "PROT":
                    self._reply(control, "200 Protection set")
                elif verb == "TYPE":
                    self._reply(control, "200 Type set")
                elif verb == "DELE":
                    self._reply(control, self.dele_reply)
                elif verb == "PASV":
                    _close(pasv)
                    pasv, text = _passive_listener()
                    self._reply(control, text)
                elif verb == "STOR":
                    self._stor(control, pasv, arg)
                    pasv = None
                elif verb == "SIZE":
                    self._size(control, arg)
                elif verb == "QUIT":
                    self._reply(control, "221 Goodbye")
                    break
                elif verb in ("NOOP", "SYST", "FEAT", "PWD"):
                    self._reply(control, "200 Ok")
                else:
                    self._reply(control, "502 Command not implemented")
        finally:
            _close(pasv)

    def _stor(self, control, pasv, name):
        if pasv is None:
            self._reply(control, "425 Use PASV first")
            return
        try:
            data, _addr = pasv.accept()
        except OSError:
            self._reply(control, "425 No data connection")
            return
        finally:
            _close(pasv)
        try:
            _nodelay(data)
            data.settimeout(30)
            # 150 before the data handshake. The client wraps only after it
            # has read that preliminary reply.
            self._reply(control, "150 Opening data connection")
            if not self.plaintext:
                try:
                    data = self._ctx.wrap_socket(data, server_side=True)
                except ssl.SSLError:
                    self._reply(control, "425 TLS handshake failed")
                    return
                reused = bool(data.session_reused)
                with self._lock:
                    self._reused.append(reused)
                if self.require_session_reuse and not reused:
                    self._reply(control, "425 TLS session was not reused")
                    return
            if self.stall_seconds:
                self._stop.wait(self.stall_seconds)
            payload = _read_all(data)
        finally:
            _close(data)
        with self._lock:
            self._files[name] = payload
        self.stor_blocked.set()
        if self.hold_before_reply is not None:
            deadline = time.monotonic() + 30
            while not self.hold_before_reply.is_set():
                if self._stop.is_set() or time.monotonic() >= deadline:
                    break
                self.hold_before_reply.wait(0.05)
        if self.stor_final_reply is None:
            return
        self._reply(control, self.stor_final_reply)

    def _size(self, control, name):
        if self.size_override is not None:
            self._reply(control, f"213 {int(self.size_override)}")
            return
        with self._lock:
            payload = self._files.get(name)
        if payload is None:
            self._reply(control, "550 No such file")
            return
        self._reply(control, f"213 {len(payload)}")

    def _reply(self, sock, line):
        text = str(line).strip()
        with self._lock:
            self._replies.append(text)
        try:
            sock.sendall(text.encode("utf-8") + b"\r\n")
        except OSError:
            pass


def _passive_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    text = f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})"
    return listener, text


def _readline(sock, pending):
    while b"\n" not in pending:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return None, pending
        if not chunk:
            return None, pending
        pending += chunk
    line, _, pending = pending.partition(b"\n")
    return line.rstrip(b"\r").decode("utf-8", "replace"), pending


def _read_all(sock):
    chunks = []
    while True:
        try:
            block = sock.recv(65536)
        except socket.timeout:
            break
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def _nodelay(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def _close(sock):
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass
