"""Per-model behavior, keyed by the SSDP ``DevModel.bambu.com`` code.

Built from what the shop has observed, not from third-party tables: those
disagree about the same code (one maps C12 to an X1, another to a P1S). The
shop P1S announces ``C12`` with a serial starting ``01P00``; 3DPF records
``C11`` as the P1P (``backend/shared/constants.py`` ``BAMBU_MODEL_CODES``).
An unknown or missing code uses the P1 profile and is logged once per printer.

``start_url_scheme`` is what a start sends. The shop check that would
compare ``ftp://`` with ``file:///sdcard/`` on a P1S in FINISH was not run
from this environment, so the P1 scheme stays ``file:///sdcard/``. Change
it here, not at the call site.
"""

import logging
import ssl
import threading
from dataclasses import dataclass, replace
from typing import Optional, Tuple

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
    # What the machine has. Reported to 3DPF as ``capabilities`` so the cloud
    # never offers a control the printer lacks.
    nozzle_count: int = 1
    chamber_sensor: bool = False
    chamber_heater: bool = False
    # Filament unit kinds this family can carry: "ams", "ams_lite", "ams_2_pro",
    # "ams_ht", plus "external" (the spool holder).
    filament_units: Tuple[str, ...] = ("ams", "external")
    # A1 and P1 firmware report stg_cur 0 ("printing") while idle.
    stg_cur_idle_bug: bool = False
    # Drying is published only on a model we have seen accept it. None yet.
    publishes_drying: bool = False
    # True once a real shop capture of this model is pinned in the replay
    # tests. Unverified models still work; the card says so.
    verified: bool = False

    @property
    def capabilities(self) -> Tuple[str, ...]:
        caps = ["pause", "resume", "stop", "light", "fans", "temperatures", "filament_units"]
        if self.nozzle_count > 1:
            caps.append("dual_nozzle")
        if self.chamber_sensor:
            caps.append("chamber_temperature")
        if self.chamber_heater:
            caps.append("chamber_heater")
        if self.publishes_drying:
            caps.append("drying")
        return tuple(caps)


# Every family starts from the start and upload settings Link already uses on
# the shop P1S. Changing those for a family is a separate, captured change;
# this table only describes the machine.
P1_PROFILE = ModelProfile(
    name="P1",
    family="p1",
    start_url_scheme="file:///sdcard/",
    ftps_session_reuse=True,
    ftps_tls_max=None,
    idle_stg_cur=255,
    vibration_cali=True,
    stg_cur_idle_bug=True,
)

_X1 = replace(P1_PROFILE, name="X1", family="x1", idle_stg_cur=-1,
              stg_cur_idle_bug=False, chamber_sensor=True)
_A1 = replace(P1_PROFILE, name="A1", family="a1", filament_units=("ams_lite", "external"))
_P2 = replace(P1_PROFILE, name="P2S", family="p2", stg_cur_idle_bug=False,
              chamber_sensor=True, filament_units=("ams", "ams_2_pro", "ams_ht", "external"))
_H2 = replace(P1_PROFILE, name="H2", family="h2", stg_cur_idle_bug=False,
              chamber_sensor=True, chamber_heater=True,
              filament_units=("ams", "ams_2_pro", "ams_ht", "external"))
_X2 = replace(_H2, name="X2D", family="x2", nozzle_count=2)
_A2 = replace(_A1, name="A2L", family="a2")

# DevModel code -> profile. Codes from SSDP ``DevModel.bambu.com``. The shop
# P1S announces C12 with a serial starting 01P00.
_PROFILES = {
    "BL-P001": replace(_X1, name="X1C"),
    "BL-P002": replace(_X1, name="X1"),
    "C13": replace(_X1, name="X1E"),
    "N6": _X2,
    "C11": replace(P1_PROFILE, name="P1P"),
    "C12": replace(P1_PROFILE, name="P1S", verified=True),
    "N7": _P2,
    "N2S": _A1,
    "N1": replace(_A1, name="A1 mini"),
    "N9": _A2,
    "O1D": replace(_H2, name="H2D", nozzle_count=2),
    "O1E": replace(_H2, name="H2D Pro", nozzle_count=2),
    "O2D": replace(_H2, name="H2D Pro", nozzle_count=2),
    "O1C": replace(_H2, name="H2C", nozzle_count=2),
    "O1C2": replace(_H2, name="H2C", nozzle_count=2),
    "O1S": replace(_H2, name="H2S"),
}

# The four models in the Design Bros shop. They are captured first (U8).
SHOP_MODEL_CODES = frozenset({"C12", "C11", "BL-P001", "N7"})

_warned_serials = set()
_warned_lock = threading.Lock()


def _code(dev_model) -> str:
    return dev_model.strip().upper() if isinstance(dev_model, str) else ""


def is_known_model(dev_model) -> bool:
    """True when Link has a profile of its own for this DevModel code."""
    return _code(dev_model) in _PROFILES


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
