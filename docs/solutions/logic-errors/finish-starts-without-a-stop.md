---
title: A leftover FINISH starts the next job with no stop first
date: 2026-09-23
category: logic-errors
module: bridge
problem_type: logic_error
component: messaging
symptoms:
  - A start while gcode_state is FINISH used to look like it still needed a stop
  - A start is refused while gcode_state is PREPARE, SLICING, RUNNING, or PAUSE
  - A reused submission id is treated as a continuation of the previous job
root_cause: logic_error
resolution_type: code_fix
severity: high
tags:
  - project-file
  - gcode-state
  - finish
  - submission-id
  - bambu
---

# A leftover FINISH starts the next job with no stop first

## Problem

After a plate finishes, Bambu leaves `gcode_state` at FINISH until the next job. Treating that leftover FINISH as "still busy" and publishing `stop` before `project_file` is the wrong start. The next job is `project_file` alone, with a new submission id.

## Symptoms

- A start on a printer whose last `gcode_state` is FINISH, IDLE, or FAILED publishes one command, `project_file`, and does not publish `stop`.
- A start while `gcode_state` is PREPARE, SLICING, RUNNING, or PAUSE publishes nothing, including when the session is only stale.
- `task_id`, `subtask_id`, and `project_id` are the same fresh id. Zero is not used.

## What Didn't Work

Sending `stop` to "clear" FINISH before the next file. The start path does not do that. FINISH is a completed plate, not a print that is still on the machine. The mapped cloud status for FINISH is NEEDS_CLEARING, which is a plate-clearing fact for 3D PrintForce. It is not the check `project_file` uses.

Clearing an unresolved AMS tray by turning it into the external spool also does not belong on this command. An unresolved tray stays unresolved.

## Solution

`project_file` is refused only while the last `gcode_state` is PREPARE, SLICING, RUNNING, or PAUSE. The check reads that state, not the session flag, so a stale connection can still be a printer that is printing. IDLE, FINISH, and FAILED call `start_print`, which publishes `project_file` and nothing else.

The submission id is epoch milliseconds modulo 2147483647, and it must differ from the previous id. P1S firmware clamps a larger id and then treats the start as the previous job. The state tracker treats zero as "no id".

`tests/test_commands.py` covers both sides: `test_idle_finish_and_failed_start_without_a_stop` expects the command list `["project_file"]`, and the busy states expect no payload even when the session is stale.

## Why This Works

The printer is ready for a new file when the firmware state is IDLE, FINISH, or FAILED. A stop is a different command (`stop_print`) and is not a prefix of start. Busy states are refused from the last `gcode_state` so a stale socket cannot start a second job on a printer that is still printing. A fresh id keeps the new start from looking like a continuation.

## Prevention

- Do not publish `stop` before `project_file` for IDLE, FINISH, or FAILED.
- Do not key that decision on mapped `status` (NEEDS_CLEARING) or on the session flag.
- Keep the submission id inside 1..2147483647 and different from the previous one.
- Keep `test_idle_finish_and_failed_start_without_a_stop` and the busy-state refusal test.

## Related Issues

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/bambu/commands.py`, `bridge/printer.py` (`start_print`), `bridge/app.py` (`_mqtt_start_print`), `tests/test_commands.py`.
