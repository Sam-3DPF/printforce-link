---
title: Clear old job files from the card, and re-upload before restarting after 0500_4003
date: 2026-10-08
category: integration-issues
module: bridge
problem_type: integration_issue
component: ftps
symptoms:
  - "A start raises print_error 0500_4003 about 20 s after the upload, after 2-8 s of PREPARE"
  - "Restarting the same copy on the card raises 0500_4003 again within seconds"
  - "The same file prints later after a fresh upload"
root_cause: unknown_printer_side
resolution_type: mitigation
severity: high
tags:
  - 0500_4003
  - sd-card
  - ftps
  - send-watchdog
---

# Clear old job files from the card, and re-upload before restarting after 0500_4003

## Problem

Shop P1S-7's Link log for 2026-10-06 to 10-08 recorded 9 `0500_4003` errors across 29 sends. The error context (U7) ruled out the leads we had:

- No session event (reset, redial, disconnect) in the window before any of the 9.
- Every upload succeeded in 3-7 s. The files were 0.49-1.06 MB, all with one plate.
- Every mapped tray read present.

Two things did stand out. The printer gave up quickly: about 20 s after the start, after only 2-8 s of PREPARE (good starts prepare for 6-14 s). And restarting the copy already on the card did not help. At 18:19 on 10-08 the watchdog restarted the same copy twice, at 18:22 and 18:26, and both failed within 6 s. Four other files failed once and then printed after a later fresh upload.

Link also never deleted the files of jobs that uploaded successfully. The printer keeps an unpacked copy of every started job in `/cache` (`<name>_plate_N.gcode`, `N_<name>.bbl`), so the shop cards held months of both. Bambu forum reports of this error were fixed by wiping or replacing the card.

The cause of `0500_4003` is still not proven. Both changes below are mitigations aimed at what the log does show.

## Solution

1. **Clear old job files before each upload.** `BambuPrinter.clear_old_job_files` runs `ftps.remove_files` in one FTPS session. It deletes names at the card root and in `/cache` that match a 3D PrintForce job name (`batch-YYYY-MM-DD-<8>-…` or `sq-…`, `is_job_file`).
   - At most 50 names per upload, so a full card drains over a few sends instead of holding one start.
   - The printer is idle at that point, because an upload only happens then.
   - Other files on the card are left alone.
   - A failure never blocks the upload. It is logged as an `sd_cleanup` event, and that printer skips cleanup for an hour.
2. **Re-upload before a restart after `0500_4003`.** When the watchdog restarts a send (republish or retry) and the printer's `print_error` is `0500_4003`, `_fresh_copy_after_parse_failure` uploads the local file again first. Cleanup runs first, so the stale `/cache` copy goes too. An upload failure falls back to restarting the copy on the card.
3. **The Link log shows it.** Each `sd_cleanup` event carries `found`, `deleted` and `failed`, so Collect log shows how many old files a printer had.

## Trade-offs

- A printer's screen can no longer reprint an old 3D PrintForce job from its card. Re-send from Ready to print instead.
- A file with a name that does not follow the 3D PrintForce pattern is never cleaned.

## Prevention

- Do not leave uploaded job files on a printer card indefinitely.
- After a parse failure, do not restart the same card copy. Upload it again.
- Tests: `tests/test_ftps.py` (`remove_files`, cleanup before upload, a failed cleanup does not block, backoff) and `tests/test_cloud_sends.py` (fresh copy after `0500_4003`, none for other restarts, upload failure falls back).
