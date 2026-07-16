"""Bridge configuration loaded from a local TOML file.

Access codes and the cloud token are secrets — the dataclass __repr__s redact them
so an accidental `log.info(config)` can't leak them, and neither is ever logged.

Access-code custody (corrected 2026-07-14, access-code courier / U7): a code is NO
LONGER "never sent to 3DPF". An access code the operator enters in the 3DPF wizard is
couriered to the bridge THROUGH the cloud — encrypted at rest, delivered once via
GET /api/bridge/printers/config, then DELETED from the cloud on the bridge's ACK. So a
code is briefly present in the cloud in flight (minutes), by design, then only here.
3DPF is a courier, not a custodian: once delivered the code lives only in this local
config.toml (chmod 600), the bridge is its permanent home, and it is never sent back up.
"""

from dataclasses import dataclass
from typing import List, Optional


class ConfigError(ValueError):
    """Raised when config.toml is missing required fields."""


@dataclass
class PrinterConfig:
    bambu_id: str      # serial number — also the key 3DPF joins reports on
    ip: str
    access_code: str   # SECRET
    name: str = ""

    def __repr__(self) -> str:
        return (f"PrinterConfig(bambu_id={self.bambu_id!r}, ip={self.ip!r}, "
                f"name={self.name!r}, access_code=***)")


@dataclass
class PrintHostConfig:
    """The OctoPrint-compatible print-host OrcaSlicer uploads to (U7).

    Optional: absent [printhost] means the bridge runs observability-only and does
    not open an inbound port. `upload_key` is the SECRET OrcaSlicer sends as
    X-Api-Key — validated locally by the bridge, never by 3DPF (KTD8).
    """
    upload_key: str            # SECRET — the OctoPrint API key
    cert_file: str             # TLS cert OrcaSlicer trusts
    key_file: str              # TLS private key
    spool_dir: str = "spool"
    queue_path: str = "queue.json"
    # 127.0.0.1 when Orca co-resides with the bridge; otherwise the specific LAN
    # interface Orca connects on. Never 0.0.0.0 by default — an upload lands next
    # to config.toml's access codes.
    host: str = "127.0.0.1"
    port: int = 8899
    max_upload_mb: int = 200

    def __repr__(self) -> str:
        return (f"PrintHostConfig(host={self.host!r}, port={self.port!r}, "
                f"spool_dir={self.spool_dir!r}, upload_key=***)")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@dataclass
class Config:
    dpf_base_url: str
    # SECRET. Optional: a consumer install obtains it via pairing (U6) and it lives in the
    # local store, not config.toml. A legacy/hand-authored config.toml may still set it.
    cloud_token: Optional[str]
    printers: List[PrinterConfig]
    printhost: "Optional[PrintHostConfig]" = None
    state_interval_seconds: int = 15
    heartbeat_interval_seconds: int = 30
    # How many poll intervals a printer may go without saying anything NEW before the
    # bridge stops believing its last payload and reports OFFLINE. See
    # `BambuPrinter.snapshot` — a printer that is unplugged mid-print does not make the
    # library raise, it just stops updating, so *silence* is the only thing left to
    # detect it by. Three intervals is the same staleness window the UI uses to dim a
    # telemetry reading. Raise it if a healthy idle printer ever flaps OFFLINE (it would
    # mean that printer's idle push cadence is slower than the window); never set it so
    # high that a dead printer keeps its last status for minutes.
    offline_after_stale_polls: int = 3

    @property
    def stale_after_seconds(self) -> int:
        """The silence a printer is allowed before it is presumed gone."""
        return self.state_interval_seconds * self.offline_after_stale_polls

    def __repr__(self) -> str:
        return (f"Config(dpf_base_url={self.dpf_base_url!r}, "
                f"printers={len(self.printers)}, cloud_token=***)")


def parse_config(raw: dict) -> Config:
    """Build a Config from an already-parsed TOML dict. Separated from file I/O
    so it is unit-testable."""
    dpf_base_url = _require(raw, "dpf_base_url")
    # cloud_token is optional: paired installs (U6) get it from the store, not this file.
    cloud_token = raw.get("cloud_token")

    printers = []
    for i, p in enumerate(raw.get("printers") or []):
        printers.append(PrinterConfig(
            bambu_id=_require(p, "bambu_id", where=f"printers[{i}]"),
            ip=_require(p, "ip", where=f"printers[{i}]"),
            access_code=_require(p, "access_code", where=f"printers[{i}]"),
            name=str(p.get("name") or ""),
        ))
    # Printers MAY be empty. With the courier + local store (U4), a printer is added via
    # the 3DPF onboarding wizard rather than a hand-authored [[printers]] block, so a
    # fresh consumer install ships a config.toml with only dpf_base_url + cloud_token and
    # gets its printers couriered at runtime. A malformed [[printers]] block is still an
    # error — a missing bambu_id/ip/access_code raised above.

    state_interval = int(raw.get("state_interval_seconds", 15))
    heartbeat_interval = int(raw.get("heartbeat_interval_seconds", 30))
    if state_interval < 1 or heartbeat_interval < 1:
        raise ConfigError("state_interval_seconds and heartbeat_interval_seconds must be >= 1 "
                          "(a 0/negative value would hammer the API with no delay)")

    stale_polls = int(raw.get("offline_after_stale_polls", 3))
    if stale_polls < 1:
        # 0 or negative makes the staleness window 0s: every printer is instantly stale
        # and the whole fleet reports OFFLINE forever.
        raise ConfigError("offline_after_stale_polls must be >= 1 (a 0/negative value "
                          "would report every printer OFFLINE on its first poll)")

    return Config(
        dpf_base_url=str(dpf_base_url).rstrip("/"),
        cloud_token=str(cloud_token) if cloud_token else None,
        printers=printers,
        printhost=_parse_printhost(raw.get("printhost")),
        state_interval_seconds=state_interval,
        heartbeat_interval_seconds=heartbeat_interval,
        offline_after_stale_polls=stale_polls,
    )


def _parse_printhost(raw) -> Optional[PrintHostConfig]:
    """Parse the optional [printhost] table. Absent -> observability-only (None).
    Present but incomplete -> ConfigError, because a half-configured inbound TLS
    endpoint that silently never starts is worse than a loud failure."""
    if not raw:
        return None
    port = int(raw.get("port", 8899))
    if port < 1 or port > 65535:
        raise ConfigError("printhost.port must be between 1 and 65535")
    max_upload_mb = int(raw.get("max_upload_mb", 200))
    if max_upload_mb < 1:
        raise ConfigError("printhost.max_upload_mb must be >= 1")
    return PrintHostConfig(
        upload_key=_require(raw, "upload_key", where="printhost"),
        cert_file=_require(raw, "cert_file", where="printhost"),
        key_file=_require(raw, "key_file", where="printhost"),
        spool_dir=str(raw.get("spool_dir") or "spool"),
        queue_path=str(raw.get("queue_path") or "queue.json"),
        host=str(raw.get("host") or "127.0.0.1"),
        port=port,
        max_upload_mb=max_upload_mb,
    )


def load_config(path: str) -> Config:
    # Imported here (not at module load) so parse_config stays importable on any
    # Python without tomli installed.
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw)


def _require(d: dict, key: str, where: str = "config"):
    value = d.get(key)
    if value is None or value == "":
        raise ConfigError(f"{where} is missing required field '{key}'")
    return value
