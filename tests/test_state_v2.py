"""Printer State v2: one test per rule in bridge/state_v2.py.

Each case is a merged Bambu ``print`` object as Link holds it. Cases named
after a shop printer (P1S-8, P1S-11) are field lessons that used to live as
patches in the 3DPF cloud; they now live here, once.
"""
from bridge.bambu.models import P1_PROFILE, profile_for
from bridge.state_v2 import STAGE_LABELS, build_state_v2

P1S = profile_for("C12", serial="S")
X1C = profile_for("BL-P001", serial="S")


def _v2(print_obj=None, *, connection="live", down_reason=None, profile=P1S,
        code="C12", known=True, user_cancelled=False):
    payload = None if print_obj is None else {"print": print_obj}
    return build_state_v2(
        payload, connection=connection, down_reason=down_reason, profile=profile,
        model_code=code, known_model=known, user_cancelled=user_cancelled, state_seq=7,
    )


# --- activity ----------------------------------------------------------------

def test_idle():
    v2 = _v2({"gcode_state": "IDLE", "mc_percent": 0})
    assert v2["activity"] == "idle"
    assert v2["job"] is None
    assert v2["stuck_job"] is False


def test_prepare_and_slicing_are_preparing():
    assert _v2({"gcode_state": "PREPARE"})["activity"] == "preparing"
    assert _v2({"gcode_state": "SLICING"})["activity"] == "preparing"


def test_running_is_printing():
    v2 = _v2({"gcode_state": "RUNNING", "mc_percent": 42, "layer_num": 30, "stg_cur": 0,
              "subtask_name": "Widget", "gcode_file": "/data/Metadata/plate_2.gcode",
              "mc_remaining_time": 12, "total_layer_num": 120})
    assert v2["activity"] == "printing"
    assert v2["stage"] == {"code": 0, "label": "Printing"}
    assert v2["job"]["name"] == "Widget"
    assert v2["job"]["plate"] == 2
    assert v2["job"]["progress"] == 42
    assert v2["job"]["remaining_s"] == 720
    assert v2["job"]["total_layers"] == 120
    assert v2["job"]["outcome"] is None


def test_running_in_a_prep_stage_at_zero_is_preparing():
    v2 = _v2({"gcode_state": "RUNNING", "mc_percent": 0, "layer_num": 0, "stg_cur": 1,
              "subtask_name": "Widget", "nozzle_target_temper": 220})
    assert v2["activity"] == "preparing"
    assert v2["stage"]["label"] == "Auto bed leveling"


def test_pause_is_paused_with_a_reason():
    cases = {6: "filament_runout", 16: "user", 17: "door_open", 26: "ams",
             34: "first_layer", 35: "nozzle_clog", 30: "gcode", 20: "hardware"}
    for stage, reason in cases.items():
        v2 = _v2({"gcode_state": "PAUSE", "stg_cur": stage, "mc_percent": 50})
        assert v2["activity"] == "paused", stage
        assert v2["pause_reason"] == reason, stage


def test_pause_with_an_unmapped_stage_and_an_error_is_error():
    v2 = _v2({"gcode_state": "PAUSE", "stg_cur": 255,
              "hms": [{"attr": 0x03000100, "code": 0x00010007}]})
    assert v2["pause_reason"] == "error"


def test_pause_reason_is_only_set_while_paused():
    assert _v2({"gcode_state": "RUNNING", "stg_cur": 6, "mc_percent": 5})["pause_reason"] is None


def test_finish_is_ended_finished_not_needs_clearing():
    v2 = _v2({"gcode_state": "FINISH", "mc_percent": 100, "subtask_name": "Widget"})
    assert v2["activity"] == "ended"
    assert v2["job"]["outcome"] == "finished"


def test_failed_is_ended_failed():
    v2 = _v2({"gcode_state": "FAILED", "mc_percent": 30, "subtask_name": "Widget",
              "print_error": 0x0300_8004})
    assert v2["activity"] == "ended"
    assert v2["job"]["outcome"] == "failed"


def test_a_cancel_is_ended_cancelled_not_idle():
    by_code = _v2({"gcode_state": "FAILED", "subtask_name": "W", "print_error": 50348044})
    by_latch = _v2({"gcode_state": "FAILED", "subtask_name": "W"}, user_cancelled=True)
    for v2 in (by_code, by_latch):
        assert v2["activity"] == "ended"
        assert v2["job"]["outcome"] == "cancelled"
    # A cancel is not a printer error.
    assert by_code["errors"] == []


def test_leftover_failed_on_a_cold_printer_is_ended_without_errors():
    # P1S-11 (2026-09-22): sticky FAILED, stg_cur 0, no file, cold targets.
    v2 = _v2({"gcode_state": "FAILED", "stg_cur": 0, "mc_percent": 0,
              "nozzle_target_temper": 0, "bed_target_temper": 0, "hms": []})
    assert v2["activity"] == "ended"
    assert v2["errors"] == []
    assert v2["stage"] == {"code": None, "label": None}


def test_unknown_or_missing_state_is_unknown_never_idle():
    assert _v2({"gcode_state": "WEIRD"})["activity"] == "unknown"
    assert _v2({"mc_percent": 3})["activity"] == "unknown"
    assert _v2(None)["activity"] == "unknown"


# --- IDLE while heating (field lessons) -------------------------------------

def test_idle_heating_for_a_file_is_preparing():
    v2 = _v2({"gcode_state": "IDLE", "mc_percent": 0, "nozzle_target_temper": 220,
              "bed_target_temper": 55, "subtask_name": "Widget"})
    assert v2["activity"] == "preparing"


def test_idle_mid_progress_is_printing():
    assert _v2({"gcode_state": "IDLE", "mc_percent": 37})["activity"] == "printing"


def test_idle_at_100_stays_idle():
    v2 = _v2({"gcode_state": "IDLE", "mc_percent": 100, "nozzle_target_temper": 220,
              "subtask_name": "Widget"})
    assert v2["activity"] == "idle"


def test_idle_with_a_failed_start_is_not_heating():
    # P1S-8 (2026-09-24): IDLE, 0%, a 38°C target, the file name, a print_error
    # and a SERIOUS HMS. That is a failed start, not a heat-up.
    v2 = _v2({"gcode_state": "IDLE", "mc_percent": 0, "nozzle_target_temper": 38,
              "subtask_name": "Widget", "print_error": 0x0300_8004})
    assert v2["activity"] == "idle"


def test_idle_heating_with_no_file_is_idle():
    assert _v2({"gcode_state": "IDLE", "nozzle_target_temper": 220})["activity"] == "idle"


# --- stuck RUNNING (was the cloud "ghost") ----------------------------------

_STUCK = {"gcode_state": "RUNNING", "mc_percent": 0, "stg": [], "nozzle_target_temper": 0,
          "cooling_fan_speed": "0", "subtask_name": "old"}


def test_cold_running_at_zero_with_nothing_queued_is_a_stuck_job():
    v2 = _v2(dict(_STUCK))
    assert v2["activity"] == "idle"
    assert v2["stuck_job"] is True


def test_stuck_needs_every_field_present():
    for key in ("stg", "nozzle_target_temper", "mc_percent"):
        obj = dict(_STUCK)
        obj.pop(key)
        assert _v2(obj)["stuck_job"] is False, key


def test_a_warm_nozzle_a_fan_or_a_queued_stage_is_a_real_start():
    assert _v2({**_STUCK, "nozzle_target_temper": 220})["stuck_job"] is False
    assert _v2({**_STUCK, "cooling_fan_speed": "15"})["stuck_job"] is False
    assert _v2({**_STUCK, "stg": [2, 14]})["stuck_job"] is False


# --- stage ------------------------------------------------------------------

def test_no_stage_sentinels():
    assert _v2({"gcode_state": "IDLE", "stg_cur": 255})["stage"]["code"] is None
    assert _v2({"gcode_state": "IDLE", "stg_cur": -1}, profile=X1C)["stage"]["code"] is None


def test_stage_zero_while_idle_is_the_a1_p1_idle_bug():
    assert _v2({"gcode_state": "IDLE", "stg_cur": 0})["stage"] == {"code": None, "label": None}


def test_an_unnamed_stage_during_a_job_says_preparing_and_keeps_the_number():
    v2 = _v2({"gcode_state": "RUNNING", "stg_cur": 74, "mc_percent": 0, "subtask_name": "W"})
    assert v2["stage"] == {"code": 74, "label": "Preparing"}


def test_every_named_stage_has_wording():
    assert all(isinstance(label, str) and label for label in STAGE_LABELS.values())


# --- errors -----------------------------------------------------------------

def test_errors_are_real_faults_with_titles():
    v2 = _v2({"gcode_state": "PAUSE", "stg_cur": 6, "print_error": 0x0300_8004})
    assert v2["errors"] == [{
        "code": "0300_8004", "severity": "SERIOUS", "title": "Filament runout",
        "detail": "The printer ran out of filament. Load a new reel and resume.",
        "source": "print_error",
    }]
    hms = _v2({"gcode_state": "PAUSE", "hms": [{"attr": 0x03000100, "code": 0x00010007}]})
    assert hms["errors"][0]["code"] == "0300_0100_0001_0007"
    assert hms["errors"][0]["severity"] == "FATAL"
    assert hms["errors"][0]["title"] == "Bed temperature fault"


def test_an_unknown_error_code_keeps_its_code_and_a_generic_title():
    v2 = _v2({"gcode_state": "PAUSE", "print_error": 0x0C00_C001})
    assert v2["errors"][0]["code"] == "0C00_C001"
    assert v2["errors"][0]["title"] == "Printer error"


def test_commands_rejected_is_reported():
    v2 = _v2({"gcode_state": "IDLE", "hms": [{"attr": 0x05000500, "code": 0x00010007}]})
    assert v2["commands_rejected"] is True
    assert _v2({"gcode_state": "IDLE", "hms": []})["commands_rejected"] is False


# --- connection -------------------------------------------------------------

def test_connection_reasons():
    assert _v2({"gcode_state": "IDLE"})["connection_reason"] == "ok"
    assert _v2({"gcode_state": "IDLE"}, connection="stale")["connection_reason"] == "silent"
    assert _v2(None, connection="offline", down_reason="auth_rejected")["connection_reason"] == "auth_rejected"
    assert _v2(None, connection="offline", down_reason="unreachable")["connection_reason"] == "unreachable"
    assert _v2(None, connection="offline")["connection_reason"] == "no_data"


def test_stale_keeps_the_last_known_activity_beside_the_connection():
    v2 = _v2({"gcode_state": "RUNNING", "mc_percent": 50}, connection="stale")
    assert v2["connection"] == "stale"
    assert v2["activity"] == "printing"


def test_offline_has_no_reading():
    v2 = _v2(None, connection="offline")
    assert v2["activity"] == "unknown"
    assert v2["job"] is None
    assert v2["errors"] == []
    assert v2["commands_rejected"] is None


# --- model ------------------------------------------------------------------

def test_model_block_says_whether_this_model_is_verified():
    assert _v2({"gcode_state": "IDLE"})["model"] == {
        "code": "C12", "name": "P1S", "family": "p1", "known": True, "verified": True}
    x1c = _v2({"gcode_state": "IDLE"}, profile=X1C, code="BL-P001")
    assert x1c["model"]["verified"] is False and x1c["model"]["known"] is True
    unknown = _v2({"gcode_state": "IDLE"}, profile=P1_PROFILE, code="Z99", known=False)
    assert unknown["model"]["known"] is False and unknown["model"]["verified"] is False


def test_capabilities_come_from_the_profile():
    assert "chamber_temperature" in _v2({"gcode_state": "IDLE"}, profile=X1C)["capabilities"]
    assert "chamber_temperature" not in _v2({"gcode_state": "IDLE"})["capabilities"]


def test_raw_firmware_fields_ride_along_for_debugging():
    v2 = _v2({"gcode_state": "running", "stg_cur": 2, "print_error": 0})
    assert v2["raw"] == {"gcode_state": "RUNNING", "stg_cur": 2, "print_error": None}


def test_malformed_values_do_not_raise():
    v2 = _v2({"gcode_state": 5, "mc_percent": "x", "stg_cur": [1], "hms": "junk",
              "nozzle_target_temper": {}, "subtask_name": 9})
    assert v2["activity"] == "unknown"
