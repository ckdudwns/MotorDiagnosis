"""Checkpoint authority, CPU parity, durable shadow inputs and explicit unknowns."""

import copy
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from ai.ai2.model_runtime import Checkpoint, artifact_bytes
from motor_diagnosis import data
from motor_diagnosis.model_inference import ModelInferenceStore
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup

try:
    import numpy as np
    import torch
    from ai.ai1.week4.ai1.freq_baseline import train_and_evaluate as training

    TORCH = hasattr(torch, "load") and hasattr(np, "asarray")
except ImportError:
    TORCH = False

DEVICE = "DEV-01-GEN-01"


def checkpoint_bytes(kind="lstm_autoencoder"):
    torch.manual_seed(42)
    payload = {
        "model_type": kind,
        "input_dim": 3,
        "seq_len": 5 if kind == "lstm_autoencoder" else None,
        "feature_names": ["third", "first", "second"],
        "scaler_mean": [3.0, 1.0, 2.0],
        "scaler_std": [2.0, 4.0, 6.0],
        "threshold": 0.5,
        "sigma": 3.0,
        "state_dict": training._MODEL_BUILDERS[kind](3).state_dict(),
    }
    content = io.BytesIO()
    torch.save(payload, content)
    return content.getvalue()


@unittest.skipUnless(TORCH, "Optional PyTorch inference runtime is not installed")
class CheckpointTest(unittest.TestCase):
    def test_report_is_not_the_model_input_authority(self):
        content = checkpoint_bytes()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "models.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("delivery/lstm_autoencoder.pt", content)
                archive.writestr(
                    "delivery/training_job_report.json",
                    '{"featureCount":999,"threshold":0}',
                )
            model = Checkpoint.load(path)
            self.assertEqual(model.names, ("third", "first", "second"))
            self.assertEqual(model.threshold, 0.5)
            self.assertEqual(model.sequence_length, 5)
            self.assertIsNone(model.describe()["sampleRateHz"])
            self.assertFalse(model.describe()["rawAdapterAvailable"])
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("other/lstm_autoencoder.pt", content)
            with self.assertRaises(ValueError):
                artifact_bytes(path, "lstm_autoencoder")

    def test_checksum_is_checked_before_restricted_deserialization(self):
        with mock.patch("torch.load") as load:
            with self.assertRaises(ValueError):
                Checkpoint(checkpoint_bytes(), expected_checksum="sha256:" + "0" * 64)
            load.assert_not_called()

    def test_checkpoint_predictions_match_existing_ai1_function(self):
        for kind in ("dense_autoencoder", "lstm_autoencoder"):
            content = checkpoint_bytes(kind)
            model = Checkpoint(content)
            matrix = (
                [[2.0, 1.0, 3.0]]
                if kind == "dense_autoencoder"
                else [[[2.0, 1.0, 3.0]] * 5]
            )
            names = ["second", "first", "third"]
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "candidate.pt"
                path.write_bytes(content)
                expected = training.score_from_artifact(
                    str(path),
                    np.asarray(matrix),
                    input_feature_names=names,
                    expected_checksum=model.checksum,
                )
            observed = model.predict(matrix, names)
            np.testing.assert_allclose(
                observed["errors"], expected["errors"], rtol=1e-6
            )
            self.assertEqual(observed["verdict"], expected["verdict"])
            self.assertFalse(observed["affectsAlerts"])

    def test_missing_coerced_nonfinite_or_wrong_shape_features_are_rejected(self):
        model = Checkpoint(checkpoint_bytes())
        good = [[[3.0, 1.0, 2.0]] * 5]
        for bad in (None, True, "1", float("nan"), float("inf"), 1e100):
            matrix = copy.deepcopy(good)
            matrix[0][0][0] = bad
            with self.subTest(bad=bad), self.assertRaises((ValueError, OverflowError)):
                model.predict(matrix, list(model.names))
        for matrix, names in (
            (good, ["third", "first", "first"]),
            (good, ["x", "y", "z"]),
            (good[0], list(model.names)),
            ([[[3, 1, 2]] * 4], list(model.names)),
            ([], list(model.names)),
        ):
            with self.assertRaises(ValueError):
                model.predict(matrix, names)

    def test_invalid_weights_never_produce_a_normal_verdict(self):
        payload = torch.load(io.BytesIO(checkpoint_bytes()), weights_only=True)
        next(iter(payload["state_dict"].values())).flatten()[0] = float("nan")
        content = io.BytesIO()
        torch.save(payload, content)
        with self.assertRaises(ValueError):
            Checkpoint(content.getvalue())

    @unittest.skipUnless(
        os.environ.get("AI2_MODEL_BUNDLE_FIXTURE"),
        "Original delivered ZIP is optional and never committed",
    )
    def test_delivered_models_run_with_their_own_saved_input_contract(self):
        for kind in ("dense_autoencoder", "lstm_autoencoder"):
            model = Checkpoint.load(
                os.environ["AI2_MODEL_BUNDLE_FIXTURE"], candidate=kind
            )
            self.assertEqual(len(model.names), 26)
            matrix = (
                [model.mean.tolist()]
                if kind == "dense_autoencoder"
                else [[model.mean.tolist()] * 5]
            )
            result = model.predict(matrix, list(model.names))
            self.assertTrue(np.isfinite(result["errors"]).all())
            self.assertFalse(result["domainValidated"])
            self.assertFalse(result["affectsAlerts"])


@unittest.skipUnless(TORCH, "Optional PyTorch inference runtime is not installed")
class ModelInferenceTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "shadow.sqlite3"
        self.model = Checkpoint(checkpoint_bytes())
        self.store = ModelInferenceStore(self.model, self.path)
        self.base = datetime.now(timezone.utc) - timedelta(seconds=150)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()
        super().tearDown()

    def window(self, index=0, **changes):
        return {
            "schemaVersion": 1,
            "deviceId": DEVICE,
            "siteId": "SITE-01",
            "assetId": "SITE-01-GEN-01",
            "sourceId": "recording-1",
            "preprocessingId": "test-only-preprocessing",
            "sampleRateHz": 1000,
            "windowIndex": index,
            "windowStartSample": index * 1000,
            "windowEndSample": (index + 1) * 1000,
            "timestamp": data.format_rfc3339(self.base + timedelta(seconds=index)),
            "features": {"first": 1.0, "second": 2.0, "third": 3.0},
            "modelVersion": self.model.checksum,
            **changes,
        }

    def ingest_window(self, index=0, **changes):
        return self.store.ingest(self.principal, DEVICE, self.window(index, **changes))

    def drain(self):
        while self.store.tick():
            pass

    def listing(self):
        return self.store.list_device(self.admin, DEVICE)

    def test_five_windows_infer_without_touching_existing_decisions(self):
        events, telemetry, registry = (
            copy.deepcopy(data.EVENTS),
            copy.deepcopy(data.TELEMETRY_RECORDS),
            copy.deepcopy(data.MODEL_VERSIONS),
        )
        for index in range(5):
            self.assertEqual(self.ingest_window(index)[1], 202)
        self.drain()
        items = self.listing()["items"]
        self.assertEqual(
            [row["status"] for row in items], ["inferred"] + ["warming_up"] * 4
        )
        self.assertIsInstance(items[0]["verdict"], bool)
        self.assertEqual(items[0]["modelVersion"], self.model.checksum)
        self.assertEqual(
            (data.EVENTS, data.TELEMETRY_RECORDS, data.MODEL_VERSIONS),
            (events, telemetry, registry),
        )
        self.assertEqual(
            self.listing()["rawInputReason"], "PREPROCESSOR_NOT_CONFIGURED"
        )

    def test_retry_conflict_gap_and_source_change_do_not_mix_sequences(self):
        first, _ = self.ingest_window(0)
        again, status = self.ingest_window(0)
        self.assertEqual((again["inputId"], status), (first["inputId"], 200))
        with self.assertRaises(data.ApiError):
            self.ingest_window(0, features={"third": 99, "first": 1, "second": 2})
        for index in (1, 3, 4):
            self.ingest_window(index)
        with self.assertRaises(data.ApiError):
            self.ingest_window(2)
        with self.assertRaises(data.ApiError):
            self.ingest_window(5, preprocessingId="different-code")
        self.ingest_window(0, sourceId="new-recording")
        self.drain()
        gap = next(r for r in self.listing()["items"] if r["windowIndex"] == 4)
        self.assertEqual(gap["reason"], "SEQUENCE_GAP")
        self.assertIsNone(gap["verdict"])

    def test_sample_and_time_discontinuities_are_not_contiguous_lstm_inputs(self):
        for source, time_gap in (("sample-gap", False), ("time-gap", True)):
            for index in range(5):
                changes = {"sourceId": source}
                if time_gap:
                    changes["timestamp"] = data.format_rfc3339(
                        self.base + timedelta(seconds=index * 30)
                    )
                elif index == 4:
                    changes.update(windowStartSample=5000, windowEndSample=6000)
                self.ingest_window(index, **changes)
        self.drain()
        for item in self.listing()["items"]:
            if item["windowIndex"] == 4:
                self.assertEqual(item["reason"], "SEQUENCE_DISCONTINUOUS")
                self.assertIsNone(item["verdict"])

    def test_restart_retains_partial_sequence_and_committed_result(self):
        for index in range(3):
            self.ingest_window(index)
        self.drain()
        self.store.close()
        self.store = ModelInferenceStore(self.model, self.path)
        for index in (3, 4):
            self.ingest_window(index)
        self.drain()
        self.assertEqual(self.listing()["items"][0]["status"], "inferred")
        self.store.close()
        self.store = ModelInferenceStore(self.model, self.path)
        self.assertEqual(self.listing()["items"][0]["status"], "inferred")

    def test_failed_input_and_result_commit_remain_retryable(self):
        self.store.db.execute(
            "CREATE TRIGGER fail_input BEFORE INSERT ON inputs BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.ingest_window()
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM streams").fetchone()[0], 0
        )
        self.store.db.execute("DROP TRIGGER fail_input")
        self.ingest_window()
        self.store.db.execute(
            "CREATE TRIGGER fail_result BEFORE UPDATE ON inputs BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.tick()
        self.assertEqual(self.listing()["items"][0]["status"], "queued")
        self.store.db.execute("DROP TRIGGER fail_result")
        self.drain()
        self.assertEqual(self.listing()["items"][0]["status"], "warming_up")

    def test_inference_failure_is_not_a_normal_result(self):
        for index in range(5):
            self.ingest_window(index)
        with mock.patch.object(
            self.model, "predict", side_effect=ValueError("numerical failure")
        ):
            self.drain()
        item = self.listing()["items"][0]
        self.assertEqual(
            (item["status"], item["reason"], item["verdict"]),
            ("not_evaluated", "INFERENCE_FAILED", None),
        )

    def test_model_replacement_does_not_relabel_old_pending_input(self):
        self.ingest_window()
        self.store.close()
        self.store = ModelInferenceStore(
            Checkpoint(checkpoint_bytes("dense_autoencoder")), self.path
        )
        self.drain()
        self.assertEqual(self.listing()["items"][0]["reason"], "MODEL_CHANGED")

    def test_bounds_permissions_mapping_and_retention_protect_history(self):
        for changes in (
            {"features": {"first": 1}},
            {"features": {"first": None, "second": 2, "third": 3}},
            {"modelVersion": "wrong"},
            {"windowIndex": True},
            {"sampleRateHz": 0},
        ):
            with self.assertRaises(data.ApiError):
                self.ingest_window(**changes)
        self.ingest_window()
        with mock.patch("motor_diagnosis.model_inference.MAX_PENDING", 1):
            with self.assertRaises(data.ApiError):
                self.ingest_window(1)
        denied = {**self.admin, "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError):
            self.store.list_device(denied, DEVICE)
        with self.assertRaises(data.ApiError):
            self.store.check_device_deletion(DEVICE)
        with mock.patch(
            "motor_diagnosis.model_inference.time.time",
            return_value=time.time() + 8 * 86400,
        ):
            self.assertEqual(self.listing()["items"], [])
        with self.assertRaises(data.ApiError):
            self.store.check_device_deletion(DEVICE)
        with self.assertRaises(data.ApiError):
            self.ingest_window()

    def test_http_input_ack_and_scoped_dashboard_integration(self):
        with mock.patch(
            "ai.ai2.model_runtime.Checkpoint.load", return_value=self.model
        ):
            server = create_server(
                "127.0.0.1", 0, auto_alerts=False, model_artifact="explicit-test.pt"
            )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10
            )
            connection.request(
                "POST",
                f"/api/devices/{DEVICE}/model-inputs",
                json.dumps(self.window()),
                {
                    "Authorization": "Bearer demo-telemetry-ingest-token",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 202, response.read())
            connection.close()
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10
            )
            connection.request(
                "GET",
                f"/api/devices/{DEVICE}/analysis",
                headers={"Authorization": "Bearer " + self.token},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200, result)
            self.assertEqual(
                result["shadowInference"]["checkpoint"]["modelVersion"],
                self.model.checksum,
            )
            self.assertIsNone(result["shadowInference"]["items"][0]["verdict"])
            connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    @unittest.skipUnless(
        os.environ.get("AI2_MODEL_BUNDLE_FIXTURE"),
        "Original delivered ZIP is optional and never committed",
    )
    def test_original_lstm_five_windows_through_real_http_worker(self):
        # Synthetic mean features exercise the delivered model, not field accuracy.
        artifact = os.environ["AI2_MODEL_BUNDLE_FIXTURE"]
        self.model = Checkpoint.load(artifact)
        server = create_server(
            "127.0.0.1",
            0,
            auto_alerts=False,
            model_artifact=artifact,
            model_checksum=self.model.checksum,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def call(method, path, body=None, token=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10
            )
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {
                    "Authorization": "Bearer " + (token or self.token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            status = response.status
            connection.close()
            return status, result

        try:
            for index in range(5):
                payload = self.window(
                    index,
                    features=dict(zip(self.model.names, self.model.mean.tolist())),
                )
                status, result = call(
                    "POST",
                    f"/api/devices/{DEVICE}/model-inputs",
                    payload,
                    "demo-telemetry-ingest-token",
                )
                self.assertEqual(status, 202, result)
            deadline = time.monotonic() + 8
            while True:
                status, result = call("GET", f"/api/devices/{DEVICE}/model-inference")
                self.assertEqual(status, 200, result)
                final = result["items"][0]
                if final["status"] != "queued" or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            self.assertEqual(final["status"], "inferred", final)
            expected = self.model.predict(
                [[self.model.mean.tolist()] * 5], list(self.model.names)
            )
            self.assertAlmostEqual(
                final["reconstructionError"], expected["errors"][0], places=7
            )
            self.assertEqual(final["verdict"], expected["verdict"][0])
            self.assertFalse(final["affectsAlerts"])
            self.assertFalse(final["domainValidated"])
            status, denied = call(
                "GET",
                f"/api/devices/{DEVICE}/model-inference",
                token="demo-telemetry-ingest-token",
            )
            self.assertEqual(status, 401, denied)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
