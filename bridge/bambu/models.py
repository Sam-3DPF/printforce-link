"""Per-model behavior, keyed by the SSDP ``DevModel.bambu.com`` code.

Built from what the shop has observed, not from third-party tables: those
disagree about the same code (one maps C12 to an X1, another to a P1S). The
shop P1S announces ``C12`` with a serial starting ``01P00``; 3DPF records
``C11`` as the P1P (``backend/shared/constants.py`` ``BAMBU_MODEL_CODES``).
An unknown or missing code uses the P1 profile and is logged once per printer.

``start_url_scheme`` is what Link sends today. The U7 shop check decides
whether a P1S wants ``ftp://`` instead; change it here, not at the call site.
"""

import logging
import ssl
import threading
from dataclasses import dataclass, replace
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelProfile:
    name: str
    family: str
    # Prefix for the project_file ``url`` of a file already on the printer.
    start_url_scheme: str
    # P1 vsFTPd rejects a data channel that does not resume the control
    # channel's TLS session.
    ftps_session_reuse: bool
    # None: no cap. Only a model known to fail above a version gets one.
    ftps_tls_max: Optional[ssl.TLSVersion]
    # stg_cur a P1 reports when no stage is running.
    idle_stg_cur: int
    vibration_cali: bool


P1_PROFILE = ModelProfile(
    name="P1",
    family="p1",
    start_url_scheme="file:///sdcard/",
    ftps_session_reuse=True,
    ftps_tls_max=None,
    idle_stg_cur=255,
    vibration_cali=True,
)

_PROFILES = {
    "C11": replace(P1_PROFILE, name="P1P"),
    "C12": replace(P1_PROFILE, name="P1S"),
}

_warned_serials = set()
_warned_lock = threading.Lock()


def _code(dev_model) -> str:
    return dev_model.strip().upper() if isinstance(dev_model, str) else ""


def profile_for(dev_model, *, serial: str = "") -> ModelProfile:
    """The profile for this DevModel code, or P1 with one warning per printer."""
    code = _code(dev_model)
    profile = _PROFILES.get(code)
    if profile is not None:
        return profile
    key = serial or code
    with _warned_lock:
        first = key not in _warned_serials
        _warned_serials.add(key)
    if first:
        logger.warning(
            "printer %s: model code %r is not in Link's profile table; using the P1 profile",
            serial or "?", code or None,
        )
    return P1_PROFILE
