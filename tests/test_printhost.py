"""Tests for the OctoPrint print-host endpoint logic (U7).

Exercises PrintHostService directly — the security-critical surface (auth, size
limits, filename sanitization, path traversal, correlation extraction, enqueue)
— without a socket or TLS. The TLS transport glue is covered by the manual
OrcaSlicer smoke test in the README.
"""
import io
import os
import zipfile

import pytest

from bridge.printhost import (
    PrintHostService,
    sanitize_upload_filename,
    read_embedded_batch_name,
    resolve_correlation,
    _parse_multipart,
)
from bridge.router import Router, QUEUED, UNRESOLVED

UPLOAD_KEY = "UPLOADSECRETKEY123"
BATCH_NAME = "batch-2026-02-20-JyBIcozw-{plate_num}.3mf"


def _service(tmp_path):
    router = Router(str(tmp_path / "queue.json"))
    return PrintHostService(
        upload_key=UPLOAD_KEY,
        spool_dir=str(tmp_path / "spool"),
        router=router,
        max_bytes=1024 * 1024,
    )


def _threemf_with_plater_name(name: str) -> bytes:
    """A minimal .3mf carrying 3DPF's plater_name metadata."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Metadata/model_settings.config",
            f'<config><plate><metadata key="plater_name" value="{name}"/></plate></config>',
        )
    return buf.getvalue()


def _multipart(filename: str, content: bytes, extra_fields=None):
    """Build a multipart/form-data body + content-type header."""
    boundary = "----boundaryXYZ"
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                 + content + b"\r\n")
    for k, v in (extra_fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Api-Key": UPLOAD_KEY,  # authed by default; the wrong-key test overrides
    }
    return headers, body


# --- filename sanitization ---------------------------------------------------

def test_sanitize_accepts_batch_and_placeholder_names():
    assert sanitize_upload_filename("batch-2026-02-20-JyBIcozw-1.3mf") == "batch-2026-02-20-JyBIcozw-1.3mf"
    assert sanitize_upload_filename(BATCH_NAME) == BATCH_NAME


@pytest.mark.parametrize("bad", [
    "../config.toml",
    "../../etc/passwd",
    "a/b.3mf",
    "a\\b.3mf",
    "..3mf",           # bare parent-ish
    "foo.toml",        # wrong extension
    "foo.3mf.toml",
    "with space.3mf",
    "\x00evil.3mf",
    "",
])
def test_sanitize_rejects_unsafe_names(bad):
    assert sanitize_upload_filename(bad) is None


# --- version auth ------------------------------------------------------------

def test_version_requires_valid_key(tmp_path):
    svc = _service(tmp_path)
    assert svc.handle_version({"X-Api-Key": UPLOAD_KEY})[0] == 200
    assert svc.handle_version({"X-Api-Key": "wrong"})[0] == 401
    assert svc.handle_version({})[0] == 401


# --- upload happy path -------------------------------------------------------

def test_upload_stores_safely_and_enqueues_with_correlation(tmp_path):
    svc = _service(tmp_path)
    content = _threemf_with_plater_name(BATCH_NAME)
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", content)

    status, payload = svc.handle_upload(headers, body)

    assert status == 201 and payload["done"] is True
    jobs = svc.router.pending()
    assert len(jobs) == 1
    job = jobs[0]
    # Embedded plater_name wins as the correlation key.
    assert job.correlation_key == BATCH_NAME
    assert job.status == QUEUED
    # Stored path is server-generated inside the spool dir — NOT the client name.
    assert os.path.dirname(job.stored_path) == str(tmp_path / "spool")
    assert os.path.basename(job.stored_path) != "batch-2026-02-20-JyBIcozw-1.3mf"
    assert os.path.exists(job.stored_path)


def test_upload_falls_back_to_filename_when_no_metadata(tmp_path):
    svc = _service(tmp_path)
    # A .3mf with no plater_name metadata -> filename is the correlation key.
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", plain.getvalue())

    status, payload = svc.handle_upload(headers, body)

    assert status == 201
    job = svc.router.pending()[0]
    assert job.correlation_key == "batch-2026-02-20-JyBIcozw-1.3mf"
    assert job.status == QUEUED


def test_upload_honors_print_flag(tmp_path):
    svc = _service(tmp_path)
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf",
                               _threemf_with_plater_name(BATCH_NAME),
                               extra_fields={"print": "true"})
    svc.handle_upload(headers, body)
    assert svc.router.pending()[0].print_flag is True


# --- security: auth + traversal + size --------------------------------------

def test_upload_rejects_wrong_key_without_touching_disk(tmp_path):
    svc = _service(tmp_path)
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", b"data")
    headers["X-Api-Key"] = "wrong"
    status, _ = svc.handle_upload(headers, body)
    assert status == 401
    assert svc.router.pending() == []
    assert not os.listdir(str(tmp_path / "spool"))


def test_upload_rejects_traversal_filename(tmp_path):
    svc = _service(tmp_path)
    headers, body = _multipart("../config.toml", _threemf_with_plater_name(BATCH_NAME))
    status, payload = svc.handle_upload(headers, body)
    assert status == 400
    assert svc.router.pending() == []
    # Nothing escaped the spool dir, and nothing was written at all.
    assert not os.path.exists(str(tmp_path / "config.toml"))
    assert not os.listdir(str(tmp_path / "spool"))


def test_upload_rejects_oversized(tmp_path):
    svc = _service(tmp_path)
    svc.max_bytes = 10
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", b"x" * 100)
    status, _ = svc.handle_upload(headers, body)
    assert status == 413
    assert svc.router.pending() == []


def test_upload_rejects_non_3mf(tmp_path):
    svc = _service(tmp_path)
    headers, body = _multipart("model.gcode", b"data")
    status, _ = svc.handle_upload(headers, body)
    assert status == 400
    assert svc.router.pending() == []


# --- unresolved is held, not dropped ----------------------------------------

def test_unroutable_name_enqueued_unresolved(tmp_path):
    svc = _service(tmp_path)
    # Valid .3mf name, but not a batch key and no metadata -> UNRESOLVED, still stored.
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    headers, body = _multipart("random-model.3mf", plain.getvalue())

    status, payload = svc.handle_upload(headers, body)

    assert status == 201
    job = svc.router.pending()[0]
    assert job.status == UNRESOLVED
    assert job.correlation_key == "random-model.3mf"
    assert os.path.exists(job.stored_path)  # held on disk, surfaced — not dropped


# --- helpers -----------------------------------------------------------------

def test_read_embedded_batch_name_handles_non_zip():
    assert read_embedded_batch_name(b"not a zip") is None


def test_resolve_correlation_prefers_metadata_over_filename():
    tmf = _threemf_with_plater_name(BATCH_NAME)
    key, status = resolve_correlation(tmf, "some-other-name.3mf")
    assert key == BATCH_NAME and status == QUEUED


def test_parse_multipart_extracts_file_and_field():
    headers, body = _multipart("x.3mf", b"BODY", extra_fields={"print": "true"})
    file_field, fields = _parse_multipart(body, headers["Content-Type"])
    assert file_field == ("x.3mf", b"BODY")
    assert fields["print"] == "true"


def test_parse_multipart_preserves_trailing_crlf_bytes():
    # Payload that legitimately ends in CR/LF must survive the boundary strip.
    payload = b"PK\x03\x04...zipbytes...\r\n\n"
    headers, body = _multipart("x.3mf", payload)
    file_field, _ = _parse_multipart(body, headers["Content-Type"])
    assert file_field == ("x.3mf", payload)  # not truncated


def test_parse_multipart_no_boundary_returns_empty():
    file_field, fields = _parse_multipart(b"whatever", "application/json")
    assert file_field is None and fields == {}


# --- byte fidelity end to end (the CR/LF-strip regression) -------------------

def test_stored_bytes_equal_uploaded_bytes_incl_trailing_lf(tmp_path):
    svc = _service(tmp_path)
    # A valid batch .3mf whose raw bytes end in 0x0A — a greedy strip would eat it.
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    payload = plain.getvalue() + b"\n\n"
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", payload)

    svc.handle_upload(headers, body)

    stored = svc.router.pending()[0].stored_path
    with open(stored, "rb") as f:
        assert f.read() == payload


# --- malformed / missing file ------------------------------------------------

def test_upload_no_file_part_is_400(tmp_path):
    svc = _service(tmp_path)
    # A multipart body with only a form field, no file part.
    boundary = "----b"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="print"\r\n\r\ntrue\r\n'
            f'--{boundary}--\r\n').encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Api-Key": UPLOAD_KEY}
    status, _ = svc.handle_upload(headers, body)
    assert status == 400
    assert svc.router.pending() == []


# --- zip-bomb guard ----------------------------------------------------------

def test_oversized_metadata_entry_skipped_falls_back_to_filename(tmp_path):
    svc = _service(tmp_path)
    from bridge.printhost import _MAX_METADATA_BYTES
    # A model_settings.config that decompresses past the cap -> metadata skipped,
    # correlation falls back to the (valid batch) filename. Compresses tiny, so the
    # 200MB body cap never trips; only the decompress guard does.
    big = io.BytesIO()
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/model_settings.config",
                    b"A" * (_MAX_METADATA_BYTES + 1024))
    headers, body = _multipart("batch-2026-02-20-JyBIcozw-1.3mf", big.getvalue())

    status, _ = svc.handle_upload(headers, body)

    assert status == 201
    job = svc.router.pending()[0]
    # Fell back to the filename rather than OOMing on the bomb.
    assert job.correlation_key == "batch-2026-02-20-JyBIcozw-1.3mf"
    assert job.status == QUEUED
