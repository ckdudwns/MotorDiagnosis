# ESP32 firmware regression testing — v1.3-signal-analysis.1

연속 800Hz/640ms 모드는 [연속 진동 구간 안내](../../docs/continuous-vibration-windows.md)를 참고하세요.
기본 대상은 `esp32-s3-devkitc-1-n8`(8MB, No PSRAM)이며 `test_vibration_window`가 추가됐습니다.
기존 `esp32-s3-devkitc-1` 환경은 16MB/PSRAM 설정을 보존한 별도 환경입니다.
2026-09-08 N8 기본은 원시 XYZ 전송입니다. [원시 전송 규격·실보드 시험](../../docs/raw-vibration-windows.md)의
8구간(5.12초) RAM 큐와 부팅 후 미전송 유실 제한을 확인하세요. 특징 전송 모드는 RAW_VIBRATION_ENABLED=0입니다.

This revision includes device health, bounded remote configuration, communication quality and opt-in edge statistics/requested waveforms while retaining the data-integrity regressions. The native test suite exercises production helper code used directly by `main.cpp`. See [analysis operations](../../docs/signal-analysis-operations.md) for the expanded contract and physical-test limitations.

## Native regression suite

Run from `firmware/esp32_edge_node`:

```powershell
$env:Path = "C:\msys64\ucrt64\bin;$env:Path"
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" test -e native
```

Expected suite size for this revision: **126 tests** (55 firmware logic + 29 device health + 22 remote configuration + 15 communication quality + 5 edge analysis).

The native compiler must support C++11. ArduinoJson and Unity are resolved by PlatformIO.
Run all five suites, or select one with `test -e native -f test_device_health` or `test -e native -f test_edge_analysis`. The native environment enables Unity double assertions for FFT numerical checks.

The analysis fixture also supports `--emit-fixture <RFC3339-UTC>` to emit a deterministic raw frame using production C++ code. Set `IOT_ANALYSIS_FIXTURE_EXE` to this executable when running `tests.test_edge_analysis`; Python checks actual backend acceptance, float32 samples and independently recomputed AI1 features. The four-channel samples are synthetic test vectors, not actual board measurements.

Item 2 (RPM acquisition/estimation) is excluded. Analysis frames remain schema v1 with four-channel statistics and optional requested waveforms. Existing telemetry keeps `rpm:null`; `tests.test_edge_analysis` also checks that the analysis contract rejects unsupported schema versions and speed fields.

## Device-health integration

See [device-health setup and behavior](../../docs/iot-device-health.md) for separate credentials,
fault codes, NVS journal boundaries, bounded retries and timestamp fallback.

After building/running `test_device_health`, run the optional backend HTTP contract tests
from the repository root. Point the environment variable at that native executable (not
the ESP32 firmware binary):

```powershell
$env:IOT_HEALTH_FIXTURE_EXE = (Resolve-Path firmware/esp32_edge_node/.pio/build/native/program.exe).Path
python -m unittest discover -s tests -p test_iot_health_contract.py -v
```

These five tests use generated C++ requests and a temporary local HTTP server. They
exercise health-only authorization, active/recovered retry behavior, metrics and
firmware validation of the actual server response. They skip when no native executable
is supplied. No production credentials, persistent production database or board is used.

Device-health native tests cover CRC/restart, FIFO/full-queue safety, ACK loss/marker
failure, same-/previous-boot time handling, recovery ordering, bounded report and sensor
retries, millis wrap, invalid metrics, stuck PCM and exact device/time response matching.
PR23 regressions additionally cover multiple replay batches, timestamp equality,
unknown/read-failed heads, metrics-only ACKs, shared fault boundaries and failed
transition-marker persistence. The C++ replay fixture uses the same production
dispatcher and payload builders as `main.cpp`. HTTP tests assert zero analysis
failures for 15 pre-fault and 15 post-recovery measurements, and verify that the
old health-first ordering fails analysis for all 15 earlier points despite 201 ACKs.

On hardware, additionally test:

- absent ADXL345 / failed I2S initialization: health servicing remains available;
- I2S read timeout, missing/constant digital channel and successful recovery window;
- initialization retry exhaustion: no infinite reinitialization, network remains alive;
- delayed vibration worker: no overlapping measurement requests or concurrent SPI reset;
- health 401/403/5xx, ACK loss and NVS write failure: no silent transition consumption;
- power cut with active/recovered pending and cold boot before UTC;
- unchanged telemetry sequence/ring/replay and no new verified ground-truth labels.

## Existing telemetry integrity regressions

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
