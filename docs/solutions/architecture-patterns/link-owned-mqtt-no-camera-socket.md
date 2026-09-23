---
title: Link owns one MQTT session per printer and does not open a camera socket
date: 2026-09-23
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
tags:
  - mqtt
  - paho-mqtt
  - bambu
  - link-session
  - device-report
  - lan-access-code
  - camera-socket
  - puback
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

`BambuPrinter` states that the session opens no camera socket. `test_connecting_a_printer_does_not_open_port_6000` fails a connect to port 6000 and fails if `session.py` contains `camera`, `6000`, or `bambulabs`. The source does not describe a camera protocol beyond that guard.

## Why This Matters

Subscribing to `device/{serial}/request` is the drop the session module names for P1S and A1. Waiting on PUBACK, or putting the inflight cap back to paho's default, is the wedge that module names. An unbounded `loop_stop` on the caller thread is why teardown is bounded. Putting `bambulabs-api` back, or opening port 6000, fails the session and telemetry guards. Those guards do not spell out a further camera failure mode.

## When to Apply

- Changing connect, subscribe, publish, inflight, TLS, or teardown on the session.
- Adding a printer dependency beside `paho-mqtt==2.1.0`.
- A change that would subscribe to `device/{serial}/request`, block on PUBACK, call `loop_stop` on the caller thread, or open port 6000.

## Examples

From the session module: commands are published to `device/{serial}/request`, and the only subscription is `device/{serial}/report`, because subscribing to the request topic drops the TCP connection on P1S and A1.

`tests/test_telemetry.py` imports the pure status logic and asserts `bambulabs_api` is not loaded.

## Related

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/bambu/session.py`, `bridge/printer.py`, `tests/test_session.py`, `requirements.txt`.
