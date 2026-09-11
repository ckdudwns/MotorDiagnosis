"""Raw count preservation, bounded ingestion, clock integrity and parity."""

import base64
import copy
from datetime import timedelta
import http.client
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from motor_diagnosis import data, device_lifecycle
from motor_diagnosis.raw_vibration import (
    ENCODING, FEATURE_PROFILE, G_PER_COUNT, NAMES, PROFILE_ID,
    RawVibrationStore, decode_samples, extract66,
)
from motor_diagnosis.server import create_server
from motor_diagnosis.vibration_windows import VibrationWindowStore
from tests.test_measured_rpm import RpmSetup

DEVICE = "DEV-01-GEN-01"


def counts():
    rng = random.Random(921)
    return [tuple(round(40*math.sin(2*math.pi*(i*(23+a*41))/800)) + rng.randrange(-10, 11)
                  for a in range(3)) for i in range(512)]


def encoded(rows):
    return base64.b64encode(b"".join(struct.pack("<hhh", *r) for r in rows)).decode()


class RawWindowTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.store = RawVibrationStore()
        self.addCleanup(self.store.close)

    def window(self, index=0, **changes):
        return {"schemaVersion": 1, "deviceId": DEVICE, "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01", "bootId": "c"*32, "windowIndex": index,
                "startUptimeUs": index*640000,
                "timestamp": (self.started + timedelta(seconds=index*.640)).isoformat(),
                "sampleRateHz": 800, "sampleCount": 512, "profileId": PROFILE_ID,
                "axes": ["X", "Y", "Z"], "unit": "count", "quality": "valid",
                "gPerCount": G_PER_COUNT, "encoding": ENCODING, "samples": encoded(counts()),
                **changes}

    def send(self, *windows, store=None):
        return (store or self.store).ingest(self.principal, DEVICE, {"windows": list(windows)})

    def test_samples_preserved_and_all_windows_independently_transformed(self):
        windows = [self.window(i) for i in range(4)]
        windows[1]["samples"] = encoded([tuple(v*2 for v in row) for row in counts()])
        self.assertEqual(self.send(*windows)[1], 202)
        for _ in windows:
            self.assertTrue(self.store.tick())
        result = self.store.list_device(self.admin, DEVICE)
        self.assertEqual(result["featureProfileId"], FEATURE_PROFILE)
        self.assertEqual(len(result["featureNames"]), 66)
        for item in result["items"]:
            w, analysis = item["window"], item["analysis"]
            self.assertEqual(w, windows[w["windowIndex"]])
            self.assertEqual(len(analysis["modelInput"]), 66)
            self.assertEqual(analysis["status"], "waiting_model")
            self.assertIsNone(analysis["verdict"])
            self.assertFalse(analysis["affectsAlerts"])
        self.assertNotEqual(result["items"][2]["analysis"]["modelInput"], result["items"][3]["analysis"]["modelInput"])

    def test_lossless_signed_little_endian_round_trip(self):
        w = self.window()
        self.assertEqual(decode_samples(w), counts())
        self.assertEqual(len(base64.b64decode(w["samples"])), 3072)

    def test_bad_encoding_shape_scale_and_legacy_fields_rejected(self):
        for patch in ({"samples": "A"*4095}, {"samples": "!"*4096}, {"samples": None},
                      {"sampleCount": 511}, {"sampleCount": True}, {"gPerCount": True},
                      {"gPerCount": .004}, {"gPerCount": float("nan")},
                      {"unit": "g"}, {"encoding": "int16be"}, {"features": [0]*21},
                      {"axes": ["Z", "Y", "X"]}, {"sampleRateHz": 12000}):
            with self.subTest(patch=patch), self.assertRaises(data.ApiError):
                self.send(self.window(**patch))

    def test_invalid_partial_data_retained_not_zero_filled(self):
        self.send(self.window(quality="sample_gap", sampleCount=3, samples=encoded(counts()[:3])))
        self.store.tick()
        item = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual(decode_samples(item["window"]), counts()[:3])
        self.assertEqual(item["analysis"]["status"], "unavailable")
        self.assertNotIn("modelInput", item["analysis"])

    def test_claimed_valid_clipping_and_constant_axis_never_normal(self):
        rows = counts()
        clipped = copy.deepcopy(rows)
        clipped[10] = (4095, 0, 0)
        constant = [(0, row[1], row[2]) for row in rows]
        for i, sample in enumerate((clipped, constant)):
            self.send(self.window(i, samples=encoded(sample)))
            with self.assertLogs("motor_diagnosis.vibration_windows", level="ERROR"):
                self.store.tick()
        self.assertTrue(all(i["analysis"]["status"] == "unavailable" for i in self.store.list_device(self.admin, DEVICE)["items"]))

    def test_count_outside_sensor_range_rejected(self):
        rows = counts()
        rows[0] = (32767, 0, 0)
        with self.assertRaises(data.ApiError):
            self.send(self.window(samples=encoded(rows)))

    def test_ack_replay_conflict_and_batch_rollback(self):
        first = self.window()
        self.send(first)
        self.assertEqual(self.send(first)[1], 200)
        conflicting = self.window(samples=encoded([(1, 2, 3)]*512))
        with self.assertRaises(data.ApiError) as error:
            self.send(self.window(1), conflicting)
        self.assertEqual(error.exception.code, "WINDOW_CONFLICT")
        self.assertEqual(len(self.store.list_device(self.admin, DEVICE)["items"]), 1)

    def test_out_of_order_lower_index_is_accepted_without_high_water_regression(self):
        self.send(self.window(5))
        result, status = self.send(self.window(2))
        self.assertEqual((result["accepted"], status), (1, 202))
        stream = self.store.db.execute(
            "SELECT idx,uptime FROM window_streams WHERE device=? AND boot=?",
            (DEVICE, "c" * 32)).fetchone()
        self.assertEqual((stream["idx"], stream["uptime"]), (5, 5 * 640000))
        self.assertEqual(self.send(self.window(6))[1], 202)
        self.assertEqual(
            {item["window"]["windowIndex"] for item in self.store.list_device(self.admin, DEVICE)["items"]},
            {2, 5, 6},
        )

    def test_out_of_order_clock_and_context_rejections_remain(self):
        self.send(self.window(5))
        with self.assertRaises(data.ApiError) as clock_error:
            self.send(self.window(2, startUptimeUs=5 * 640000))
        self.assertEqual(clock_error.exception.code, "WINDOW_SEQUENCE_CONFLICT")

        self.store.db.execute(
            "UPDATE window_streams SET context=? WHERE device=? AND boot=?",
            (json.dumps(["SITE-OTHER", "SITE-OTHER-MOT-01", PROFILE_ID]), DEVICE, "c" * 32),
        )
        self.store.db.commit()
        with self.assertRaises(data.ApiError) as context_error:
            self.send(self.window(6))
        self.assertEqual(context_error.exception.code, "WINDOW_SEQUENCE_CONFLICT")

    def test_frozen_utc_cumulative_clock_rejected_atomically(self):
        first = self.window()
        with self.assertRaises(data.ApiError) as error:
            self.send(first, self.window(1, timestamp=first["timestamp"]), self.window(2, timestamp=first["timestamp"]))
        self.assertEqual(error.exception.code, "TIMESTAMP_UPTIME_MISMATCH")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM raw_clock_anchors").fetchone()[0], 0)
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"], [])
        self.send(first, self.window(1), self.window(2))

    def test_slow_cumulative_drift_not_hidden_by_small_adjacent_error(self):
        for i in range(4):
            self.send(self.window(i, timestamp=(self.started + timedelta(seconds=i*.340)).isoformat()))
        with self.assertRaises(data.ApiError):
            self.send(self.window(4, timestamp=(self.started + timedelta(seconds=4*.340)).isoformat()))

    def test_backpressure_and_batch_limit_preserve_data(self):
        self.store.max_rows = 1
        with self.assertRaises(data.ApiError) as error:
            self.send(self.window(), self.window(1))
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"], [])
        with self.assertRaises(data.ApiError):
            self.send(*(self.window(i) for i in range(5)))

    def test_history_raw_queue_and_clock_anchor_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"raw.sqlite3"
            store = RawVibrationStore(path)
            self.send(self.window(), store=store)
            store.close()
            store = RawVibrationStore(path)
            try:
                with self.assertRaises(data.ApiError):
                    device_lifecycle.check_history(DEVICE)
                with self.assertRaises(data.ApiError):
                    self.send(self.window(2, timestamp=self.window()["timestamp"]), store=store)
                self.assertTrue(store.tick())
                self.assertEqual(decode_samples(store.list_device(self.admin, DEVICE)["items"][0]["window"]), counts())
            finally:
                store.close()

    def test_mapping_and_permissions(self):
        with self.assertRaises(data.ApiError):
            self.send(self.window(assetId="SITE-01-MOT-02"))
        with self.assertRaises(data.ApiError):
            self.store.ingest({"permissions": ["telemetry:ingest"], "allowedDeviceIds": ["OTHER"]}, DEVICE, {"windows": [self.window()]})
        with self.assertRaises(data.ApiError):
            self.store.list_device({"role": "AI1"}, DEVICE)

    def test_raw_and_feature_database_cannot_be_accidentally_shared(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"shared.sqlite3"
            store = RawVibrationStore(path)
            try:
                self.send(self.window(), store=store)
                with self.assertRaisesRegex(ValueError, "different database"):
                    VibrationWindowStore(path)
                self.assertEqual(len(store.list_device(self.admin, DEVICE)["items"]), 1)
            finally:
                store.close()

    def test_http_endpoint_and_raw_retention_query(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            path = f"/api/devices/{DEVICE}/raw-vibration-windows"
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            connection.request("POST", path, json.dumps({"windows": [self.window()]}),
                               {"Authorization": "Bearer demo-telemetry-ingest-token", "Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 202, body)
            connection.close()
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            connection.request("GET", path, headers={"Authorization": "Bearer " + self.token})
            response = connection.getresponse()
            result = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(result["limit"], 20)
            self.assertEqual(result["items"][0]["window"]["samples"], self.window()["samples"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @unittest.skipUnless(os.environ.get("IOT_WINDOW_FIXTURE_EXE"), "Native fixture not configured")
    def test_native_counts_reach_raw_store_and_match_firmware_base21(self):
        fixture = json.loads(subprocess.check_output(
            [os.environ["IOT_WINDOW_FIXTURE_EXE"], "--emit-fixture"], text=True, timeout=10))
        w = self.window(samples=encoded(fixture["counts"]))
        self.send(w)
        self.store.tick()
        item = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual([list(r) for r in decode_samples(item["window"])], fixture["counts"])
        expected = [struct.unpack("<f", struct.pack("<f", v))[0] for v in fixture["features"]]
        for a, b in zip(item["analysis"]["modelInput"][:21], expected):
            self.assertAlmostEqual(a, b, delta=1e-10 + abs(b)*1e-7)


class RawFeatureParityTest(unittest.TestCase):
    def test_reference_training66_and_numeric_policy(self):
        try:
            import numpy as np
            from ai.ai1.mcc5_training.spectral import extract66 as reference, CONTRACT
        except ImportError as exc:
            self.skipTest(f"Optional research dependencies: {exc}")
        rows = counts()
        expected = reference(np.asarray(rows, dtype=np.float64)*G_PER_COUNT)
        expected[:21] = expected[:21].astype(np.float32).astype(np.float64)
        actual = extract66(rows)
        self.assertEqual(NAMES, CONTRACT["featureNames"])
        np.testing.assert_allclose(actual, expected, rtol=1e-7, atol=1e-10)
        self.assertEqual(actual[:21], expected[:21].tolist())

    def test_repeatable_bounded_cpu_extraction(self):
        rows = counts()
        a = extract66(rows)
        self.assertEqual(a, extract66(rows))
        self.assertEqual(len(a), 66)
        self.assertTrue(all(math.isfinite(v) for v in a))


if __name__ == "__main__":
    unittest.main()
