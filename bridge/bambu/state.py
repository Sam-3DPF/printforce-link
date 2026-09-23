"""Parse `print.net.info` addresses out of a Bambu report.

Each interface address is a little-endian uint32. ``0`` means that interface
has no address. These are candidates only: the fleet still proves the serial
before it dials one.
"""
import ipaddress
from typing import List, Optional


def net_info_ips(doc) -> List[str]:
    """Every usable IPv4 in ``doc["print"]["net"]["info"]``, in order.

    Missing, zero, and non-uint32 entries are ignored. Loopback, multicast,
    and reserved addresses are ignored too: they are not a LAN interface the
    bridge can dial. The same address twice is returned once.
    """
    info = _info_list(doc)
    if info is None:
        return []
    found: List[str] = []
    for entry in info:
        ip = _entry_ip(entry)
        if ip is None or ip in found:
            continue
        found.append(ip)
    return found


def _info_list(doc):
    if not isinstance(doc, dict):
        return None
    print_obj = doc.get("print")
    if not isinstance(print_obj, dict):
        return None
    net = print_obj.get("net")
    if not isinstance(net, dict):
        return None
    info = net.get("info")
    if not isinstance(info, list):
        return None
    return info


def _entry_ip(entry) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    return _ipv4_from_le(entry.get("ip"))


def _ipv4_from_le(value) -> Optional[str]:
    # bool is an int. True would decode as 1.0.0.0, which the printer did not send.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > 0xFFFFFFFF:
        return None
    try:
        addr = ipaddress.IPv4Address(value.to_bytes(4, "little"))
    except (ipaddress.AddressValueError, OverflowError):
        return None
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return None
    return str(addr)
