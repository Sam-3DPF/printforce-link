---
title: Link owns one MQTT session per printer and does not open a camera socket
date: 2026-09-23
last_updated: 2026-09-30
category: architecture-patterns
module: bridge
problem_type: architecture_pattern
component: messaging
severity: high
applies_when:
  - Opening or reconnecting a Bambu printer from PrintForce Link
  - Choosing which device topics to subscribe to or publish on
  - Replacing the session client or adding a second MQTT client for the same printer
  - Publishing commands on the session
  - Adding a camera feed or any socket to the printer on port 6000
  - Changing how a dropped or refused client is redialled
  - Changing when a silent session is reset
  - Changing when the send watchdog hard-resets a session
tags:
  - mqtt
  - paho-mqtt
  - bambu
  - link-session
  - device-report
  - lan-access-code
  - camera-socket
  - puback
  - reconnect
  - 0500_4003
---

# Link owns one MQTT session per printer and does not open a camera socket

## Context

PrintForce Link talks to each Bambu printer with its own MQTT client. The printer is the broker. This is the tree at tag `v0.1.31`, merged in [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54). `requirements.txt` pins `paho-mqtt==2.1.0` and does not depend on `bambulabs-api`.

## Guidance

Keep one paho session per printer. Do not add `bambulabs-api`, and do not open a camera socket.

The wire contract in `bridge/bambu/session.py` is MQTT 3.1.1 on port 8883, username `bblp`, password the LAN access code, TLS 1.2 with the printer's self-signed certificate unchecked. Each connect builds a new client id and a clean session.

Subscribe only to `device/{serial}/report`. Publish commands to `device/{serial}/request`. P1S and A1 brokers drop the TCP connection if a client subscribes to the request topic, so that topic is publish-only. After CONNACK the session subscribes to the report topic and then publishes `pushall` and `get_version`. A message on any other topic is dropped.

Do not wait for a PUBACK. The broker matches those acks unevenly enough that paho's default inflight cap of 20 wedges the session after a handful of commands. The cap is raised to 1000, and `publish` returns paho's immediate result.

`loop_stop` joins the network thread with no timeout. Teardown runs that join on a daemon helper and gives up after 1.5 seconds.

paho never redials a client that reached CONNACK. `build_paho_client` passes `reconnect_on_failure=False`. An in-place redial re-sends every QoS 1 message the broker has not PUBACKed (`Client._messages_reconnect_reset_out`), and Bambu's broker rarely PUBACKs. On 2026-09-30 every reconnect in the shop logs replayed old `project_file` starts. Bambuddy fixed the same bug with a fresh client (#1136). After any drop or refused CONNACK the session marks the client ended. The watchdog `tick` then runs `hard_reset`, which stops that client on the daemon helper and opens a new one. The redial backoff is 5, 10, 20, then 30 seconds, and a CONNACK resets it. If the replacement client fails to start (`connect_async` or `loop_start` raises), `_open` ends it as well, which schedules the next redial on the grown backoff. `_redial_if_due` logs the failure instead of raising it. Before this, a failed `_open` left the client marked not ended, so no redial was ever scheduled again and only the fleet backstop could recover the printer. paho still retries a client's first connect, because nothing is published before CONNACK. The new client sends only `pushall` and `get_version`. The send watchdog and the command mailbox own command retries.

The connection labels survive redials. The down-since stamp clears only on CONNACK, so a switched-off printer still reads `unreachable` 60 seconds after the drop. A refused redial keeps `refused`. A clean disconnect within 10 seconds of a report still ends the client. It only skips the offline clock.

A silent session is not reset while the printer is unpacking or preparing a file. The printer gives the session a hold (`set_reset_hold`). The hold names `PREPARE` or `SLICING` from the merged `gcode_state`, or `phase_a` for 90 seconds after an accepted `project_file`, and after those 90 seconds while no report has arrived since that start. While it holds, the stale branch records `reset_held` once and leaves the client alone. Bambuddy #1150 and #1678 found that resetting a P1 during this window raises `0500_4003`. A socket that is actually down still redials.

The hold has a ceiling, `_RESET_HOLD_MAX_SECONDS` (150 seconds of silence, measured from the later of the last report and the last CONNACK). A P1 unpacks a file in up to about 135 seconds (Bambuddy). The merged `gcode_state` only changes on a report, so a stalled stream would otherwise freeze PREPARE and hold forever. Past the ceiling the stale branch records `reset_held` with `expired: true` once and takes the normal cooldown-gated reset. The ceiling sits below the fleet backstop's 300 seconds (`Fleet.recover_dead_sessions`, `_RECOVERY_SILENCE_SECONDS`), so the session resets first and the backstop needs no hold check.

The send watchdog asks the same question. On a phase A timeout with no republish pending, `_advance_cloud_send` skips its `hard_reset` while the session is connected, no report has arrived since this attempt's start (`silent_for()` is at least the attempt's age), and the attempt is younger than `_RESET_HOLD_MAX_SECONDS`. It checks again on the next pass. After the ceiling, or when reports are flowing without an echo, it resets as before. A changed `gcode_file` still enters phase B without a reset.

`BambuPrinter` states that the session opens no camera socket. `test_connecting_a_printer_does_not_open_port_6000` fails a connect to port 6000 and fails if `session.py` contains `camera`, `6000`, or `bambulabs`. The source does not describe a camera protocol beyond that guard.

## Why This Matters

Subscribing to `device/{serial}/request` is the drop the session module names for P1S and A1. Waiting on PUBACK, or putting the inflight cap back to paho's default, is the wedge that module names. An unbounded `loop_stop` on the caller thread is why teardown is bounded. Letting paho redial in place replays starts the printer already has. A reset while the printer unpacks is the one known lead for `0500_4003`. Putting `bambulabs-api` back, or opening port 6000, fails the session and telemetry guards. Those guards do not spell out a further camera failure mode.

## When to Apply

- Changing connect, subscribe, publish, inflight, TLS, or teardown on the session.
- Adding a printer dependency beside `paho-mqtt==2.1.0`.
- A change that would subscribe to `device/{serial}/request`, block on PUBACK, call `loop_stop` on the caller thread, or open port 6000.
- A change that would let paho redial a used client, clear paho's private out-queue, drop to QoS 0, or reset a silent session without asking the reset hold.
- A change that would lift the hold's ceiling above the fleet backstop's 300 seconds, or let a failed client start leave no redial scheduled.

## Examples

From the session module: commands are published to `device/{serial}/request`, and the only subscription is `device/{serial}/report`, because subscribing to the request topic drops the TCP connection on P1S and A1.

`tests/test_telemetry.py` imports the pure status logic and asserts `bambulabs_api` is not loaded.

`test_a_reconnect_never_replays_starts_published_before_the_drop` in `tests/test_session.py` models paho's in-place redial from the real `build_paho_client` setting. Two starts are published, the socket drops, and the printer comes back at once. The broker must then receive only `pushall` and `get_version`. `test_a_silent_session_is_not_reset_while_preparing_and_is_while_idle` in `tests/test_liveness.py` holds the reset for 90 seconds of silence in PREPARE and SLICING, and resets while IDLE. `test_the_unpacking_hold_ends_after_150s_of_silence` pins the ceiling, and `test_a_session_silent_since_the_start_is_held_until_the_ceiling` the silent-since-start hold. `test_phase_a_does_not_reset_a_session_silent_since_the_start` and its two neighbours in `tests/test_send_pipeline.py` pin the send watchdog's side. `test_a_redial_whose_client_fails_to_start_is_redialled_again` in `tests/test_session.py` pins the failed-`_open` redial.

## Related

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/bambu/session.py`, `bridge/printer.py`, `tests/test_session.py`, `tests/test_liveness.py`, `requirements.txt`.
- The two-phase send watchdog: `docs/solutions/logic-errors/two-phase-send-watchdog.md`.
- Plan `2026-09-30-001-fix-p1s-start-replay-ams-presence` (3D-PrintForce repo), unit U1.
