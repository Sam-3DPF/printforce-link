---
title: Bambu file upload is implicit FTPS on port 990 with a size check
date: 2026-09-23
category: integration-issues
module: bridge
problem_type: integration_issue
component: messaging
symptoms:
  - A stock FTP_TLS client never finishes the handshake on the printer
  - The data connection is refused when it does not resume the control TLS session
  - A trailing 426 or a missing 226 was treated as success for a short file
root_cause: wrong_api
resolution_type: code_fix
severity: high
tags:
  - ftps
  - implicit-tls
  - port-990
  - file-upload
  - bambu
---

# Bambu file upload is implicit FTPS on port 990 with a size check

## Problem

The printer's file port is implicit FTPS. A normal explicit-TLS client waits for a plaintext 220 and then sends AUTH TLS. The printer has already wrapped the socket, so that client never finishes the handshake and the sliced file never lands.

## Symptoms

- `ftplib.FTP_TLS` cannot complete the banner exchange on port 990.
- P1 vsftpd refuses a data connection that does not resume the control connection's TLS session.
- STOR of a name that already exists returns 553.
- A trailing 426, or a 226 that never arrives, can follow a transfer that is already complete or one that was cut short.

## What Didn't Work

Using stock `FTP_TLS` (plaintext banner, then AUTH TLS). Opening the data socket without the control session. Treating the STOR reply alone as proof the file arrived. Waiting out the full size-based deadline for a 226 that a P1 sends as soon as the data connection closes, which held the printer lock after the bytes were on disk. Calling SSL `unwrap` on the data socket. Some P1 firmware hangs on that shutdown after STOR.

## Solution

`_ImplicitFtps` wraps the control socket before it reads the banner, on port 990, user `bblp`, password the LAN access code. The data socket wraps with the control connection's TLS session. The remote name is deleted before STOR. A 550 from DELE is fine (nothing was there). The data socket is closed without `unwrap`.

Success is SIZE matching the local file. A trailing 426 or a missing 226 is success only in that case. A short copy is `storage`, not uploaded. One transfer per printer at a time. The access code is not put in an exception or a log line.

The deadline is `max(30 seconds, size_bytes / (25 * 1024)) + 5 seconds`. 25 KiB/s is the slowest rate that still gets the full window. The closing reply wait is 2 seconds, not the whole deadline.

## Why This Works

Implicit TLS means the first bytes on port 990 are a handshake, not a 220. Session reuse is what vsftpd on the P1 allows for the data port. SIZE is the proof that survives a messy STOR reply. The access code stays out of errors because those strings are logged.

## Prevention

- Do not point `ftplib.FTP_TLS.connect` at port 990 and expect AUTH TLS to run.
- Resume the control TLS session on the data socket.
- DELE before STOR. Confirm with SIZE. Do not treat 426 alone as success or as failure.
- Keep the access code out of exception text.
- `tests/test_ftps.py` includes `test_explicit_tls_cannot_finish_the_implicit_handshake` and the SIZE mismatch cases.

## Related Issues

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/bambu/ftps.py`, `tests/test_ftps.py`.
