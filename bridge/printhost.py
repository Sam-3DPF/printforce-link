"""OctoPrint-compatible print-host endpoint (U7).

OrcaSlicer can upload a sliced file straight to an "OctoPrint" host. We present
the minimal slice of that API OrcaSlicer actually calls — `GET /api/version` and
`POST /api/files/local` — over TLS, authenticated by the upload-only LAN key
(the `X-Api-Key` OrcaSlicer sends). An accepted upload is stored on disk under a
server-generated name and enqueued on the Router (U9 drains it).

Security posture (this endpoint sits next to config.toml, which holds printer
access codes — a path-traversal write is a secrets-exfil primitive):

  * The stored file's path is ALWAYS server-generated (`<uuid>.3mf`) inside the
    spool dir. The client filename is never used to build a path, so traversal
    is structurally impossible regardless of what the client sends.
  * The client filename is still VALIDATED (basename only, allowlisted charset,
    `.3mf` extension) and a name carrying a separator / `..` is REJECTED, not
    quietly basenamed — a client trying to traverse is a signal, not an accident.
  * Auth is constant-time compared. Uploads over `max_bytes` are refused before
    they are written.

The transport (HTTPServer + ssl) is a thin adapter over PrintHostService, whose
methods take raw headers/body and return `(status, json)` — so the security
logic is unit-tested without a socket, and only the ~20 lines of TLS glue need
the manual OrcaSlicer smoke test (see README).
"""

from __future__ import annotations

import hmac
import http.server
import io
import logging
import os
import re
import ssl
import uuid
import zipfile
from typing import Optional, Tuple

from .router import Job, Router, QUEUED, UNRESOLVED

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 200 * 1024 * 1024  # 200 MB — a large multi-plate .3mf, not a DoS
_MAX_METADATA_BYTES = 4 * 1024 * 1024   # model_settings.config is a few KB; cap the decompress
# Bound how long a single request may hold the connection, so a stalled or lying
# Content-Length can't wedge the server waiting on bytes that never arrive.
_REQUEST_TIMEOUT_SECONDS = 30

# A validated upload filename: basename chars only, no separators, no "..".
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._{}-]*\.3mf$", re.IGNORECASE)

# A batch correlation key (see print_batch_service.generate_batch_filename):
# batch-YYYY-MM-DD-<shortid>... — the {plate_num} form and legacy form both start
# this way. Used only as a cheap "does this look routable?" sniff; the cloud
# endpoint does the authoritative resolution.
_BATCH_KEY_RE = re.compile(r"^batch-\d{4}-\d{2}-\d{2}-[A-Za-z0-9]{8}")

# Where 3DPF embeds the batch_name inside the merged .3mf (threemf_processor).
_PLATER_NAME_ENTRY = "Metadata/model_settings.config"
_PLATER_NAME_RE = re.compile(r'key="plater_name"\s+value="([^"]+)"')


def sanitize_upload_filename(name: str) -> Optional[str]:
    """Return the safe basename of an upload filename, or None to REJECT.

    Rejects anything with a path separator, a parent ref, a null byte, a
    disallowed character, or a non-`.3mf` extension. Note this validates the
    client's name for correlation + signalling only; it is never used to build
    the storage path.
    """
    if not name or "\x00" in name:
        return None
    # A separator or parent-ref means the client tried to steer the path. Reject
    # rather than basename it away, so the attempt is visible.
    if "/" in name or "\\" in name or ".." in name:
        return None
    if os.path.basename(name) != name:
        return None
    if not _SAFE_NAME_RE.match(name):
        return None
    return name


def read_embedded_batch_name(threemf_bytes: bytes) -> Optional[str]:
    """Best-effort read of the `plater_name` 3DPF embeds in the merged .3mf.

    This is the "optional hardening" correlation path (U1): if OrcaSlicer's
    re-slice preserved the metadata we get the batch key directly; if not, the
    caller falls back to the filename. Never raises — a non-zip or a rewritten
    settings file just yields None.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as zf:
            if _PLATER_NAME_ENTRY not in zf.namelist():
                return None
            info = zf.getinfo(_PLATER_NAME_ENTRY)
            # The 200 MB body cap bounds the COMPRESSED size only; a zip bomb whose
            # settings entry inflates to gigabytes would OOM the bridge here. This
            # config file is a few KB in practice — refuse to decompress a wildly
            # oversized one and fall back to the filename.
            if info.file_size > _MAX_METADATA_BYTES:
                logger.warning("plater_name entry is %d bytes (> %d cap); skipping metadata",
                               info.file_size, _MAX_METADATA_BYTES)
                return None
            with zf.open(_PLATER_NAME_ENTRY) as fh:
                text = fh.read(_MAX_METADATA_BYTES).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    match = _PLATER_NAME_RE.search(text)
    return match.group(1) if match else None


def resolve_correlation(threemf_bytes: bytes, safe_filename: str) -> Tuple[Optional[str], str]:
    """Derive the batch correlation key + queue status for an upload.

    Order (U7): embedded metadata first, filename fallback. If neither looks like
    a batch key the job is still enqueued — UNRESOLVED, carrying the filename so a
    human can triage it — never dropped.
    """
    embedded = read_embedded_batch_name(threemf_bytes)
    if embedded and _BATCH_KEY_RE.match(embedded):
        return embedded, QUEUED
    if _BATCH_KEY_RE.match(safe_filename):
        return safe_filename, QUEUED
    # Nothing routable — hold it, surfaced, under whatever name we have.
    return safe_filename or embedded, UNRESOLVED


def _parse_multipart(body: bytes, content_type: str):
    """Minimal multipart/form-data parser (stdlib `cgi` is gone in 3.13).

    Returns (file_field, form_fields): file_field is (filename, bytes) or None;
    form_fields maps simple text field names to their string value. Only what the
    OctoPrint upload needs — a `file` part and an optional `print` flag.
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return None, {}
    boundary = (m.group(1) or m.group(2)).strip().encode()
    delimiter = b"--" + boundary
    file_field = None
    form_fields = {}
    for part in body.split(delimiter):
        # Strip ONLY the boundary framing — the leading CRLF after the boundary
        # line and the single trailing CRLF before the next boundary. A greedy
        # .strip(b"\r\n") would eat CR/LF bytes that are part of the binary .3mf
        # payload (a zip can legitimately end in 0x0A/0x0D), silently truncating
        # the stored file.
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part == b"--":  # preamble / closing "--" sentinel
            continue
        header_blob, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = header_blob.decode("utf-8", errors="replace")
        disp = next((ln for ln in headers.splitlines()
                     if ln.lower().startswith("content-disposition")), "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if file_m:
            file_field = (file_m.group(1), content)
        elif name_m:
            form_fields[name_m.group(1)] = content.decode("utf-8", errors="replace")
    return file_field, form_fields


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class PrintHostService:
    """Transport-independent handling of the OctoPrint print-host requests."""

    def __init__(self, upload_key: str, spool_dir: str, router: Router,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.upload_key = upload_key or ""
        self.spool_dir = spool_dir
        self.router = router
        self.max_bytes = max_bytes
        os.makedirs(self.spool_dir, exist_ok=True)

    def check_key(self, headers) -> bool:
        """Constant-time compare of the X-Api-Key header against the upload key."""
        supplied = _header(headers, "X-Api-Key") or ""
        if not self.upload_key or not supplied:
            return False
        return hmac.compare_digest(supplied, self.upload_key)

    def handle_version(self, headers) -> Tuple[int, dict]:
        """OctoPrint's `/api/version` — OrcaSlicer probes it (with the key) to
        validate the host. Key-gated: a wrong/absent key is 401, so the "Test"
        button proves the key is right, not merely that something is listening."""
        if not self.check_key(headers):
            return 401, {"error": "invalid or missing X-Api-Key"}
        return 200, {
            "api": "0.1",
            "server": "1.0.0",
            "text": "3DPF Bambu Bridge (OctoPrint-compatible)",
        }

    def handle_upload(self, headers, body: bytes) -> Tuple[int, dict]:
        """`POST /api/files/local`: auth, size-check, parse, sanitize, store, enqueue."""
        if not self.check_key(headers):
            return 401, {"error": "invalid or missing X-Api-Key"}

        if len(body) > self.max_bytes:
            return 413, {"error": "file exceeds maximum upload size"}

        content_type = _header(headers, "Content-Type") or ""
        file_field, form_fields = _parse_multipart(body, content_type)
        if not file_field:
            return 400, {"error": "no file part in multipart body"}

        raw_name, file_bytes = file_field
        if len(file_bytes) > self.max_bytes:
            return 413, {"error": "file exceeds maximum upload size"}

        safe_name = sanitize_upload_filename(raw_name)
        if safe_name is None:
            # Traversal attempt / junk name — refuse it. Never touch the disk.
            logger.warning("rejected upload with unsafe filename %r", raw_name)
            return 400, {"error": "unsafe or unsupported filename"}

        # Path is ALWAYS server-generated inside the spool dir — never the client name.
        stored_path = os.path.join(self.spool_dir, f"{uuid.uuid4().hex}.3mf")
        with open(stored_path, "wb") as f:
            f.write(file_bytes)

        correlation_key, status = resolve_correlation(file_bytes, safe_name)
        print_flag = _truthy(form_fields.get("print", ""))
        job = self.router.enqueue(Job.new(
            stored_path=stored_path,
            correlation_key=correlation_key,
            print_flag=print_flag,
            status=status,
        ))

        # OctoPrint-shaped success so OrcaSlicer treats it as accepted.
        return 201, {
            "done": True,
            "files": {
                "local": {
                    "name": safe_name,
                    "origin": "local",
                    "refs": {"resource": f"/api/files/local/{safe_name}"},
                }
            },
            # Non-OctoPrint extras, harmless to Orca, useful for the bridge's own logs.
            "job_id": job.id,
            "status": status,
        }


def _header(headers, name: str) -> Optional[str]:
    """Case-insensitive header lookup working for both a dict and an
    http.client.HTTPMessage."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        # HTTPMessage.get is already case-insensitive; a plain dict is not, so
        # fall through to a manual scan when a dict misses.
        value = headers.get(name)
        if value is not None:
            return value
        lowered = name.lower()
        for k, v in headers.items():
            if k.lower() == lowered:
                return v
    return None


# --- TLS transport (thin; manual OrcaSlicer smoke test covers this) -----------

def make_handler(service: PrintHostService):
    class _Handler(http.server.BaseHTTPRequestHandler):
        # Per-connection socket timeout: a client that opens a connection (or
        # declares a large Content-Length) then stalls is dropped instead of
        # holding a worker. Paired with the threaded server so one slow client
        # can't wedge the whole print-host.
        timeout = _REQUEST_TIMEOUT_SECONDS

        # Quiet the default stderr access log; route through our logger.
        def log_message(self, fmt, *args):  # noqa: N802
            logger.debug("printhost %s", fmt % args)

        def _send(self, status: int, payload: dict):
            import json
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/api/version":
                self._send(*service.handle_version(self.headers))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/api/files/local":
                self._send(404, {"error": "not found"})
                return
            # A non-numeric / absent Content-Length is a clean 400, never an
            # uncaught int() -> 500 traceback. (A chunked upload with no length
            # also lands here as an empty body -> "no file part" 400.)
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len) if raw_len is not None else 0
            except ValueError:
                self._send(400, {"error": "invalid Content-Length"})
                return
            # Refuse an over-limit upload by its declared length before reading it
            # all into memory.
            if length > service.max_bytes:
                self._send(413, {"error": "file exceeds maximum upload size"})
                return
            try:
                body = self.rfile.read(length) if length else b""
            except (TimeoutError, OSError):
                # Stalled/short body within the socket timeout — drop it, don't wedge.
                logger.warning("printhost upload read timed out or failed; dropping")
                return
            self._send(*service.handle_upload(self.headers, body))

    return _Handler


class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True  # a stuck request thread must not block process exit


def build_server(service: PrintHostService, host: str, port: int,
                 certfile: str, keyfile: str) -> "_ThreadingHTTPServer":
    """Bind the socket and load the TLS cert. Raises here — in the CALLER'S
    thread — on a bad cert path or a port already in use, so a misconfigured
    print-host fails loudly at startup instead of dying silently inside a daemon
    thread while the bridge keeps reporting healthy.

    Threaded so one slow/stalled client can't wedge the whole endpoint; paired
    with the Router's lock, which makes concurrent enqueue safe."""
    httpd = _ThreadingHTTPServer((host, port), make_handler(service))
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    except BaseException:
        httpd.server_close()  # release the bound port before propagating
        raise
    return httpd


def serve(service: PrintHostService, host: str, port: int, certfile: str, keyfile: str):
    """Bind (loud on failure) then serve forever. `host` should be 127.0.0.1 when
    Orca co-resides, else the specific LAN interface Orca connects to — never
    0.0.0.0 by default (this endpoint can write files next to the access codes)."""
    httpd = build_server(service, host, port, certfile, keyfile)
    logger.info("print-host listening on https://%s:%s (spool=%s)", host, port,
                service.spool_dir)
    httpd.serve_forever()
