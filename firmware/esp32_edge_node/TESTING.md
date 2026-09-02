# ESP32 firmware regression testing — v1.2-beta.11.8.5

This revision addresses the second PR #13 data-integrity review round. The native test suite exercises production helper code used directly by `main.cpp`.

## Native regression suite

Run from `firmware/esp32_edge_node`:

```powershell
$env:Path = "C:\msys64\ucrt64\bin;$env:Path"
Remove-Item -Recurse -Force .pio\build\native -ErrorAction SilentlyContinue
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" test -e native
```

Expected suite size for this revision: **55 tests**.

Coverage includes:

- strict top-level ACK validation: valid 200/201 contracts, empty/malformed JSON, nested ACK fields, duplicate keys, wrong device/sequence, invalid duplicate semantics, non-integer sequence;
- canonical first-send/replay payload equality after float32 ring round-trip;
- bounded replay for a full backlog;
- sequence write/read-back gating;
- transient LittleFS read I/O vs proven CRC/schema corruption policy;
- backward-compatible schema-v1 unresolved-ring migration (`epochSeconds=0`) plus schema-v1 transitional packed metadata and schema-v2 reads;
- per-boot offline session metadata and exact monotonic-delta UTC reconstruction;
- stale/previous-session anchor rejection and multi-reboot behavior;
- ACK-loss/power-cut watermark recovery;
- one-spare-slot full-ring write-first transaction and power-cut recovery;
- bounded rejected/isolation archive, single-scan/bounded cleanup work, and ring-first power-cut safety for immediate permanent rejects;
- RFC3339 `/api/health` parsing using the v1.3 response shape;
- HTTPS-by-default transport policy with explicit local-development HTTP opt-in only.

## ESP32 build

```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" run -e esp32-s3-devkitc-1
```

Then upload only after the native suite and ESP32 build pass.

## Hardware fault-injection hooks

All hooks must be `false` in committed production source. Enable **one at a time** for manual regression testing, upload, observe the expected behavior, then return it to `false`.

- `TEST_FORCE_RING_READ_IO_FAIL=true`: queued read must pause replay. Queue head and consumed watermark must not advance.
- `TEST_FORCE_RING_WRITE_FAIL=true`: new local persistence must fail without consuming an existing oldest record.
- `TEST_FORCE_ISOLATION_WRITE_FAIL=true`: a newly rejected packet must fall back to the persistent ring; a queued rejected packet must remain queued.
- Existing `TEST_FORCE_SEQUENCE_NVS_FAIL=true`: no packet may be created or transmitted for an unpersisted sequence.
- Existing `TEST_FORCE_LITTLEFS_MOUNT_FAIL=true`: boot must fail safe without formatting the filesystem.

## Power-cut / reboot cases

The automated tests model the state transitions below; hardware power-cut tests should be repeated before final release when practical.

1. **ACK loss before watermark commit** — replay after reboot is allowed only as an idempotent duplicate; no new sequence is reused.
2. **Full-ring spare write power cut** — if power fails after the new spare record is verified but before the oldest logical consume is durable, recovery retains the newest logical capacity and advances only the safe boundary.
3. **Offline boot with no UTC, then same-boot time sync** — the durable session anchor reconstructs capture time from the actual `millis()` delta, not a nominal 3.99 s cadence.
4. **Offline boot with no UTC, power cut before any time anchor, then reboot** — old-session records must never receive the new boot's anchor. They are isolated as UTC-unresolvable raw captures rather than assigned a fabricated timestamp.
5. **Upgrade from legacy PR #13 unresolved ring format** — schema-v1 records with `RING_FLAG_TIME_UNRESOLVED` and `epochSeconds=0` remain recoverable, are not counted as corruption, and are isolated before their ring source is consumed.
6. **Immediate permanent reject + power cut during isolation replacement** — the packet is first write/read-back verified in the ring; a cut after destination deletion but before rename leaves the ring source recoverable.
7. **Partially written/corrupt time anchor** — anchor blob magic/version/session/epoch/CRC validation rejects it.

## Backend `/api/health` dependency

API v1.3 defines `timestamp` as an RFC3339 string. Example:

```json
{
  "ok": true,
  "service": "Bind Edge AI backend",
  "timestamp": "2026-09-02T09:31:28Z"
}
```

The firmware now follows this contract. A legacy numeric Unix epoch health timestamp is intentionally rejected so implementation and specification cannot silently drift again.

## Transport policy

Production/default configuration refuses plaintext `http://` backend URLs. Configure HTTPS and `BACKEND_CA_CERT_VALUE` for production.

For a trusted local development backend only, an ignored `secrets.h` may explicitly set:

```cpp
#define ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE true
```

Do not enable that flag in committed/default configuration.
