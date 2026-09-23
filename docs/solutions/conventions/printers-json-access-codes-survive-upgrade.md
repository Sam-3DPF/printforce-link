---
title: printers.json access codes stay put so an upgrade does not re-pair
date: 2026-09-23
category: conventions
module: bridge
problem_type: convention
component: data_model
severity: critical
applies_when:
  - Changing the on-disk printer store
  - Shipping a Link upgrade to a shop Mac that is already paired
  - Loading printers after the cloud has acknowledged their access codes
tags:
  - printers-json
  - access-code
  - pairing
  - upgrade
  - bambu
---

# printers.json access codes stay put so an upgrade does not re-pair

## Context

The shop Mac keeps each printer's LAN access code in `printers.json` next to `config.toml`. The cloud couriers a code once. After the agent acks that delivery, the cloud deletes its copy. Tag `v0.1.31` (merged in [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54)) still reads the same key. An upgrade that rewrites or renames it makes every printer look unpaired, and the cloud cannot send the code again.

## Guidance

The file is a JSON object with `cloud_token` and `printers`. `printers` is a map keyed by serial. Each entry keeps `access_code`. `local_ip`, `name`, and `model` are optional. `configs()` connects a printer only when both `access_code` and `local_ip` are present. A missing code is not a printer Link can dial.

v0.1.31 added an optional `model` field (the SSDP DevModel code) on the same entry. It did not rename `access_code`, did not change the serial key, and did not require a new pair. `model` is ignored when blank. Existing files that have no `model` still load.

Write the file atomically and `chmod 600`. Do not log the contents. A corrupt file starts empty so startup survives. That empty start does not recover codes the cloud has already deleted. The operator would have to enter them again.

The MQTT session password and the FTPS password are this stored access code. Changing the session code must not invent a second store.

## Why This Matters

The access code is a device-control secret. The cloud's copy exists only until `ack_printers_config`. After that, `printers.json` is the copy that reconnects the farm on every launch, including the launch right after a self-update. A migration that drops `access_code` fails closed (the printer is omitted) and cannot be repaired by the courier.

## When to Apply

- Editing `PrinterStore` load or save.
- Adding a field to a printer entry. Add it as optional. Do not rewrite the file in a shape older builds cannot read if a rollback is still possible.
- Any change that would clear `access_code` because a new field is missing.

## Examples

Before v0.1.31 an entry was `access_code`, optional `local_ip`, optional `name`. After v0.1.31 the same entry may also have `model`. `configs()` still requires `access_code` and `local_ip` and passes `model` through as an empty string when it is absent.

## Related

- Merged [PR #54](https://github.com/Sam-3DPF/printforce-link/pull/54), tag `v0.1.31`.
- `bridge/store.py`, `bridge/dpf_client.py` (`get_printers_config`, `ack_printers_config`), README security note.
