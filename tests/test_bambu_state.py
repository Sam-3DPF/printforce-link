"""print.net.info addresses are little-endian uint32s.

Merged report rules that are not an address: a fault list, a layer total,
and an AMS colour that a partial delta must not blank.
"""
from bridge.bambu.state import PrinterState, net_info_ips


def _le(ip):
    return int.from_bytes(bytes(int(part) for part in ip.split(".")), "little")


def _doc(entries):
    return {"print": {"net": {"info": entries}}}


def test_net_info_little_endian_uint32_decodes_to_dotted_quad():
    assert net_info_ips(_doc([{"ip": 0x0A08A8C0}])) == ["192.168.8.10"]
    assert _le("192.168.8.10") == 0x0A08A8C0


def test_net_info_returns_every_valid_ip_and_skips_junk():
    doc = _doc([
        {"ip": 0},
        {"ip": 0x0A08A8C0},
        "nope",
        None,
        {"mask": 1},
        {"ip": "0x0A08A8C0"},
        {"ip": True},
        {"ip": 1.5},
        {"ip": -1},
        {"ip": 0x100000000},
        {"ip": _le("127.0.0.1")},
        {"ip": _le("224.0.0.1")},
        {"ip": _le("255.255.255.255")},
        {"ip": 0x0A08A8C0},
        {"ip": _le("192.168.8.11")},
        {"ip": _le("169.254.1.10")},
    ])
    assert net_info_ips(doc) == ["192.168.8.10", "192.168.8.11", "169.254.1.10"]


def test_net_info_missing_or_malformed_is_ignored():
    assert net_info_ips(None) == []
    assert net_info_ips({}) == []
    assert net_info_ips({"print": {}}) == []
    assert net_info_ips({"print": {"net": {}}}) == []
    assert net_info_ips({"print": {"net": {"info": "nope"}}}) == []
    assert net_info_ips({"print": {"net": {"info": []}}}) == []
    assert net_info_ips({"print": "nope"}) == []


_FAULT = {"attr": 0x03000100, "code": 0x00010002}


def _state():
    return PrinterState("P1", monotonic=lambda: 0.0, wall_clock=lambda: 0.0)


def _print(state):
    return state.view()["payload"]["print"]


def _running(subtask_id, **fields):
    body = {
        "gcode_state": "RUNNING",
        "subtask_id": subtask_id,
        "gcode_file": f"{subtask_id}.gcode",
        "subtask_name": str(subtask_id),
    }
    body.update(fields)
    return {"print": body}


def test_explicit_empty_hms_clears_without_a_new_print():
    state = _state()
    state.ingest(_running("1", hms=[_FAULT]))
    state.ingest(_running("1", hms=[]))
    assert _print(state)["hms"] == []


def test_a_frame_that_omits_hms_keeps_the_fault_for_the_same_print():
    state = _state()
    state.ingest(_running("1", hms=[_FAULT]))
    state.ingest(_running("1", mc_percent=10))
    assert _print(state)["hms"] == [_FAULT]


def test_explicit_zero_layer_total_keeps_a_positive_total_for_the_same_print():
    state = _state()
    state.ingest(_running("1", total_layer_num=300))
    state.ingest(_running("1", total_layer_num=0))
    assert _print(state)["total_layer_num"] == 300
    state.ingest({"print": {
        "gcode_state": "FINISH",
        "subtask_id": "1",
        "gcode_file": "1.gcode",
        "subtask_name": "1",
        "total_layer_num": 0,
    }})
    assert _print(state)["total_layer_num"] == 300


def test_a_frame_that_omits_the_layer_total_keeps_300():
    state = _state()
    state.ingest(_running("1", total_layer_num=300))
    state.ingest(_running("1", nozzle_temper=200.0))
    assert _print(state)["total_layer_num"] == 300


def test_a_new_print_stores_that_frames_layer_total():
    state = _state()
    state.ingest(_running("1", total_layer_num=300))
    state.ingest(_running("2", total_layer_num=12))
    assert _print(state)["total_layer_num"] == 12


def test_a_new_print_stores_an_explicit_layer_total_of_zero():
    state = _state()
    state.ingest(_running("1", total_layer_num=300))
    state.ingest(_running("2", total_layer_num=0))
    assert _print(state)["total_layer_num"] == 0


def test_a_partial_ams_delta_keeps_a_loaded_color():
    state = _state()
    state.ingest({"print": {"gcode_state": "IDLE", "ams": {
        "tray_exist_bits": "1",
        "ams": [{"id": "0", "tray": [
            {"id": "0", "tray_color": "FF6A13FF", "tray_type": "PLA"},
        ]}],
    }}})
    state.ingest({"print": {"gcode_state": "IDLE", "ams": {
        "tray_exist_bits": "1",
        "ams": [{"id": "0", "tray": [{"id": "0"}]}],
    }}})
    tray = _print(state)["ams"]["ams"][0]["tray"][0]
    assert tray["tray_color"] == "FF6A13FF"
