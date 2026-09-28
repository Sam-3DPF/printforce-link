# Link state contract v2

The wire between PrintForce Link and 3D PrintForce for **what a printer is doing**
and **what happened to a print**. Link decides both. 3D PrintForce stores them as
sent and adds only farm facts. A copy of this file lives in each repo
(`printforce-link/docs/references/` and `3D-PrintForce/docs/references/`); change
both together.

Plan: `3D-PrintForce/docs/plans/2026-09-27-001-feat-printer-connection-reliability-plan.md`.
Audit behind it: `3D-PrintForce/docs/references/printer-state-audit-2026-09-27.md`.

## 1. Per-printer `v2` object

Each printer report in `POST /api/bridge/printers/state` carries the legacy flat
fields (unchanged) plus one nested object, `v2`:

| Field | Type | Meaning |
| --- | --- | --- |
| `contract` | `"state_v2"` | Version marker |
| `state_seq` | int | Rises with every report from this Link (seeded from wall-clock ms). The cloud drops a report older than the one it stored |
| `connection` | `live` \| `stale` \| `offline` | Is this report a fresh reading from the current session |
| `connection_reason` | `ok` \| `silent` \| `unreachable` \| `auth_rejected` \| `refused` \| `commands_ignored` \| `no_data` | Why not live |
| `activity` | `idle` \| `preparing` \| `printing` \| `paused` \| `ended` \| `unknown` | What the machine is doing. `ended` = the last job reached an outcome and nothing new started. Never a farm word |
| `stuck_job` | bool | The firmware holds a cold RUNNING job at 0%. `activity` is `idle`; Link clears it before a start |
| `stage` | `{code, label}` | Firmware stage number and our wording. `label` is `Preparing` for an unnamed stage during a job |
| `pause_reason` | `user` \| `gcode` \| `filament_runout` \| `ams` \| `door_open` \| `first_layer` \| `nozzle_clog` \| `hardware` \| `error` \| `unknown` \| null | Only when `activity=paused` |
| `job` | object \| null | `{name, file, plate, progress, layer, total_layers, remaining_s, outcome}`. `outcome` is `finished` \| `failed` \| `cancelled` when `activity=ended` |
| `errors` | list | Real faults only: `{code, severity (FATAL/SERIOUS/COMMON/INFO), title, detail, source (hms/print_error)}`. Cancel echoes are never errors |
| `commands_rejected` | bool \| null | The printer is refusing commands (Developer Mode off). null = no reading |
| `model` | object | `{code, name, family, known, verified}` |
| `capabilities` | list of strings | `pause`, `resume`, `stop`, `light`, `fans`, `temperatures`, `filament_units`, `dual_nozzle`, `chamber_temperature`, `chamber_heater`, `drying` |
| `raw` | object | `{gcode_state, stg_cur, print_error}` for debugging only. The cloud must not decide anything from `raw` |

A `stale` report keeps the last known `activity` and `job`. An `offline` report has
`activity=unknown`, `job=null`, `errors=[]`, `commands_rejected=null`.

## 2. Lifecycle events

Each printer report carries `events`: this printer's unacknowledged events, oldest
first. They live on Link's disk (`events.json`) until acknowledged.

| Field | Meaning |
| --- | --- |
| `id` | uuid hex, unique forever |
| `seq` | int, rises for this Link install, never reused |
| `bambu_id` | serial |
| `type` | `print_started` \| `print_paused` \| `print_resumed` \| `print_finished` \| `print_failed` \| `print_cancelled` |
| `origin` | `link` (Link started this print) \| `external` (touchscreen, Bambu Studio, SD card) |
| `submission_id` | Link's start id, when `origin=link` |
| `batch_id`, `plate` | 3DPF batch and plate, when `origin=link`. The cloud never matches file names |
| `gcode_file`, `subtask_name` | Names on the printer |
| `at` | ISO-8601 UTC when Link saw it |
| `observed` | false when Link inferred it after a restart (the printer was already there) |
| `progress`, `stage` | At the moment of the event |
| `by` | On `print_cancelled`: `link` (someone pressed Stop in 3DPF), `printer` (stopped on the machine), `link_cleared_stuck_job` (nothing printed), `unknown` |
| `print_error` | On `print_failed` / `print_paused`, when present |

Rules Link follows (one print = one cycle): a print opens on `print_started`
(RUNNING with a file after a known non-RUNNING state) or silently on the first
RUNNING of a session; pause and resume stay inside it; it closes on exactly one
terminal. A first push already at FINISH emits nothing.

**Acknowledgment.** The state response carries `events_acked: [id, ...]` — the ids
the cloud has durably applied. Link drops exactly those. An id not listed is sent
again. Applying is idempotent on `id`.

## 3. What the cloud does with it

- **Stores `v2` as sent.** It does not re-derive machine state from `raw` or from
  flat legacy fields.
- **Applies events once**, keyed on `id`:

| Event | Farm effect |
| --- | --- |
| `print_started` | Plate flag cleared (a print on the machine means the plate was cleared). Link batch → printing |
| `print_finished` | Plate flag set. Link batch → completion cascade |
| `print_failed` | Plate flag set. Link batch → failed; auto-queue hold `print_failed` |
| `print_cancelled` | Plate flag set, **except** `by=link_cleared_stuck_job`. Link batch → back to the Sliced Queue as not printing |
| `print_paused`, `print_resumed` | Recorded only |

- **Plate flag** is one field (`printers.bed_uncleared_at`). Set only by the events
  above. Cleared only by Clear Print Bed or `print_started`.
- **Link silent.** When a Link sends nothing for 60 s, the cloud marks its printers
  `connection=offline, connection_reason=link_offline`. This is the only machine
  field the cloud writes, and Link's next report replaces it.
- **Card status** is one server function, `display_status(v2, farm)`:

| Condition (first match) | Status | Reason shown |
| --- | --- | --- |
| `connection` not `live` | OFFLINE | by `connection_reason` |
| `activity=unknown` | OFFLINE | "Waiting for printer status" |
| `printing` / `preparing` | PRINTING | `stage.label` |
| `paused` | PAUSED | pause reason wording |
| plate flag set | NEEDS_CLEARING | last outcome: finished / failed / cancelled |
| auto-queue hold | ERROR | hold reason |
| `idle` / `ended` | IDLE | — |

- **Free for the next file:** `connection=live`, `activity` in {`idle`, `ended`},
  `commands_rejected` not true, no `FATAL` entry in `errors`, plate flag clear,
  no auto-queue hold, queue enabled. (`stuck_job` is fine: Link clears it.)
  Clear failure always releases the hold; a printer that still reports a
  `FATAL` alarm stays out of the queue until the alarm goes away.
- **Never:** send a Stop Link did not get from an operator; gate Link on a guessed
  status; keep its own copy of Bambu codes.

## 4. Commands (mailbox and doorbell)

Every operator command (pause, resume, stop, light, temperature, dismiss, …)
is one row in `printer_commands`. Its id is also sent in the older desired
state `control` slot, so a Link that reads both runs it once.

- **Wait:** `GET /api/bridge/commands/wait?timeout=25` (bridge token). It returns
  as soon as the farm has an open command or a hint, else empty lists after
  `timeout`:
  `{"commands": [{"id", "bambu_id", "action", "params", "expires_in_ms"}], "hints": ["send"]}`.
  `send` means a send was authorized: post state now to receive it.
- **Deadline:** relative (`expires_in_ms`). Link computes its own deadline and
  never runs a command after it (`failed: expired_on_link`).
- **Ack:** `POST /api/bridge/commands/{id}/ack` with
  `{"state": "published" | "applied" | "rejected" | "failed", "reason"?, "reply"?}`.
  A command stays in the wait until it is acked or expires. A final state
  is never overwritten; `published` can still become `applied` or `rejected`.
- **Printer replies:** a mailbox command is published with its own
  `sequence_id`. When the printer answers with `result`, Link acks `applied`
  or `rejected` (the printer's `reason`) with `reply`. No answer leaves it
  `published`. A printer refusing all commands (HMS `0500_0500_0001_0007`)
  settles it as `rejected: developer_mode_off`.
- **Once:** Link keeps the ids it published on disk. A command id is published
  at most once, ever.
- **Cloud rules:** stop supersedes an open pause or resume; a newer temperature
  supersedes the same heater's older one; everything else runs in order. A
  published pause / resume / stop is `applied` when Link's state shows it.

## 5. Change rules

- Add fields; never rename or repurpose one. Both repos' contract tests read the
  example payloads in `tests/fixtures/contract/` (Link) and
  `backend/tests/fixtures/link_contract/` (3DPF).
- A wrong card is fixed in Link with a replay fixture, not patched in the cloud.
