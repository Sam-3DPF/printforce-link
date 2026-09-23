---
title: One worker per printer and the fleet lock guards membership only
date: 2026-09-23
category: architecture-patterns
module: bridge
problem_type: architecture_pattern
component: background_job
severity: high
applies_when:
  - Uploading a file or publishing a start, pause, resume, or stop
  - Reading fleet snapshots on the report loop
  - Adding or removing a printer while a transfer is in flight
  - Deciding whether a self-update restart may proceed
tags:
  - printer-worker
  - fleet-lock
  - report-loop
  - ftps
  - bambu
---

# One worker per printer and the fleet lock guards membership only

## Context

A farm report used to wait on whatever the fleet lock was held across. Connect, FTPS upload, and MQTT publish are slow, and one stuck printer stalled every other printer's report. Tag `v0.1.31`, merged in [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), gives each serial its own command thread.

## Guidance

Each active serial has one `PrinterWorker`: a daemon thread and a bounded queue (default 8). `submit` returns a future. A full queue fails that future with `WorkerBusy` and does not block the caller. There is no shared queue and no shared worker capacity.

The fleet lock covers membership only. `snapshot` copies the member list under the lock and reads each printer outside it. `submit`, upload, start, and the other controls take the lock long enough to find the worker, then release it before the work runs. Refresh and the snapshot-triggered AMS pushall run on that printer's worker, so the report loop does not publish or sleep.

`stop` sets a cancel event and joins for at most half a second. An in-flight upload notices the cancel between blocks. A slower exit is left on the daemon thread so removing a printer cannot deadlock behind the transfer. A busy worker counts as busy for the self-update restart.

Cloud sends for one serial run on that serial's worker. A serial whose send is still running is not queued twice.

## Why This Matters

The report loop is what 3D PrintForce uses to decide the farm is alive. If that loop waits on one printer's upload or on a broker that never acks, every other printer looks frozen and a self-update can stall. A stuck command now occupies only that serial's queue.

## When to Apply

- Adding a printer command, an upload, or a cloud send.
- Holding the fleet lock across a socket, a download, or a sleep.
- Joining a printer thread from the report path without a short timeout.

## Examples

`Fleet.submit` documents that the caller is not blocked and the fleet lock is not held across the work. `PrinterWorker` documents that the report loop must not wait on connect, upload, or MQTT, and that `stop` does not join past half a second.

## Related

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/printer_worker.py`, `bridge/fleet.py`.
