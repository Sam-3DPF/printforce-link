"""print.net.info addresses are little-endian uint32s."""
from bridge.bambu.state import net_info_ips


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
