"""project_file is refused while a print is on the machine, and the payload
carries a fresh submission id plus an AMS map that does not turn an
unresolved tray into the external spool.
"""

import pytest

from bridge.config import PrinterConfig
from bridge.printer import BambuPrinter


_BUSY = ("PREPARE", "SLICING", "RUNNING", "PAUSE")
_READY = ("IDLE", "FINISH", "FAILED")
_ID_CAP = 2147483647


class _Session:
    def __init__(self, state="live"):
        self.state = state
        self.payloads = []

    def publish(self, payload):
        self.payloads.append(payload)
        return True


def _printer(*, model="C12", session_state="live"):
    cfg = PrinterConfig(
        bambu_id="01P00C000000001", ip="10.0.0.5", access_code="x",
        name="P1S", model=model,
    )
    printer = BambuPrinter(cfg)
    printer._session = _Session(session_state)
    return printer


def _body(printer):
    assert len(printer._session.payloads) == 1
    return printer._session.payloads[0]["print"]


def _set_state(printer, gcode_state):
    printer.state.ingest({"print": {"gcode_state": gcode_state}}, 1.0)


@pytest.mark.parametrize("gcode_state", _BUSY)
def test_project_file_is_refused_while_busy_even_if_the_session_is_stale(gcode_state):
    printer = _printer(session_state="stale")
    _set_state(printer, gcode_state)
    assert printer.start_print("job.3mf", [0], 1) is False
    assert printer._session.payloads == []


@pytest.mark.parametrize("gcode_state", _READY)
def test_idle_finish_and_failed_start_without_a_stop(gcode_state):
    printer = _printer()
    _set_state(printer, gcode_state)
    assert printer.start_print("job.3mf", [0], 1) is True
    commands = [
        payload.get("print", {}).get("command")
        for payload in printer._session.payloads
    ]
    assert commands == ["project_file"]


def test_an_unknown_state_may_still_start():
    printer = _printer()
    assert printer.start_print("job.3mf", [0], 1) is True
    assert _body(printer)["command"] == "project_file"


def test_consecutive_starts_get_different_ids_inside_the_firmware_cap():
    printer = _printer()
    assert printer.start_print("one.3mf", [0], 1) is True
    assert printer.start_print("two.3mf", [0], 1) is True
    seen = []
    for payload in printer._session.payloads:
        body = payload["print"]
        assert body["task_id"] == body["subtask_id"] == body["project_id"]
        value = int(body["task_id"])
        assert 0 < value <= _ID_CAP
        seen.append(value)
    assert seen[0] != seen[1]


def test_unresolved_tray_maps_to_255_255_and_does_not_force_external():
    printer = _printer()
    assert printer.start_print("job.3mf", [-1, 0], 1) is True
    body = _body(printer)
    assert body["ams_mapping"] == [-1, 0]
    assert body["ams_mapping2"] == [
        {"ams_id": 255, "slot_id": 255},
        {"ams_id": 0, "slot_id": 0},
    ]
    assert body["use_ams"] is True


def test_all_unresolved_keeps_use_ams_true():
    printer = _printer()
    assert printer.start_print("job.3mf", [-1, -1], 1) is True
    body = _body(printer)
    assert body["use_ams"] is True
    assert body["ams_mapping2"] == [
        {"ams_id": 255, "slot_id": 255},
        {"ams_id": 255, "slot_id": 255},
    ]


def test_use_ams_is_false_only_when_every_entry_is_external():
    external = _printer()
    assert external.start_print("job.3mf", [254, 255], 1) is True
    assert _body(external)["use_ams"] is False

    mixed = _printer()
    assert mixed.start_print("job.3mf", [254, 0], 1) is True
    assert _body(mixed)["use_ams"] is True


def test_start_url_comes_from_the_p1s_profile():
    printer = _printer(model="C12")
    assert printer.profile.start_url_scheme == "file:///sdcard/"
    assert printer.start_print("batch-a.3mf", [0], 1) is True
    assert _body(printer)["url"] == "file:///sdcard/batch-a.3mf"
    assert _body(printer)["vibration_cali"] is True


def test_start_document_matches_the_p1_field_set():
    """The start document carries the calibration and file fields, and the URL
    stays the P1 SD-card form.
    """
    printer = _printer(model="C12")
    assert printer.start_print("batch-a.3mf", [0], 1) is True
    body = _body(printer)
    assert body["sequence_id"] == "20000"
    assert body["command"] == "project_file"
    assert body["file"] == "batch-a.3mf"
    assert body["md5"] == ""
    assert body["bed_type"] == "auto"
    assert body["bed_leveling"] is False
    assert body["auto_bed_leveling"] == 2
    assert body["flow_cali"] is False
    assert body["extrude_cali_flag"] == 2
    assert body["extrude_cali_manual_mode"] == 0
    assert body["layer_inspect"] is False
    assert body["timelapse"] is False
    assert body["cfg"] == "0"
    assert body["profile_id"] == "0"
    assert body["nozzle_offset_cali"] == 0
    assert body["subtask_name"] == "batch-a"
    assert body["url"] == "file:///sdcard/batch-a.3mf"
    assert body["vibration_cali"] is True
    assert "stop" not in [
        payload.get("print", {}).get("command")
        for payload in printer._session.payloads
    ]


def test_a_sent_start_is_remembered_so_the_print_is_origin_link():
    printer = _printer()
    assert printer.start_print("benchy.3mf", [0], 1) is True
    submission_id = _body(printer)["subtask_id"]
    printer.state.ingest({"print": {"gcode_state": "IDLE"}}, 1.0)
    printer.state.ingest({
        "print": {
            "gcode_state": "RUNNING",
            "subtask_id": submission_id,
            "gcode_file": "benchy.3mf",
            "subtask_name": "benchy.3mf",
        },
    }, 2.0)
    view = printer.state.view()
    assert view["print_origin"] == "link"
    assert view["print_submission_id"] == submission_id
    kinds = [event["type"] for event in view["events"]]
    assert kinds == ["print_started"]


def test_a_refused_start_is_not_registered():
    printer = _printer()
    _set_state(printer, "RUNNING")
    assert printer.start_print("job.3mf", [0], 1) is False
    printer.state.ingest({
        "print": {
            "gcode_state": "RUNNING",
            "subtask_id": "1",
            "gcode_file": "job.3mf",
        },
    }, 2.0)
    assert printer.state.view()["print_origin"] != "link"


def _print_body(mapping):
    printer = _printer()
    assert printer.start_print("job.3mf", mapping, 1) is True
    return _body(printer)


def test_external_trays_publish_flat_minus_one_and_use_ams_false():
    body = _print_body([254, 255])
    assert body["ams_mapping"] == [-1, -1]
    assert body["ams_mapping2"] == [
        {"ams_id": 255, "slot_id": 0},
        {"ams_id": 255, "slot_id": 0},
    ]
    assert body["use_ams"] is False


def test_unresolved_tray_stays_unresolved_and_use_ams_stays_true():
    body = _print_body([-1])
    assert body["ams_mapping"] == [-1]
    assert body["ams_mapping2"] == [{"ams_id": 255, "slot_id": 255}]
    assert body["use_ams"] is True


def test_ams_ht_and_a2l_and_regular_zero_use_their_own_encodings():
    ht = _print_body([128])
    assert ht["ams_mapping"] == [128]
    assert ht["ams_mapping2"] == [{"ams_id": 128, "slot_id": 0}]
    a2l = _print_body([24])
    assert a2l["ams_mapping"] == [0]
    assert a2l["ams_mapping2"] == [{"ams_id": 16, "slot_id": 0}]
    regular = _print_body([0])
    assert regular["ams_mapping"] == [0]
    assert regular["ams_mapping2"] == [{"ams_id": 0, "slot_id": 0}]


def test_tray_five_load_uses_a_local_slot_and_a_global_target():
    from bridge.bambu.commands import build_change_filament, filament_load_target

    ams_id, slot_id, target = filament_load_target(5)
    assert (ams_id, slot_id, target) == (1, 1, 5)
    body = build_change_filament(ams_id=ams_id, slot_id=slot_id, target=target)["print"]
    assert body["curr_temp"] == -1
    assert body["tar_temp"] == -1
    external = filament_load_target(254)
    assert external == (255, 254, 254)


def test_drying_builder_cools_at_20_then_0_and_fills_pla():
    from bridge.bambu.commands import build_drying

    started = build_drying(mode=1, temp=45, duration=2, filament="")["print"]
    assert started["cooling_temp"] == 20
    assert started["filament"] == "PLA"
    assert started["humidity"] == 0
    assert started["close_power_conflict"] is False
    stopped = build_drying(mode=0, temp=45, duration=2, filament="", loaded_type="PETG")["print"]
    assert stopped["cooling_temp"] == 0
    assert stopped["filament"] == "PETG"


def test_direct_commands_publish_the_p1_documents():
    printer = _printer()
    _set_state(printer, "RUNNING")
    assert printer.handle_control("gcode_line", {"param": "G1 X1\n"}) is True
    assert printer.handle_control("gcode_line", {"param": "G1 Y1"}) is True
    assert printer.handle_control("bed_temperature", {"target": 60}) is True
    assert printer.handle_control("nozzle_temperature", {"target": 210, "nozzle": 0}) is True
    assert printer.handle_control("chamber_temperature", {"target": 40}) is True
    assert printer.handle_control("print_speed", {"param": "2"}) is True
    assert printer.handle_control("fan_speed", {"p": 1, "s": 255}) is True
    assert printer.handle_control("fan_speed", {"p": 2, "s": 0}) is True
    assert printer.handle_control("fan_speed", {"p": 3, "s": 10}) is True
    assert printer.handle_control("fan_speed", {"p": 10, "s": 128}) is True
    assert printer.handle_control("airduct", {"modeId": 1}) is True
    assert printer.handle_control("home", {"axis": "Z"}) is True
    assert printer.handle_control("move", {"axis": "X", "distance": 10, "speed": 3000}) is True
    assert printer.handle_control("motors_off", {}) is True
    assert printer.handle_control("motors_on", {}) is True
    assert printer.handle_control("skip_objects", {"obj_list": [3, 4]}) is True
    assert printer.handle_control("select_extruder", {"extruder_index": 1}) is True
    assert printer.handle_control("timelapse", {"on": True}) is True
    assert printer.handle_control("calibration", {"option": 8}) is True
    assert printer.handle_control("chamber_light", {"on": True}) is True
    texts = [item.get("print", {}).get("param") for item in printer._session.payloads]
    assert "G1 X1\n" in texts
    assert "M140 S60" in texts
    assert "M104 T0 S210" in texts
    assert "M141 S40" in texts
    assert "M106 P1 S255" in texts
    assert "M106 P10 S128" in texts
    assert "G28" in texts
    assert texts.count("G91") == 1 and "G0 X10 F3000" in texts and "G90" in texts
    assert "M18" in texts and "M17" in texts
    assert "M981 S1 P20000" in texts
    lines = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "gcode_line"]
    assert lines[0]["sequence_id"] == "1"
    assert lines[1]["sequence_id"] == "2"
    assert int(lines[1]["sequence_id"]) == int(lines[0]["sequence_id"]) + 1
    speeds = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "print_speed"]
    assert speeds[0]["param"] == "2" and speeds[0]["sequence_id"] == "0"
    air = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "set_airduct"]
    assert air[0]["modeId"] == 1 and air[0]["submode"] == -1
    skipped = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "skip_objects"]
    assert skipped[0]["obj_list"] == [3, 4]
    chosen = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "select_extruder"]
    assert chosen[0]["extruder_index"] == 1
    option = [item["print"] for item in printer._session.payloads if item.get("print", {}).get("command") == "calibration"]
    assert option[0]["option"] == 8
    lights = [item["system"] for item in printer._session.payloads if "system" in item]
    assert [item["led_node"] for item in lights] == ["chamber_light", "chamber_light2"]
    assert {item["led_mode"] for item in lights} == {"on"}
    assert {"pushing": {"sequence_id": "0", "command": "pushall"}} in printer._session.payloads
    home_index = texts.index("G28")
    assert "Z" not in texts[home_index]


def test_skip_outside_a_print_publishes_nothing_and_is_handled():
    printer = _printer()
    _set_state(printer, "IDLE")
    assert printer.handle_control("skip_objects", {"obj_list": [1]}) is True
    assert printer._session.payloads == []


def test_pause_resume_and_stop_include_sequence_zero():
    printer = _printer()
    assert printer.pause_print() is True
    assert printer.resume_print() is True
    assert printer.stop_print() is True
    commands = [item["print"] for item in printer._session.payloads]
    assert commands[0] == {"sequence_id": "0", "command": "pause"}
    assert commands[1]["command"] == "resume" and commands[1]["sequence_id"] == "0"
    assert commands[2]["command"] == "stop" and commands[2]["sequence_id"] == "0"


def test_filament_slot_write_k_select_and_hms_actions():
    printer = _printer()
    printer.state.ingest({
        "print": {"gcode_state": "PAUSE", "subtask_id": "4242"},
    }, 1.0)
    printer._last_submission_id = 999
    assert printer.handle_control("filament_load", {"tray": 5}) is True
    assert printer.handle_control("filament_load", {"tray": 254}) is True
    assert printer.handle_control("filament_unload", {"slot": "named-holder"}) is True
    assert printer.handle_control("ams_control", {"param": "pause"}) is True
    assert printer.handle_control("ams_control", {"param": "reset"}) is True
    assert printer.handle_control("filament_setting", {
        "ams_id": 0, "tray_id": 1, "color": "aabbccdd",
    }) is True
    assert printer.handle_control("filament_setting", {"external": True, "color": "ff00ff00"}) is True
    assert printer.handle_control("filament_setting_reset", {"ams_id": 0, "tray_id": 1}) is True
    assert printer.handle_control("extrusion_cali_sel", {
        "tray_id": 5, "setting_id": "nope", "filament_id": "GFA00",
    }) is True
    assert printer.handle_control("ignore", {"err": "0500050000010007", "job_id": "999"}) is True
    assert printer.handle_control("idle_ignore", {"err": "0500050000010007"}) is True
    assert printer.handle_control("clean_print_error", {}) is True
    assert printer.handle_control("check_assistant", {}) is True
    bodies = [item.get("print", {}) for item in printer._session.payloads]
    load = bodies[0]
    assert load["command"] == "ams_change_filament"
    assert load["ams_id"] == 1 and load["slot_id"] == 1 and load["target"] == 5
    assert load["curr_temp"] == -1 and load["tar_temp"] == -1
    external = bodies[1]
    assert external["ams_id"] == 255 and external["slot_id"] == 254
    unload = bodies[2]
    assert unload["slot_id"] == 255 and unload["target"] == 255
    assert bodies[3]["command"] == "ams_control" and bodies[3]["param"] == "pause"
    assert bodies[4]["param"] == "reset"
    color = bodies[5]
    assert color["tray_color"] == "AABBCCDD"
    assert "aabbccdd" not in str(color)
    external_write = bodies[6]
    assert external_write["ams_id"] == 255 and external_write["tray_id"] == 254
    reset = bodies[7]
    assert reset["tray_color"] == "00000000"
    assert reset["tray_type"] == "" and reset["tray_info_idx"] == ""
    ksel = bodies[8]
    assert ksel["command"] == "extrusion_cali_sel"
    assert ksel["tray_id"] == 5
    assert "setting_id" not in ksel
    ignored = bodies[9]
    assert ignored["command"] == "ignore"
    assert ignored["param"] == "reserve"
    assert ignored["job_id"] == "4242"
    assert ignored["job_id"] != "999"
    assert ignored["err"] == str(int("0500050000010007", 16))
    idle = bodies[10]
    assert idle["command"] == "idle_ignore"
    assert "resume" not in [item.get("command") for item in bodies[10:]]
    assert bodies[11]["command"] == "clean_print_error"
    assert printer.snapshot().get("job_id") is None
    assert "job_id" not in printer.snapshot()


def test_a_calibration_table_reply_does_not_replace_nozzle_diameter():
    printer = _printer()
    printer.state.ingest({
        "print": {"gcode_state": "IDLE", "nozzle_diameter": "0.4"},
    }, 1.0)
    printer._on_mqtt_report({
        "print": {"command": "extrusion_cali_get", "nozzle_diameter": "0.8"},
    })
    assert printer.state.view()["payload"]["print"]["nozzle_diameter"] == "0.4"
    printer._on_mqtt_report({
        "info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.08.00.00"}]},
        "print": {"gcode_state": "IDLE"},
    })
    assert printer.state.view()["payload"]["info"]["module"][0]["sw_ver"] == "01.08.00.00"
