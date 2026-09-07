"""Persistent model identities must outlive the optional inference runtime.

These lifecycle/HTTP tests deliberately require no Torch, NumPy or model ZIP.
The checkpoint double computes no ML result; model accuracy is not under test.
"""

import copy
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from motor_diagnosis import data
from motor_diagnosis.model_inference import ModelInferenceStore
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup

DEVICE = "DEV-01-GEN-01"


class CheckpointDouble:
    checksum = "sha256:" + "a" * 64
    names = ("feature",)
    kind = "dense_autoencoder"
    sequence_length = 1

    def describe(self):
        return {"modelVersion": self.checksum, "modelType": self.kind}

    def predict(self, matrix, names):
        return {"errors": [0.25], "verdict": [False], "threshold": 1.0}


class ModelHistoryRestartTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        # Exercise URI escaping for Windows/Unicode paths as well.
        self.path = Path(self.folder.name) / "model history #1.sqlite3"
        self.checkpoint = CheckpointDouble()
        self.server = None
        self.thread = None

    def tearDown(self):
        self.stop_server()
        self.folder.cleanup()
        super().tearDown()

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.thread.join()
            self.server.server_close()
            self.server = self.thread = None

    def start_server(self, enabled=False):
        self.stop_server()
        with mock.patch(
            "ai.ai2.model_runtime.Checkpoint.load", return_value=self.checkpoint
        ) as load:
            self.server = create_server(
                "127.0.0.1",
                0,
                auto_alerts=False,
                model_database=self.path,
                model_artifact="checkpoint-double.pt" if enabled else None,
            )
            self.assertEqual(load.call_count, 1 if enabled else 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def call(self, method, path, body=None, *, ingest=False):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        try:
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer "
                    + ("demo-telemetry-ingest-token" if ingest else self.token),
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def payload(self):
        return {
            "schemaVersion": 1,
            "modelVersion": self.checkpoint.checksum,
            "deviceId": DEVICE,
            "siteId": "SITE-01",
            "assetId": "SITE-01-GEN-01",
            "sourceId": "history-only-test",
            "preprocessingId": "test-double",
            "sampleRateHz": 1000,
            "windowIndex": 0,
            "windowStartSample": 0,
            "windowEndSample": 1000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "features": {"feature": 1.0},
        }

    def seed_history(self, *, prune=False):
        store = ModelInferenceStore(self.checkpoint, self.path)
        try:
            result, status = store.ingest(self.principal, DEVICE, self.payload())
            self.assertEqual(status, 202)
            store.tick()
            if prune:
                with store.db:
                    store.db.execute("DELETE FROM inputs")
                    store.db.execute("DELETE FROM streams")
            return result["inputId"]
        finally:
            store.close()

    def test_disabled_restart_blocks_http_delete_and_keeps_original_result(self):
        self.start_server(enabled=True)
        status, accepted = self.call(
            "POST", f"/api/devices/{DEVICE}/model-inputs", self.payload(), ingest=True
        )
        self.assertEqual(status, 202, accepted)
        while self.server.model_inference.tick():
            pass
        status, protected = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(
            (status, protected["error"]["code"]), (409, "DEVICE_HAS_HISTORY")
        )
        before = copy.deepcopy(data.get_device(DEVICE))

        self.start_server(enabled=False)
        self.assertIsNone(self.server.model_inference)
        self.assertEqual(
            self.call("GET", f"/api/devices/{DEVICE}/model-inference")[0], 409
        )
        status, protected = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(status, 409, protected)
        self.assertEqual(protected["error"]["code"], "DEVICE_HAS_HISTORY")
        self.assertEqual(data.get_device(DEVICE), before)

        self.start_server(enabled=True)
        status, result = self.call("GET", f"/api/devices/{DEVICE}/model-inference")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["items"][0]["inputId"], accepted["inputId"])
        self.assertEqual(result["items"][0]["status"], "inferred")
        self.assertEqual(data.get_device(DEVICE), before)

    def test_disabled_restart_blocks_orphaned_id_registration(self):
        self.seed_history()
        # Recreate a device deleted by an old release, without deleting history.
        data.DEVICES[:] = [row for row in data.DEVICES if row["id"] != DEVICE]
        self.start_server(enabled=False)
        status, result = self.call(
            "POST",
            "/api/sites/SITE-01/devices",
            {
                "id": DEVICE,
                "assetId": "SITE-01-GEN-01",
                "mappingStatus": "inactive",
                "certificateId": "replacement-certificate",
                "certificateFingerprint": "sha256:" + "b" * 64,
            },
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "DEVICE_HAS_HISTORY")
        self.assertFalse(any(row["id"] == DEVICE for row in data.DEVICES))

    def test_identity_only_history_still_blocks_after_payload_retention(self):
        self.seed_history(prune=True)
        self.start_server(enabled=False)
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual((status, result["error"]["code"]), (409, "DEVICE_HAS_HISTORY"))

    def test_guard_is_read_only_and_does_not_process_pending_inputs(self):
        store = ModelInferenceStore(self.checkpoint, self.path)
        try:
            store.ingest(self.principal, DEVICE, self.payload())
        finally:
            store.close()
        before = self.path.read_bytes()
        self.start_server(enabled=False)
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(status, 409, result)
        self.stop_server()
        self.assertEqual(self.path.read_bytes(), before)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                db.execute("SELECT status,result FROM inputs").fetchone(),
                ("queued", None),
            )
        finally:
            db.close()

    def test_fresh_disabled_server_does_not_create_db_or_block_unrelated_identity(self):
        self.start_server(enabled=False)
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(status, 200, result)
        status, result = self.call(
            "POST",
            "/api/sites/SITE-01/devices",
            {
                "id": DEVICE,
                "assetId": "SITE-01-GEN-01",
                "mappingStatus": "inactive",
                "certificateId": "replacement-certificate",
                "certificateFingerprint": "sha256:" + "b" * 64,
            },
        )
        self.assertEqual(status, 201, result)
        self.assertFalse(self.path.exists())

    def test_newly_committed_history_is_not_missed_by_a_startup_snapshot(self):
        self.start_server(enabled=False)
        self.server.model_history.check_device_deletion(DEVICE)
        self.assertFalse(self.path.exists())
        self.seed_history()
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual((status, result["error"]["code"]), (409, "DEVICE_HAS_HISTORY"))

    def test_unreadable_database_is_not_treated_as_empty_history(self):
        self.path.write_bytes(b"not a SQLite database")
        self.start_server(enabled=False)
        before = copy.deepcopy(data.get_device(DEVICE))
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(
            (status, result["error"]["code"]), (503, "MODEL_HISTORY_UNAVAILABLE")
        )
        self.assertEqual(data.get_device(DEVICE), before)

    def test_missing_identity_schema_is_fail_closed(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TABLE legacy_placeholder(value TEXT)")
            db.commit()
        finally:
            db.close()
        self.start_server(enabled=False)
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(
            (status, result["error"]["code"]), (503, "MODEL_HISTORY_UNAVAILABLE")
        )

    def test_known_database_disappearing_or_read_failure_blocks_mutation(self):
        self.seed_history()
        self.start_server(enabled=False)
        with mock.patch(
            "motor_diagnosis.model_history.sqlite3.connect",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(
            (status, result["error"]["code"]), (503, "MODEL_HISTORY_UNAVAILABLE")
        )
        # All paths belong to this test's own TemporaryDirectory.
        backup = self.path.with_suffix(".backup")
        self.assertEqual(backup.resolve().parent, Path(self.folder.name).resolve())
        self.path.rename(backup)
        status, result = self.call("DELETE", f"/api/devices/{DEVICE}")
        self.assertEqual(
            (status, result["error"]["code"]), (503, "MODEL_HISTORY_UNAVAILABLE")
        )

    def test_close_and_failed_server_initialization_unregister_guard(self):
        self.seed_history()
        self.start_server(enabled=False)
        self.stop_server()
        # Lifecycle gate must not retain guards owned by a stopped server.
        data.device_lifecycle.check_history(DEVICE)
        with mock.patch(
            "motor_diagnosis.server.AnalysisStore",
            side_effect=RuntimeError("startup failed"),
        ):
            with self.assertRaises(RuntimeError):
                create_server(
                    "127.0.0.1", 0, model_database=self.path, auto_alerts=False
                )
        data.device_lifecycle.check_history(DEVICE)

    def test_disabled_protection_runs_without_any_ml_imports(self):
        self.seed_history()
        script = r"""
import builtins, sys
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'numpy', 'librosa'} or name == 'ai.ai2.model_runtime':
        raise AssertionError('Disabled history guard imported ML runtime: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from motor_diagnosis.server import create_server
from motor_diagnosis import data
server = create_server('127.0.0.1', 0, model_database=sys.argv[2], auto_alerts=False)
try:
    assert server.model_inference is None
    user = data.current_user_for_token(data.authenticate({'username':'admin','password':'admin123'})['session']['token'])
    try:
        data.delete_device(user, 'DEV-01-GEN-01')
    except data.ApiError as exc:
        assert (exc.status, exc.code) == (409, 'DEVICE_HAS_HISTORY'), str(exc)
    else:
        raise AssertionError('Historical ID was deleted without ML runtime')
finally:
    server.server_close()
print('no-ML history protection passed')
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(Path(__file__).resolve().parents[1]),
                str(self.path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no-ML history protection passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
