# Adaptive HTTPS transmission — development verification

Date: 2026-09-10  
Branch: `feat/adaptive-https-transmission`  
Base: `348f1ce`  
Initial verification was recorded before commit `5287cc7`. The PR44 follow-up below is a local change; no deployment, board upload or flash format was performed.

## Current edge transmission state policy

ADXL345 raw windows are measured and classified continuously, approximately once
per 0.66 seconds. Measurement frequency is deliberately different from upload
frequency: the device does not upload every raw window.

| State | Transition and raw-upload rule |
|---|---|
| `NORMAL` | Enter `ANOMALY_ACTIVE` after **three consecutive** abnormal windows and upload that third raw window immediately. While normal, upload one current raw window every five minutes of measurement time. |
| `ANOMALY_ACTIVE` | Continue judging every window, but upload one current raw window every ten seconds. Return to `NORMAL` after **five consecutive** normal windows and immediately upload the normal window that confirms recovery. |

Window index/time discontinuity, FIFO-quality failure, missing baseline and severe
limits are separate protective/quality conditions and remain immediate priority
uploads. They are not pump-fault verdicts. `recoveryMs` remains accepted in the
remote configuration for compatibility but is not used by this five-valid-window
recovery policy.

## Results

| Check | Result |
|---|---|
| Related Python suite | 130 run: 124 passed, 6 skipped, no failures/errors |
| New policy C++ native tests | 9 passed |
| Existing vibration C++ native tests | 6 passed |
| Native XYZ/21-feature fixture → raw API/store | Executed with `IOT_WINDOW_FIXTURE_EXE` |
| Existing N8 build | Passed |
| Opt-in N8 adaptive build | Passed; static RAM 205,832 / 327,680 bytes; app 1,074,037 / 3,342,336 bytes |
| Whitespace/error check | `git diff --check` passed |

The Python suite includes the raw window, RF66 runtime/confirmation/events, feature window, operations and new transmission-policy modules. Some imported test classes also run under unittest module discovery; the total is executed cases, not a claim of 130 newly added independent scenarios. Six skipped checks are not counted as passes.

The Windows native tests were compiled with the already installed Zig C/C++ toolchain and Unity. A first PlatformIO native invocation could not locate host gcc/g++; the direct native builds ran the actual test sources successfully. CI still runs `pio test -e native` on Ubuntu and now also builds the opt-in adaptive profile.

## New coverage

- Missing/invalid normal baseline keeps fast transfer, not a five-minute blind period.
- Three consecutive low/high RMS windows, five consecutive normal recovery windows,
  immediate severe/quality conditions, discontinuity resets, five-minute normal
  raw cadence and ten-second active raw cadence.
- Five-minute frozen drain cutoff, timer wrap, early storage-pressure drain and priority selection.
- Failed/mismatched ACK cannot authorize deletion; exact per-window identity checks, status/count consistency and digest format.
- Real local HTTP endpoint: four-window priority receipt → earlier four-window archive receipt → duplicate replay ACK.
- Legacy strict ordering, authentication/mapping validation, conflicting duplicate and atomic batch rollback remain in the existing suite.
- Priority inference preempts queued archive work; archival processing does not alter live event state.
- Older backfills build their own historical three-window confirmation without rewinding live state.
- SQLite restart preserves first-delivery metadata, deduplication and confirmation lane heads.
- Periodic input freshness uses a 360-second inspection allowance; processing backlog uses server receipt time for adaptive batches. RF66 live event freshness remains 30 seconds.

## Reproduction

```sh
python -m unittest tests.test_transmission_policy tests.test_rf66_confirmation tests.test_rf66_events tests.test_raw_vibration tests.test_vibration_windows tests.test_operations tests.test_rf66 -q
pio test -d firmware/esp32_edge_node -e native
pio run -d firmware/esp32_edge_node -e esp32-s3-devkitc-1-n8
pio run -d firmware/esp32_edge_node -e esp32-s3-devkitc-1-n8-adaptive
```

The native raw parity check additionally needs `IOT_WINDOW_FIXTURE_EXE` to point to the compiled `test_vibration_window` executable supporting `--emit-fixture`. Counts of skipped checks can differ without that fixture or optional model-test dependencies.

## Not established by these checks

- No physical ESP32 FIFO timing, TLS throughput, live heap/stack headroom or acoustic interference measurement.
- No physical LittleFS power-cut/flash-wear test. CRC and source-retention code are implemented; actual filesystem crash behavior still requires board tests.
- No five-minute measured end-to-end run on the target motor and no commissioned detection thresholds.
- No in-place partition migration: the opt-in profile relocates LittleFS and removes the second OTA app slot. Existing files need backup and a deliberate migration plan.
- No RF66 accuracy improvement or guaranteed delivery deadline is claimed.

The implementer-facing handoff and board test matrix are in [IoT proposal](IoT_전송정책_개발제안서_20260910.md).

## PR44 follow-up: summary replay completion (2026-09-10)

Base reviewed: `5287cc7`. The old summary gate only noticed an empty queue at the
next loop entry. A fresh summary was enqueued after replay in the same loop, so
the periodic drain could remain enabled indefinitely.

The main loop now uses the host-testable `SummarySchedule` dispatcher. It checks
the queue immediately after bounded replay returns, before the caller acquires
and enqueues a fresh summary. An empty queue clears draining and starts a new
300,000 ms wait from completion. Partial or failed replay leaves pending records
eligible for retry. The urgent/storage-fault/health-journal bypass remains enabled;
raw-window batching and the backend are unchanged.

Regression verification:

- Extracted the original gate behavior into the same dispatcher used by firmware;
  all four new native tests failed before adding the post-replay completion check.
- After the fix, all 13 adaptive native tests pass (9 existing + 4 new).
- The 6 existing vibration native tests also pass: 19 C++ tests in total.
- New cases cover three successive five-minute batches with immediate fresh
  enqueue, partial/failed replay, completion-time accounting for HTTP latency,
  priority bypass and normal recovery, an empty queue, and `millis()` wraparound.
- Both N8 builds pass. Adaptive static RAM remains 205,832 / 327,680 bytes;
  app size is 1,074,069 / 3,342,336 bytes. The existing N8 build remains unchanged
  at 209,944 RAM bytes and 1,052,333 app bytes.
- No backend changes were made or backend tests rerun for this focused follow-up.
  No physical board measurement or upload was performed.
