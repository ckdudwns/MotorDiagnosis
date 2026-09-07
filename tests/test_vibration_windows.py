"""Every-window persistence/authorization tests; no physical-board claims."""
import copy
from datetime import timedelta
import http.client
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest import mock

from motor_diagnosis import data, device_lifecycle
from motor_diagnosis.server import create_server
from motor_diagnosis.vibration_windows import VibrationWindowStore
from motor_diagnosis.window_features import PROFILE_ID, derive, feature_names
from tests.test_measured_rpm import RpmSetup

DEVICE = "DEV-01-GEN-01"


class FakeCheckpoint:
    kind = "dense_autoencoder"
    sequence_length = 1
    checksum = "sha256:" + "a" * 64

    def __init__(self, variant="base21"):
        self.names = feature_names(variant)
        self.calls = []

    def predict(self, matrix, names):
        self.calls.append((matrix, names))
        return {"errors": [0.25], "verdict": [False], "threshold": 0.5}


class WindowTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.store = VibrationWindowStore()
        self.addCleanup(self.store.close)

    def window(self, index=0, **changes):
        return {"schemaVersion": 1, "deviceId": DEVICE, "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01", "bootId": "b" * 32,
                "windowIndex": index, "startUptimeUs": index * 640000,
                "timestamp": (self.started + timedelta(milliseconds=index * 640)).isoformat(),
                "sampleRateHz": 800, "sampleCount": 512, "profileId": PROFILE_ID,
                "axes": ["X", "Y", "Z"], "unit": "g", "quality": "valid",
                "features": [0.1, 0.2, 3.0, 0.001, 0.002, 0.003, 0.004] * 3,
                **changes}

    def send(self, *windows, store=None, principal=None):
        return (store or self.store).ingest(principal or self.principal, DEVICE, {"windows": list(windows)})

    def test_all_windows_processed_not_averaged_and_model_unavailable_is_not_normal(self):
        values = [self.window(i) for i in range(16)]
        result, status = self.send(*values)
        self.assertEqual((result["accepted"], status), (16, 202))
        for _ in values:
            self.assertTrue(self.store.tick())
        self.assertFalse(self.store.tick())
        items = self.store.list_device(self.admin, DEVICE)["items"]
        self.assertEqual(len(items), 16)
        self.assertEqual({i["window"]["windowIndex"] for i in items}, set(range(16)))
        self.assertTrue(all(i["analysis"]["status"] == "waiting_model" and
                            i["analysis"]["verdict"] is None for i in items))

    def test_ack_loss_replay_and_conflict_are_atomic(self):
        self.send(self.window())
        result, status = self.send(self.window())
        self.assertEqual((result["accepted"], status), (0, 200))
        conflicting = self.window(features=[0.2, 0.4, 3, 1, 2, 3, 4] * 3)
        with self.assertRaises(data.ApiError) as error:
            self.send(self.window(1), conflicting)
        self.assertEqual(error.exception.code, "WINDOW_CONFLICT")
        self.assertEqual(len(self.store.list_device(self.admin, DEVICE)["items"]), 1)
        self.send(self.window(1))

    def test_skipped_windows_and_new_boot_are_visible(self):
        self.send(self.window(3), self.window(5), self.window(0, bootId="c" * 32))
        items = self.store.list_device(self.admin, DEVICE)["items"]
        self.assertEqual([i["missingWindowsBefore"] for i in items], [0, 1, 3])

    def test_invalid_quality_retained_without_fabricated_features(self):
        self.send(self.window(quality="fifo_overrun", sampleCount=200, features=None))
        self.store.tick()
        item = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual(item["analysis"]["status"], "unavailable")
        self.assertEqual(item["analysis"]["reason"], "fifo_overrun")
        self.assertIsNone(item["analysis"]["verdict"])

    def test_malformed_units_counts_axes_features_and_booleans_rejected(self):
        for patch in ({"unit": "mm/s"}, {"sampleRateHz": 12000}, {"sampleCount": 511},
                      {"axes": ["Z", "Y", "X"]}, {"schemaVersion": True},
                      {"windowIndex": True}, {"features": [0] * 21},
                      {"quality": "sample_gap"}, {"features": [True] * 21},
                      {"features": [float("nan")] * 21}, {"bootId": "bad"},
                      {"profileId": "edge-statistics-v1"}, {"extra": 1}):
            with self.subTest(patch=patch), self.assertRaises(data.ApiError):
                self.send(self.window(**patch))
        malformed = self.window()
        del malformed["siteId"]
        with self.assertRaises(data.ApiError):
            self.send(malformed)

    def test_scope_mapping_and_read_access_are_enforced(self):
        with self.assertRaises(data.ApiError):
            self.send(self.window(), principal={"permissions": ["telemetry:ingest"], "allowedDeviceIds": ["OTHER"]})
        with self.assertRaises(data.ApiError):
            self.send(self.window(assetId="SITE-01-MOT-02"))
        with self.assertRaises(data.ApiError):
            self.store.list_device({"role": "AI1"}, DEVICE)

    def test_registered_dotted_id_accepted_by_both_http_window_endpoints(self):
        from motor_diagnosis import ingest_auth, raw_vibration
        from tests.test_raw_vibration import counts, encoded

        # Keep the existing ready asset/rollout and replace its active mapping
        # through the same registration functions used by the HTTP handlers.
        data.update_device(self.admin, DEVICE, {"mappingStatus": "inactive"})
        asset = data.get_asset("SITE-01", "SITE-01-GEN-01")
        device = data.create_device(self.admin, "SITE-01", {
            "id": "DEV.REVIEW.01", "assetId": asset["id"],
            "certificateId": "review-cert", "certificateFingerprint": "a" * 64,
        })
        self.assertIsNotNone(ingest_auth.DEVICE.fullmatch(device["id"]))
        window = self.window(deviceId=device["id"], assetId=asset["id"])
        raw = {**window, "profileId": raw_vibration.PROFILE_ID, "unit": "count",
               "encoding": raw_vibration.ENCODING, "gPerCount": .0039,
               "samples": encoded(counts())}
        del raw["features"]
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for endpoint, value in (("vibration-windows", window), ("raw-vibration-windows", raw)):
                with self.subTest(endpoint=endpoint):
                    path = f"/api/devices/{device['id']}/{endpoint}"
                    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
                    try:
                        connection.request("POST", path, json.dumps({"windows": [value]}), {
                            "Authorization": "Bearer demo-telemetry-ingest-token",
                            "Content-Type": "application/json",
                        })
                        response = connection.getresponse()
                        body = json.load(response)
                        self.assertEqual(response.status, 202, body)
                        self.assertEqual(body["deviceId"], device["id"])
                        connection.request("GET", path, headers={"Authorization": "Bearer " + self.token})
                        response = connection.getresponse()
                        body = json.load(response)
                        self.assertEqual(response.status, 200, body)
                        self.assertEqual(body["items"][0]["window"], value)
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_envelope_ids_require_registration_canonical_spelling(self):
        from motor_diagnosis.vibration_windows import normalize
        for key in ("deviceId", "siteId", "assetId"):
            for value in (None, 123, "", "   ", " dev.review.01", "dev.review.01"):
                with self.subTest(key=key, value=value), self.assertRaises(data.ApiError):
                    normalize(self.window(**{key: value}))

    def test_capacity_preserves_retryable_batch_and_watermark(self):
        with mock.patch("motor_diagnosis.vibration_windows.MAX_PENDING", 1):
            with self.assertRaises(data.ApiError) as error:
                self.send(self.window(), self.window(1))
            self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"], [])
        self.send(self.window(), self.window(1))

    def test_history_and_queue_survive_restart_without_model(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "windows.sqlite3"
            store = VibrationWindowStore(database)
            self.send(self.window(), store=store)
            store.close()
            store = VibrationWindowStore(database)
            try:
                with self.assertRaises(data.ApiError):
                    device_lifecycle.check_history(DEVICE)
                self.assertTrue(store.tick())
                with mock.patch("motor_diagnosis.vibration_windows.time.time", return_value=10**10):
                    store.prune()
                self.assertEqual(store.list_device(self.admin, DEVICE)["items"], [])
                with self.assertRaises(data.ApiError):
                    device_lifecycle.check_history(DEVICE)
                with self.assertRaises(data.ApiError):
                    self.send(self.window(), store=store) # Pruned IDs cannot become new work.
            finally:
                store.close()

    def test_exact_single_window_model_and_ratios(self):
        checkpoint = FakeCheckpoint("ratios36")
        store = VibrationWindowStore(checkpoint=checkpoint, variant="ratios36")
        try:
            self.send(self.window(), self.window(1), store=store)
            store.tick(); store.tick()
            self.assertEqual(len(checkpoint.calls), 2)
            for matrix, names in checkpoint.calls:
                self.assertEqual((len(matrix), len(matrix[0]), len(names)), (1, 36, 36))
                self.assertEqual(matrix[0][-5:], [0.1, 0.2, 0.3, 0.4, 2.0])
            self.assertEqual(store.list_device(self.admin, DEVICE)["items"][0]["analysis"]["status"], "completed")
        finally:
            store.close()
        checkpoint.names = ["cwru"] * 26
        with self.assertRaises(ValueError):
            VibrationWindowStore(checkpoint=checkpoint)

    def test_real_torch_single_window_adapter(self):
        try:
            import torch
            from ai.ai1.week4.ai1.freq_baseline import train_and_evaluate as training
            from ai.ai2.model_runtime import Checkpoint
        except ImportError:
            self.skipTest("Optional PyTorch runtime is not installed")
        names = feature_names("ratios36")
        torch.manual_seed(7)
        content = io.BytesIO()
        torch.save({"model_type": "dense_autoencoder", "input_dim": 36,
                    "seq_len": None, "feature_names": names,
                    "scaler_mean": [0.0] * 36, "scaler_std": [1.0] * 36,
                    "threshold": 100.0, "sigma": 3.0,
                    "state_dict": training._MODEL_BUILDERS["dense_autoencoder"](36).state_dict()}, content)
        checkpoint = Checkpoint(content.getvalue())
        store = VibrationWindowStore(checkpoint=checkpoint, variant="ratios36")
        try:
            self.send(self.window(), store=store)
            self.assertTrue(store.tick())
            actual = store.list_device(self.admin, DEVICE)["items"][0]["analysis"]
            expected = checkpoint.predict([derive(self.window()["features"], "ratios36")], names)
            self.assertEqual(actual["status"], "completed")
            self.assertEqual(actual["error"], expected["errors"][0])
            self.assertEqual(actual["modelVersion"], checkpoint.checksum)
            self.assertFalse(actual["affectsAlerts"])
        finally:
            store.close()

    def test_model_failure_never_blocks_following_window_or_reports_normal(self):
        checkpoint = FakeCheckpoint()
        checkpoint.predict = mock.Mock(side_effect=[RuntimeError("test failure"),
                           {"verdict": [False], "errors": [0.1], "threshold": 1}])
        store = VibrationWindowStore(checkpoint=checkpoint)
        try:
            self.send(self.window(), self.window(1), store=store)
            with self.assertLogs("motor_diagnosis.vibration_windows", level="ERROR"):
                store.tick()
            store.tick()
            items = store.list_device(self.admin, DEVICE)["items"]
            self.assertEqual([i["analysis"]["status"] for i in items], ["completed", "unavailable"])
            self.assertIsNone(items[1]["analysis"]["verdict"])
        finally:
            store.close()

    def test_http_post_and_authenticated_get(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            path = f"/api/devices/{DEVICE}/vibration-windows"
            connection.request("POST", path, json.dumps({"windows": [self.window()]}),
                               {"Authorization": "Bearer demo-telemetry-ingest-token", "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 202, response.read())
            connection.close()
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            connection.request("GET", path, headers={"Authorization": "Bearer " + self.token})
            response = connection.getresponse()
            content = json.load(response)
            self.assertEqual(response.status, 200, content)
            self.assertEqual(len(content["items"]), 1)
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(os.environ.get("IOT_WINDOW_FIXTURE_EXE"), "Native window fixture executable not configured")
    def test_cpp_features_match_independent_periodic_hann_reference_and_ingest(self):
        fixture = json.loads(subprocess.check_output(
            [os.environ["IOT_WINDOW_FIXTURE_EXE"], "--emit-fixture"], text=True, timeout=10))
        expected = []
        n = 512
        taper = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
        normalization = n * sum(w * w for w in taper)
        for axis in range(3):
            raw = [row[axis] * 0.0039 for row in fixture["counts"]]
            mean = sum(raw) / n
            ac = [v - mean for v in raw]
            variance = sum(v * v for v in ac) / n
            expected += [math.sqrt(variance), max(abs(v) for v in ac),
                         sum(v ** 4 for v in ac) / n / variance ** 2]
            bands = [0.0] * 4
            for k in range(1, n // 2 + 1):
                frequency = k * 800 / n
                real = sum(ac[i] * taper[i] * math.cos(2 * math.pi * k * i / n) for i in range(n))
                imag = sum(ac[i] * taper[i] * math.sin(2 * math.pi * k * i / n) for i in range(n))
                power = (real * real + imag * imag) * (1 if k == n // 2 else 2) / normalization
                if frequency < 350:
                    band = 0 if frequency < 50 else 1 if frequency < 100 else 2 if frequency < 200 else 3
                    bands[band] += power
            expected += bands
        for actual, reference in zip(fixture["features"], expected):
            self.assertAlmostEqual(actual, reference, delta=1e-10 + abs(reference) * 1e-8)
        self.send(self.window(features=fixture["features"]))
        self.store.tick()
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"][0]["analysis"]["status"], "waiting_model")
