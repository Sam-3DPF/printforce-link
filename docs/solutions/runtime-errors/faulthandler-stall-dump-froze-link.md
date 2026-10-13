---
title: The stall dump froze Link; the report loop now has a self-check that exits
date: 2026-10-12
category: runtime-errors
module: bridge
problem_type: runtime_error
component: infrastructure
symptoms:
  - Link at 99.5% CPU, state R, writing nothing to printforce-link.log
  - No more MQTT frames in logs/printer-*.jsonl
  - launchd KeepAlive did nothing because the process never exited
  - The last thread dump in the log stops partway, on a frame with an impossible line number
root_cause: thread_violation
resolution_type: code_fix
severity: critical
tags:
  - macos
  - faulthandler
  - report-loop
  - watchdog
  - launchd
---

# The stall dump froze Link; the report loop now has a self-check that exits

## Problem

On 2026-10-12 at 14:21 the shop Mac's Link (v0.1.48) stopped reporting and stayed running. It sat at 99.5% CPU until it was restarted by hand at 14:55. The cloud had been slow since about 13:00, and each slow report set off a stack dump. One of those dumps is what stopped Link.

## Symptoms

- The last log line is an ordinary `POST /api/bridge/heartbeat 200`.
- The dump just before it lists 6 threads where every other dump lists 31, and ends on `File "copy.py", line 69017320 in deepcopy`.
- Every thread is silent afterwards: the report loop, the command doorbell, and all nine printer sessions.

## What Didn't Work

- Looking for state that grows with each failed report. There is none. `events.json` was empty, a report carries only that pass's snapshot, and each failed pass took the same 35 seconds from 13:00 to 14:21.
- Blaming the thread in `copy.deepcopy`. It is a printer's MQTT thread copying one incoming message, which it does for every message. It matters only because it was running Python at the moment the dump read its stack.
- Relying on launchd. KeepAlive restarts a process that exits. This one did not exit.

## Solution

`arm_report_loop_dump` and `faulthandler.dump_traceback_later` are gone from `bridge/app.py`. `bridge/loop_watchdog.py` replaces them. The loop calls `beat()` at the top of each pass, and one daemon thread watches the time since the last beat:

- 60 seconds: it logs every thread's stack once, read in Python with `sys._current_frames()`.
- 10 minutes: it writes `report loop has not started a pass for 600s ... exiting with status 70` and calls `os._exit(70)`. launchd and the Windows task start a fresh Link.
- Each check also pushes a `SIGALRM` 120 seconds out. Link installs no handler for it. If no Python thread can run at all, nothing pushes it again and the kernel ends the process.

## Why This Works

Three things combined, and each is true on its own:

1. `dump_traceback_later` runs a C thread that reads other threads' frames without the interpreter lock. A thread that is running Python changes those frames while they are read. A failed report holds a pass for 35 seconds, the dump fired at 30, so it fired about 45 times an hour against nine busy MQTT threads.
2. That C thread blocks every signal. On macOS a thread that faults with the signal blocked does not crash; the kernel re-runs the faulting instruction forever. A ten-line C program that blocks all signals in a thread and reads a bad pointer showed this on the shop Mac: 98.6% CPU, and the process never died.
3. `cancel_dump_traceback_later` waits for that thread to finish and does not release the interpreter lock while it waits. The report loop called it at the top of the next pass, so every Python thread stopped.

The new dump holds the interpreter lock, so the frames it reads cannot change. The exit does not depend on the stuck thread. The alarm does not depend on Python.

A pass that ends with a failed report still beats. A cloud outage does not restart Link.

## Prevention

- Do not use `faulthandler.dump_traceback_later` or `faulthandler.dump_traceback` on a timer in Link. `tests/test_loop_watchdog.py` fails if `bridge.app` imports `faulthandler` again.
- Do not install a `SIGALRM` handler. The backstop works because the default action ends the process.
- Keep the exit message off the `logging` module. A stuck thread can be holding the handler lock.
- Do not move a long transfer onto the report loop without raising `EXIT_AFTER_SECONDS`. Uploads run on printer workers; a send's download is the longest thing left on the loop.
- An exit by the backstop leaves no line in Link's log. The sign is a `Loaded Config` line with no exit message before it.

## Related Issues

- 3D-PrintForce PR #921 fixed the slow `POST /api/bridge/printers/state` that set the dumps off.
