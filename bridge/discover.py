"""Discover Bambu printers on the LAN via their SSDP broadcasts.

Bambu printers announce themselves with SSDP NOTIFY datagrams on UDP 1990 (and
2021). The headers carry the printer's IP, serial (USN), name, and model in the
clear — everything except the access code. This is exactly how Bambu Studio /
OrcaSlicer / SimplyPrint find printers, so onboarding a printer in 3DPF becomes
"pick it from the discovered list, then type the access code."

`parse_ssdp_notify()` is pure and unit-tested; `discover()` does the socket I/O.

Run it standalone:  python -m bridge.discover
"""

import re
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

SSDP_PORTS = (1990, 2021)
SSDP_MCAST = "239.255.255.250"


@dataclass
class DiscoveredPrinter:
    ip: str
    serial: str
    name: str = ""
    model: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"ip": self.ip, "serial": self.serial, "name": self.name, "model": self.model}


def parse_ssdp_notify(data: bytes, src_ip: str = "") -> Optional[DiscoveredPrinter]:
    """Parse one SSDP datagram into a DiscoveredPrinter, or None if it isn't a
    Bambu printer announcement.

    A Bambu NOTIFY looks like (subset)::

        NOTIFY * HTTP/1.1
        Location: 192.168.86.40
        NT: urn:bambulab-com:device:3dprinter:1
        USN: 01P00A3A3000666
        DevModel.bambu.com: C12
        DevName.bambu.com: P1S-1

    The serial is required; IP falls back to the datagram's source address when
    the Location header is absent.
    """
    text = data.decode("utf-8", "ignore")
    hdr: Dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            hdr[key.strip().lower()] = value.strip()

    # Only Bambu 3D-printer announcements — identified by the NT namespace or the
    # printer's *.bambu.com headers. Ignore generic UPnP/SSDP devices.
    is_bambu = "bambulab" in hdr.get("nt", "") or any(k.endswith(".bambu.com") for k in hdr)
    if not is_bambu:
        return None

    serial = hdr.get("usn", "").strip()
    if not serial:
        return None

    location = hdr.get("location", "")
    match = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", location)
    ip = match.group(1) if match else src_ip

    return DiscoveredPrinter(
        ip=ip,
        serial=serial,
        name=hdr.get("devname.bambu.com", "").strip(),
        model=hdr.get("devmodel.bambu.com", "").strip(),
    )


def _open_socket(port: int, iface_ip: str) -> Optional[socket.socket]:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass  # not all platforms have SO_REUSEPORT
        sock.bind(("", port))
        iface = socket.inet_aton(iface_ip) if iface_ip else socket.inet_aton("0.0.0.0")
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_MCAST), iface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        return sock
    except OSError:
        # Port already bound (e.g. Bambu Studio open) — the other port still
        # catches the same broadcasts, so this is not fatal.
        return None


def discover(timeout: float = 8.0, iface_ip: str = "") -> List[DiscoveredPrinter]:
    """Listen for SSDP NOTIFY broadcasts for `timeout` seconds and return the
    unique Bambu printers found (deduplicated by serial)."""
    socks = [s for s in (_open_socket(p, iface_ip) for p in SSDP_PORTS) if s is not None]
    if not socks:
        return []

    found: Dict[str, DiscoveredPrinter] = {}
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select(socks, [], [], 0.5)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                printer = parse_ssdp_notify(data, addr[0])
                if printer is not None:
                    found[printer.serial] = printer
    finally:
        for sock in socks:
            sock.close()
    return sorted(found.values(), key=lambda p: (p.name or "", p.ip))


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Discover Bambu printers on the LAN via SSDP.")
    parser.add_argument("--timeout", type=float, default=8.0, help="seconds to listen (default 8)")
    parser.add_argument("--iface", default="", help="local interface IP to listen on (default: any)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    printers = discover(args.timeout, args.iface)

    if args.json:
        print(json.dumps([p.to_dict() for p in printers], indent=2))
        return

    if not printers:
        print("No Bambu printers found. Check they're on this network and in LAN Mode,")
        print("then try a longer --timeout.")
        return

    print(f"Found {len(printers)} Bambu printer(s):\n")
    print(f"  {'NAME':12}  {'IP':15}  {'MODEL':6}  SERIAL")
    for p in printers:
        print(f"  {p.name or '(unnamed)':12}  {p.ip:15}  {p.model or '?':6}  {p.serial}")
    print("\nAdd each to config.toml with its access code (printer screen -> LAN-Only mode).")


if __name__ == "__main__":
    main()
