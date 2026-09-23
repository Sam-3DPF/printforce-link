"""LAN start URL and the prepare-percent rule for a file already on the printer.

The upload itself is ``bridge.bambu.ftps``: port 990 is implicit TLS, and a
trailing 426 is only success when SIZE matches. P1 / X1 / A1 start from
``file:///sdcard/``. H2-family printers use an FTP URL. P1 prepare percent 99
means the 3mf is already on disk.
"""

from __future__ import annotations

# Defined next to the uploader. Callers still import it from here.
from .bambu.commands import project_file_url
from .bambu.ftps import UploadCancelled
from .coerce import as_int

# Kept for callers that name a family. A start uses the model profile's scheme.
_FAMILY_SCHEME = {
    "h2": "ftp:///",
    "p1": "file:///sdcard/",
}


def lan_start_url(remote_name: str, *, family: str = "p1") -> str:
    """MQTT `project_file` URL for a file already on the printer.

    The scheme is the same one the model profile carries. P1 / X1 / A1 start
    from the SD card path. H2-family printers use an FTP URL. A caller that
    already passed a scheme (`//` in the name) is left alone.
    """
    scheme = _FAMILY_SCHEME.get(family, _FAMILY_SCHEME["p1"])
    return project_file_url(remote_name, scheme)


def prepare_download_complete(percent) -> bool:
    """True when a P1 prepare percent means the 3mf is already on the printer.

    P1 often stalls at 99 and never reports 100. Waiting for 100 fails a send
    that already landed. X1 can report 100 before the file exists; that case
    still waits for RUNNING elsewhere.
    """
    value = as_int(percent, None)
    return value is not None and value >= 99


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
