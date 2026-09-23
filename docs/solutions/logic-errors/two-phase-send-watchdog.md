---
title: A start is confirmed by a two-phase watchdog that republishes only after reconnect
date: 2026-09-23
category: logic-errors
module: bridge
problem_type: logic_error
component: background_job
symptoms:
  - MQTT start_print returning true was treated as proof the printer had started
  - A phase A timeout reset the session and the same pass published again before the broker answered
  - That failed publish was latched as the final failure, so attempts two and three never ran
  - A failure latch for one printer was cleared by another printer's pass
root_cause: async_timing
resolution_type: code_fix
severity: high
tags:
  - send-watchdog
  - project-file
  - hard-reset
  - republish
  - bambu
---

# A start is confirmed by a two-phase watchdog that republishes only after reconnect

## Problem

`project_file` returning true only means the publish was accepted locally. The printer may still be silent, or it may have echoed the submission id without leaving the ready state. A send needs a deadline, a named reason, and one upload. Resetting the session and publishing again in the same breath drops the retry.

## Symptoms

- A send was marked dispatched on the publish result, or retried in a loop with no deadline.
- After a phase A timeout, the hard reset returned before the broker answered, the immediate republish failed, and the send latched failed.
- Attempts two and three never ran.
- One printer's pass cleared another printer's failure latch.

## What Didn't Work

Treating a true return from `start_print` as "the job is running". Republishing in the same pass as `hard_reset`. Counting a publish that returns false while the session is down as a spent attempt. Resetting again during phase B while the printer is still parsing the file. That second reset is what produces HMS `0500_4003`. Reporting failure on an empty cloud ack, which loses the send. Clearing every failure latch when any printer is visited.

## Solution

Phase A is 90 seconds. An active state confirms the send: status PRINTING or PAUSED, or `gcode_state` PREPARE, SLICING, RUNNING, or PAUSE. An echo of this send's submission id with no active state yet moves to phase B. Phase B is 180 seconds and confirms only on an active state.

A phase A timeout hard-resets the session and sets `pending_republish`. The next publish waits until the session is connected. A printer with no session (the test fakes) may publish on the following pass. A publish that returns false while the session is down stays pending and does not increment the attempt. If that reconnect window expires with no publish, the attempt count advances. After three attempts the timeout reasons are `no_echo` (phase A) and `no_active` (phase B). `commands_rejected` fails on the pass the snapshot says so. It does not wait for the attempt budget.

A phase B timeout publishes again without a reset. The file is uploaded once. A stop while the send is still being confirmed drops it and does not report failure (`_cancel_cloud_send`). `report_failed` is latched only when the cloud returns a non-empty ack. An empty ack returns without clearing the send, so the next pass tries again. The latch for one send drops when that send leaves the desired queue, not when another printer is processed.

`_cloud_send_session_connected` is the wait: a real session must be connected before `_republish_start`. `decide` returns `republish` while `pending_republish` is set, and `reset_retry` again only if that wait itself runs out.

## Why This Works

The printer's own state is the confirmation, not the MQTT return value. An echo means the printer saw this submission id, so phase B waits longer without another reset. The reset tears down the client and opens another before the broker has accepted a publish, so a publish in that pass cannot succeed and must not spend an attempt. Resetting during the parse window is a different failure (`0500_4003`), so phase B retries the publish on the session that is already up.

## Prevention

- Do not confirm a cloud send from the boolean `start_print` returns.
- Do not publish in the same pass as `hard_reset`. Wait until `session.connected` is true, and do not count a false publish while disconnected.
- Do not hard-reset on a phase B timeout.
- Upload once. Latch `report_failed` only after a non-empty ack, and drop that latch only when that send is gone from the desired queue.
- Keep the command probe off until a captured shop `get_version` reply is pinned in a test (`COMMAND_PROBE_ENABLED` is false).

## Related Issues

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/send_pipeline.py`, `bridge/app.py` (`_advance_cloud_send`, `_cloud_send_session_connected`).
