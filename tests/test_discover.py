import ipaddress
import socket
import time

from bridge.discover import (
    DiscoveredPrinter,
    discover,
    expand_probe_ips,
    msearch_packet,
    parse_ssdp_notify,
)

# A real Bambu P1S SSDP NOTIFY captured on the LAN (2026-07-13).
BAMBU_NOTIFY = (
    b"NOTIFY * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1900\r\n"
    b"Server: UPnP/1.0\r\n"
    b"Location: 192.168.86.40\r\n"
    b"NT: urn:bambulab-com:device:3dprinter:1\r\n"
    b"USN: 01P00A3A3000666\r\n"
    b"Cache-Control: max-age=1800\r\n"
    b"DevModel.bambu.com: C12\r\n"
    b"DevName.bambu.com: P1S-1\r\n"
    b"DevSignal.bambu.com: -60\r\n"
    b"DevConnect.bambu.com: lan\r\n"
)


def test_parse_bambu_notify():
    p = parse_ssdp_notify(BAMBU_NOTIFY, "192.168.86.40")
    assert p == DiscoveredPrinter(ip="192.168.86.40", serial="01P00A3A3000666", name="P1S-1", model="C12")


def test_ip_falls_back_to_source_when_no_location():
    data = BAMBU_NOTIFY.replace(b"Location: 192.168.86.40\r\n", b"")
    p = parse_ssdp_notify(data, "192.168.86.99")
    assert p is not None
    assert p.ip == "192.168.86.99"          # source address of the datagram
    assert p.serial == "01P00A3A3000666"


def test_msearch_asks_bambu_printers_to_answer():
    packet = msearch_packet(2021).decode("ascii")
    assert packet.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert "HOST: 239.255.255.250:2021\r\n" in packet
    assert 'MAN: "ssdp:discover"\r\n' in packet
    assert "ST: urn:bambulab-com:device:3dprinter:1\r\n" in packet


def test_parse_msearch_response():
    """A solicited reply is an HTTP 200 with the same Bambu headers as a NOTIFY."""
    data = BAMBU_NOTIFY.replace(b"NOTIFY * HTTP/1.1\r\n", b"HTTP/1.1 200 OK\r\n")
    p = parse_ssdp_notify(data, "192.168.8.223")
    assert p is not None
    assert p.serial == "01P00A3A3000666"
    assert p.ip == "192.168.86.40"


def test_ignores_non_bambu_ssdp():
    # A generic UPnP device announcement (has USN, but no Bambu markers) is not a printer.
    data = (
        b"NOTIFY * HTTP/1.1\r\n"
        b"NT: urn:schemas-upnp-org:device:MediaServer:1\r\n"
        b"USN: uuid:1234::urn:schemas-upnp-org:device:MediaServer:1\r\n"
        b"Location: http://192.168.86.5:8200/\r\n"
    )
    assert parse_ssdp_notify(data, "192.168.86.5") is None


def test_ignores_bambu_notify_without_serial():
    data = BAMBU_NOTIFY.replace(b"USN: 01P00A3A3000666\r\n", b"")
    assert parse_ssdp_notify(data, "192.168.86.40") is None


def test_garbage_datagram_returns_none():
    assert parse_ssdp_notify(b"\x00\x01\x02not http at all", "192.168.86.7") is None


def test_discover_returns_empty_when_no_sockets(monkeypatch):
    # Privileged ports and the ephemeral fallback can all fail. discover()
    # still returns [] and does not raise.
    import bridge.discover as d
    monkeypatch.setattr(d, "_open_socket", lambda *args, **kwargs: None)
    monkeypatch.setattr(d, "_open_ephemeral_socket", lambda *args, **kwargs: None)
    assert discover(timeout=0.1) == []


def test_virtual_printer_serial_is_dropped_and_neighbor_is_kept():
    virtual = BAMBU_NOTIFY.replace(b"USN: 01P00A3A3000666\r\n", b"USN: 01P00A391800001\r\n")
    assert parse_ssdp_notify(virtual, "192.168.8.10") is None
    neighbor = BAMBU_NOTIFY.replace(b"USN: 01P00A3A3000666\r\n", b"USN: 01P00A391800002\r\n")
    kept = parse_ssdp_notify(neighbor, "192.168.8.10")
    assert kept is not None
    assert kept.serial == "01P00A391800002"


def test_both_privileged_binds_failing_sends_from_an_ephemeral_port(monkeypatch):
    import bridge.discover as d

    sent = []

    class Spy(socket.socket):
        def sendto(self, data, *args):
            addr = args[0] if args else None
            sent.append((bytes(data), addr, self.getsockname()[1]))
            return len(data)

    monkeypatch.setattr(d, "_open_socket", lambda *args, **kwargs: None)
    monkeypatch.setattr(d, "_private_scan_hosts", lambda: [])
    monkeypatch.setattr(d.socket, "socket", Spy)
    discover(timeout=0.2)
    assert sent, "expected an M-SEARCH from an ephemeral socket"
    assert {src for _, _, src in sent}.isdisjoint({1990, 2021})
    dest_ports = {addr[1] for _, addr, _ in sent if addr is not None}
    assert {1990, 2021} <= dest_ports
    assert any(data.startswith(b"M-SEARCH ") for data, _, _ in sent)


def test_no_iface_joins_private_address_and_skips_loopback(monkeypatch):
    import bridge.discover as d

    joined = []

    class Spy(socket.socket):
        def setsockopt(self, level, opt, value):
            if opt == socket.IP_ADD_MEMBERSHIP and isinstance(value, (bytes, bytearray)) and len(value) >= 8:
                joined.append(socket.inet_ntoa(value[4:8]))
            return super().setsockopt(level, opt, value)

    monkeypatch.setattr(
        d,
        "_interface_ipv4s",
        lambda: ["127.0.0.1", "169.254.8.8", "192.168.4.20", "192.168.5.20"],
    )
    monkeypatch.setattr(d, "_private_scan_hosts", lambda: [])
    monkeypatch.setattr(d.socket, "socket", Spy)
    discover(timeout=0.15, probe_ips=["192.168.4.9"])
    assert "192.168.4.20" in joined
    assert "192.168.5.20" in joined
    assert "127.0.0.1" not in joined
    assert "169.254.8.8" not in joined


def test_search_repeats_during_the_listen(monkeypatch):
    import bridge.discover as d

    monkeypatch.setattr(d, "_SEARCH_REPEAT_SECONDS", 0.05)
    times = []
    real = d._solicit

    def wrapped(sock, probe_ips=None):
        times.append(time.monotonic())
        return real(sock, probe_ips)

    monkeypatch.setattr(d, "_solicit", wrapped)
    discover(timeout=0.2, probe_ips=["10.1.1.1"])
    assert times
    assert max(times) - min(times) >= 0.05


def test_silent_cold_listen_probes_private_8883_then_unicasts(monkeypatch):
    import bridge.discover as d

    private_ips = [ip for ip in d._interface_ipv4s() if d._is_private_unicast(ip)]
    assert private_ips, "need a private IPv4 on this host to accept TCP 8883"
    private_ip = private_ips[0]
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((private_ip, 8883))
    server.listen(1)
    probed = []
    unicasts = []

    class Spy(socket.socket):
        def connect(self, addr):
            probed.append(tuple(addr))
            return super().connect(addr)

        def sendto(self, data, *args):
            if args:
                unicasts.append(args[0])
            return super().sendto(data, *args)

    monkeypatch.setattr(d, "_private_scan_hosts", lambda: [private_ip, "8.8.8.8"])
    monkeypatch.setattr(d.socket, "socket", Spy)
    timeout = 1.0
    started = time.monotonic()
    try:
        discover(timeout=timeout)
    finally:
        server.close()
    elapsed = time.monotonic() - started
    assert elapsed < timeout + 0.35
    assert (private_ip, 8883) in probed
    assert not any(addr[0] == "8.8.8.8" for addr in probed)
    assert any(
        isinstance(addr, tuple) and addr[0] == private_ip and addr[1] in (1990, 2021)
        for addr in unicasts
    )


def test_sweep_keeps_a_reply_that_arrives_after_the_budget(monkeypatch):
    import bridge.discover as d

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    found = {}

    def probe(_hosts, _deadline, on_accept=None):
        if on_accept is not None:
            on_accept("192.168.8.40")
        sock.sendto(BAMBU_NOTIFY, sock.getsockname())
        return ["192.168.8.40"]

    monkeypatch.setattr(d, "_unicast_search", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(d, "_probe_8883", probe)
    try:
        d._sweep_private_8883([sock], found, time.monotonic() - 1)
    finally:
        sock.close()
    assert "01P00A3A3000666" in found


def test_sweep_selects_for_a_reply_that_arrives_after_the_probe(monkeypatch):
    import bridge.discover as d

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    found = {}
    sent = {"n": 0}
    real_select = d.select.select

    def select_then_deliver(reads, writes, errors, timeout=None):
        if sock in reads and sent["n"] == 0:
            sent["n"] = 1
            sock.sendto(BAMBU_NOTIFY, sock.getsockname())
            return real_select(reads, writes, errors, 0)
        return real_select(reads, writes, errors, timeout)

    def probe(_hosts, _deadline, on_accept=None):
        return ["192.168.8.40"]

    monkeypatch.setattr(d, "_unicast_search", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(d, "_probe_8883", probe)
    monkeypatch.setattr(d.select, "select", select_then_deliver)
    try:
        d._sweep_private_8883([sock], found, time.monotonic() + 0.25)
    finally:
        sock.close()
    assert sent["n"] == 1
    assert "01P00A3A3000666" in found


def test_cold_listen_that_hears_a_printer_does_not_sweep(monkeypatch):
    import threading

    import bridge.discover as d

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)

    handed = {"used": False}

    def open_socket(_port, _iface_ips):
        if handed["used"]:
            return None
        handed["used"] = True
        return sock

    def hosts():
        raise AssertionError("cold sweep host list was built")

    def deliver():
        time.sleep(0.05)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(BAMBU_NOTIFY, sock.getsockname())
        finally:
            sender.close()

    monkeypatch.setattr(d, "_open_socket", open_socket)
    monkeypatch.setattr(d, "_open_ephemeral_socket", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(d, "_private_scan_hosts", hosts)
    thread = threading.Thread(target=deliver)
    thread.start()
    try:
        found = discover(timeout=0.35)
    finally:
        thread.join(1)
    assert any(printer.serial == "01P00A3A3000666" for printer in found)


def test_known_private_probe_does_not_sweep(monkeypatch):
    import bridge.discover as d

    def hosts():
        raise AssertionError("cold sweep host list was built")

    monkeypatch.setattr(d, "_private_scan_hosts", hosts)
    discover(timeout=0.15, probe_ips=["192.168.8.20"])


def test_public_probe_ip_does_not_open_the_cold_sweep(monkeypatch):
    import bridge.discover as d

    def hosts():
        raise AssertionError("cold sweep host list was built")

    monkeypatch.setattr(d, "_private_scan_hosts", hosts)
    discover(timeout=0.1, probe_ips=["8.8.8.8"])


def test_public_interface_range_is_not_a_scan_target(monkeypatch):
    import bridge.discover as d

    monkeypatch.setattr(d, "_interface_ipv4s", lambda: ["8.8.8.8", "203.0.113.4", "192.168.9.4"])
    hosts = d._private_scan_hosts()
    assert "192.168.9.40" in hosts
    assert "192.168.9.4" not in hosts
    assert "8.8.8.8" not in hosts
    assert "203.0.113.4" not in hosts
    assert "192.168.9.0" not in hosts
    assert "192.168.9.255" not in hosts
    assert all(ipaddress.ip_address(host).is_private for host in hosts)


def test_expand_probe_ips_covers_the_reserved_address_on_a_known_lan():
    # P1S-8 is already on 192.168.8.126. P1S-5 moved there (reserved .246) while
    # Link still dials 192.168.86.28. Multicast SSDP is silent, so the sweep has
    # to ask the rest of the known private /24.
    probes = expand_probe_ips(["192.168.86.28", "192.168.8.126"])
    assert "192.168.8.126" in probes
    assert "192.168.8.246" in probes
    assert "192.168.8.188" in probes
    assert "192.168.86.28" in probes
    assert "8.8.8.8" not in expand_probe_ips(["8.8.8.8", "192.168.8.126"])


def test_expand_probe_ips_skips_network_and_broadcast():
    probes = expand_probe_ips(["192.168.8.126"])
    assert "192.168.8.0" not in probes
    assert "192.168.8.255" not in probes
    assert probes.count("192.168.8.126") == 1
