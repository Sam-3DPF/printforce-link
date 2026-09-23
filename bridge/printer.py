"""One Bambu printer over a Link-owned MQTT session.

Reports arrive on `device/{serial}/report` and are merged here. Commands are
published to `device/{serial}/request`. The session opens no camera socket.
`map_status`, `decode_hms`, `parse_telemetry`, `merge_status_payload`, and
`ams.parse_ams` stay free of the network client so they can be unit-tested
without a printer.
"""

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

from .ams import (
    ams_has_color,
    ams_needs_pushall,
    idle_trays_needing_rfid,
    parse_ams,
    parse_tray_exist_bits,
    save_remembered_ams,
)
from .bambu.log import PrinterLog
from .bambu.diagnostic import proves_serial, run_connection_diagnostic
from .bambu.session import LinkSession
from .bambu.hms import (
    commands_rejected as hms_commands_rejected,
    decode_hms,
    fault_print_error,
    hms_faults,
    is_cancel_failed,
    reported_print_error,
)
from .bambu.state import (
    PrinterState,
    _MAX_FIRMWARE_TEXT,
    _norm_error_code,
    merge_status_payload,
)
from .bambu_alerts import describe_hms
from .coerce import as_float, as_int, clean_str
from .config import PrinterConfig
from .transfer import lan_start_url, store_on_printer

logger = logging.getLogger(__name__)

# Bambu gcode_state -> the Printer.status vocabulary 3DPF accepts
# (IDLE / PRINTING / PAUSED / NEEDS_CLEARING / ERROR / OFFLINE).
#
# FINISH -> NEEDS_CLEARING: the print is done but the plate isn't cleared yet.
# PAUSE  -> PAUSED: Bambu's enum spells it PAUSE. It is NOT a flavour of PRINTING —
#           a paused printer is making no progress and is waiting on the operator.
_STATE_MAP = {
    "IDLE": "IDLE",
    "PREPARE": "PRINTING",
    "SLICING": "PRINTING",
    "RUNNING": "PRINTING",
    "PAUSE": "PAUSED",
    "FINISH": "NEEDS_CLEARING",
    "FAILED": "ERROR",
}

# gcode_states in which a print is on the machine and its clock should be running...
_PRINT_IN_PROGRESS = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
# ...and the ones that end it.
_PRINT_ENDED = frozenset({"FINISH", "FAILED"})
# We trust the bridge's own stopwatch only if we positively saw the machine NOT
# printing on the poll before the print began. Assert, never assume — a blank or
# unknown prior state is not evidence of an idle machine.
_PRINT_START_EVIDENCE = frozenset({"IDLE", "FINISH", "FAILED"})

# stg_cur values that need retry_filament_action before resume_print (KTD6).
# 6 = runout, 17/20 = load, 21 = unload / AMS, 24 = AMS lost, 35 = clog.
_FILAMENT_RETRY_STAGES = frozenset({6, 17, 20, 21, 24, 35})

# A print runs for hours, occasionally days — never months. Anything past this is a
# corrupt timestamp, not a print.
_MAX_PLAUSIBLE_PRINT_SECONDS = 30 * 24 * 60 * 60
# Epoch floor (2001-09-09). `gcode_start_time` is "0" on a printer that never set it.
_MIN_PLAUSIBLE_EPOCH = 1_000_000_000
# The stopwatch and the printer's own start time should agree to within a poll or two.
# Past this, something is wrong and we say so out loud — see _warn_if_sources_disagree.
_DURATION_DISAGREEMENT_SECONDS = 120

# How long a printer may say nothing NEW before the bridge stops believing its last
# payload (see `BambuPrinter.snapshot`). The real window comes from config
# (`Config.stale_after_seconds` = state_interval x offline_after_stale_polls); this is
# the fallback for a `BambuPrinter` built without one, and equals that default (15s x 3).
_DEFAULT_STALE_AFTER_SECONDS = 45
# pushall is expensive on the printer. Ask a few times after connect / a partial AMS,
# then wait for Refresh.
_MAX_FULL_STATUS_ATTEMPTS = 3
# A pushall reply can land behind a mid-print delta. Sample across this window
# so the full AMS has time to arrive on the report callback.
_ABSORB_SAMPLES = 6
_ABSORB_SAMPLE_SECONDS = 0.4

_MQTT_COMMANDS = {
    "pause_print": {"print": {"command": "pause"}},
    "resume_print": {"print": {"command": "resume"}},
    "stop_print": {"print": {"command": "stop"}},
    "retry_filament_action": {"print": {"command": "ams_control", "param": "resume"}},
}


def _default_session_factory(ip, access_code, serial, on_report):
    return LinkSession(ip, access_code, serial, on_report=on_report)


def _mqtt_command_name(payload) -> str:
    if isinstance(payload, dict):
        for key in ("print", "pushing", "info"):
            body = payload.get(key)
            if isinstance(body, dict):
                command = body.get("command")
                if isinstance(command, str) and command.strip():
                    return command.strip()[:64]
    return "command"


def _bounded_text(value, *, upper: bool = False) -> Optional[str]:
    text = clean_str(value)
    if text is None:
        return None
    text = text.upper() if upper else text
    return text[:_MAX_FIRMWARE_TEXT]


def _identifier_present(value) -> bool:
    """Redact firmware identifiers to presence only; zero is Bambu's empty sentinel."""
    if value is None or value == 0:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    # An unexpected non-zero/non-string value cannot safely be treated as empty.
    return True


def _failed_ready_fields(print_obj: Dict) -> Dict:
    """Bounded/redacted evidence used to identify a historical sticky FAILED state."""
    hms_present = "hms" in print_obj
    hms = print_obj.get("hms")
    stg = print_obj.get("stg")
    return {
        "gcode_state": _bounded_text(print_obj.get("gcode_state"), upper=True),
        "hms_present": hms_present,
        "hms_empty": hms_present and isinstance(hms, list) and not hms,
        # Never send task/project ids. Their presence is enough for the safety gate.
        "has_active_file": (
            _identifier_present(print_obj.get("gcode_file"))
            or _identifier_present(print_obj.get("subtask_name"))
        ),
        "has_active_task": (
            _identifier_present(print_obj.get("task_id"))
            or _identifier_present(print_obj.get("subtask_id"))
        ),
        "has_active_project": _identifier_present(print_obj.get("project_id")),
        # None means the field was absent; only an explicitly empty queue qualifies.
        "stage_queue_empty": isinstance(stg, list) and not stg if "stg" in print_obj else None,
        "print_type": _bounded_text(print_obj.get("print_type")),
    }


def _is_zero_or_absent_error(value) -> bool:
    code = _norm_error_code(value)
    return not code or not code.strip("0")


def _is_historical_failed_candidate(print_obj: Dict, fields: Dict) -> bool:
    print_type = (fields.get("print_type") or "").strip().lower()
    return (
        fields.get("gcode_state") == "FAILED"
        and _is_zero_or_absent_error(print_obj.get("print_error"))
        and fields.get("hms_present") is True
        and fields.get("hms_empty") is True
        and fields.get("has_active_file") is False
        and fields.get("has_active_task") is False
        and fields.get("has_active_project") is False
        and as_int(print_obj.get("mc_percent"), None) == 0
        and as_float(print_obj.get("nozzle_target_temper"), None) == 0
        and as_float(print_obj.get("bed_target_temper"), None) == 0
        and fields.get("stage_queue_empty") is True
        and print_type in ("", "idle")
    )


# stg_cur leftovers that still mean "no work on the machine."
# X1 idle is -1. P1 idle is 255. A cooled P1 after a sticky FAILED often
# keeps 0 (Bambu's "printing" id) with no file, 0% progress, and 0 targets —
# P1S-11 on 2026-09-22. That is leftover idle, not a live fault.
_IDLE_STG_CUR = frozenset({None, 0, -1, 255})
_LEFTOVER_BLOCKING_HMS = frozenset({"FATAL", "SERIOUS"})


def _is_cool_target(value) -> bool:
    target = as_float(value, None)
    return target is None or target == 0


def _is_leftover_idle_failed(print_obj: Dict, fields: Dict) -> bool:
    """Sticky FAILED after the machine is already idle.

    historical_failed_ready stays a separate, stricter signal for the cloud
    assignment gate. This remap is the farm-visible status: do not leave
    ERROR on a cooled printer with no file, no print_error, and no HMS.
    """
    if (fields.get("gcode_state") or "").upper() != "FAILED":
        return False
    if not _is_zero_or_absent_error(print_obj.get("print_error")):
        return False
    if fields.get("has_active_file") is True:
        return False
    # Explicit 0 — a FAILED dump that omits percent is not leftover idle.
    if as_int(print_obj.get("mc_percent"), None) != 0:
        return False
    if as_int(print_obj.get("stg_cur"), None) not in _IDLE_STG_CUR:
        return False
    if not _is_cool_target(print_obj.get("nozzle_target_temper")):
        return False
    if not _is_cool_target(print_obj.get("bed_target_temper")):
        return False
    if "hms" in print_obj:
        hms = print_obj.get("hms")
        if not (isinstance(hms, list) and not hms):
            return False
    if (fields.get("hms_count") or 0) > 0:
        return False
    if fields.get("hms_severity") in _LEFTOVER_BLOCKING_HMS:
        return False
    return True


def promote_live_idle(status: str, print_obj: Optional[dict]) -> str:
    """gcode IDLE during heat-up or a moving print is still a live print.

    Bambu can leave gcode_state at IDLE while the nozzle and bed are commanded
    up for a file that is already on the machine, and again for a beat of
    mid-print. 3DPF treats wire IDLE as a free bed and will auto-start the
    next file. Actual temperature is not the signal: a finished plate cools
    through the same numbers with the heaters off. Progress 100 stays IDLE
    so a finished plate can remain uncleared in the cloud. A FAILED state
    that map_status already folded to IDLE (user cancel, leftover fail) is
    not promoted.
    """
    if status != "IDLE" or not isinstance(print_obj, dict):
        return status
    if (print_obj.get("gcode_state") or "").strip().upper() != "IDLE":
        return status
    progress = as_int(print_obj.get("mc_percent"), None)
    if progress is not None and 0 < progress < 100:
        return "PRINTING"
    if progress == 100:
        return status
    nozzle_target = as_float(print_obj.get("nozzle_target_temper"), None) or 0
    bed_target = as_float(print_obj.get("bed_target_temper"), None) or 0
    if nozzle_target <= 0 and bed_target <= 0:
        return status
    named = (
        _identifier_present(print_obj.get("gcode_file"))
        or _identifier_present(print_obj.get("subtask_name"))
    )
    if named:
        return "PRINTING"
    return status


def map_status(gcode_state: Optional[str], *, print_error=None,
               hms_code=None, hms=None, user_cancelled=False) -> str:
    """Map a Bambu gcode_state to a 3DPF printer status.

    Unknown and blank states map to **OFFLINE, never IDLE**. IDLE is the sole
    authorization for dispatch, so it has to be positively asserted by the printer: a
    fail-open default would dispatch a job onto a busy printer, deduct its filament,
    and stamp the batch PRINTING for a print that never starts. The window is real,
    not theoretical — nothing has arrived until the first report, so on every
    bridge start there is an interval in which each printer, *including one
    mid-print*, has no gcode_state at all.

    A cancel-failed print (`50348044` / HMS `0300_400C`) maps to IDLE, not ERROR,
    so the next Start is not blocked. FAILED without a cancel code stays ERROR.
    Only remap ERROR — a leftover cancel code on RUNNING must not hide a live print.
    """
    mapped = _STATE_MAP.get((gcode_state or "").strip().upper(), "OFFLINE")
    if mapped == "ERROR" and (
        user_cancelled
        or is_cancel_failed(print_error=print_error, hms_code=hms_code, hms=hms)
    ):
        return "IDLE"
    return mapped


def parse_telemetry(status: dict) -> Dict:
    """Extract the live telemetry the printer already reports.

    Every field is optional. Feed this the *merged* payload (see
    `merge_status_payload`), never a raw one: Bambu reports are partial deltas, so any
    key can be missing from any single push.
    """
    print_obj = (status or {}).get("print") if isinstance(status, dict) else None
    if not isinstance(print_obj, dict):
        print_obj = {}

    telemetry = {
        "progress_percent": as_int(print_obj.get("mc_percent"), None),
        "layer_num": as_int(print_obj.get("layer_num"), None),
        "total_layer_num": as_int(print_obj.get("total_layer_num"), None),
        "remaining_seconds": _minutes_to_seconds(print_obj.get("mc_remaining_time")),
        "nozzle_temper": as_float(print_obj.get("nozzle_temper"), None),
        "nozzle_target_temper": as_float(print_obj.get("nozzle_target_temper"), None),
        "bed_temper": as_float(print_obj.get("bed_temper"), None),
        "bed_target_temper": as_float(print_obj.get("bed_target_temper"), None),
        "chamber_temper": as_float(print_obj.get("chamber_temper"), None),
        "gcode_file": clean_str(print_obj.get("gcode_file")),
        "subtask_name": clean_str(print_obj.get("subtask_name")),  # the human-friendly job name
        "nozzle_diameter": as_float(print_obj.get("nozzle_diameter"), None),
        # The print stage. It is the only field that says *why* a print paused
        # (6 = filament runout, 16 = user, 35 = nozzle clog) — gcode_state only ever
        # says PAUSE. X1 sends -1 and P1 sends 255 for "no stage"; both are None.
        "stage": _valid_stage(print_obj.get("stg_cur")),
        "tray_exist_bits": parse_tray_exist_bits(status),
        # 0 and a low word below 0x4000 are status, not a fault. Cancel codes
        # stay so an older ingest can still tell 50348044 from a real fail.
        "print_error": _print_error_str(print_obj.get("print_error")),
    }
    raw_hms = print_obj.get("hms")
    telemetry.update(decode_hms(raw_hms))
    telemetry.update(describe_hms(
        hms_code=telemetry.get("hms_code"),
        print_error=telemetry.get("print_error"),
    ))
    telemetry.update(_failed_ready_fields(print_obj))
    # No merged document: these are "no information", same as an offline report.
    # A real payload with nothing wrong is an empty fault list and false.
    if isinstance(status, dict):
        telemetry["hms_faults"] = hms_faults(raw_hms)
        telemetry["fault_print_error"] = fault_print_error(print_obj.get("print_error"))
        telemetry["commands_rejected"] = hms_commands_rejected(raw_hms)
    else:
        telemetry["hms_faults"] = None
        telemetry["fault_print_error"] = None
        telemetry["commands_rejected"] = None
    return telemetry


class PrintStopwatch:
    """How long the print on this machine actually ran, measured rather than estimated.

    Nothing else in the system knows the observed duration — it is what replaces the
    slicer's estimate in the cost snapshot — so a missing value is preferable to a wrong
    one, and every branch here prefers reporting nothing over inventing a number.
    """

    def __init__(self, printer_id: str, monotonic=time.monotonic, wall_clock=time.time):
        self._printer_id = printer_id
        # Injectable clocks — the stopwatch is otherwise untestable.
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self._prev_gcode_state: Optional[str] = None
        self._started_monotonic: Optional[float] = None
        self._started_epoch: Optional[int] = None
        self._start_observed = False
        self._duration_seconds: Optional[int] = None
        self._source: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[int]:
        return self._duration_seconds

    @property
    def source(self) -> Optional[str]:
        # Which clock produced `duration_seconds`: "bridge" | "printer" | None.
        return self._source

    def observe(self, gcode_state, print_obj: Dict) -> None:
        """Take one poll's reading. The duration is measured on the in-progress -> ended
        edge, then **latched**: it is reported on every subsequent poll for as long as
        the printer stays in the ended state, not just on the single poll where the edge
        happened. The completion report retries until 3DPF acks it, so a duration that
        existed for exactly one poll would be lost to the first dropped POST and could
        never be recovered.
        """
        state = (gcode_state or "").strip().upper() if isinstance(gcode_state, str) else ""

        if state in _PRINT_IN_PROGRESS:
            if self._started_monotonic is None:
                self._started_monotonic = self._monotonic()
                self._start_observed = self._prev_gcode_state in _PRINT_START_EVIDENCE
                logger.info("printer %s: print in progress (%s)", self._printer_id, state)
            if self._started_epoch is None:
                self._started_epoch = _valid_epoch(print_obj.get("gcode_start_time"))
            # A new print invalidates the previous one's measurement.
            self._duration_seconds = None
            self._source = None

        elif state in _PRINT_ENDED:
            if self._started_monotonic is not None or self._started_epoch is not None:
                self._duration_seconds, self._source = self._measure()
                self._started_monotonic = None
                self._started_epoch = None
                self._start_observed = False
                logger.info("printer %s: print %s after %ss (source=%s)", self._printer_id,
                            state, self._duration_seconds, self._source)
            # Otherwise the bridge came up to find an already-ended print (an uncleared
            # plate from yesterday). We never saw it run, so we do not know how long it
            # took and we say so, rather than inventing a number.

        else:  # IDLE, blank, unknown: no print on the machine.
            self._started_monotonic = None
            self._started_epoch = None
            self._start_observed = False
            self._duration_seconds = None
            self._source = None

        self._prev_gcode_state = state

    def _measure(self) -> Tuple[Optional[int], Optional[str]]:
        """(seconds, source) for the print that just ended.

        The bridge's own stopwatch wins **when the bridge watched the print start**: it
        is a monotonic delta, so no clock skew, timezone, or firmware quirk can corrupt
        it, and it is exactly what the operator experienced (pauses included).

        The printer's `gcode_start_time` (epoch seconds) covers the one case the
        stopwatch cannot: the bridge restarted, or connected, while a print was already
        running, so its stopwatch only ever saw the tail. Reporting that tail as the
        print's duration would silently *under*-report — worse than reporting nothing,
        because the cost snapshot falls back to the slicer's estimate on a null but will
        happily believe a wrong number.
        """
        if self._start_observed and self._started_monotonic is not None:
            elapsed = int(self._monotonic() - self._started_monotonic)
            if elapsed > 0:
                self._warn_if_sources_disagree(elapsed)
                return elapsed, "bridge"

        if self._started_epoch is not None:
            elapsed = int(self._wall_clock() - self._started_epoch)
            if 0 < elapsed <= _MAX_PLAUSIBLE_PRINT_SECONDS:
                return elapsed, "printer"
            logger.warning("printer %s: gcode_start_time implies an implausible duration "
                           "(%ss) — reporting no duration rather than a wrong one",
                           self._printer_id, elapsed)

        return None, None

    def _warn_if_sources_disagree(self, stopwatch_seconds: int) -> None:
        """Make a stopwatch/printer disagreement visible instead of silent.

        The two should agree to within a poll interval whenever the bridge watched the
        whole print. A material gap means one of the assumptions underneath this is
        wrong — the printer's clock is skewed, or (more likely) the bridge was
        unreachable at the moment the print actually began, so its stopwatch missed the
        head of the run and is under-reporting. Either way it silently understates the
        job's cost, which is precisely what the observed duration exists to fix.

        Logged rather than acted on: which source to believe cannot be settled without a
        real print to check against. Watch this line on the first live one.
        """
        if self._started_epoch is None:
            return
        printer_seconds = int(self._wall_clock() - self._started_epoch)
        if abs(printer_seconds - stopwatch_seconds) > _DURATION_DISAGREEMENT_SECONDS:
            logger.warning(
                "printer %s: print duration sources disagree — bridge stopwatch %ss vs "
                "printer gcode_start_time %ss. Reporting the stopwatch. If the printer's "
                "figure is the right one, the bridge missed the start of this print.",
                self._printer_id, stopwatch_seconds, printer_seconds)


class BambuPrinter:
    """A printer connection plus the state the bridge must keep for it: the merged MQTT
    payload, that payload's liveness, and the running print's stopwatch."""

    def __init__(self, cfg: PrinterConfig, stopwatch: Optional[PrintStopwatch] = None,
                 stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
                 monotonic=time.monotonic, ams_cache_path: Optional[str] = None,
                 sleep=time.sleep, session_factory=None, log_path=None):
        self._cfg = cfg
        # IP is a cache, the serial (bambu_id) is the identity. Seeded from config, then
        # updated by reconnect() when SSDP finds the serial at a new address (U1) — so a
        # DHCP lease change self-heals instead of stranding the printer at a stale IP.
        self._ip = cfg.ip
        self._session = None
        self._session_factory = session_factory or _default_session_factory
        # Merged payload, freshness, cancel latch, and net.info live here. The
        # report loop reads a copy; it does not share the MQTT thread's dict.
        self.state = PrinterState(
            cfg.bambu_id, ams_cache_path=ams_cache_path, monotonic=monotonic,
        )
        self._stopwatch = stopwatch or PrintStopwatch(cfg.bambu_id)
        self._monotonic = monotonic               # injectable — staleness is otherwise untestable
        self._sleep = sleep
        self._stale_after_seconds = stale_after_seconds

        self._offline = False                     # for logging the edge, not every poll
        self._seen_connack_at = None
        # The stopwatch is fed from the MQTT thread and read from snapshot.
        self._stopwatch_lock = threading.Lock()
        self._historical_failed_streak = 0
        self._asked_full_status = False
        self._full_status_attempts = 0
        self._last_gcode_state: Optional[str] = None
        self._ams_cache_path = ams_cache_path
        # The report loop and this printer's worker both call snapshot(). The lock
        # keeps the stopwatch and the report built on one thread at a time.
        self._snapshot_lock = threading.Lock()
        # None: unit tests refresh AMS inline. The fleet sets a worker submit so
        # snapshot itself does not publish or sleep.
        self._defer = None
        self._deferred_pending = False
        # One ring for the life of this object. rebuild_session and reconnect
        # replace the client and keep this log.
        self._log = PrinterLog(
            cfg.bambu_id,
            secrets=(cfg.access_code,) if isinstance(cfg.access_code, str) else (),
            file_path=log_path,
        )
        # Last commands_rejected answer. None until a payload has been merged.
        self._command_acceptance = None

    @property
    def bambu_id(self) -> str:
        return self._cfg.bambu_id

    @property
    def current_ip(self) -> str:
        """The address the bridge is currently dialing — seeded from config, then updated
        by reconnect() when SSDP finds the serial somewhere new (U1)."""
        return self._ip

    @property
    def is_offline(self) -> bool:
        """Whether the last snapshot reported this printer OFFLINE. The fleet reads this
        to decide which printers to re-discover and reconnect (U1)."""
        return self._offline

    @property
    def connection_state(self) -> str:
        """Session state: connecting, live, stale, commands_ignored, or offline."""
        session = self._session
        if session is None:
            return "offline"
        return getattr(session, "state", "offline")

    @property
    def down_reason(self):
        """Why the session is not live. None while it is connecting or live.

        This is the report's ``connect_error``, except a live report forces
        None: a reason left over from the previous gap must not mark a report
        whose ``connection`` is live. One of auth_rejected, refused,
        unreachable, silent_session, commands_ignored.
        """
        session = self._session
        if session is None:
            return None
        return getattr(session, "down_reason", None)

    @property
    def had_session(self) -> bool:
        session = self._session
        if session is None:
            return False
        return bool(getattr(session, "had_session", False))

    @property
    def log(self):
        """MQTT and session ring for this serial. It outlives the client."""
        return self._log

    @property
    def commands_rejected(self):
        """True when the merged HMS list contains MQTT command verification failed.

        False when a payload has been merged and that code is absent. None
        when no payload has been merged — absence is not acceptance. An
        offline report sends None for this field even if a payload is still
        retained; this property keeps answering from the payload, which is
        what the connection check reads. Queries still answer while the code
        is present; ``project_file`` is dropped by the printer.
        """
        payload = self.state.view().get("payload")
        if not isinstance(payload, dict):
            return None
        print_obj = payload.get("print")
        hms = print_obj.get("hms") if isinstance(print_obj, dict) else None
        return hms_commands_rejected(hms)

    def collect_log(self) -> Dict:
        """Both rings, oldest first, for the collect_log upload."""
        return self._log.export()

    def address_candidates(self) -> list:
        """IPs from the last ``print.net.info`` block. A report with no net block leaves them."""
        return self.state.address_candidates()

    def proves_serial_at(self, ip: str) -> bool:
        """True when a temporary session at ``ip`` is this printer.

        The access code stays in this object. Callers that decide whether to
        dial ``ip`` use this instead of reading the secret.
        """
        if not ip:
            return False
        return proves_serial(
            ip, self.bambu_id, self._cfg.access_code, log=self._log,
        )

    def diagnose(self, trigger: str = "operator") -> Dict:
        """Run the connection check against the address this printer is dialing.

        Blocks for up to about half a minute, so callers run it on the printer
        worker. The access code stays inside this object and the check.
        """
        return run_connection_diagnostic(
            self._ip, self.bambu_id, self._cfg.access_code,
            printer=self, trigger=trigger,
        )

    def silent_for(self, now=None):
        """Seconds the session has been quiet, for the fleet backstop."""
        session = self._session
        fn = getattr(session, "silent_for", None) if session is not None else None
        if not callable(fn):
            return None
        return fn(now)

    def rebuild_session(self) -> None:
        """Replace the MQTT client in place.

        The merged payload, the stopwatch, and the cancel latch belong to this
        object. A same-IP recovery must not construct a new printer to get a
        new client.
        """
        session = self._session
        if session is None:
            return
        session.hard_reset()

    def connect(self) -> None:
        self._connect(self._ip)

    def _make_session(self, ip: str):
        session = self._session_factory(
            ip, self._cfg.access_code, self._cfg.bambu_id, self._on_mqtt_report,
        )
        # Bound before start() so the first connect event lands in the ring.
        bind = getattr(session, "set_log", None)
        if callable(bind):
            bind(self._log)
        return session

    def _connect(self, ip: str) -> None:
        session = self._make_session(ip)
        # connect_async returns before the broker answers. Commit the address only
        # after that start did not raise, so a failed dial leaves current_ip alone
        # and reconcile_connections retries instead of treating a client that never
        # started as an in-progress paho retry (U1).
        session.start()
        self._session = session
        self._ip = ip
        logger.info("connected to printer %s (%s) at %s", self.bambu_id, self._cfg.name, ip)
        self._asked_full_status = False
        self._full_status_attempts = 0
        self._request_ams_if_needed()

    def reconnect(self, new_ip: Optional[str] = None) -> None:
        """Rebuild the MQTT client, optionally at a new IP after the printer's DHCP lease
        moved (U1). Closes the old client best-effort, drops the merged payload and its
        freshness baseline (they described the old address), then connects fresh. The next
        snapshot rebuilds live state and flips the printer back online on its own — so this
        does not reset the `_offline` flag, leaving snapshot() to log the real recovery.

        `current_ip` advances only on a successful connect (see `_connect`): a reconnect
        that can't reach the new address leaves the printer targeting the old one, so the
        next reconcile retries rather than silently stranding it."""
        target_ip = new_ip or self._ip
        self.disconnect()
        self.state.clear()
        # A new client is a new session even before its CONNACK. Queued
        # lifecycle events stay; the payload drop is not an ack.
        self.state.new_session()
        self._seen_connack_at = None
        self._historical_failed_streak = 0
        self._last_gcode_state = None
        self._connect(target_ip)

    def disconnect(self) -> None:
        """Best-effort close of the MQTT session (used by reconnect and fleet removal).
        Never raises — a printer being torn down must not take the loop down with it."""
        session = self._session
        self._session = None
        self._asked_full_status = False
        self._full_status_attempts = 0
        if session is None:
            return
        try:
            session.disconnect()
        except Exception as e:
            logger.debug("printer %s: closing the MQTT session raised (%s)",
                         self.bambu_id, type(e).__name__)

    def set_defer(self, defer) -> None:
        """Hand AMS refresh to ``defer`` instead of running it inside snapshot.

        ``pushall`` plus the absorb window sleeps. On the report loop that stalls
        every other printer. Unit tests leave this unset and still refresh inline.
        """
        self._defer = defer

    def upload_and_start(self, file_path: str, ams_mapping, plate_number: int = 1,
                         remote_name: Optional[str] = None, cancel=None) -> bool:
        """FTPS-upload the sliced `.3mf` and MQTT-start it (U9's dispatch primitive).

        `ams_mapping` is the **explicit** filament→AMS-tray mapping (R11), a `list[int]`
        of global 0-based tray indices in the sliced file's filament order — the
        deterministic override for the auto-map-by-color hang. The router computes it
        from the printer's own live slot colors (see Dispatcher._ams_mapping).

        **Load-bearing, verify on the first real prints (R-B):** this assumes the sliced
        file's filament order matches the batch's `required_colors` order. Bambu also
        auto-maps by color at start (U1 GO / KTD7), so a correct color *set* prints right
        even if the order is off — but a wrong explicit mapping could misroute a slot.
        Watch the first prints; if the order is wrong, THIS is the single place to fix.

        Raises on a transport error (bad FTPS, dropped MQTT) so the router
        re-queues the job rather than losing it; returns the printer's start result
        otherwise.
        """
        name = self.upload_file(file_path, remote_name=remote_name, cancel=cancel)
        return self.start_print(name, ams_mapping, plate_number)

    def upload_file(self, file_path: str, remote_name: Optional[str] = None,
                    cancel=None) -> str:
        """FTPS-upload only. Split from start so a live stop can abort after the push.

        ``cancel`` is the printer worker's event. The FTPS loop checks it between
        blocks so removing the printer does not wait out the file.
        """
        if self._session is None:
            raise RuntimeError("printer not connected")
        name = remote_name or os.path.basename(file_path)
        store_on_printer(self._ip, self._cfg.access_code, file_path, name, cancel=cancel)
        logger.info("printer %s: uploaded %s", self.bambu_id, name)
        return name

    def start_print(self, remote_name: str, ams_mapping, plate_number: int = 1) -> bool:
        """MQTT-start a file already on the printer. A True return is not an ack."""
        if self._session is None:
            raise RuntimeError("printer not connected")
        url = lan_start_url(remote_name)
        started = self._publish_command({
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": f"Metadata/plate_{int(plate_number)}.gcode",
                "url": url,
                "subtask_name": remote_name,
                "use_ams": True,
                "ams_mapping": list(ams_mapping),
            },
        })
        logger.info("printer %s: started %s (plate %s, ams_mapping=%s, url=%s) -> %s",
                    self.bambu_id, remote_name, plate_number, list(ams_mapping),
                    url, started)
        return bool(started)

    def pause_print(self) -> bool:
        """Publish pause. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("pause_print")

    def resume_print(self) -> bool:
        """Publish resume. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("resume_print")

    def stop_print(self) -> bool:
        """Publish stop. True is not an ack — confirm via the next gcode_state."""
        return self._mqtt_command("stop_print")

    def request_full_status(self, *, read_idle_rfid: bool = True) -> bool:
        """Ask the printer for a full MQTT dump so AMS trays land in the next snapshot.

        P1-series printers only send AMS on `pushing.pushall`, not on the incremental
        reports the poll already reads. True is not an ack — the next `snapshot()`
        that carries a `slots` list is the confirmation. Automatic pushall
        (connect, snapshot, print-end) and Refresh both send `ams_get_rfid`
        when loaded trays still have no hex.
        """
        if self._session is None:
            raise RuntimeError("printer not connected")
        result = self._publish_command({
            "pushing": {"sequence_id": "0", "command": "pushall"},
        })
        logger.info("printer %s: pushall -> %s", self.bambu_id, result)
        if result:
            self._absorb_status_after_pushall(read_idle_rfid=read_idle_rfid)
        return bool(result)

    def _absorb_status_after_pushall(self, *, read_idle_rfid: bool = False) -> None:
        """Merge the pushall window, then ask idle trays to read RFID if still blank.

        A P1 delta can replace `print.ams` before the next poll. Reports are
        merged on the MQTT callback; this loop only waits for those merges and
        for `ams_get_rfid` replies.
        """
        if self._absorb_until_complete():
            return
        if read_idle_rfid and self._request_idle_rfid():
            self._absorb_until_complete()

    def _absorb_until_complete(self) -> bool:
        for i in range(_ABSORB_SAMPLES):
            if i:
                self._sleep(_ABSORB_SAMPLE_SECONDS)
            if self._ams_complete():
                return True
        return self._ams_complete()

    def _ams_complete(self) -> bool:
        payload = self.state.view()["payload"] or {}
        return not ams_needs_pushall(payload)

    def register_submission(self, submission_id) -> None:
        """Remember a Link submission id for origin matching on this printer."""
        self.state.register_submission(submission_id)

    def ack_events(self, ids) -> None:
        """Drop lifecycle events included in a report POST the cloud accepted."""
        self.state.ack_events(ids)

    def emit_recovered(self, kind, submission_id) -> None:
        """Queue an unobserved link finish. See ``PrinterState.emit_recovered``."""
        self.state.emit_recovered(kind, submission_id)

    def _on_mqtt_report(self, doc) -> None:
        """Ingest one report. Runs on the paho network thread."""
        self._note_session_boundary()
        self.state.ingest(doc, self._monotonic())
        self._observe_stopwatch()
        self._note_command_acceptance()

    def _note_command_acceptance(self) -> None:
        """Log when the printer starts or stops refusing commands.

        The first merged payload that is not refusing is not an edge: there
        was no previous answer. A later report whose merged ``hms`` no longer
        contains the code is ``commands_accepted``.
        """
        current = self.commands_rejected
        if not isinstance(current, bool):
            return
        previous = self._command_acceptance
        self._command_acceptance = current
        if previous is current or (previous is None and current is False):
            return
        log = self._log
        if log is None:
            return
        kind = "commands_rejected" if current else "commands_accepted"
        try:
            log.record_event(kind)
        except Exception:
            logger.debug("printer %s: command acceptance was not recorded", self.bambu_id)

    def _note_session_boundary(self) -> None:
        """A new CONNACK forgets previous-state knowledge, not queued events.

        Compared here because the session object is replaced on reconnect and
        on an in-place rebuild. The first report after that CONNACK is a
        first push.
        """
        session = self._session
        connack_at = getattr(session, "connack_at", None) if session is not None else None
        if connack_at is None or connack_at == self._seen_connack_at:
            return
        self._seen_connack_at = connack_at
        self.state.new_session()

    def _observe_stopwatch(self) -> None:
        """Feed the stopwatch from the same merged state the edge tracker saw.

        Snapshot does not observe. A poll that builds a stale or offline
        report must not advance the clock, and a second observe of the state
        already taken on the MQTT thread would measure the gap between the
        message and the poll instead of the print.
        """
        sample = self.state.stopwatch_sample()
        if sample is None:
            return
        gcode_state, gcode_start_time = sample
        with self._stopwatch_lock:
            self._stopwatch.observe(gcode_state, {"gcode_start_time": gcode_start_time})

    def _stopwatch_reading(self):
        with self._stopwatch_lock:
            return self._stopwatch.duration_seconds, self._stopwatch.source

    def _publish_command(self, payload: dict) -> bool:
        session = self._session
        accepted = False
        if session is not None:
            try:
                accepted = bool(session.publish(payload))
            except Exception:
                accepted = False
        self._record_command(payload, accepted)
        return accepted

    def _record_command(self, payload, accepted: bool) -> None:
        log = self._log
        if log is None:
            return
        try:
            log.record_event(
                "command",
                name=_mqtt_command_name(payload),
                accepted=bool(accepted),
            )
        except Exception:
            logger.debug("printer %s: command was not recorded", self.bambu_id)

    def _request_idle_rfid(self) -> bool:
        """`ams_get_rfid` is the printer command HA uses to read one P1 tray."""
        trays = list(idle_trays_needing_rfid(self.state.view()["payload"] or {}))
        if not trays:
            return False
        asked = False
        for ams_id, slot_id in trays:
            ok = self._publish_command({
                "print": {
                    "sequence_id": "0",
                    "command": "ams_get_rfid",
                    "ams_id": ams_id,
                    "slot_id": slot_id,
                },
            })
            if ok:
                asked = True
                logger.info(
                    "printer %s: ams_get_rfid ams=%s slot=%s",
                    self.bambu_id, ams_id, slot_id,
                )
        return asked

    def _request_ams_if_needed(self) -> None:
        """Ask for a full dump after connect, and again while loaded trays have no hex.

        `pushall` alone does not read idle P1 RFID. When the dump still has
        bit-present trays with no hex, follow it with `ams_get_rfid` the same
        way Refresh does. Print-end resets the attempt budget so a Refresh
        that ran during the job can try again once the printer is idle.
        """
        if self._full_status_attempts >= _MAX_FULL_STATUS_ATTEMPTS:
            return
        try:
            if self.request_full_status(read_idle_rfid=True):
                self._full_status_attempts += 1
                self._asked_full_status = True
        except Exception:
            logger.info("printer %s: full AMS dump not available yet", self.bambu_id)

    def _remember_ams(self) -> None:
        payload = self.state.view()["payload"]
        if not isinstance(payload, dict):
            return
        print_obj = payload.get("print")
        ams = print_obj.get("ams") if isinstance(print_obj, dict) else None
        if ams_has_color(ams):
            save_remembered_ams(self._ams_cache_path, self.bambu_id, ams)

    def retry_filament_action(self) -> bool:
        """Retry a halted AMS / load / runout action, then the caller resumes."""
        return self._mqtt_command("retry_filament_action")

    def resume_from_stage(self, stage: Optional[int] = None) -> bool:
        """Cloud sent `resume`. Pick MQTT from live stg_cur (KTD6)."""
        if stage is None:
            try:
                stage = self.snapshot().get("stage")
            except Exception:
                stage = None
        if stage in _FILAMENT_RETRY_STAGES:
            self.retry_filament_action()
        return self.resume_print()

    def _mqtt_command(self, method_name: str) -> bool:
        if self._session is None:
            raise RuntimeError("printer not connected")
        payload = _MQTT_COMMANDS.get(method_name)
        if payload is None:
            raise RuntimeError(f"printer client has no {method_name}()")
        result = self._publish_command(payload)
        logger.info("printer %s: %s -> %s", self.bambu_id, method_name, result)
        return bool(result)

    def snapshot(self) -> Dict:
        """One state report for this printer (never raises):

            {
              "bambu_id": str,
              "status": IDLE | PRINTING | PAUSED | NEEDS_CLEARING | ERROR | OFFLINE,
              "slots": [{slot_number, color_hex, filament_type}] | None,  # see below
              <the telemetry fields, flat>,                        # see parse_telemetry
              "gcode_state": str | None,                            # bounded firmware state
              "hms_present": bool, "hms_empty": bool,
              "has_active_file": bool, "has_active_task": bool,
              "has_active_project": bool,                           # ids are never exposed
              "stage_queue_empty": bool | None, "print_type": str | None,
              "historical_failed_ready": bool,                      # separate from status
              "print_duration_seconds": int | None,                # observed, not estimated
              "print_duration_source": "bridge" | "printer" | None,
              "events": [lifecycle event, ...],                    # pending until the POST acks
              "print_origin": "link" | "external" | None,          # None when idle or unknown
              "print_submission_id": str | None,                   # matched Link id; None offline
              "session_seq": int,                                  # bumps on each CONNACK
              "session_gcode_seen": bool,                          # this session carried gcode_state
              "local_ip": str | None,                               # address currently dialed
              "connection": "live" | "stale" | "offline",
              "last_message_age_seconds": float | None,             # None if no report yet
              "connect_error": str | None,                          # session down_reason; None when live
              "session_started_at": str | None,                     # ISO-8601 UTC of this CONNACK
            }

        The telemetry is **flat on the report, not nested** — that is what
        `bridge_state_service.ingest_printer_state` reads
        (`{bambu_id, status, slots, plus the telemetry fields}`). `status` keeps
        today's mapping, so an older ingest keeps working. `connection` and the
        three stamps beside it are additive; unknown keys are ignored on the far side.

        **`connection` and `status` are different facts.** `live` means the socket
        is up and a report on this session arrived within `stale_after_seconds`.
        `status` is then `map_status` of `gcode_state`, unchanged. `stale` means
        a merged payload exists but that report is not live: the socket is up and
        quiet past the window, the session is `stale` / `commands_ignored` /
        `connecting`, or the socket has been down only long enough that the
        session has not yet called it unreachable. `status` on that report stays
        OFFLINE — IDLE is still the only authorization for dispatch — and the
        telemetry and `slots` are the last merged payload, not a fresh reading.
        `offline` means there is no merged payload, or the session is `offline`
        (socket down past its unreachable window, `unreachable`, `auth_rejected`,
        or `refused` with nothing recent). Telemetry is null and `slots` is None.

        **`slots` is None in two different cases, and `[]` is neither of them.**
        None means "no AMS information": the printer has not reported a unit list
        yet, or this report is `offline` and is not claiming to see the AMS.
        `[]` means the printer said the AMS has no units. An offline report that
        sent `[]` would make the cloud delete every slot row. See `parse_ams`.

        `hms_severity`, `hms_code`, `hms_count`, `hms_title`, `hms_detail`, and
        `print_error` are the legacy HMS fields. Severity 0, and a `print_error`
        whose low 16 bits are below 0x4000, are dropped. Cancel echoes stay in
        those fields (`0300_400C`, `0500_400E`, print_error `50348044`) until
        3DPF reads `hms_faults`, `user_cancelled`, or the lifecycle events.

        `hms_faults`, `fault_print_error`, and `commands_rejected` are additive.
        `hms_faults` is real faults only (severity 0 and cancel echoes removed),
        worst first, at most 10, each `{"code", "severity"}` with `code` all
        16 hex digits. `fault_print_error` is `print_error` with cancel codes
        removed too. `commands_rejected` is true when HMS contains
        `0500050000010007`. On a live or stale report these are a reading of
        the merged payload (`hms_faults: []` and `commands_rejected: false`
        when nothing is wrong). On an offline report all three are None: no
        information, same rule as `slots: None`.

        A message from before this session's CONNACK is not `live`. Replaying it
        as a fresh print is how a printer that just reconnected looked busy, or
        IDLE, on data from the previous connection. The merged payload is kept
        either way, so the next report merges onto it; `reconnect()` is what
        drops that payload, because an address change is a different machine
        until the serial is proved again.

        This method does not publish and does not sleep: bringing the socket
        back is the session watchdog and the fleet backstop. A false OFFLINE
        costs a poll of dispatch (visible, and fails closed); a false IDLE
        costs a print.
        """
        with self._snapshot_lock:
            return self._snapshot_impl()

    def _snapshot_impl(self) -> Dict:
        try:
            connected = self._is_connected()
        except Exception as e:
            # A session that cannot answer is unreachable. This is not how a
            # printer normally dies — the link flag is — and it is the only
            # exception read as "unreachable".
            return self._go_offline(f"unreadable ({type(e).__name__})")

        view = self.state.view()
        label = self._connection_label(view, connected=connected)
        if label != "live":
            reason = self._offline_reason(view, connected=connected)
            if label == "stale":
                self._note_offline(reason)
                try:
                    return self._stale_snapshot(view)
                except Exception:
                    logger.exception(
                        "printer %s: parsing its telemetry raised — this is a BRIDGE BUG, not an "
                        "unreachable printer. Reporting OFFLINE so nothing dispatches to it.",
                        self.bambu_id)
                    return self._offline_snapshot(view)
            return self._go_offline(reason, view)

        if self._offline:
            # Covers both a recovery and the first payload after a bridge start.
            logger.info("printer %s: online — reporting live telemetry", self.bambu_id)
            self._offline = False

        try:
            payload = view.get("payload") or {}
            gcode_state = None
            print_obj = payload.get("print")
            if isinstance(print_obj, dict):
                raw_state = print_obj.get("gcode_state")
                if isinstance(raw_state, str):
                    gcode_state = raw_state.strip().upper()
            if (
                self._last_gcode_state in _PRINT_IN_PROGRESS
                and gcode_state in (_PRINT_ENDED | {"IDLE", "PAUSE"})
            ):
                self._full_status_attempts = 0
            self._last_gcode_state = gcode_state
            if ams_needs_pushall(payload):
                # Inline only when no worker is wired. Otherwise this publishes
                # pushall and sleeps in the absorb window, on the report loop.
                # Those reports merge before this snapshot is built, so the
                # copy taken above is stale and has to be read again.
                self._defer_or_run(self._request_ams_if_needed)
                view = self.state.view()
                payload = view.get("payload") or {}
            self._remember_ams()
            return self._build_snapshot(payload, fresh=self.state.take_fresh(), view=view)
        except Exception:
            # NOT an unreachable printer — a bug in the bridge's own parsing. Letting it
            # pass for one would silently delete a *live* printer from the UI, leaving a
            # single "unreadable" line as the only trace. Loud, with a traceback, on
            # every poll it happens: an OFFLINE printer that logs ERROR is a bridge bug,
            # an OFFLINE printer that logs WARNING is an absent printer.
            logger.exception(
                "printer %s: parsing its telemetry raised — this is a BRIDGE BUG, not an "
                "unreachable printer. Reporting OFFLINE so nothing dispatches to it.",
                self.bambu_id)
            return self._offline_snapshot(view)

    def _defer_or_run(self, fn) -> None:
        """One deferred refresh at a time. Every snapshot would otherwise queue
        another pushall behind a long upload and fill that printer's queue."""
        defer = self._defer
        if defer is None:
            fn()
            return
        if self._deferred_pending:
            return
        self._deferred_pending = True

        def _run():
            try:
                fn()
            finally:
                self._deferred_pending = False

        future = defer(_run)
        if future is None or future.done():
            self._deferred_pending = False

    def _connection_label(self, view: Dict, *, connected: bool) -> str:
        """``live``, ``stale``, or ``offline``. Separate from ``status``.

        ``live`` requires a report on *this* session inside the same window that
        makes ``status`` OFFLINE when it expires. A CONNACK with no report yet
        is not live: the retained payload belongs to the previous client.

        A session that does not publish ``state`` is judged by the socket. An
        up socket is treated as live-or-stale; a down socket is offline. The
        "down for at most 60s" stale window is the session keeping a non-offline
        state until it marks the socket unreachable — this method does not run
        a second timer.
        """
        payload = view.get("payload") if isinstance(view, dict) else None
        has_state = isinstance(payload, dict) and bool(payload)
        message_at = view.get("last_message_monotonic") if isinstance(view, dict) else None
        in_window = False
        if message_at is not None:
            in_window = (self._monotonic() - message_at) <= self._stale_after_seconds
        if connected and has_state and in_window and self._message_in_current_session(message_at):
            return "live"
        if not has_state:
            return "offline"
        session_state = self._reported_session_state(connected)
        reason = self.down_reason
        if session_state == "offline" or reason in ("unreachable", "auth_rejected"):
            return "offline"
        if reason == "refused" and not in_window:
            return "offline"
        return "stale"

    def _reported_session_state(self, connected: bool) -> str:
        session = self._session
        if session is None:
            return "offline"
        state = getattr(session, "state", None)
        if isinstance(state, str) and state:
            return state
        return "live" if connected else "offline"

    def _message_in_current_session(self, message_at) -> bool:
        """True when ``message_at`` is at or after this client's CONNACK.

        No CONNACK stamp means there is no session boundary to enforce. A real
        session sets one before any report can arrive; a stand-in that never
        handshakes has nothing older to exclude.
        """
        if message_at is None:
            return False
        session = self._session
        if session is None:
            return False
        connack_at = getattr(session, "connack_at", None)
        if connack_at is None:
            return True
        return message_at >= connack_at

    def _offline_reason(self, view: Dict, *, connected: bool) -> str:
        if not connected:
            return "MQTT link is down"
        payload = view.get("payload") if isinstance(view, dict) else None
        message_at = view.get("last_message_monotonic") if isinstance(view, dict) else None
        if not payload or message_at is None:
            return "no MQTT payload received yet"
        if not self._message_in_current_session(message_at):
            return "no report in this session yet"
        silent_for = self._monotonic() - message_at
        return (
            f"nothing new for {silent_for:.0f}s (> {self._stale_after_seconds:.0f}s) — "
            f"the printer is gone, or the MQTT session is wedged"
        )

    def _note_offline(self, reason: str) -> None:
        """Log the edge into OFFLINE. The merged payload stays.

        Logged once, not every poll: a printer that has been unplugged for a
        week should not emit a warning every 15 seconds forever. The fresh
        edge is discarded so the first live snapshot after the gap is not
        credited with a change that happened before it.
        """
        if not self._offline:
            logger.warning("printer %s -> OFFLINE: %s", self.bambu_id, reason)
            self._offline = True
        self._historical_failed_streak = 0
        self.state.discard_fresh()

    def _go_offline(self, reason: str, view: Optional[Dict] = None) -> Dict:
        """Report the null telemetry shape. Does not drop the merged payload.

        Dropping it made the next report look like a first contact and threw
        away trays the printer had already described. ``reconnect()`` still
        clears the payload when the address changes.
        """
        self._note_offline(reason)
        return self._offline_snapshot(view)

    def _lifecycle_fields(self, view: Optional[Dict], *, connection: str) -> Dict:
        """Pending edges, and who owns the print currently on the machine.

        ``events`` is a copy of the unacked queue. The same ids are sent
        again until ``ack_events`` drops them. ``print_origin`` is None when
        the merged state is idle or this session has not seen a print state.

        ``print_submission_id`` is the registered Link id that matched
        ``subtask_id`` or ``task_id``. The firmware ids themselves stay off
        the report. An offline report has no current match, so the field is
        None there even if the retained payload would still classify.
        ``session_seq`` identifies the CONNACK. ``session_gcode_seen`` is
        false until a report in this session included ``gcode_state``.
        """
        events = view.get("events") if isinstance(view, dict) else None
        if not isinstance(events, list):
            events = []
        origin = view.get("print_origin") if isinstance(view, dict) else None
        if origin not in ("link", "external"):
            origin = None
        seq = view.get("session_seq") if isinstance(view, dict) else 0
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = 0
        seen = isinstance(view, dict) and view.get("session_gcode_seen") is True
        matched = view.get("print_submission_id") if isinstance(view, dict) else None
        if connection == "offline" or not isinstance(matched, str) or not matched.strip():
            matched = None
        else:
            matched = matched.strip()
        return {
            "events": events,
            "print_origin": origin,
            "print_submission_id": matched,
            "session_seq": seq,
            "session_gcode_seen": seen,
        }

    def _contract_fields(self, view: Optional[Dict], connection: str) -> Dict:
        message_at = view.get("last_message_monotonic") if isinstance(view, dict) else None
        age = None
        if message_at is not None:
            age = round(max(0.0, self._monotonic() - message_at), 1)
        session = self._session
        started = getattr(session, "session_started_at", None) if session is not None else None
        if started is not None and not isinstance(started, str):
            started = None
        return {
            "connection": connection,
            "last_message_age_seconds": age,
            "connect_error": None if connection == "live" else self.down_reason,
            "session_started_at": started,
        }

    def _build_snapshot(self, payload: Dict, *, fresh: bool, view: Dict) -> Dict:
        print_obj = payload.get("print")
        if not isinstance(print_obj, dict):
            print_obj = {}
        gcode_state = print_obj.get("gcode_state")
        duration_seconds, duration_source = self._stopwatch_reading()
        telemetry = parse_telemetry(payload)
        if fresh and _is_historical_failed_candidate(print_obj, telemetry):
            self._historical_failed_streak += 1
        else:
            # Duplicate/non-fresh reports and every disqualifier break consecutiveness.
            self._historical_failed_streak = 0
        user_cancelled = bool(view.get("user_cancelled"))
        status = map_status(
            gcode_state if isinstance(gcode_state, str) else None,
            print_error=telemetry.get("print_error"),
            hms_code=telemetry.get("hms_code"),
            hms=print_obj.get("hms"),
            user_cancelled=user_cancelled,
        )
        if status == "ERROR" and _is_leftover_idle_failed(print_obj, telemetry):
            status = "IDLE"
        status = promote_live_idle(status, print_obj)
        report = {
            "bambu_id": self.bambu_id,
            "status": status,
            # None — not [] — while this printer's payload carries no AMS unit list,
            # which is its normal state between connecting and the first full push. The
            # cloud DELETES slot rows to match a reported list, so an `[]` here wipes the
            # trays of a live machine that simply has not been asked yet. See `parse_ams`.
            "slots": parse_ams(payload),
            **telemetry,   # flat, not nested — see snapshot()
            # Leftover idle FAILED is remapped to IDLE above. This stricter
            # signal still lets the cloud check assignment state on dumps that
            # do not meet leftover-idle (file leftover, heat, HMS).
            "historical_failed_ready": self._historical_failed_streak >= 2,
            "user_cancelled": user_cancelled,
            "print_duration_seconds": duration_seconds,
            "print_duration_source": duration_source,
            "local_ip": self._ip or None,
        }
        report.update(self._lifecycle_fields(view, connection="live"))
        report.update(self._contract_fields(view, "live"))
        return report

    def _stale_snapshot(self, view: Dict) -> Dict:
        """Last telemetry, status OFFLINE, ``connection: stale``.

        Status stays OFFLINE so an older ingest still refuses to dispatch.
        The stopwatch is not observed: a quiet printer must not keep a print
        clock running. ``slots`` is ``parse_ams`` of the retained payload,
        which is None when that payload never carried a unit list and a real
        list when it did — not ``[]`` standing in for "we are not looking".
        """
        payload = view.get("payload") or {}
        duration_seconds, duration_source = self._stopwatch_reading()
        report = {
            "bambu_id": self.bambu_id,
            "status": "OFFLINE",
            "slots": parse_ams(payload),
            **parse_telemetry(payload),
            "historical_failed_ready": False,
            "user_cancelled": bool(view.get("user_cancelled")),
            "print_duration_seconds": duration_seconds,
            "print_duration_source": duration_source,
            "local_ip": self._ip or None,
        }
        report.update(self._lifecycle_fields(view, connection="stale"))
        report.update(self._contract_fields(view, "stale"))
        return report

    def _offline_snapshot(self, view: Optional[Dict] = None) -> Dict:
        """Null telemetry and ``slots: None``.

        ``[]`` would tell the cloud the AMS is empty and delete every slot
        row. None means this report is not an AMS reading. The merged payload
        is left in ``PrinterState`` for the next message to merge onto.
        """
        if view is None:
            view = self.state.view()
        self._historical_failed_streak = 0
        report = {
            "bambu_id": self.bambu_id,
            "status": "OFFLINE",
            "slots": None,
            **parse_telemetry(None),
            "historical_failed_ready": False,
            "user_cancelled": False,
            "print_duration_seconds": None,
            "print_duration_source": None,
            "local_ip": self._ip or None,
            # No HMS reading. None, not [] — same rule as slots.
            "hms_faults": None,
            "fault_print_error": None,
            "commands_rejected": None,
        }
        report.update(self._lifecycle_fields(view, connection="offline"))
        report.update(self._contract_fields(view, "offline"))
        return report

    def _is_connected(self) -> bool:
        """Is the MQTT session to this printer actually up?

        In LAN-only mode the printer runs the broker, so the session keepalive
        is a liveness check on the machine. Unplug it and the session goes
        False while the merged payload is still in ``state``.
        """
        session = self._session
        if session is None:
            return False
        return bool(session.connected)


def _minutes_to_seconds(value) -> Optional[int]:
    """`mc_remaining_time` is in MINUTES, and everything downstream of the bridge speaks
    seconds — so it is converted once, here, at the boundary.

    Callers that treat the field as seconds are wrong by 60x — ha-bambulab reads
    it as `timedelta(minutes=...)`. Getting this wrong is a silent 60x error on
    the single number the operator looks at most.
    """
    minutes = as_int(value, None)
    if minutes is None or minutes < 0:
        return None
    return minutes * 60


def _valid_epoch(value) -> Optional[int]:
    """`gcode_start_time` is Unix epoch seconds, and a string on the wire. Printers
    that never set it report "0", which would otherwise measure a print as having
    started in 1970."""
    epoch = as_int(value, None)
    if epoch is None or epoch < _MIN_PLAUSIBLE_EPOCH:
        return None
    return epoch


def _print_error_str(value) -> Optional[str]:
    """0 and a low word below 0x4000 are not faults.

    Cancel codes stay in this field. 3DPF still detects a user cancel from
    ``50348044`` / ``0300400C`` here until it reads ``hms_faults``.
    """
    return reported_print_error(value)


def _valid_stage(value) -> Optional[int]:
    """`stg_cur` is Bambu's print stage. Idle sentinels are not a pause reason.

    X1 sends -1. P1 sends 255. Both mean "no stage." Persisted verbatim they
    become a fake stage in `printer_telemetry.stage` (a TEXT column): every idle
    P1 would look paused. Nulled here, at the same boundary where a negative
    ETA and a zero start time become None.
    """
    stage = as_int(value, None)
    if stage is None or stage < 0 or stage == 255:
        return None
    return stage
