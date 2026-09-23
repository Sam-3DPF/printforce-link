---
title: The Mac startup health check must finish before printer connect
date: 2026-09-23
category: runtime-errors
module: bridge
problem_type: runtime_error
component: infrastructure
symptoms:
  - A new Link build on the shop Mac was deleted and the previous build restored
  - v0.1.29 rolled back because update-healthy never appeared within two minutes
  - One half-open printer socket used up the startup window
root_cause: async_timing
resolution_type: code_fix
severity: critical
tags:
  - macos
  - self-update
  - health-check
  - connect-all
  - startup
---

# The Mac startup health check must finish before printer connect

## Problem

The macOS self-update watchdog keeps a new build only if `update-healthy` appears within two minutes of the swap. v0.1.29 wrote that file only after the first cloud report, and startup connect waited on each printer for a broker ack. One half-open socket used the whole window, the watchdog rolled the build back, and the shop Mac never stayed on the new agent.

## Symptoms

- After an automatic update, the Mac returns to the previous Link build.
- The candidate version is recorded as a failed startup health check.
- Startup spends the health window inside printer connect instead of reaching 3D PrintForce.

## What Didn't Work

Writing `update-healthy` only after printers had connected and the first state report had been posted. Connecting printers one after another and waiting on each broker. A half-open MQTT socket does not ack, so that wait runs until the process is killed.

## Solution

`main` calls `_confirm_startup_health` before `fleet.connect_all`. That posts a heartbeat and, when 3D PrintForce answers, writes the healthy marker. A miss is not fatal. The report loop confirms again after the first successful state post.

`connect_all` does not let one printer hold the rest. It starts every connect together and spends one shared budget, `_DEFAULT_CONNECT_TIMEOUT_SECONDS` (12 seconds), across the fleet. A printer that does not answer is logged, reported OFFLINE, and retried later. The same bound is what reconnects already use.

The watchdog itself is the detached `complete-update.sh` loop: up to 120 one-second checks for the health file, then SIGTERM and restore of the backup. That loop is unchanged. The process has to create the file before those 120 seconds elapse, which means the heartbeat cannot wait on the fleet.

## Why This Works

The watchdog measures time from process start, not from "printers look healthy". Reaching 3D PrintForce is the signal that this build can run. Printer connect is allowed to be slow after that marker exists. Sharing one 12-second budget means a dead address cannot consume the two minutes even if the heartbeat is late and the report loop is the second chance.

## Prevention

- Do not move `_confirm_startup_health` back to after `connect_all`.
- Do not connect printers serially at startup, and do not raise the shared connect budget toward the 120-second watchdog.
- A miss on the first heartbeat stays non-fatal only because a later successful state post confirms again. Do not remove that second confirmation.
- Landed in [PR #38](https://github.com/Sam-3DPF/printforce-link/pull/38), and still the startup order at tag `v0.1.31`.

## Related Issues

- [PR #38](https://github.com/Sam-3DPF/printforce-link/pull/38) fixed the rollback of v0.1.29.
- `bridge/app.py` (`_confirm_startup_health` and the call before `connect_all`), `bridge/fleet.py` (`connect_all`), `bridge/updater.py` (the 120-second health loop).
