"""Raw conversion parity and durable HTTP shadow pipeline; not field accuracy."""

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ai.ai2.raw_preprocessing import (
    RawPreprocessor,
    RawInputError,
    NAMES,
    BANDS,
    canonical,
)
from ai.ai2.model_runtime import Checkpoint, contiguous_matrix
from motor_diagnosis import data
from motor_diagnosis.model_inference import ModelInferenceStore
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup
from tests.test_model_inference import DEVICE, TORCH

CAPTURE_BASE = datetime.now(timezone.utc) - timedelta(seconds=120)

try:
    import numpy as np
    import torch
    import librosa
    from ai.ai1.week2.ai1.feature_extraction import extract_features as extraction
    from ai.ai1.week4.ai1.freq_baseline import train_and_evaluate as training

    AVAILABLE = TORCH and hasattr(librosa, "feature")
except ImportError:
    AVAILABLE = False


def model_bytes(kind="lstm_autoencoder"):
    torch.manual_seed(13)
    payload = {
        "model_type": kind,
        "input_dim": 26,
        "seq_len": 5 if kind == "lstm_autoencoder" else None,
        "feature_names": list(
            reversed(NAMES)
        ),  # Must follow the checkpoint, not sorted output.
        "scaler_mean": [0.0] * 26,
        "scaler_std": [1.0] * 26,
        "threshold": 1.0,
        "sigma": 3.0,
        "state_dict": training._MODEL_BUILDERS[kind](26).state_dict(),
    }
    handle = io.BytesIO()
    torch.save(payload, handle)
    return handle.getvalue()


def profile_for(model, **changes):
    # Explicit synthetic test settings, NOT recovered training settings.
    return {
        "schemaVersion": 1,
        "extractor": "ai1-week2-26-v1",
        "modelVersion": model.checksum,
        "sampleRateHz": 12000,
        "windowSamples": 2048,
        "frameLength": 2048,
        "hopLength": 512,
        "nMfcc": 13,
        "bandEdgesHz": BANDS.copy(),
        "signalType": "vibration",
        "channel": "vibrationX",
        "unit": "g",
        **changes,
    }


def raw_window(adapter, index=0, **changes):
    config = adapter.profile
    count, rate = config["windowSamples"], config["sampleRateHz"]
    start = index * count
    samples = (
        0.3 * np.sin(2 * np.pi * 130 * np.arange(start, start + count) / rate)
        + 0.01 * np.cos(2 * np.pi * 3100 * np.arange(start, start + count) / rate)
    ).astype("<f4")
    base = CAPTURE_BASE
    return {
        "schemaVersion": 1,
        "modelVersion": adapter.checkpoint.checksum,
        "preprocessingId": adapter.identity,
        "deviceId": DEVICE,
        "siteId": "SITE-01",
        "assetId": "SITE-01-GEN-01",
        "sourceId": "synthetic-recording-1",
        "signalType": "vibration",
        "channel": config["channel"],
        "unit": config["unit"],
        "sampleRateHz": rate,
        "windowIndex": index,
        "windowStartSample": start,
        "windowEndSample": start + count,
        "quality": "valid",
        "timestamp": (base + timedelta(seconds=start / rate))
        .isoformat()
        .replace("+00:00", "Z"),
        "samplesFloat32LE": base64.b64encode(samples.tobytes()).decode(),
        **changes,
    }


@unittest.skipUnless(AVAILABLE, "Optional raw feature runtime is unavailable")
class RawPreprocessingTest(unittest.TestCase):
    def setUp(self):
        self.model = Checkpoint(model_bytes())
        self.adapter = RawPreprocessor(self.model, profile_for(self.model))

    def test_raw_features_equal_existing_ai1_extractor_and_checkpoint_order(self):
        payload = raw_window(self.adapter)
        signal = np.frombuffer(
            base64.b64decode(payload["samplesFloat32LE"]), dtype="<f4"
        ).astype(np.float64)
        expected = extraction.extract_all_features(
            signal,
            extraction.FeatureConfig(
                sample_rate=12000,
                frame_length=2048,
                hop_length=512,
                n_mfcc=13,
                band_edges=tuple(BANDS),
            ),
        )
        with mock.patch(
            "librosa.resample", side_effect=AssertionError("No resampling")
        ):
            prepared = self.adapter.transform(payload)
        self.assertEqual(list(prepared["features"]), list(self.model.names))
        np.testing.assert_array_equal(
            list(prepared["features"].values()), [expected[k] for k in self.model.names]
        )
        self.assertEqual(len(prepared["features"]), 26)
        self.assertNotIn("vibration_peak_hz", prepared["features"])
        self.assertFalse(self.adapter.describe()["domainValidated"])

    def test_profiles_are_explicit_model_bound_and_do_not_admit_empty_bands(self):
        for changes in (
            {"sampleRateHz": 800},
            {"sampleRateHz": True},
            {"windowSamples": 128},
            {"frameLength": 257},
            {"nMfcc": 0},
            {"bandEdgesHz": [0, 400]},
            {"modelVersion": "sha256:wrong"},
            {"signalType": "acoustic"},
            {"sampleRateHz": 192000, "windowSamples": 64, "frameLength": 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RawPreprocessor(self.model, profile_for(self.model, **changes))
        profile = profile_for(self.model)
        del profile["unit"]
        with self.assertRaises(ValueError):
            RawPreprocessor(self.model, profile)

    def test_profile_is_immutable_and_changed_settings_change_identity(self):
        before = self.adapter.identity
        leaked = self.adapter.profile
        leaked["sampleRateHz"] = 800
        self.assertEqual(self.adapter.profile["sampleRateHz"], 12000)
        changed = RawPreprocessor(self.model, profile_for(self.model, unit="m/s^2"))
        self.assertNotEqual(changed.identity, before)
        with self.assertRaises(RawInputError) as error:
            changed.transform(raw_window(self.adapter))
        self.assertEqual(error.exception.code, "PREPROCESSOR_CHANGED")

    def test_profile_work_budget_and_strict_capture_timestamp(self):
        with self.assertRaises(ValueError):
            RawPreprocessor(
                self.model,
                profile_for(
                    self.model, windowSamples=65536, frameLength=8192, hopLength=1
                ),
            )
        for stamp in (
            "20260907T100000Z",
            "2026-W37-1T10:00:00Z",
            "2026-09-07T10:00:00,123Z",
            "2026-02-30T10:00:00Z",
            "2026-09-07T10:00:00",
        ):
            with self.subTest(stamp=stamp), self.assertRaises(RawInputError):
                self.adapter.transform(raw_window(self.adapter, timestamp=stamp))

    def test_bad_quality_units_rate_channel_length_encoding_and_samples_fail(self):
        good = raw_window(self.adapter)
        for changes in (
            {"sampleRateHz": 800},
            {"channel": "vibrationY"},
            {"signalType": "acoustic"},
            {"unit": "pcm24"},
            {"quality": "unknown"},
            {"quality": "invalid"},
            {"windowEndSample": 2047},
            {"windowIndex": True},
            {"samplesFloat32LE": "bad"},
            {"samplesFloat32LE": None},
            {"windowStartSample": -1},
            {"sourceId": " "},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.adapter.transform({**good, **changes})
        for bad in (float("nan"), float("inf"), 1e20):
            samples = np.zeros(2048, dtype="<f4")
            samples[0] = bad
            with self.assertRaises(RawInputError):
                self.adapter.transform(
                    {
                        **good,
                        "samplesFloat32LE": base64.b64encode(
                            samples.tobytes()
                        ).decode(),
                    }
                )

    def test_missing_mfcc_dependency_never_fills_zeros(self):
        with mock.patch.object(extraction, "_HAS_LIBROSA", False):
            with self.assertRaises(ImportError):
                RawPreprocessor(self.model, profile_for(self.model))
            with self.assertRaises(ImportError):
                self.adapter.transform(raw_window(self.adapter))

    def test_download_integrity_checked_and_current_800hz_capture_rejected(self):
        from tests.test_edge_analysis import frame

        original = frame(request_id="a" * 32)
        envelope = {
            "id": "frame-1",
            "frame": original,
            "sha256": hashlib.sha256(canonical(original).encode()).hexdigest(),
        }
        with self.assertRaises(RawInputError) as error:
            self.adapter.capture_window(envelope, quality="valid")
        self.assertEqual(error.exception.code, "RAW_PROFILE_MISMATCH")
        envelope["frame"]["sequence"] += 1
        with self.assertRaises(RawInputError) as error:
            self.adapter.capture_window(envelope, quality="valid")
        self.assertEqual(error.exception.code, "CAPTURE_CHECKSUM_MISMATCH")

    def test_dense_and_lstm_raw_results_equal_direct_ai1_model_execution(self):
        for kind in ("dense_autoencoder", "lstm_autoencoder"):
            content = model_bytes(kind)
            model = Checkpoint(content)
            adapter = RawPreprocessor(model, profile_for(model))
            windows = [
                adapter.transform(raw_window(adapter, i))
                for i in range(model.sequence_length)
            ]
            matrix = contiguous_matrix(windows, model)
            with tempfile.TemporaryDirectory() as folder:
                artifact = Path(folder) / "model.pt"
                artifact.write_bytes(content)
                expected = training.score_from_artifact(
                    str(artifact),
                    np.asarray(matrix),
                    input_feature_names=list(model.names),
                    expected_checksum=model.checksum,
                )
            observed = model.predict(matrix, list(model.names))
            np.testing.assert_allclose(
                observed["errors"], expected["errors"], rtol=1e-6
            )
            self.assertEqual(observed["verdict"], expected["verdict"])

    def test_cli_produces_prepared_windows_and_refuses_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = model_bytes()
            model = Checkpoint(content)
            adapter = RawPreprocessor(model, profile_for(model))
            (root / "model.pt").write_bytes(content)
            (root / "profile.json").write_text(
                json.dumps(profile_for(model)), encoding="utf-8"
            )
            (root / "input.json").write_text(
                json.dumps({"windows": [raw_window(adapter, i) for i in range(5)]}),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "-m",
                "ai.ai2.raw_preprocessing",
                "--artifact",
                str(root / "model.pt"),
                "--profile",
                str(root / "profile.json"),
                "--input",
                str(root / "input.json"),
                "--output",
                str(root / "out.json"),
            ]
            run = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads((root / "out.json").read_text())
            self.assertEqual(len(result["windows"]), 5)
            self.assertFalse(result["result"]["affectsAlerts"])
            before = (root / "out.json").read_bytes()
            run = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(before, (root / "out.json").read_bytes())

    @unittest.skipUnless(
        os.environ.get("AI2_MODEL_BUNDLE_FIXTURE"),
        "Original delivered models are not committed",
    )
    def test_actual_delivered_dense_and_lstm_accept_extracted_26_features(self):
        for kind in ("dense_autoencoder", "lstm_autoencoder"):
            model = Checkpoint.load(
                os.environ["AI2_MODEL_BUNDLE_FIXTURE"], candidate=kind
            )
            adapter = RawPreprocessor(model, profile_for(model))
            windows = [
                adapter.transform(raw_window(adapter, i))
                for i in range(model.sequence_length)
            ]
            result = model.predict(contiguous_matrix(windows, model), list(model.names))
            self.assertEqual(len(result["errors"]), 1)
            self.assertTrue(np.isfinite(result["errors"]).all())
            self.assertFalse(result["domainValidated"])


@unittest.skipUnless(AVAILABLE, "Optional raw feature runtime is unavailable")
class RawModelStoreTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "raw.sqlite3"
        self.model = Checkpoint(model_bytes())
        self.adapter = RawPreprocessor(self.model, profile_for(self.model))
        self.store = ModelInferenceStore(
            self.model, self.path, preprocessor=self.adapter
        )
        self.base = datetime.now(timezone.utc) - timedelta(seconds=120)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()
        super().tearDown()

    def window(self, i=0, **changes):
        period = (
            self.adapter.profile["windowSamples"] / self.adapter.profile["sampleRateHz"]
        )
        return raw_window(
            self.adapter,
            i,
            timestamp=(self.base + timedelta(seconds=i * period))
            .isoformat()
            .replace("+00:00", "Z"),
            **changes,
        )

    def ingest(self, payload):
        return self.store.ingest(self.principal, DEVICE, payload, raw=True)

    def drain(self):
        while self.store.tick():
            pass

    def items(self):
        return self.store.list_device(self.admin, DEVICE)["items"]

    def test_accept_is_durable_and_feature_work_runs_off_ingest_and_db_lock(self):
        old = copy.deepcopy((data.EVENTS, data.TELEMETRY_RECORDS, data.MODEL_VERSIONS))
        with mock.patch.object(
            self.adapter, "transform", side_effect=AssertionError("Not in ingest")
        ):
            for i in range(5):
                self.assertEqual(self.ingest(self.window(i))[1], 202)
        fn = self.adapter.transform

        def convert(payload):
            outcome = []
            reader = threading.Thread(
                target=lambda: outcome.append(
                    self.store.list_device(self.admin, DEVICE)
                )
            )
            reader.start()
            reader.join(3)
            self.assertFalse(
                reader.is_alive(), "Extraction holds the database or global store lock"
            )
            return fn(payload)

        with mock.patch.object(self.adapter, "transform", side_effect=convert):
            self.drain()
        self.assertEqual(self.items()[0]["status"], "inferred")
        self.assertEqual(
            self.items()[0]["preprocessing"]["preprocessingId"], self.adapter.identity
        )
        self.assertEqual(
            old, (data.EVENTS, data.TELEMETRY_RECORDS, data.MODEL_VERSIONS)
        )

    def test_partial_raw_sequence_and_result_survive_restart(self):
        for i in range(3):
            self.ingest(self.window(i))
        self.drain()
        self.store.close()
        self.store = ModelInferenceStore(
            self.model, self.path, preprocessor=self.adapter
        )
        for i in (3, 4):
            self.ingest(self.window(i))
        self.drain()
        result = self.items()[0]
        self.assertEqual(result["status"], "inferred")
        self.store.close()
        self.store = ModelInferenceStore(
            self.model, self.path, preprocessor=self.adapter
        )
        self.assertEqual(self.items()[0], result)

    def test_duplicates_conflicts_and_raw_prepared_mixing(self):
        raw = self.window()
        first, _ = self.ingest(raw)
        duplicate, status = self.ingest(copy.deepcopy(raw))
        self.assertEqual((duplicate["inputId"], status), (first["inputId"], 200))
        other = copy.deepcopy(raw)
        other["samplesFloat32LE"] = base64.b64encode(
            np.zeros(2048, dtype="<f4").tobytes()
        ).decode()
        with self.assertRaises(data.ApiError) as error:
            self.ingest(other)
        self.assertEqual(error.exception.code, "MODEL_INPUT_CONFLICT")
        with self.assertRaises(data.ApiError) as error:
            self.store.ingest(
                self.principal, DEVICE, self.adapter.transform(self.window(1))
            )
        self.assertEqual(error.exception.code, "MODEL_SEQUENCE_CONFLICT")

    def test_raw_gap_and_small_time_discontinuity_never_produce_verdict(self):
        for i in (0, 1, 3, 4):
            self.ingest(self.window(i, sourceId="gap"))
        for i in range(5):
            window = self.window(i, sourceId="time-gap")
            if i == 4:
                window["timestamp"] = (
                    data.parse_rfc3339("timestamp", window["timestamp"])
                    + timedelta(milliseconds=50)
                ).isoformat()
            self.ingest(window)
        self.drain()
        finals = [r for r in self.items() if r["windowIndex"] == 4]
        self.assertEqual(
            {r["reason"] for r in finals}, {"SEQUENCE_GAP", "SEQUENCE_DISCONTINUOUS"}
        )
        self.assertTrue(all(r["verdict"] is None for r in finals))

    def test_profile_change_or_disable_does_not_reprocess_old_raw_with_new_settings(
        self,
    ):
        self.ingest(self.window())
        changed = RawPreprocessor(self.model, profile_for(self.model, hopLength=64))
        self.store.close()
        self.store = ModelInferenceStore(self.model, self.path, preprocessor=changed)
        self.drain()
        self.assertEqual(self.items()[0]["reason"], "PREPROCESSOR_CHANGED")
        self.assertIsNone(self.items()[0]["verdict"])
        self.store.preprocessor = self.adapter
        self.ingest(self.window(0, sourceId="disabled"))
        self.store.preprocessor = None
        self.drain()
        self.assertEqual(self.items()[0]["reason"], "PREPROCESSOR_NOT_CONFIGURED")

    def test_failed_extraction_and_commit_are_not_normal_or_lost(self):
        self.store.db.execute(
            "CREATE TRIGGER fail_input BEFORE INSERT ON inputs BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.ingest(self.window())
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM streams").fetchone()[0], 0
        )
        self.store.db.execute("DROP TRIGGER fail_input")
        for i in range(5):
            self.ingest(self.window(i))
        with mock.patch.object(
            self.adapter, "transform", side_effect=RuntimeError("MFCC failed")
        ):
            self.store.tick()
        first = next(r for r in self.items() if r["windowIndex"] == 0)
        self.assertEqual(first["reason"], "PREPROCESSING_FAILED")
        self.assertIsNone(first["verdict"])
        self.store.db.execute(
            "CREATE TRIGGER fail_result BEFORE UPDATE ON inputs BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.tick()
        row = self.store.db.execute(
            "SELECT status,prepared FROM inputs WHERE idx=1"
        ).fetchone()
        self.assertEqual(tuple(row), ("queued", None))
        self.store.db.execute("DROP TRIGGER fail_result")
        self.drain()
        self.assertEqual(self.items()[0]["reason"], "SEQUENCE_DISCONTINUOUS")

    def test_mapping_permission_capacity_and_retention_guards(self):
        with self.assertRaises(data.ApiError):
            self.ingest(self.window(assetId="SITE-01-MOT-01"))
        with self.assertRaises(data.ApiError):
            self.store.ingest({"kind": "invalid"}, DEVICE, self.window(), raw=True)
        with mock.patch("motor_diagnosis.model_inference.MAX_RAW_RECORDS", 1):
            self.ingest(self.window())
            with self.assertRaises(data.ApiError) as error:
                self.ingest(self.window(1))
            self.assertEqual(error.exception.status, 503)
            self.assertEqual(self.ingest(self.window())[1], 200)
        with self.assertRaises(data.ApiError):
            data.delete_device(self.admin, DEVICE)
        with mock.patch(
            "motor_diagnosis.model_inference.time.time",
            return_value=time.time() + 8 * 86400,
        ):
            self.store.prune()
        self.assertEqual(self.items(), [])
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM identities").fetchone()[0], 1
        )

    def test_additive_migration_keeps_old_prepared_input_unchanged(self):
        path = Path(self.folder.name) / "old.sqlite3"
        body = canonical(self.adapter.transform(self.window()))
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE inputs(
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                device TEXT NOT NULL,site TEXT NOT NULL,asset TEXT NOT NULL,
                model TEXT NOT NULL,source TEXT NOT NULL,idx INTEGER NOT NULL,
                captured REAL NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL,
                status TEXT NOT NULL,result TEXT,UNIQUE(device,model,source,idx))""")
            db.execute(
                "INSERT INTO inputs(id,device,site,asset,model,source,idx,captured,digest,body,status) VALUES(?,?,?,?,?,?,?,?,?,?,'queued')",
                (
                    "old",
                    DEVICE,
                    "SITE-01",
                    "SITE-01-GEN-01",
                    self.model.checksum,
                    "synthetic-recording-1",
                    0,
                    self.base.timestamp(),
                    hashlib.sha256(body.encode()).hexdigest(),
                    body,
                ),
            )
        db.close()
        store = ModelInferenceStore(self.model, path, preprocessor=self.adapter)
        try:
            row = store.db.execute(
                "SELECT body,status,input_kind,prepared FROM inputs"
            ).fetchone()
            self.assertEqual(tuple(row), (body, "queued", "prepared", None))
            store.tick()
            self.assertEqual(
                store.list_device(self.admin, DEVICE)["items"][0]["status"],
                "warming_up",
            )
        finally:
            store.close()

    def test_disabled_raw_ingest_and_profile_without_model_fail_explicitly(self):
        self.store.preprocessor = None
        with self.assertRaises(data.ApiError) as error:
            self.ingest(self.window())
        self.assertEqual(error.exception.code, "PREPROCESSOR_NOT_CONFIGURED")
        with self.assertRaises(ValueError):
            create_server(
                "127.0.0.1",
                0,
                auto_alerts=False,
                model_preprocessing_profile="unused.json",
            )

    def test_pending_raw_input_is_not_scored_with_a_replaced_checkpoint(self):
        self.ingest(self.window())
        other = Checkpoint(model_bytes("dense_autoencoder"))
        self.store.close()
        self.store = ModelInferenceStore(
            other, self.path, preprocessor=RawPreprocessor(other, profile_for(other))
        )
        self.drain()
        self.assertEqual(self.items()[0]["reason"], "MODEL_CHANGED")
        self.assertIsNone(self.items()[0]["verdict"])

    @unittest.skipUnless(
        os.environ.get("AI2_MODEL_BUNDLE_FIXTURE"),
        "Original delivered model is not committed",
    )
    def test_original_lstm_raw_http_worker_and_read_auth(self):
        self.model = Checkpoint.load(os.environ["AI2_MODEL_BUNDLE_FIXTURE"])
        self.adapter = RawPreprocessor(
            self.model, profile_for(self.model, windowSamples=16384)
        )
        path = Path(self.folder.name) / "profile.json"
        path.write_text(json.dumps(self.adapter.profile), encoding="utf-8")
        server = create_server(
            "127.0.0.1",
            0,
            auto_alerts=False,
            model_artifact=os.environ["AI2_MODEL_BUNDLE_FIXTURE"],
            model_checksum=self.model.checksum,
            model_preprocessing_profile=path,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def call(method, suffix, body=None, token=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10
            )
            connection.request(
                method,
                f"/api/devices/{DEVICE}/{suffix}",
                json.dumps(body) if body is not None else None,
                {
                    "Authorization": "Bearer " + (token or self.token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            result, status = json.loads(response.read()), response.status
            connection.close()
            return status, result

        try:
            raw = [self.window(i) for i in range(5)]
            for window in raw:
                status, result = call(
                    "POST", "model-raw-inputs", window, "demo-telemetry-ingest-token"
                )
                self.assertEqual(status, 202, result)
            deadline = time.monotonic() + 15
            while True:
                status, result = call("GET", "model-inference")
                self.assertEqual(status, 200, result)
                if (
                    result["items"][0]["status"] != "queued"
                    or time.monotonic() > deadline
                ):
                    break
                time.sleep(0.02)
            final = result["items"][0]
            self.assertEqual(final["status"], "inferred", final)
            expected = self.model.predict(
                contiguous_matrix([self.adapter.transform(w) for w in raw], self.model),
                list(self.model.names),
            )
            self.assertAlmostEqual(
                final["reconstructionError"], expected["errors"][0], places=6
            )
            self.assertEqual(final["verdict"], expected["verdict"][0])
            self.assertFalse(final["domainValidated"])
            self.assertEqual(result["rawInputStatus"], "configured_unverified")
            self.assertEqual(
                call("GET", "model-inference", token="demo-telemetry-ingest-token")[0],
                401,
            )
            self.assertEqual(call("POST", "model-raw-inputs", raw[0])[0], 403)
            incompatible = {**raw[0], "sampleRateHz": 800}
            self.assertEqual(
                call(
                    "POST",
                    "model-raw-inputs",
                    incompatible,
                    "demo-telemetry-ingest-token",
                )[0],
                400,
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
