"""LAN file push: FTPS store and the MQTT start URL a P1/X1/A1 expects.

Bambulabs-api hides some of this. The shop printers are P1-family, so Link owns
the P1 rules: `file:///sdcard/` on start, do not hang the TLS data socket on
close, treat a trailing 426 as success when SIZE matches, and treat prepare
percent 99 as the file already on disk.
"""

from __future__ import annotations

import ftplib
import os
import ssl
from typing import Optional

from .coerce import as_int

_FTPS_PORT = 990
_FTPS_USER = "bblp"


def lan_start_url(remote_name: str, *, family: str = "p1") -> str:
    """MQTT `project_file` URL for a file already on the printer.

    P1 / X1 / A1 start from the SD card path. H2-family printers use an FTP
    URL. A caller that already passed a scheme (`//` in the name) is left alone.
    """
    name = (remote_name or "").strip()
    if not name:
        raise ValueError("remote file name is required")
    if "//" in name:
        return name
    name = name.lstrip("/")
    if family == "h2":
        return f"ftp:///{name}"
    return f"file:///sdcard/{name}"


def prepare_download_complete(percent) -> bool:
    """True when a P1 prepare percent means the 3mf is already on the printer.

    P1 often stalls at 99 and never reports 100. Waiting for 100 fails a send
    that already landed. X1 can report 100 before the file exists; that case
    still waits for RUNNING elsewhere.
    """
    value = as_int(percent, None)
    return value is not None and value >= 99


class UploadCancelled(Exception):
    """The printer was removed while STOR was still sending blocks."""


class _CancellableReader:
    """Raise between reads so a generic ``storbinary`` can stop without a subclass."""

    def __init__(self, handle, cancel):
        self._handle = handle
        self._cancel = cancel

    def read(self, size=-1):
        if self._cancel is not None and self._cancel.is_set():
            raise UploadCancelled("upload cancelled")
        return self._handle.read(size)


class _ShopFtps(ftplib.FTP_TLS):
    """Implicit-TLS FTPS that does not unwrap the data connection.

    Some P1 firmware hangs on the SSL shutdown after STOR. The bytes are
    already on disk by then; hanging the upload thread looks like a failed send.
    ``cancel`` is checked between blocks so fleet removal does not wait out the file.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._upload_cancel = None

    def set_upload_cancel(self, cancel) -> None:
        self._upload_cancel = cancel

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                if self._upload_cancel is not None and self._upload_cancel.is_set():
                    raise UploadCancelled("upload cancelled")
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        try:
            return self.voidresp()
        except ftplib.error_temp as exc:
            if "426" not in str(exc):
                raise
            return str(exc)


def _tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def store_on_printer(
    host: str,
    access_code: str,
    local_path: str,
    remote_name: str,
    *,
    port: int = _FTPS_PORT,
    timeout: float = 30.0,
    ftp_factory=None,
    cancel=None,
) -> str:
    """STOR `local_path` as `remote_name`. Raises if the remote size does not match.

    ``cancel`` is a ``threading.Event``. When it is set, the next block raises
    ``UploadCancelled`` and the control connection is closed.
    """
    if not host:
        raise ValueError("printer address is required")
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    expected = os.path.getsize(local_path)
    factory = ftp_factory or _ShopFtps
    ftp = factory()
    setter = getattr(ftp, "set_upload_cancel", None)
    if callable(setter):
        setter(cancel)
    try:
        if hasattr(ftp, "ssl_version"):
            try:
                ftp.context = _tls_context()
            except Exception:
                pass
        ftp.connect(host, port, timeout=timeout)
        if callable(getattr(ftp, "auth", None)):
            try:
                ftp.auth()
            except Exception:
                pass
        ftp.login(_FTPS_USER, access_code)
        if callable(getattr(ftp, "prot_p", None)):
            try:
                ftp.prot_p()
            except Exception:
                pass
        with open(local_path, "rb") as handle:
            source = _CancellableReader(handle, cancel) if cancel is not None else handle
            try:
                reply = ftp.storbinary(f"STOR {remote_name}", source)
            except (ftplib.error_temp, ftplib.error_reply, ftplib.error_perm) as exc:
                if "426" not in str(exc):
                    raise
                reply = str(exc)
        if not remote_size_matches(ftp, remote_name, expected):
            raise RuntimeError(
                f"remote size of {remote_name} does not match {expected} bytes"
            )
        return str(reply or "")
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def remote_size_matches(ftp, remote_name: str, expected: int) -> bool:
    """True when the printer's SIZE for `remote_name` equals `expected`."""
    size = remote_file_size(ftp, remote_name)
    return size is not None and size == expected


def remote_file_size(ftp, remote_name: str) -> Optional[int]:
    size_fn = getattr(ftp, "size", None)
    if callable(size_fn):
        try:
            value = size_fn(remote_name)
            return as_int(value, None)
        except Exception:
            pass
    send = getattr(ftp, "sendcmd", None)
    if not callable(send):
        return None
    try:
        reply = send(f"SIZE {remote_name}")
    except Exception:
        return None
    if not isinstance(reply, str):
        return None
    parts = reply.split()
    if len(parts) < 2:
        return None
    return as_int(parts[-1], None)


def listing_has_file(names, remote_name: str) -> bool:
    """True when an NLST result mentions `remote_name`.

    X1 includes the directory in the name; P1 returns a bare filename.
    """
    if not remote_name:
        return False
    bare = remote_name.rstrip("/").split("/")[-1]
    for entry in names or []:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if text == remote_name or text == bare or text.endswith("/" + bare):
            return True
    return False
