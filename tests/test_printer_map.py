from bridge.printer import map_status


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
