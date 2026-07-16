from bridge.discover import DiscoveredPrinter, discover, parse_ssdp_notify

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
    # When neither SSDP port can be bound, discover() degrades to [] (never raises).
    import bridge.discover as d
    monkeypatch.setattr(d, "_open_socket", lambda port, iface_ip: None)
    assert discover(timeout=0.1) == []
