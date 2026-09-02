# ESP32 firmware regression testing

This firmware uses two complementary test layers.

## 1. Native automated tests

Run from `firmware/esp32_edge_node`:

```powershell
$env:Path = "C:\msys64\ucrt64\bin;$env:Path"
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" test -e native
```

The native suite exercises production helper functions used by `main.cpp`, not
standalone test-only copies.

Covered policies include:

- ACK body validation: `accepted`, `deviceId`, `sequence`, and 200/201 duplicate contract.
- Canonical first-send/replay JSON stability using the same float32 values stored by the ring.
- Unlabeled telemetry contract.
- Full-backlog replay batch limiting.
- Sequence advancement only after durable NVS write/read-back agreement.
- Cold-boot ordinal-to-UTC anchor timestamp calculation.
- ACK-loss / power-cut recovery around the durable consumed watermark.
- Queue-span recovery and next-ring-ordinal behavior.

## 2. Hardware regression tests

The following failure conditions still require the ESP32 because they involve
Wi-Fi, NVS, LittleFS, sensors, or real power interruption.

### NVS sequence persistence failure

1. Set `TEST_FORCE_SEQUENCE_NVS_FAIL = true`.
2. Confirm acquisition continues but packet creation fails.
3. Confirm the failed candidate sequence is neither sent nor enqueued.
4. Restore the flag to `false`.
5. Confirm the same candidate is then durably allocated once and subsequent
   sequences continue monotonically.

### LittleFS mount failure without format

1. Create an offline backlog.
2. Set `TEST_FORCE_LITTLEFS_MOUNT_FAIL = true`.
3. Confirm all mount retries fail and firmware explicitly refuses format.
4. Restore the flag to `false` and reboot.
5. Confirm the original backlog is recovered and replayed.

### Network-free cold boot

1. Disable the network before powering the ESP32.
2. Cold boot.
3. Confirm new measurements continue with `UTC unresolved` and are persisted.
4. Restore network without rebooting.
5. Confirm a durable UTC anchor is created and the queued data replays FIFO.

### Full backlog / interleaved replay

1. Accumulate more than one replay batch.
2. Restore network.
3. Confirm at most `REPLAY_MAX_RECORDS_PER_LOOP` old records replay before the
   loop returns to a fresh synchronized acquisition.
4. Confirm fresh measurements are appended behind the remaining backlog.

### Replay power interruption

1. Interrupt power while replay is active.
2. Reboot.
3. Confirm queue recovery starts after the durable consumed watermark.
4. Already-accepted records in the uncommitted watermark window may replay and
   must receive safe `HTTP 200 duplicate:true` ACKs.

## CI

`.github/workflows/firmware-ci.yml` runs native tests and a clean-checkout ESP32
build. Since `include/secrets.h` is intentionally ignored, the CI build also
verifies that `secrets.example.h` fallback keeps a fresh checkout buildable.

The CI additionally rejects reintroduction of `LittleFS.begin(true)`.
