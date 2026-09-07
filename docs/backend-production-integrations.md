# Backend production integrations

The application process now persists general backend state in the SQLite file
selected by `STATE_DB_PATH` (default: `output/runtime.sqlite3`). The alert outbox
and MQTT retry queue continue to use their own databases. Unit tests that call
`create_server()` without `state_database` remain isolated in memory.

## Runtime flow

Accepted telemetry is stored and scored with the existing AI2 baseline scorer.
The score, active asset rule, sensor-fault state, and durable lifecycle checkpoint
are passed to the existing AI2 event lifecycle. Started, updated, merged, and
closed events are stored with their rule/model versions and trigger evidence. The
alert outbox discovers newly started events through its existing worker.

The global anomaly parameters update all asset rules atomically. Transmission
interval and edge-buffer parameters are reported as firmware-managed and reject
runtime writes. Per-device delay/replay configuration uses the separate,
allowlisted [remote configuration channel](limited-remote-config.md); it does
not turn global parameters or destructive buffer resizing into remote controls.

## Added API contracts

- `GET /api/health` returns an RFC3339 UTC `timestamp`.
- `POST /api/devices/{deviceId}/health` accepts authenticated RSSI, reboot count,
  buffer usage, firmware version, and sensor fault/recovery reports. Use
  `DEVICE_HEALTH_TOKEN` outside demo environments.
- `GET|POST /api/events/{eventId}/notes` lists or creates categorized notes.
- `PATCH|DELETE /api/events/{eventId}/notes/{noteId}` updates or soft-deletes one
  note while retaining author and version history.
- `GET /api/events/{eventId}/notes/{noteId}/history` returns note history.
- `GET /api/datasets/export?...&format=xlsx` returns `manifest` and `rows` sheets
  from the same export snapshot used by CSV.

Internal dataset freezing now requires a complete active install point for each
selected asset. Missing metadata returns `DATASET_INSTALLATION_INCOMPLETE` with a
structured `error.details` payload. Legacy internal datasets without this marker
are also blocked from baseline/model registration.

## Failure and timing contracts

- A failed state commit restores application memory and lifecycle checkpoints
  before returning an error. Rejected telemetry diagnostics are persisted in a
  separate successful transaction; they do not admit the rejected measurement.
- Read-only state access does not save a checkpoint. A site summary batches any
  newly detected offline transitions into at most one checkpoint.
- Storage recovery history is published only when its checkpoint has committed.
- Shutdown stops accepting new requests and drains accepted requests before
  closing the state DB. Client socket inactivity is limited to 30 seconds.
- `durationSec` measures elapsed telemetry timestamps for both entry and exit.
  Faster samples do not shorten it; buffered samples use measurement time.
  The shared AI2 lifecycle keeps count-based defaults for its other callers and
  accepts older checkpoints without the optional elapsed-duration settings.
- Sensor health reports close open asset events and reset pending streaks even
  without another telemetry sample. Fault/recovery boundaries are durable;
  delayed measurements at or before the boundary are stored but not reanalyzed.
- Only the currently active device mapping can apply a sensor-health boundary
  to the asset lifecycle. Inactive/replaced devices retain their own fault and
  recovery history without affecting the replacement's events or entry streak.
- Raw retention runs before a persistent server begins accepting requests and
  every 60 seconds in a separate worker, even when automatic alerts are disabled.
  Cleanup saves only when records are removed, rolls back on storage failure,
  and retries on the next tick. Live reads exclude expired records while cleanup
  is pending; frozen dataset/evidence snapshots keep their existing contract.
- Live telemetry never fabricates replacement samples for an empty asset or time
  window, including after every raw measurement expires. CSV/XLSX exports contain
  headers but no measurement rows and report zero records in the manifest.
  Explicitly injected demo samples remain available to demo-inclusive reads;
  live dataset exports read the real telemetry store directly and exclude them.
- Sensor event details expose the sensor health rule snapshot. Categorized notes
  validate PATCH input before mutation and use a durable change sequence for
  latest ordering, including writes in the same second.
- XLSX strings use OOXML escapes for XML-forbidden characters and protect literal
  escape-shaped text. The export snapshot and CSV values remain unchanged.
