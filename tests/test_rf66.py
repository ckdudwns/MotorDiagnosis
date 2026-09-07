"""RF66 safety gates and durable single-window shadow inference."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from motor_diagnosis import data, device_lifecycle, rf66
from motor_diagnosis.raw_vibration import RawVibrationStore, extract66, NAMES
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup
from tests import test_raw_vibration as raw_tests

DEVICE = raw_tests.DEVICE
HASH = "sha256:631c29cdcff2fc79843f607c38c0b37c9d1df63e36838bcd88a242942ba23c80"


class PackageTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "model.zip"
        self.content = b"not a pickle; metadata tests must never deserialize"
        self.checksum = "sha256:" + hashlib.sha256(self.content).hexdigest()
        self.contract = {"featureNames": list(NAMES)}
        self.spec = {"modelKind": "random_forest", "modelInputShape": [1, 66],
                     "sequenceLength": 1, "rawShape": [512, 3], "axes": ["X", "Y", "Z"],
                     "unit": "g", "sampleRateHz": 800, "windowHopSamples": 512,
                     "numericPolicy": rf66.NUMERIC_POLICY, "sklearnVersion": "1.9.0",
                     "fieldValidated": False, "affectsAlerts": False, "threshold": .7,
                     "contract": self.contract}
        patch = mock.patch.object(rf66, "CONTRACT_SHA256", rf66._contract_digest(self.contract))
        patch.start()
        self.addCleanup(patch.stop)
        self.rule = {"classes": [False, True], "comparison": ">", "threshold": .7,
                     "modelSequenceLength": 1, "confirmation": dict(rf66.CONFIRMATION_RULE)}

    def write_package(self, *, corrupt=False):
        files = {"model/candidate.joblib": self.content,
                 "input-contract.json": json.dumps(self.spec).encode(),
                 "decision-rule.json": json.dumps(self.rule).encode(),
                 "environment.json": json.dumps({"packages": rf66.PACKAGES}).encode()}
        manifest = {"modelVersion": self.checksum,
                    "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
        if corrupt:
            files["model/candidate.joblib"] += b"tampered"
        with zipfile.ZipFile(self.path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr("MANIFEST.json", json.dumps(manifest))

    def test_trusted_hash_required_before_opening_or_deserializing(self):
        for checksum in (None, "", self.checksum[7:], "sha256:" + "z" * 64):
            with self.subTest(checksum=checksum), self.assertRaises(ValueError):
                rf66.RF66Model.load(self.path, expected_checksum=checksum)

    def test_manifest_and_independent_checksum(self):
        self.write_package()
        self.assertEqual(rf66._read_package(self.path, self.checksum)[0], self.content)
        with self.assertRaisesRegex(ValueError, "trusted model checksum"):
            rf66.RF66Model.load(self.path, expected_checksum="sha256:" + "a" * 64)
        self.write_package(corrupt=True)
        with self.assertRaisesRegex(ValueError, "manifest checksum"):
            rf66.RF66Model.load(self.path, expected_checksum=self.checksum)

    def test_changed_contract_order_and_rule_fail_closed(self):
        mutations = [(self.spec, "sampleRateHz", 12000), (self.spec, "sequenceLength", 5),
                     (self.spec, "modelKind", "dense_autoencoder"),
                     (self.spec, "threshold", float("nan")), (self.rule, "comparison", ">="),
                     (self.rule, "classes", [0, 1]), (self.rule, "threshold", .5),
                     (self.rule, "confirmation", {**rf66.CONFIRMATION_RULE, "requiredHits": 2}),
                     (self.contract, "featureNames", list(reversed(NAMES)))]
        for target, key, value in mutations:
            old = target[key]
            target[key] = value
            self.write_package()
            with self.subTest(key=key), self.assertRaises(ValueError):
                rf66.RF66Model.load(self.path, expected_checksum=self.checksum)
            target[key] = old

    def test_versions_checked_before_joblib(self):
        self.write_package()
        with mock.patch.object(rf66, "version", return_value="unsupported"):
            with self.assertRaisesRegex(ValueError, "runtime version mismatch"):
                rf66.RF66Model.load(self.path, expected_checksum=self.checksum)

    def test_duplicate_and_oversized_archive(self):
        self.write_package()
        with mock.patch.object(rf66, "MAX_ARCHIVE_BYTES", 10):
            with self.assertRaises(ValueError):
                rf66._read_package(self.path, self.checksum)
        with zipfile.ZipFile(self.path, "a") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("model/candidate.joblib", self.content)
        with self.assertRaises(ValueError):
            rf66._read_package(self.path, self.checksum)


class FakeRF(rf66.RF66Model):
    def __init__(self, score=.8, checksum="sha256:" + "a" * 64):
        super().__init__(None, checksum, .7)
        self.score, self.calls = score, []

    def predict_window(self, values):
        self.calls.append(values)
        return {"score": self.score, "threshold": self.threshold,
                "verdict": self.score > self.threshold}


class StoreTest(RpmSetup):
    window = raw_tests.RawWindowTest.window
    send = raw_tests.RawWindowTest.send

    def setUp(self):
        super().setUp()
        self.model = FakeRF()
        self.store = RawVibrationStore(model=self.model)
        self.addCleanup(self.store.close)

    def analysis(self, store=None):
        return (store or self.store).list_device(self.admin, DEVICE)["items"][0]["analysis"]

    def test_every_window_persisted_and_duplicate_not_reinferred(self):
        windows = [self.window(i) for i in range(4)]
        self.send(*windows)
        for _ in windows:
            self.assertTrue(self.store.tick())
        self.assertFalse(self.store.tick())
        self.assertEqual(self.send(*windows)[1], 200)
        self.assertFalse(self.store.tick())
        self.assertEqual(len(self.model.calls), 4)
        for item in self.store.list_device(self.admin, DEVICE)["items"]:
            result = item["analysis"]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["modelVersion"], self.model.checksum)
            self.assertEqual(result["score"], .8)
            self.assertTrue(result["verdict"])
            self.assertFalse(result["affectsAlerts"])
            self.assertTrue(result["confirmationApplied"])
            self.assertNotIn("error", result)
            self.assertEqual(len(result["modelInput"]), 66)

    def test_strict_threshold(self):
        self.model.score = self.model.threshold
        self.send(self.window())
        self.store.tick()
        self.assertFalse(self.analysis()["verdict"])

    def test_gap_is_recorded_but_valid_current_window_is_independent(self):
        self.send(self.window(), self.window(3))
        self.store.tick()
        self.store.tick()
        item = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual(item["missingWindowsBefore"], 2)
        self.assertEqual(item["analysis"]["status"], "completed")
        self.assertEqual(len(self.model.calls), 2)

    def test_result_write_failure_retains_one_retryable_row(self):
        self.send(self.window())
        self.store.db.execute("""CREATE TEMP TRIGGER fail_result BEFORE UPDATE OF result
            ON vibration_windows BEGIN SELECT RAISE(ABORT, 'test disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.tick()
        self.assertEqual(self.analysis()["status"], "queued")
        self.store.db.execute("DROP TRIGGER fail_result")
        self.store.tick()
        self.assertEqual(self.analysis()["status"], "completed")
        self.assertEqual(len(self.store.list_device(self.admin, DEVICE)["items"]), 1)
        self.assertEqual(len(self.model.calls), 2)

    def test_queued_work_survives_restart_with_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.db"
            store = RawVibrationStore(path)
            try:
                self.send(self.window(), store=store)
            finally:
                store.close()
            store = RawVibrationStore(path, model=self.model)
            try:
                self.assertTrue(store.tick())
                self.assertEqual(self.analysis(store)["status"], "completed")
                self.assertEqual(self.analysis(store)["modelVersion"], self.model.checksum)
            finally:
                store.close()

    def test_bad_quality_clipping_constant_and_failure_are_not_normal(self):
        self.send(self.window(quality="sensor_unavailable", sampleCount=0, samples=""),
                  self.window(1, samples=raw_tests.encoded([(4095, 0, 0)] * 512)),
                  self.window(2, samples=raw_tests.encoded([(1, 2, 3)] * 512)))
        for _ in range(3):
            self.store.tick()
        self.assertEqual(self.model.calls, [])
        self.send(self.window(3))
        with mock.patch.object(self.model, "predict_window", side_effect=RuntimeError("test failure")):
            self.store.tick()
        for item in self.store.list_device(self.admin, DEVICE)["items"]:
            result = item["analysis"]
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["verdict"])
            self.assertIsNone(result["score"])
            self.assertEqual(result["modelVersion"], self.model.checksum)

    def test_invalid_prediction_not_saved_as_completed(self):
        for index, score in enumerate((float("nan"), float("inf"), -1, 1.1, True)):
            self.model.score = score
            self.send(self.window(index))
            self.store.tick()
            self.assertEqual(self.analysis()["status"], "unavailable")
            self.assertIsNone(self.analysis()["score"])

    def test_restart_disable_replace_preserves_results_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.db"
            store = RawVibrationStore(path, model=self.model)
            try:
                self.send(self.window(), self.window(1), store=store)
                store.tick()  # second remains queued across restart
            finally:
                store.close()
            store = RawVibrationStore(path)
            try:
                with self.assertRaises(data.ApiError):
                    device_lifecycle.check_history(DEVICE)
                store.tick()
                self.assertEqual(self.analysis(store)["status"], "waiting_model")
            finally:
                store.close()
            replacement = FakeRF(checksum="sha256:" + "b" * 64)
            store = RawVibrationStore(path, model=replacement)
            try:
                self.assertFalse(store.tick())  # no implicit backfill of final states
                self.send(self.window(2), store=store)
                store.tick()
                rows = store.list_device(self.admin, DEVICE)["items"]
                self.assertEqual(rows[0]["analysis"]["modelVersion"], replacement.checksum)
                self.assertEqual(rows[1]["analysis"]["status"], "waiting_model")
                self.assertEqual(rows[2]["analysis"]["modelVersion"], self.model.checksum)
                with self.assertRaises(data.ApiError):
                    device_lifecycle.check_history(DEVICE)
            finally:
                store.close()

    def test_server_configuration_failure_and_http_result(self):
        with self.assertRaises(ValueError):
            create_server("127.0.0.1", 0, rf66_checksum=HASH)
        with mock.patch.object(rf66.RF66Model, "load", side_effect=ValueError("bad artifact")):
            with self.assertRaises(ValueError):
                create_server("127.0.0.1", 0, rf66_artifact="test", rf66_checksum=HASH)
        # Failure cleanup permits a subsequent normal start.
        with mock.patch.object(rf66.RF66Model, "load", return_value=self.model) as loader:
            server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False,
                                   rf66_artifact="test", rf66_checksum=HASH)
        loader.assert_called_once_with("test", expected_checksum=HASH)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # create_server resets master state; acquire its session and principal.
            token = data.authenticate({"username": "admin", "password": "admin123"})["session"]["token"]
            path = f"/api/devices/{DEVICE}/raw-vibration-windows"
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                connection.request("POST", path, json.dumps({"windows": [self.window()]}),
                                   {"Authorization": "Bearer demo-telemetry-ingest-token",
                                    "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202, response.read())
                server.raw_vibration.tick()  # serialized with background worker
                connection.request("GET", path, headers={"Authorization": "Bearer " + token})
                response = connection.getresponse()
                body = json.load(response)
                self.assertEqual(response.status, 200)
                self.assertEqual(body["items"][0]["analysis"]["score"], .8)
                self.assertEqual(body["configuredModel"]["modelVersion"], self.model.checksum)
                self.assertEqual(body["eventPolicy"]["mode"], "shadow")
                resolve_path = "/api/events/RF66-MISSING/rf66-resolve"
                connection.request("POST", resolve_path, json.dumps({"reason": "inspected"}),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 401, response.read())
                connection.request("POST", resolve_path, json.dumps({"reason": "inspected"}),
                                   {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse()
                body = json.load(response)
                self.assertEqual(response.status, 404)
                self.assertEqual(body["error"]["code"], "RF66_EVENT_NOT_FOUND")
                self.assertIsNone(server.model_inference)
                self.assertIsNone(server.vibration_windows.checkpoint)
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(os.environ.get("RF66_TEST_ARTIFACT"), "Trusted RF66 handoff not configured")
class HandoffTest(RpmSetup):
    window = raw_tests.RawWindowTest.window

    def setUp(self):
        super().setUp()
        self.path = os.environ["RF66_TEST_ARTIFACT"]
        self.model = rf66.RF66Model.load(self.path, expected_checksum=HASH)

    def read(self, name):
        with zipfile.ZipFile(self.path) as archive:
            return json.loads(archive.read(name))

    def test_packaged_scores_and_known_false_negative(self):
        for name in ("normal", "fault"):
            sample = self.read("examples/" + name + ".json")
            for window in sample["windows"]:
                result = self.model.predict_window(window["expectedFeatures66"])
                self.assertAlmostEqual(result["score"], window["expectedScore"], places=12)
                self.assertEqual(result["verdict"], window["expectedSingleAnomaly"])
                # The supplied fault examples really are missed; do not relabel them.
                if name == "fault":
                    self.assertFalse(result["verdict"])

    def test_real_predict_shape_finiteness_and_strict_threshold(self):
        import numpy as np

        for values in ([0.] * 21, [[0.] * 66], [float("nan")] * 66):
            with self.assertRaises(ValueError):
                self.model.predict_window(values)
        threshold = self.model.threshold
        with mock.patch.object(self.model.model, "predict_proba",
                               return_value=np.array([[1-threshold, threshold]])):
            self.assertFalse(self.model.predict_window([0.] * 66)["verdict"])
        for probabilities in ([[float("nan"), .5]], [[.5]], [[.9, .9]], [[-1., 2.]]):
            with mock.patch.object(self.model.model, "predict_proba", return_value=np.array(probabilities)):
                with self.assertRaises(ValueError):
                    self.model.predict_window([0.] * 66)

    def test_payload_metadata_estimator_shape_and_class_order_rejected(self):
        import io
        import joblib
        import numpy as np

        # Only the independently checked bytes are deserialized here as well.
        content, spec = rf66._read_package(self.path, HASH)
        payload = joblib.load(io.BytesIO(content))
        altered = dict(payload, threshold=.123)
        with mock.patch.object(joblib, "load", return_value=altered):
            with self.assertRaisesRegex(ValueError, "payload/contract mismatch"):
                rf66.RF66Model.load(self.path, expected_checksum=HASH)
        for attribute, value in (("n_features_in_", 21), ("classes_", np.array([True, False])),
                                 ("classes_", np.array([0, 1])), ("n_outputs_", 2)):
            original = getattr(payload["model"], attribute)
            setattr(payload["model"], attribute, value)
            with mock.patch.object(joblib, "load", return_value=payload):
                with self.assertRaisesRegex(ValueError, "binary RandomForestClassifier"):
                    rf66.RF66Model.load(self.path, expected_checksum=HASH)
            setattr(payload["model"], attribute, original)

    def test_raw_golden_features_inference_and_storage(self):
        golden = self.read("examples/quantization-golden.json")
        values = extract66(golden["stressCounts"])
        for actual, expected in zip(values, golden["expectedQuantized66"]):
            self.assertAlmostEqual(actual, expected, delta=1e-12 + abs(expected)*1e-12)
        store = RawVibrationStore(model=self.model)
        try:
            window = self.window(samples=raw_tests.encoded(golden["stressCounts"]))
            store.ingest(self.principal, DEVICE, {"windows": [window]})
            store.tick()
            result = store.list_device(self.admin, DEVICE)["items"][0]["analysis"]
            self.assertEqual(result["status"], "completed")
            self.assertAlmostEqual(result["score"], golden["expectedScoresCleanQuantized"][1], places=12)
            self.assertEqual(result["verdict"], golden["expectedAnomalyCleanQuantized"][1])
            self.assertEqual(result["modelVersion"], HASH)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
