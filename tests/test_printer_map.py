from bridge.printer import is_cancel_failed, map_status, promote_live_idle


def test_map_status_known_states():
    assert map_status("RUNNING") == "PRINTING"
    assert map_status("running") == "PRINTING"    # case-insensitive
    assert map_status("PREPARE") == "PRINTING"
    assert map_status("SLICING") == "PRINTING"
    assert map_status("FINISH") == "NEEDS_CLEARING"
    assert map_status("FAILED") == "ERROR"
    assert map_status("IDLE") == "IDLE"


def test_map_status_pause_is_paused_not_printing():
    """Bambu's enum spells it PAUSE. Folded into PRINTING, a paused printer reads as
    PRINTING forever — nothing can surface it, and nothing can resume it."""
    assert map_status("PAUSE") == "PAUSED"


def test_map_status_unknown_and_blank_default_to_offline_never_idle():
    """IDLE is the sole authorization for dispatch, so it has to be positively asserted
    by the printer: defaulting to it means a job dispatched onto a busy printer, its
    filament deducted, and a batch stamped PRINTING for a print that never starts.

    The window is real — `mqtt_dump()` returns {} until the first MQTT push lands, so
    on every bridge start there is an interval in which each printer, including one
    mid-print, has no gcode_state at all."""
    assert map_status("") == "OFFLINE"
    assert map_status(None) == "OFFLINE"
    assert map_status("WEIRD_STATE") == "OFFLINE"
    assert map_status("UNKNOWN") == "OFFLINE"    # the library's own fallback member


def test_promote_live_idle_heatup_and_midprint_are_printing():
    heating = {
        "gcode_state": "IDLE",
        "gcode_file": "batch-2026-09-22-4rnNgqiE-1.3mf",
        "mc_percent": 0,
        "nozzle_target_temper": 220,
        "bed_target_temper": 45,
    }
    assert promote_live_idle("IDLE", heating) == "PRINTING"
    moving = {
        "gcode_state": "IDLE",
        "mc_percent": 34,
        "nozzle_target_temper": 0,
        "bed_target_temper": 0,
    }
    assert promote_live_idle("IDLE", moving) == "PRINTING"


def test_promote_live_idle_finished_plate_and_cancel_stay_idle():
    finished = {
        "gcode_state": "IDLE",
        "gcode_file": "batch-2026-09-22-VI1M9oQN-1.3mf",
        "mc_percent": 100,
        "nozzle_target_temper": 0,
        "bed_target_temper": 0,
    }
    assert promote_live_idle("IDLE", finished) == "IDLE"
    assert promote_live_idle("IDLE", {"gcode_state": "FAILED", "mc_percent": 40}) == "IDLE"
    assert promote_live_idle("PRINTING", {"gcode_state": "IDLE", "mc_percent": 10}) == "PRINTING"
    assert promote_live_idle("IDLE", {"gcode_state": "IDLE", "mc_percent": 0}) == "IDLE"


def test_map_status_cancel_failed_is_idle_not_error():
    assert map_status("FAILED", print_error="50348044") == "IDLE"
    assert map_status("FAILED", hms_code="0300_400C") == "IDLE"
    assert map_status("FAILED", hms_code="0300_400C_0000_0000") == "IDLE"
    assert map_status("FAILED", hms_code="0500_400E") == "IDLE"
    assert map_status("FAILED", print_error="12345") == "ERROR"
    assert map_status("FAILED") == "ERROR"
    assert map_status("RUNNING", print_error="50348044") == "PRINTING"


def test_is_cancel_failed_reads_print_error_and_hms():
    assert is_cancel_failed(print_error="50348044")
    assert is_cancel_failed(hms_code="0300_400C")
    assert not is_cancel_failed(print_error="999")


def test_map_status_latched_cancel_survives_cleared_print_error():
    assert map_status("FAILED", print_error=None, user_cancelled=True) == "IDLE"
    assert map_status("FAILED", print_error="0", user_cancelled=True) == "IDLE"
    assert map_status("FAILED", print_error=None, user_cancelled=False) == "ERROR"
