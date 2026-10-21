# ESP32 Edge Node v2

This is an isolated firmware path for the ESP32-S3 DevKitC-1 WROOM-1-N16R8.
The existing `firmware/esp32_edge_node` implementation is not changed.

## Implemented behavior

- ADXL345 X/Y/Z sampling at 800 Hz, 512 samples per window (640 ms).
- Mean removal and nine dimensionless features:
  `cf_a_1..3`, `sk_a_1..3`, `ku_a_1..3`.
- Raw samples and RMS/peak values are not stored or sent to the server.
- Normal operation selects the latest valid completed window for each UTC
  25-second slot.
- Anomaly policy keeps the existing 3-consecutive-entry and 5-consecutive-
  recovery rules. Thresholds are test-only compile-time settings in
  `include/config/app_config.h`.
- Invalid windows do not change policy state or consecutive counters.
- Event types are exactly `periodic`, `anomaly_start`, `anomaly_active`, and
  `recovery`.

## Server contract

The firmware sends one window per request:

```text
POST https://motordiagnosis-api.duckdns.org/api/devices/DEV-01-MOT-02/periodic-snapshots
Authorization: Bearer <telemetry:ingest token>
Content-Type: application/json
```

The request has exactly two top-level keys: `window` and `transmission`.
`window.schemaVersion` is `2`; the wire `windowIndex` is a common report
sequence for all event types and resets to zero on reboot with a new `bootId`.
The internal per-measurement index is not sent.

`window.timestamp` is the UTC start time of the measured window and
`startUptimeUs` is its monotonic start time. `periodicSlotEpoch` is the UTC
25-second boundary in Unix seconds for `periodic`, and `null` for other event
types.

`integrity.digest` is SHA-256 of the final transmitted feature numbers in this
order:

```text
cf_a_1, cf_a_2, cf_a_3, sk_a_1, sk_a_2, sk_a_3, ku_a_1, ku_a_2, ku_a_3
```

Each number is IEEE-754 float64, little-endian (72 bytes total). The JSON
values and digest use the same six-decimal final values. For an invalid report,
`features` is `null` and the digest is SHA-256 of an empty byte string:
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The upload task deletes a retry file only when the response is a strict match:
HTTP 202 with `accepted=1` and `duplicate=false`, or HTTP 200 with
`accepted=0` and `duplicate=true`, plus matching device/policy, sensor,
bootId, windowIndex, eventType, featureDigest, and `durablyStored=true`.

## Required local configuration

Copy the example and fill in local credentials. `secrets.h` is ignored and must
not be committed.

```powershell
Copy-Item include/config/secrets.example.h include/config/secrets.h
```

Required values are Wi-Fi SSID/password, the device ingest token, and the
backend CA certificate. The device/site/asset/sensor IDs are in
`include/config/app_config.h`:

```text
deviceId = DEV-01-MOT-02
siteId   = SITE-01
assetId  = SITE-01-MOT-02
sensorId = SENSOR-02
```

## Verification

```powershell
pio test -e native
pio run -e esp32-s3-devkitc-1-n16r8
```

Before flashing a build that changes the local LittleFS record format, format
the filesystem explicitly. Automatic format-on-mount is disabled so a failed
mount cannot erase retry data.
