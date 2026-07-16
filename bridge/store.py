"""The bridge's own durable store of the printer config it has been couriered (U4).

Replaces the hand-authored `config.toml` `[[printers]]` blocks (KTD8): the operator no
longer edits a file. The onboarding wizard sends a printer's access code through the
courier; the reconciler pulls it and writes it HERE; and on restart THIS store — not
`config.toml` — is the source of truth for which printers to connect and their codes.

Holds access codes (device-control secrets) at rest, so the file is written `chmod 600`
and its contents are never logged. Serial (bambu_id) is the key; IP is a cache that the
courier seeds and U1's self-healing keeps current.
"""
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

from .config import PrinterConfig

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH = "printers.json"


class PrinterStore:
    def __init__(self, path: str = _DEFAULT_STORE_PATH):
        self._path = path
        self._printers: Dict[str, Dict] = {}   # bambu_id -> {access_code, local_ip?, name?}
        self._cloud_token: Optional[str] = None  # the paired durable credential (U6)
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return                              # first run — an empty store is normal
        except (ValueError, OSError) as e:
            # A corrupt/unreadable store must not crash startup; begin empty and let the
            # courier re-deliver. Never log the contents (they hold codes).
            logger.warning("printer store %s unreadable (%s); starting empty",
                           self._path, type(e).__name__)
            return
        if not isinstance(raw, dict):
            return
        printers = raw.get("printers")
        if isinstance(printers, dict):
            self._printers = {k: v for k, v in printers.items() if isinstance(v, dict)}
        token = raw.get("cloud_token")
        if isinstance(token, str) and token:
            self._cloud_token = token

    def _save(self) -> None:
        """Atomic write, chmod 600 (the file holds access codes)."""
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".printers-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"cloud_token": self._cloud_token, "printers": self._printers}, f)
            os.replace(tmp, self._path)         # atomic — a crash mid-write can't truncate the store
            os.chmod(self._path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def upsert(self, bambu_id: str, access_code: str, local_ip: Optional[str] = None,
               name: str = "") -> None:
        """Record (or update) a printer's couriered code + address."""
        entry = self._printers.get(bambu_id, {})
        entry["access_code"] = access_code
        if local_ip:
            entry["local_ip"] = local_ip
        if name:
            entry["name"] = name
        self._printers[bambu_id] = entry
        self._save()

    def remove(self, bambu_id: str) -> None:
        if bambu_id in self._printers:
            del self._printers[bambu_id]
            self._save()

    def has(self, bambu_id: str) -> bool:
        return bambu_id in self._printers

    def get_cloud_token(self) -> Optional[str]:
        """The durable cloud credential from pairing (U6), or None if not yet paired."""
        return self._cloud_token

    def set_cloud_token(self, token: str) -> None:
        self._cloud_token = token
        self._save()

    def configs(self) -> List[PrinterConfig]:
        """The stored printers as PrinterConfigs, for building/reconciling the fleet.

        A stored code with no address yet is retained but omitted here — it is not
        connectable until an IP is known (the wizard normally supplies one; otherwise a
        later courier delivery or discovery fills it in)."""
        out = []
        for bambu_id, entry in self._printers.items():
            code = entry.get("access_code")
            ip = entry.get("local_ip")
            if not code or not ip:
                continue
            out.append(PrinterConfig(bambu_id=bambu_id, ip=ip, access_code=code,
                                     name=entry.get("name", "")))
        return out
