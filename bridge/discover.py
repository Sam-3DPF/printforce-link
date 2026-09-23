"""Discover Bambu printers on the LAN via their SSDP broadcasts.

Bambu printers announce themselves with SSDP NOTIFY datagrams on UDP 1990 (and
2021). The headers carry the printer's IP, serial (USN), name, and model in the
clear — everything except the access code. This is exactly how Bambu Studio /
OrcaSlicer / SimplyPrint find printers, so onboarding a printer in 3DPF becomes
"pick it from the discovered list, then type the access code."

`parse_ssdp_notify()` is pure and unit-tested; `discover()` does the socket I/O.

Run it standalone:  python -m bridge.discover
"""

import fcntl
import ipaddress
import re
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

SSDP_PORTS = (1990, 2021)
SSDP_MCAST = "239.255.255.250"
SSDP_BROADCAST = "255.255.255.255"
# Bambuddy's virtual printer. A shop that also runs one must not onboard it.
_VIRTUAL_SERIAL_SUFFIX = "391800001"
_MQTT_PORT = 8883
_SEARCH_REPEAT_SECONDS = 1.0
_SELECT_SLICE_SECONDS = 0.5
# Held back from the listen when a cold sweep may still be needed, so the
# whole call still returns inside the timeout the caller already passed.
_SWEEP_RESERVE_SECONDS = 1.5
_SWEEP_BATCH = 64
_SWEEP_CONNECT_TIMEOUT = 0.2
_MAX_SCAN_PREFIXES = 3
# Linux SIOCGIFADDR is 0x8915. Darwin's _IOWR('i', 33, struct ifreq) is the other.
_SIOCGIFADDR = 0xC0206921 if sys.platform == "darwin" else 0x8915


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
    if not serial or serial.endswith(_VIRTUAL_SERIAL_SUFFIX):
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


def expand_probe_ips(known_ips: Iterable[str], *, max_subnets: int = 3) -> List[str]:
    """Unicast M-SEARCH targets: each known private address plus the rest of its /24.

    Multicast/broadcast SSDP is silent on this farm (the discovery table stays
    empty, including for printers that are already pushing). A reserved address
    on the same LAN as a stored 192.168.8.x printer is then invisible unless we
    ask that subnet directly. Public or malformed addresses are ignored so a
    bad pin cannot scan the internet.
    """
    ordered: List[str] = []
    seen = set()
    prefixes: List[str] = []

    def _add(ip: str) -> None:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)

    for raw in known_ips:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            addr = ipaddress.ip_address(raw.strip())
        except ValueError:
            continue
        if addr.version != 4 or not addr.is_private:
            continue
        _add(str(addr))
        network = ipaddress.ip_network(f"{addr}/24", strict=False)
        prefix = str(network.network_address).rsplit(".", 1)[0]
        if prefix in prefixes or len(prefixes) >= max_subnets:
            continue
        prefixes.append(prefix)
        for host in network.hosts():
            _add(str(host))
    return ordered


def msearch_packet(port: int) -> bytes:
    """Ask Bambu printers to answer now, instead of waiting for a NOTIFY.

    A passive listen only hears whoever happens to broadcast during the window.
    Studio and SimplyPrint send this M-SEARCH; the reply uses the same headers
    `parse_ssdp_notify` already reads.
    """
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_MCAST}:{port}\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "MX: 1\r\n"
        "ST: urn:bambulab-com:device:3dprinter:1\r\n"
        "\r\n"
    ).encode("ascii")


def _interface_ipv4s() -> List[str]:
    """IPv4 addresses configured on this host. Standard library only."""
    found: List[str] = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                res = fcntl.ioctl(
                    sock.fileno(),
                    _SIOCGIFADDR,
                    struct.pack("256s", name.encode("utf-8")[:15]),
                )
            except OSError:
                continue
            ip = socket.inet_ntoa(res[20:24])
            if ip not in seen:
                seen.add(ip)
                found.append(ip)
    finally:
        sock.close()
    return found


def _ipv4(ip: str):
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return addr


def _is_real_unicast(addr: ipaddress.IPv4Address) -> bool:
    return not (
        addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified
    )


def _is_private_unicast(ip: str) -> bool:
    addr = _ipv4(ip)
    return addr is not None and addr.is_private and _is_real_unicast(addr)


def _interfaces_to_join(iface_ip: str) -> List[str]:
    """Interface addresses for IP_ADD_MEMBERSHIP.

    An explicit `iface_ip` is used as given. Otherwise every non-loopback,
    non-link-local IPv4 on the host is joined, so a second NIC is not missed.
    """
    if iface_ip:
        return [iface_ip]
    found: List[str] = []
    seen = set()
    for raw in _interface_ipv4s():
        addr = _ipv4(raw)
        if addr is None or not _is_real_unicast(addr):
            continue
        text = str(addr)
        if text not in seen:
            seen.add(text)
            found.append(text)
    return found


def _private_scan_hosts() -> List[str]:
    """Hosts on this machine's private /24s. Public interfaces contribute nothing.

    The walk is `expand_probe_ips`. This machine's own addresses are removed
    so the sweep does not dial the bridge itself.
    """
    seeds: List[str] = []
    own = set()
    for raw in _interface_ipv4s():
        addr = _ipv4(raw)
        if addr is None:
            continue
        text = str(addr)
        own.add(text)
        if addr.is_private and _is_real_unicast(addr):
            seeds.append(text)
    return [
        ip for ip in expand_probe_ips(seeds, max_subnets=_MAX_SCAN_PREFIXES) if ip not in own
    ]


def _outbound_ports(sock: socket.socket) -> Sequence[int]:
    """SSDP ports to address. An ephemeral socket still searches 1990 and 2021."""
    try:
        port = sock.getsockname()[1]
    except OSError:
        return SSDP_PORTS
    if port in SSDP_PORTS:
        return (port,)
    return SSDP_PORTS


def _solicit(sock: socket.socket, probe_ips: Optional[Iterable[str]] = None) -> None:
    """Best-effort. A network that drops multicast can still answer a broadcast
    or a unicast M-SEARCH to an address we already know (or the rest of its /24)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    probes = list(probe_ips or [])
    for port in _outbound_ports(sock):
        packet = msearch_packet(port)
        destinations = [(SSDP_MCAST, port), (SSDP_BROADCAST, port)]
        for ip in probes:
            destinations.append((ip, port))
        for dest in destinations:
            try:
                sock.sendto(packet, dest)
            except OSError:
                continue


def _join_group(sock: socket.socket, iface_ip: str) -> None:
    iface = socket.inet_aton(iface_ip) if iface_ip else socket.inet_aton("0.0.0.0")
    mreq = struct.pack("4s4s", socket.inet_aton(SSDP_MCAST), iface)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def _join_interfaces(sock: socket.socket, iface_ips: Iterable[str]) -> None:
    joined = False
    for ip in iface_ips:
        try:
            _join_group(sock, ip)
            joined = True
        except OSError:
            continue
    if not joined:
        try:
            _join_group(sock, "")
        except OSError:
            pass


def _open_bound_socket(port: int, iface_ips: Iterable[str]) -> Optional[socket.socket]:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass  # not all platforms have SO_REUSEPORT
        sock.bind(("", port))
        _join_interfaces(sock, iface_ips)
        sock.setblocking(False)
        return sock
    except OSError:
        # Port already bound (e.g. Bambu Studio open) — the other port, or an
        # ephemeral socket, still sends the search.
        if sock is not None:
            sock.close()
        return None


def _open_socket(port: int, iface_ips: Iterable[str]) -> Optional[socket.socket]:
    return _open_bound_socket(port, iface_ips)


def _open_ephemeral_socket(iface_ips: Iterable[str]) -> Optional[socket.socket]:
    """Bind port 0 when 1990 and 2021 are both taken, and search those ports anyway."""
    return _open_bound_socket(0, iface_ips)


def _collect(socks: Iterable[socket.socket], found: Dict[str, DiscoveredPrinter]) -> None:
    for sock in socks:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                break
            printer = parse_ssdp_notify(data, addr[0])
            if printer is not None:
                found[printer.serial] = printer


def _probe_8883(hosts: Iterable[str], deadline: float, on_accept=None) -> List[str]:
    """Private hosts that accept TCP 8883 before `deadline`. Connections are parallel.

    `on_accept` runs as soon as a connect succeeds, while time remains, so a
    search can go out before later batches use up the reserve.
    """
    open_hosts: List[str] = []
    pending: List[tuple] = []
    host_iter = iter(hosts)

    def _finish(sock: socket.socket, ip: str, ok: bool) -> None:
        sock.close()
        if not ok:
            return
        open_hosts.append(ip)
        if on_accept is not None and time.monotonic() < deadline:
            on_accept(ip)

    def _fill() -> bool:
        consumed = False
        while len(pending) < _SWEEP_BATCH and time.monotonic() < deadline:
            try:
                ip = next(host_iter)
            except StopIteration:
                break
            consumed = True
            if not _is_private_unicast(ip):
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setblocking(False)
                sock.connect((ip, _MQTT_PORT))
            except BlockingIOError:
                pending.append((ip, sock, time.monotonic()))
            except OSError:
                sock.close()
            else:
                _finish(sock, ip, True)
        return consumed or bool(pending)

    try:
        while time.monotonic() < deadline:
            if not _fill():
                break
            if not pending:
                continue
            now = time.monotonic()
            if now >= deadline:
                break
            oldest = min(started for _ip, _sock, started in pending)
            connect_left = _SWEEP_CONNECT_TIMEOUT - (now - oldest)
            wait = min(max(0.0, connect_left), deadline - now)
            watch = [sock for _ip, sock, _started in pending]
            try:
                _readable, writable, errors = select.select([], watch, watch, max(0.0, wait))
            except OSError:
                break
            ready_ids = {id(sock) for sock in writable}
            ready_ids.update(id(sock) for sock in errors)
            now = time.monotonic()
            still: List[tuple] = []
            for ip, sock, started in pending:
                if id(sock) in ready_ids:
                    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    _finish(sock, ip, err == 0)
                elif now - started >= _SWEEP_CONNECT_TIMEOUT or now >= deadline:
                    sock.close()
                else:
                    still.append((ip, sock, started))
            pending = still
    finally:
        for _ip, sock, _started in pending:
            sock.close()
    return open_hosts


def _unicast_search(socks: Sequence[socket.socket], ip: str) -> None:
    for sock in socks:
        for port in _outbound_ports(sock):
            packet = msearch_packet(port)
            try:
                sock.sendto(packet, (ip, port))
            except OSError:
                continue


def _sweep_private_8883(
    socks: Sequence[socket.socket],
    found: Dict[str, DiscoveredPrinter],
    deadline: float,
) -> None:
    def _on_accept(ip: str) -> None:
        _unicast_search(socks, ip)
        _collect(socks, found)

    opened = _probe_8883(_private_scan_hosts(), deadline, _on_accept)
    # The accept-time drain runs before a printer has time to answer. Replies
    # that land while later hosts are probed stay in the socket buffer.
    _collect(socks, found)
    if not opened:
        return
    # Keep reading until the caller's deadline. One shop has more than one
    # printer, and the first reply often arrives before the others.
    while time.monotonic() < deadline:
        ready, _, _ = select.select(
            list(socks), [], [], max(0.0, deadline - time.monotonic())
        )
        if ready:
            _collect(ready, found)


def discover(timeout: float = 8.0, iface_ip: str = "",
             probe_ips: Optional[Iterable[str]] = None) -> List[DiscoveredPrinter]:
    """Ask the LAN for Bambu printers and return within `timeout` seconds.

    Repeats an M-SEARCH on each SSDP port (multicast and broadcast) during the
    listen. When `probe_ips` is set, also unicasts to those addresses and the
    rest of each private /24 — a reserved DHCP address on the same LAN is found
    even when multicast is dropped. A non-empty `probe_ips` does not port-scan.

    With no interface address, the group is joined on each real IPv4. Loopback
    and link-local are skipped. If both 1990 and 2021 fail to bind, the search
    goes out from an ephemeral socket. When nothing is stored and the listen
    hears nothing, private hosts on this machine's subnets are probed on TCP
    8883 and a search is unicast to the ones that accept. That sweep spends
    only the time still left in `timeout`. Virtual serials ending in 391800001
    are dropped. Results are deduplicated by serial.
    """
    iface_ips = _interfaces_to_join(iface_ip)
    socks = [s for s in (_open_socket(port, iface_ips) for port in SSDP_PORTS) if s is not None]
    if not socks:
        ephemeral = _open_ephemeral_socket(iface_ips)
        if ephemeral is not None:
            socks.append(ephemeral)
    if not socks:
        return []

    probe_list = list(probe_ips or [])
    targets = expand_probe_ips(probe_list)
    # Empty means nothing is stored yet. A public pin is not empty: it must
    # not turn into a scan of this machine's private subnets.
    cold = not probe_list
    found: Dict[str, DiscoveredPrinter] = {}
    start = time.monotonic()
    end = start + max(0.0, timeout)
    listen_end = end
    if cold:
        reserve = min(_SWEEP_RESERVE_SECONDS, max(0.0, timeout) * 0.5)
        listen_end = end - reserve
    next_search = start
    try:
        while True:
            now = time.monotonic()
            horizon = end if found or not cold else listen_end
            if now >= horizon:
                break
            if now >= next_search:
                for sock in socks:
                    _solicit(sock, targets)
                next_search = now + _SEARCH_REPEAT_SECONDS
            remaining = horizon - time.monotonic()
            until_search = next_search - time.monotonic()
            wait = min(_SELECT_SLICE_SECONDS, remaining, max(0.0, until_search))
            if wait <= 0:
                continue
            ready, _, _ = select.select(socks, [], [], wait)
            if ready:
                _collect(ready, found)
        if cold and not found and time.monotonic() < end:
            _sweep_private_8883(socks, found, end)
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
