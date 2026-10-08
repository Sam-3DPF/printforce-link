---
title: A late 226 from a slow SD card was read as the SIZE reply and good uploads were deleted
date: 2026-09-30
category: integration-issues
module: bridge
problem_type: integration_issue
component: messaging
symptoms:
  - "Every send to one printer fails with upload failed (storage)"
  - "Link log shows ftps upload ... kind=storage after the full transfer time"
  - The file is gone from the SD card after the failed upload
  - A manual STOR of the same file on the same printer succeeds
root_cause: async_timing
resolution_type: code_fix
severity: high
tags:
  - ftps
  - size-check
  - closing-reply
  - sd-card
  - bambu
  - p1s
---

# A late 226 from a slow SD card was read as the SIZE reply and good uploads were deleted

## Problem

Shop P1S-6 failed 41 of 41 uploads from 2026-09-28 to 2026-09-30, and P1S-5 failed 6 of 11. Each file had landed on the card intact. Link reported "upload failed (storage)", deleted the file, and the print never started.

## Symptoms

- The cloud shows the send came back as `upload_failed; storage`.
- The Link log line is `ftps upload host=... bytes=... seconds=... kind=storage`. `seconds` is the full transfer time (10 to 15 s for a 400 to 500 KB file), not an early refusal.
- The affected printers stall their MQTT reports during FTP activity. Link marks them OFFLINE ("nothing new for 50s").
- Other printers on the same Link and the same files upload fine.

## What Didn't Work

- **Reading the Link log for the FTP reply.** `_perm` (`bridge/bambu/ftps.py:563`) maps every non-530 permanent error to `storage`, and the size check raises `storage` for a mismatch. The log never shows the actual reply, so it looks like a full or broken card.
- **Checking the card.** The card was not full. A 20-byte and a 600 KB test file both uploaded and passed SIZE by hand.
- **Suspecting the file.** The same file uploads fine through a manual STOR that blocks on the closing reply.

## Solution

Reproduce with both paths on the same connection, against the real printer:

- Link's `ftps.upload`: `kind=storage`, "remote size does not match".
- A manual `ntransfercmd("STOR ...")`, send, close, then `ftp.voidresp()`: `226` arrives about 12.5 s after the start, then `SIZE` answers `213 520136`, which is exact.

After STOR, `_await_closing` (`bridge/bambu/ftps.py:481`) waits only `_CLOSING_REPLY_SECONDS = 2.0` (`bridge/bambu/ftps.py:66`) for the closing reply. That wait keeps a printer that never sends 226 from holding the upload for the whole deadline. On a slow card the 226 comes later than 2 s. `SIZE` is already sent, and `ftplib.FTP.size()` returns the first reply it reads. That is the stale `226`, so `size()` returns `None`. `_require_size` sees `None != expected`, deletes the good file, and raises `storage`.

`_read_size` (`bridge/bambu/ftps.py:516`, [PR #83](https://github.com/Sam-3DPF/printforce-link/pull/83)) sends SIZE itself and skips one late closing reply before reading the 213:

```python
ftp.putcmd(f"SIZE {remote_path}")
try:
    resp = ftp.getresp()
except ftplib.error_temp as exc:      # a late 426 raises
    if not str(exc).lstrip().startswith("426"):
        raise
    _arm(ftp, deadline_at, clock)     # the second read gets the time left, not a fresh window
    resp = ftp.getresp()
else:
    if resp.startswith("226"):
        _arm(ftp, deadline_at, clock)
        resp = ftp.getresp()
```

The 2 s closing wait is unchanged, so printers that never send 226 are not slowed down.

## Why This Works

On the shop printers observed, vsftpd answers in command order: the late `226` came first, then the `213`. After STOR, the only reply that can be pending ahead of SIZE's is the one closing reply. Skipping exactly one 226 or 426 brings the replies back in line. A genuinely short file still fails, because the 213 behind the skipped reply carries the wrong size. `test_late_226_with_mismatched_size_is_storage` checks that case.

## Prevention

- An FTP client that stops waiting for a reply has not cancelled it. The next command reads it. Any timed-out wait for a reply must be followed by skipping that reply, or the connection must be dropped.
- `tests/test_ftps.py:217` and `:231` cover a late 226 and a late 426. Both fail when run against `main`'s `ftps.py`. They set `_CLOSING_REPLY_SECONDS` to 0.2 s and delay the reply 0.7 s with the fixture's `stor_reply_delay_seconds`, so they stay meaningful if the constant changes.
- Spot slow cards from the Link log: median `bytes / seconds` per `host=` on `ftps upload` lines. The healthy shop cards run about 100 to 165 KB/s. The two failing cards ran about 35 and 48 KB/s.
- Residual: `_cleanup_partial` has the same shape (a late 226 read as DELE's reply). The impact is low because the connection closes straight after.

## Diagnostic notes for the next printer bug

- **Listing a P1 card.** A stock `ftplib` `retrlines("LIST")` hung during this investigation. The `_read_list` docstring (`bridge/bambu/ftps.py:665`) says a P1 data socket hangs on SSL shutdown. Read the data socket raw and close it without `unwrap`, as `_read_list` does.
- **Traces of started jobs.** The printer unpacks each started job into `/cache/<name>_plate_1.gcode` and then writes `/cache/1_<name>.bbl`, a small JSON copy of the start command. A gcode with no `.bbl` means the printer unpacked the file and then refused the start. That was the shop's separate `print_error 0500_4003` case, still open at the time of writing.
- **Link used to delete a file only when a size check fails or an upload is cancelled.** It never removed the files of jobs that uploaded successfully, so the shop cards held months of them. Since 2026-10-08 it clears old job files before each upload; see [old-job-files-and-0500-4003.md](old-job-files-and-0500-4003.md).

## Related Issues

- [implicit-ftps-upload.md](implicit-ftps-upload.md): the closing-reply and SIZE-check design this refines.
- [PR #83](https://github.com/Sam-3DPF/printforce-link/pull/83): the fix. It was not merged when this was written, and the shop Mac gets it only after a tagged release.
