# CellKeeper for Home Assistant

A long-term history of CellKeeper BMS packs, kept in its own database so it outlives the recorder's
10 days. It works with the CellKeeper HMI's ESPHome entities as they are, with nothing else to set
up: no add-on, no automations, no `recorder:` changes.

**Phase 1 (this version) keeps every event occurrence.** For each of a pack's event sensors
(`sensor.ck0_event_*`), it records when the event was raised, when it escalated or cleared, and how
many times it happened. The CellKeeper dashboard can then show events from further back than the
recorder keeps.

## Install

1. In HACS, add this repository as a custom repository (type **Integration**) and download
   **CellKeeper**.
2. Restart Home Assistant.
3. Go to Settings → Devices & services → Add integration → **CellKeeper**.

It finds every pack by its event sensors, and picks up a pack added later by itself.

On its first start it reads the recorder's history (10 days by default), so the log starts with
what's already there.

## What it stores, and where

The database is `/config/cellkeeper/cellkeeper.db` (SQLite). Home Assistant's backups include it, and
the integration holds its writes while a backup copies it.

**Occurrences.** Each row is one occurrence of one event:

| Field | Meaning |
|---|---|
| `kind` | how it was seen; see below |
| `level`, `start_level` | the level it reached, and the level it started at |
| `raised_at` | when it was raised |
| `escalated_at` | when it escalated |
| `cleared_at` | when it cleared |
| `clear` | `cleared`, or `bms_restart` (the BMS restarted and forgot it) |
| `n` | the number of occurrences the row covers |
| `count_from`, `count_to` | the sensor's count before and after |
| `time_known`, `raised_after` | when `time_known` is false, the event happened while the HMI or Home Assistant was away: some time between `raised_after` and `raised_at` |

The `kind` of a row is one of:
- `raised`
- `again`: happened again while active
- `between`: raised and cleared between two readings
- `active`: already active when logging began

Repeats within 10 minutes of each other fold into one row.

**Markers.** The database also keeps markers: `hmi_offline`, `hmi_online` and `logging_started`.

## Websocket API

| Command | Returns |
|---|---|
| `cellkeeper/packs` | the packs being logged |
| `cellkeeper/events/list` | `{prefix, start?, end?, event?, before_id?, limit?}`: occurrences, newest first, and markers |
| `cellkeeper/events/subscribe` | `{prefix?}`: new and changed occurrences as they happen |
| `cellkeeper/info` | the database's path and size, and its row counts |

Times are seconds since the epoch (UTC).

## Development

Tests use `pytest-homeassistant-custom-component`:

```bash
uv venv --python 3.14 .venv && . .venv/bin/activate
uv pip install pytest-homeassistant-custom-component
python -m pytest
```
