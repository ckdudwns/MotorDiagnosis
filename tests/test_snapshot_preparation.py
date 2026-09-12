"""Stage 2: durable input preparation with no implicit legacy model fallback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

from motor_diagnosis import data, operations, rf66, raw_vibration
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore, canonical
from motor_diagnosis.server import create_server
from motor_diagnosis.snapshot_input import ADAPTER_ID
from tests.test_measured_rpm import RpmSetup
from tests import test_periodic_snapshots as fixtures
from tests import test_raw_vibration as raw_fixtures
from tests import test_rf66 as rf_fixtures

DEVICE = fixtures.DEVICE


class PreparationTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.store = PeriodicSnapshotStore()
        self.addCleanup(self.store.close)

    def payload(self, **changes):
        return fixtures.SnapshotTest.payload(self, 0, **changes)

    def send(self, **changes):
        return self.store.ingest(self.principal, DEVICE, self.payload(**changes))

    def latest(self):
        return self.store.list_device(self.admin, DEVICE)["items"][0]

    def test_exact_axis_scale_shape_preserved_without_legacy_features_or_model(self):
        rows = [(x + 300, y - 100, z + 200) for x, y, z in raw_fixtures.counts()]
        payload = self.payload(samples=raw_fixtures.encoded(rows))
        self.store.ingest(self.principal, DEVICE, payload)
        with mock.patch.object(raw_vibration, "extract66", side_effect=AssertionError("legacy feature path")), \
             mock.patch.object(rf66.RF66Model, "load", side_effect=AssertionError("legacy model path")):
            self.assertTrue(self.store.tick())
        item = self.latest()
        self.assertEqual(item["window"], payload["window"])
        analysis = item["analysis"]
        self.assertEqual((analysis["status"], analysis["reason"]), ("waiting_model", "MODEL_NOT_CONFIGURED"))
        self.assertEqual(analysis["inputPreparation"], {"adapterId": ADAPTER_ID, "status": "ready"})
        prepared = analysis["preparedInput"]
        self.assertEqual((prepared["shape"], prepared["axes"], prepared["unit"]), ([512, 3], ["X", "Y", "Z"], "g"))
        self.assertEqual(prepared["values"], [[v * .0039 for v in row] for row in rows])
        self.assertFalse(prepared["meanRemoved"])
        self.assertTrue(all(analysis[k] is None for k in ("score", "threshold", "verdict", "modelVersion")))
        self.assertFalse(analysis["affectsAlerts"])
        self.assertFalse(analysis["inferenceEnabled"])
        self.assertNotIn("confirmation", analysis)
        self.assertNotIn("modelInput", analysis)

    def test_waiting_model_is_final_for_this_stage_not_reprocessed_every_tick(self):
        first, _ = self.send()
        self.store.tick()
        result = self.latest()["analysis"]
        for _ in range(4):
            self.assertFalse(self.store.tick())
        retry, status = self.send()
        self.assertEqual((status, retry["processingStatus"]), (200, "waiting_model"))
        self.assertEqual(retry["acknowledged"], first["acknowledged"])
        self.assertEqual(self.latest()["analysis"], result)

    def test_no_fabricated_samples_for_reported_invalid_quality(self):
        self.send(quality="sensor_unavailable", samples=None, sampleCount=0)
        self.assertFalse(self.store.tick())
        self.assertEqual(self.latest()["analysis"]["status"], "unavailable")
        self.assertNotIn("preparedInput", self.latest()["analysis"])
        self.assertIsNone(self.latest()["window"]["samples"])

    def test_constant_raw_data_is_not_silently_rejected_by_rf66_rules(self):
        self.send(samples=raw_fixtures.encoded([(1, 2, 3)] * 512))
        self.store.tick()
        self.assertEqual(self.latest()["analysis"]["status"], "waiting_model")
        for actual, expected in zip(self.latest()["analysis"]["preparedInput"]["values"][0], [.0039, .0078, .0117]):
            self.assertAlmostEqual(actual, expected)

    def test_clipped_counts_are_unavailable_not_a_normal_prediction(self):
        rows = raw_fixtures.counts()
        rows[0] = (-4096, 0, 4095)
        self.send(samples=raw_fixtures.encoded(rows))
        self.store.tick()
        self.assertEqual(self.latest()["analysis"]["reason"], "clipped")
        self.assertIsNone(self.latest()["analysis"]["verdict"])
        self.assertNotIn("preparedInput", self.latest()["analysis"])

    def test_old_accepted_inbox_survives_reception_age_limit(self):
        self.send()
        with mock.patch("time.time", return_value=time.time() + 3 * 86400):
            self.store.tick()
        self.assertEqual(self.latest()["analysis"]["status"], "waiting_model")

    def test_corrupt_queued_body_isolated_without_blocking_next_row(self):
        self.send()
        self.send(windowIndex=1, startUptimeUs=660000,
                  timestamp=(self.started + timedelta(seconds=.66)).isoformat())
        with self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET body='{}' WHERE idx=0")
        self.assertTrue(self.store.tick())
        self.assertTrue(self.store.tick())
        statuses = self.store.list_device(self.admin, DEVICE)["statuses"]
        self.assertEqual(statuses, {"unavailable": 1, "waiting_model": 1})
        row = self.store.db.execute("SELECT result FROM periodic_snapshots WHERE idx=0").fetchone()
        self.assertEqual(json.loads(row[0])["reason"], "SNAPSHOT_INTEGRITY_MISMATCH")

    def test_sample_integrity_is_checked_again_during_preparation(self):
        self.send()
        payload = self.payload(integrity={"algorithm": "sha256", "digest": "0" * 64})
        body = canonical(payload)
        with self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET body=?,digest=?",
                                  (body, hashlib.sha256(body.encode()).hexdigest()))
        self.store.tick()
        self.assertEqual(self.latest()["analysis"]["reason"], "SNAPSHOT_INTEGRITY_MISMATCH")

    def test_failed_preparation_commit_keeps_original_queued_for_retry(self):
        first, _ = self.send()
        self.store.db.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.Error):
                self.store.tick()
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.latest()["analysis"]["status"], "queued")
        self.assertEqual(self.send()[0]["acknowledged"], first["acknowledged"])
        self.assertTrue(self.store.tick())
        self.assertEqual(self.latest()["analysis"]["status"], "waiting_model")

    def test_restart_replays_old_schema1_queue_without_touching_final_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshots.sqlite3"
            store = PeriodicSnapshotStore(path)
            try:
                ack, _ = store.ingest(self.principal, DEVICE, self.payload())
            finally:
                store.close()
            store = PeriodicSnapshotStore(path)
            try:
                self.assertEqual(store.db.execute("SELECT version FROM periodic_snapshot_schema").fetchone()[0], 3)
                self.assertTrue(store.tick())
                prepared = store.list_device(self.admin, DEVICE)["items"][0]["analysis"]
            finally:
                store.close()
            store = PeriodicSnapshotStore(path)
            try:
                self.assertFalse(store.tick())
                self.assertEqual(store.list_device(self.admin, DEVICE)["items"][0]["analysis"], prepared)
                self.assertEqual(store.ingest(self.principal, DEVICE, self.payload())[0]["acknowledged"], ack["acknowledged"])
            finally:
                store.close()

    def test_two_worker_connections_prepare_exactly_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshots.sqlite3"
            one, two = PeriodicSnapshotStore(path), PeriodicSnapshotStore(path)
            try:
                one.ingest(self.principal, DEVICE, self.payload())
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(s.tick) for s in (one, two)]
                    self.assertEqual(sorted(f.result(timeout=10) for f in futures), [False, True])
                self.assertEqual(one.list_device(self.admin, DEVICE)["statuses"], {"waiting_model": 1})
            finally:
                one.close()
                two.close()

    def test_snapshot_import_tree_contains_no_legacy_rf66_or_ml_runtime(self):
        script = "import sys; import motor_diagnosis.periodic_snapshots; " + \
                 "assert not any(n in sys.modules for n in ('motor_diagnosis.raw_vibration','motor_diagnosis.vibration_windows','motor_diagnosis.rf66','motor_diagnosis.rf66_events','ai.ai2.model_runtime','numpy','sklearn'))"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_worker_drains_inbox_and_ignores_retired_rf66_config(self):
        with mock.patch.object(rf66.RF66Model, "load", side_effect=AssertionError("must not load")) as loader:
            server = create_server("127.0.0.1", 0, auto_alerts=False, rf66_artifact="missing.zip",
                                   rf66_checksum="ignored", rf66_event_mode="alerts")
        try:
            loader.assert_not_called()
            self.assertIsNone(server.raw_vibration.model)
            self.assertIsNone(server.raw_vibration.worker)
            server.raw_vibration.start()
            self.assertIsNone(server.raw_vibration.worker)
            self.assertFalse(server.raw_vibration.events.notification_allowed({"rf66Transition": "open"}))
            principal = data.telemetry_principal_for_token("demo-telemetry-ingest-token")
            server.periodic_snapshots.ingest(principal, DEVICE, self.payload())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with server.periodic_snapshots.lock:
                    status = server.periodic_snapshots.db.execute("SELECT status FROM periodic_snapshots").fetchone()[0]
                if status == "waiting_model":
                    break
                threading.Event().wait(.02)
            self.assertEqual(status, "waiting_model")
            worker = server.periodic_snapshots.worker
        finally:
            server.server_close()
        self.assertFalse(worker.is_alive())

    def test_server_preserves_legacy_results_and_queued_rows_without_inference_or_pruning(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.sqlite3"
            legacy = raw_vibration.RawVibrationStore(path, model=rf_fixtures.FakeRF())
            try:
                legacy.ingest(self.principal, DEVICE, {"windows": [raw_fixtures.RawWindowTest.window(self)]})
                legacy.tick()
                legacy.ingest(self.principal, DEVICE, {"windows": [raw_fixtures.RawWindowTest.window(self, 1)]})
                original = [tuple(r) for r in legacy.db.execute("SELECT * FROM vibration_windows ORDER BY ordinal")]
                incidents = [tuple(r) for r in legacy.db.execute("SELECT * FROM rf66_incidents")]
            finally:
                legacy.close()
            server = create_server("127.0.0.1", 0, auto_alerts=False, raw_window_database=path)
            try:
                self.assertFalse(server.raw_vibration.tick())
                with mock.patch("time.time", return_value=time.time() + 5 * 86400):
                    server.raw_vibration.prune()
                self.assertEqual([tuple(r) for r in server.raw_vibration.db.execute("SELECT * FROM vibration_windows ORDER BY ordinal")], original)
                self.assertEqual([tuple(r) for r in server.raw_vibration.db.execute("SELECT * FROM rf66_incidents")], incidents)
            finally:
                server.server_close()

    def test_inspect_reports_preparation_backlog_but_not_model_wait_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            with closing(PeriodicSnapshotStore(project / operations.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"])) as store:
                store.ingest(self.principal, DEVICE, self.payload())
                now = time.time() + 121
                report = operations.inspect(project, {}, DEVICE, now=now)
                self.assertIn("PERIODIC_SNAPSHOT_DB_PATH:PREPARATION_BACKLOG", [i["code"] for i in report["issues"]])
                store.tick()
                report = operations.inspect(project, {}, DEVICE, now=now)
                self.assertNotIn("PERIODIC_SNAPSHOT_DB_PATH:PREPARATION_BACKLOG", [i["code"] for i in report["issues"]])
                latest = report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]["latest"]
                self.assertEqual(latest["inputPreparation"]["status"], "ready")
                self.assertEqual(latest["status"], "waiting_model")
