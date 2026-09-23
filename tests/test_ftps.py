"""Implicit FTPS uploads against a local server that speaks TLS before the 220."""
import logging
import shutil
import socket
import threading
import time
from dataclasses import replace

import ftplib
import pytest

from bridge.bambu.models import P1_PROFILE
from bridge.config import PrinterConfig
from bridge.gencert import generate
from bridge.printer import BambuPrinter
from tests.fixtures.implicit_ftps_server import ImplicitFtpsServer

SECRET = "ACCESS-CODE-9f3a-NOT-IN-ERRORS"
_CONNECT = 2.0


@pytest.fixture(scope="session")
def ftps_cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not available")
    directory = tmp_path_factory.mktemp("ftps-cert")
    try:
        return generate("127.0.0.1", str(directory))
    except FileNotFoundError:
        pytest.skip("openssl is not available")


@pytest.fixture
def implicit_server(ftps_cert):
    running = []

    def open_server(**kwargs):
        kwargs.setdefault("password", SECRET)
        server = ImplicitFtpsServer(ftps_cert[0], ftps_cert[1], **kwargs)
        server.start()
        running.append(server)
        return server

    yield open_server
    for server in running:
        server.stop()


def _upload():
    from bridge.bambu.ftps import upload
    return upload


def _file(tmp_path, payload=b"3mf-bytes"):
    path = tmp_path / "job.3mf"
    path.write_bytes(payload)
    return path, payload


def _secret_hidden(caplog, *texts):
    blob = "\n".join(texts) + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in blob


def _log_text(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


def test_deadline_scales_with_file_size():
    """max(base_seconds, size_bytes / (25 * 1024)) + overhead_seconds.

    Defaults are a 30s base and 5s of command overhead. 25 KiB/s is the
    slowest transfer the deadline still waits out.
    """
    from bridge.bambu.ftps import transfer_deadline_seconds

    floor = 25 * 1024
    assert transfer_deadline_seconds(0) == 35.0
    assert transfer_deadline_seconds(floor) == 35.0
    assert transfer_deadline_seconds(floor * 30) == 35.0
    assert transfer_deadline_seconds(floor * 100) == 105.0
    assert transfer_deadline_seconds(floor * 100, base_seconds=200) == 205.0
    assert transfer_deadline_seconds(0, base_seconds=0, overhead_seconds=0.5) == 0.5


def test_explicit_tls_cannot_finish_the_implicit_handshake(implicit_server):
    """FTP_TLS.connect reads a plaintext 220, then AUTH TLS. Port 990 has neither."""
    server = implicit_server()
    ftp = ftplib.FTP_TLS()
    with pytest.raises(Exception):
        ftp.connect("127.0.0.1", server.port, timeout=0.5)
        ftp.auth()
    try:
        ftp.close()
    except Exception:
        pass


def test_implicit_client_stores_the_file(implicit_server, tmp_path, caplog):
    server = implicit_server()
    path, payload = _file(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="bridge.bambu.ftps"):
        name = _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=server.port, connect_timeout=_CONNECT, profile=P1_PROFILE,
        )
    assert name == "job.3mf"
    assert server.snapshot()["files"]["job.3mf"] == payload
    _secret_hidden(caplog)


def test_data_socket_reuses_the_control_session(implicit_server, tmp_path):
    server = implicit_server(require_session_reuse=True)
    path, payload = _file(tmp_path)
    _upload()(
        "127.0.0.1", SECRET, str(path), "job.3mf",
        port=server.port, connect_timeout=_CONNECT, profile=P1_PROFILE,
    )
    assert server.snapshot()["reused"] == [True]
    assert server.snapshot()["files"]["job.3mf"] == payload


def test_data_socket_without_session_reuse_is_refused(implicit_server, tmp_path):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server(require_session_reuse=True)
    path, _payload = _file(tmp_path)
    profile = replace(P1_PROFILE, ftps_session_reuse=False)
    with pytest.raises(FtpsError) as caught:
        _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=server.port, connect_timeout=_CONNECT, profile=profile,
        )
    assert caught.value.kind in ("network", "storage")
    assert SECRET not in str(caught.value)
    assert server.snapshot()["reused"] == [False]


def test_stor_is_preceded_by_dele_and_a_550_is_ok(implicit_server, tmp_path):
    server = implicit_server(dele_reply="550 No such file.")
    path, payload = _file(tmp_path)
    _upload()(
        "127.0.0.1", SECRET, str(path), "job.3mf",
        port=server.port, connect_timeout=_CONNECT,
    )
    snap = server.snapshot()
    verbs = [line.split(" ", 1)[0].upper() for line in snap["commands"]]
    assert verbs.index("DELE") < verbs.index("STOR")
    assert "550 No such file." in snap["replies"]
    assert snap["files"]["job.3mf"] == payload


def test_size_mismatch_is_storage(implicit_server, tmp_path):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server(size_override=1)
    path, _payload = _file(tmp_path)
    with pytest.raises(FtpsError) as caught:
        _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=server.port, connect_timeout=_CONNECT,
        )
    assert caught.value.kind == "storage"
    assert SECRET not in str(caught.value)


def test_trailing_426_with_matching_size_succeeds(implicit_server, tmp_path):
    server = implicit_server(stor_final_reply="426 Failure reading network stream.")
    path, payload = _file(tmp_path)
    name = _upload()(
        "127.0.0.1", SECRET, str(path), "job.3mf",
        port=server.port, connect_timeout=_CONNECT,
    )
    assert name == "job.3mf"
    assert server.snapshot()["files"]["job.3mf"] == payload


def test_missing_226_with_matching_size_succeeds(implicit_server, tmp_path):
    server = implicit_server(stor_final_reply=None)
    path, payload = _file(tmp_path)
    name = _upload()(
        "127.0.0.1", SECRET, str(path), "job.3mf",
        port=server.port, connect_timeout=_CONNECT,
        base_seconds=1, overhead_seconds=5,
    )
    assert name == "job.3mf"
    assert server.snapshot()["files"]["job.3mf"] == payload


def test_trailing_426_with_mismatched_size_is_storage(implicit_server, tmp_path):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server(
        stor_final_reply="426 Failure reading network stream.",
        size_override=1,
    )
    path, _payload = _file(tmp_path)
    with pytest.raises(FtpsError) as caught:
        _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=server.port, connect_timeout=_CONNECT,
        )
    assert caught.value.kind == "storage"
    assert SECRET not in str(caught.value)


def test_plaintext_on_the_implicit_port_is_a_handshake_failure(implicit_server, tmp_path, caplog):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server(plaintext=True)
    path, _payload = _file(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="bridge.bambu.ftps"):
        with pytest.raises(FtpsError) as caught:
            _upload()(
                "127.0.0.1", SECRET, str(path), "job.3mf",
                port=server.port, connect_timeout=_CONNECT,
            )
    assert caught.value.kind == "handshake"
    assert caught.value.kind != "storage"
    _secret_hidden(caplog, str(caught.value))


def test_wrong_password_is_auth(implicit_server, tmp_path, caplog):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server()
    path, _payload = _file(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="bridge.bambu.ftps"):
        with pytest.raises(FtpsError) as caught:
            _upload()(
                "127.0.0.1", "not-the-code", str(path), "job.3mf",
                port=server.port, connect_timeout=_CONNECT,
            )
    assert caught.value.kind == "auth"
    assert "not-the-code" not in str(caught.value)
    assert "not-the-code" not in _log_text(caplog)
    assert SECRET not in str(caught.value)
    _secret_hidden(caplog, str(caught.value))
    # The server saw the password; the client must not repeat it.
    assert any(line.startswith("PASS ") for line in server.snapshot()["commands"])


def test_connection_refused_is_network(tmp_path):
    from bridge.bambu.ftps import FtpsError

    path, _payload = _file(tmp_path)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(FtpsError) as caught:
        _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=port, connect_timeout=_CONNECT,
        )
    assert caught.value.kind == "network"
    assert SECRET not in str(caught.value)


def test_stalled_transfer_past_the_deadline_is_timeout(implicit_server, tmp_path):
    from bridge.bambu.ftps import FtpsError

    server = implicit_server(stall_seconds=5)
    path, _payload = _file(tmp_path, b"x" * 64)
    with pytest.raises(FtpsError) as caught:
        _upload()(
            "127.0.0.1", SECRET, str(path), "job.3mf",
            port=server.port, connect_timeout=_CONNECT,
            base_seconds=0.2, overhead_seconds=0.2,
        )
    assert caught.value.kind == "timeout"
    assert SECRET not in str(caught.value)
    assert any(line.startswith("USER ") for line in server.snapshot()["commands"])


def test_missing_local_file_is_not_found(tmp_path):
    from bridge.bambu.ftps import FtpsError

    missing = tmp_path / "absent.3mf"
    with pytest.raises(FtpsError) as caught:
        _upload()("127.0.0.1", SECRET, str(missing), "absent.3mf")
    assert caught.value.kind == "not_found"
    assert SECRET not in str(caught.value)


def test_upload_cancelled_is_the_transfer_exception():
    from bridge.bambu.ftps import UploadCancelled as FromFtps
    from bridge.transfer import UploadCancelled as FromTransfer

    assert FromTransfer is FromFtps


def test_cancel_between_blocks_raises_upload_cancelled(implicit_server, tmp_path, caplog):
    from bridge.bambu.ftps import UploadCancelled, _BLOCK_BYTES

    server = implicit_server()
    payload = b"a" * (_BLOCK_BYTES + 50)
    path, _payload = _file(tmp_path, payload)

    class _CancelOnSecondCheck(threading.Event):
        """The client checks once per block. The second check is the second block."""

        def __init__(self):
            super().__init__()
            self.checks = 0

        def is_set(self):
            self.checks += 1
            if self.checks >= 2:
                super().set()
            return super().is_set()

    cancel = _CancelOnSecondCheck()
    with caplog.at_level(logging.DEBUG, logger="bridge.bambu.ftps"):
        with pytest.raises(UploadCancelled) as caught:
            _upload()(
                "127.0.0.1", SECRET, str(path), "job.3mf",
                port=server.port, connect_timeout=_CONNECT, cancel=cancel,
            )
    assert SECRET not in str(caught.value)
    _secret_hidden(caplog, str(caught.value))
    deadline = time.monotonic() + 2
    stored = b""
    while time.monotonic() < deadline:
        stored = server.snapshot()["files"].get("job.3mf", b"")
        if stored:
            break
        time.sleep(0.01)
    assert 0 < len(stored) < len(payload)
    assert len(stored) == _BLOCK_BYTES


def test_second_upload_to_the_same_printer_waits(implicit_server, tmp_path, monkeypatch):
    hold = threading.Event()
    server = implicit_server(hold_before_reply=hold)
    path, _payload = _file(tmp_path, b"abc")
    calls = []
    real_connect = socket.create_connection

    def spy(address, *args, **kwargs):
        calls.append((address, time.monotonic()))
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", spy)
    errors = []

    def run(name):
        try:
            _upload()(
                "127.0.0.1", SECRET, str(path), name,
                port=server.port, connect_timeout=_CONNECT,
                base_seconds=2, overhead_seconds=4,
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run, args=("a.3mf",), daemon=True)
    second = threading.Thread(target=run, args=("b.3mf",), daemon=True)
    started_second = False
    first.start()
    try:
        assert server.stor_blocked.wait(5), "first STOR did not reach the hold"
        while len(calls) < 2 and first.is_alive():
            time.sleep(0.01)
        opened = len(calls)
        second.start()
        started_second = True
        time.sleep(0.3)
        assert len(calls) == opened, "second upload connected while the first was still in STOR"
        hold.set()
        first.join(5)
        second.join(5)
    finally:
        hold.set()
        first.join(2)
        if started_second:
            second.join(2)
    assert errors == []
    assert not first.is_alive() and not second.is_alive()
    snap = server.snapshot()
    verbs = [line.split(" ", 1)[0].upper() for line in snap["commands"]]
    assert verbs.count("STOR") == 2
    assert snap["accepted_at"][1] >= snap["closed_at"][0]


def test_printer_upload_records_events_and_hides_the_access_code(
    implicit_server, tmp_path, monkeypatch, caplog,
):
    import bridge.bambu.ftps as ftps_mod

    server = implicit_server()
    path, payload = _file(tmp_path, b"printer-bytes")
    seen = {}
    real = ftps_mod.upload

    def wrapped(host, access_code, local_path, remote_name, **kwargs):
        seen["profile"] = kwargs.get("profile")
        seen["cancel"] = kwargs.get("cancel")
        kwargs["port"] = server.port
        return real(host, access_code, local_path, remote_name, **kwargs)

    monkeypatch.setattr(ftps_mod, "upload", wrapped)
    cancel = threading.Event()
    printer = BambuPrinter(PrinterConfig(
        bambu_id="01P00C000000001", ip="127.0.0.1", access_code=SECRET,
        name="P1S", model="C12",
    ))
    printer._session = object()
    with caplog.at_level(logging.DEBUG):
        assert printer.upload_file(str(path), cancel=cancel) == "job.3mf"
    assert seen["cancel"] is cancel
    assert seen["profile"] is not None
    assert seen["profile"].ftps_session_reuse is True
    assert seen["profile"].name == "P1S"
    assert server.snapshot()["files"]["job.3mf"] == payload
    events = [event for event in printer.log.export()["events"] if event.get("kind") == "upload"]
    assert [event.get("phase") for event in events] == ["start", "result"]
    assert events[1]["result"] == "ok"
    assert events[1]["bytes"] == len(payload)
    assert events[1]["seconds"] >= 0
    blob = " ".join(record.getMessage() for record in caplog.records)
    assert SECRET not in blob
    assert SECRET not in str(events)
    assert "[redacted]" not in str(events)


def test_upload_requires_a_connected_session(tmp_path):
    path, _payload = _file(tmp_path)
    printer = BambuPrinter(PrinterConfig(
        bambu_id="01P00C000000001", ip="127.0.0.1", access_code=SECRET, name="P1S",
    ))
    with pytest.raises(RuntimeError):
        printer.upload_file(str(path))
