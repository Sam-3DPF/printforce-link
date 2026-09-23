---
title: Connection is separate from print status
date: 2026-09-23
category: architecture-patterns
module: bridge
problem_type: architecture_pattern
component: messaging
severity: high
applies_when:
  - Building or ingesting a printer report
  - Deciding whether a printer may receive a dispatch
  - Treating a quiet socket as idle or as offline
  - Replaying an older report after reconnect
tags:
  - connection
  - print-status
  - gcode-state
  - offline
  - dispatch
---

# Connection is separate from print status

## Context

A dropped socket and a finished plate are different facts. Folding them into one status made a quiet printer look idle, which is the only state 3D PrintForce treats as allowed to start a job. Tag `v0.1.31`, merged in [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), reports both facts on every snapshot.

## Guidance

`status` stays the mapped firmware state the ingest already understands: IDLE, PRINTING, PAUSED, NEEDS_CLEARING, ERROR, or OFFLINE. `connection` sits beside it and is only `live`, `stale`, or `offline`.

`live` means the socket is up and a report on this session arrived inside the stale window. On that report, status starts as `map_status` of the firmware state. A cancel-failed FAILED becomes IDLE inside that function. A leftover idle FAILED is then reported IDLE, and a live IDLE that still looks mid-print (progress between 0 and 100, or a named file with a hot nozzle or bed target) is reported PRINTING. Unknown and blank firmware states map to OFFLINE, never IDLE. `stale` means a merged payload exists but that report is not live. `status` on that report is OFFLINE, and the telemetry and AMS slots are the last merged payload. `offline` means there is no merged payload, or the session is down past its unreachable window, rejected, or refused with nothing recent. Telemetry is null and `slots` is None, not an empty list. An empty list means the printer said the AMS has no units. None means this report is not claiming to see the AMS.

A message from before this session's CONNACK is not `live`. The merged payload is kept so the next report can merge onto it. `reconnect()` drops that payload, because an address change is a different machine until the serial is proved again.

`snapshot` does not publish and does not sleep. Bringing the socket back is the session watchdog and the fleet backstop. Reports are posted for this pass. There is no replay of the last good report.

## Why This Matters

Unknown firmware state maps to OFFLINE, never IDLE, because a fail-open IDLE would dispatch onto a printer the bridge has not heard from. A false OFFLINE only skips one dispatch and fails closed. An older ingest that ignores `connection` still refuses a non-live printer, because `status` is OFFLINE whenever the report is not live. A cloud send also requires `desired_status` IDLE, `connection` live, and firmware state IDLE, FINISH, or FAILED. Status IDLE is not the only printer status that passes that gate: a live FINISH is NEEDS_CLEARING and a live FAILED is ERROR, and both can upload when the firmware state is ready.

## When to Apply

- Changing what `snapshot` puts in `status` or `connection`.
- Treating `gcode_state` IDLE on a stale payload as permission to upload or start.
- Replaying a previous report when this pass has nothing new.
- Sending `slots: []` for "no information".

## Examples

`BambuPrinter.snapshot` states the split: `connection` and `status` are different facts, and `status` on a stale report stays OFFLINE because IDLE is still the only authorization for dispatch. `tests/test_report_contract.py` states the same contract for the fields beside status.

## Related

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/printer.py` (`snapshot`), `tests/test_report_contract.py`.
