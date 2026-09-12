"""Stage 3 plumbing uses deterministic test doubles, never a deployed model."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest import mock

from motor_diagnosis import data, operations, rf66
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.server import create_server
from motor_diagnosis.snapshot_model import CONTRACT_ID, INPUT_CONTRACT, SnapshotModelAdapter
from tests.test_measured_rpm import RpmSetup
from tests import test_periodic_snapshots as fixtures

DEVICE = fixtures.DEVICE


def metadata(**changes):
    return {"contractId": CONTRACT_ID, "modelId": "test-only", "modelVersion": "test-v1",
            "preprocessingVersion": "test-preprocess-v1", "inputContract": copy.deepcopy(INPUT_CONTRACT),
            "scope": {"deviceId": DEVICE, "siteId": "SITE-01", "assetId": "SITE-01-GEN-01"},
            "scoreType": "test_error", "threshold": .5, "comparison": ">", **changes}


class SnapshotInferenceTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)

    def adapter(self, output=None, **changes):
        self.predict = mock.Mock(return_value={"score": .75} if output is None else output)
        return SnapshotModelAdapter(metadata(**changes), self.predict)

    def store(self, model=None, path=":memory:"):
        store = PeriodicSnapshotStore(path, model=model)
        self.addCleanup(store.close)
        return store

    def send(self, store, slot=0, **changes):
        payload = fixtures.SnapshotTest.payload(self, slot, **changes)
        return store.ingest(self.principal, DEVICE, payload)

    def latest(self, store):
        return store.list_device(self.admin, DEVICE)["items"][0]

    def prepare(self, store, slot=0, **changes):
        ack, status = self.send(store, slot, **changes)
        self.assertEqual(status, 202)
        self.assertTrue(store.tick())
        return ack

    def test_unconfigured_default_never_creates_jobs_or_calls_a_model(self):
        store = self.store()
        self.prepare(store)
        self.assertFalse(store.inference.tick())
        view = store.list_device(self.admin, DEVICE)
        self.assertFalse(view["inferenceEnabled"])
        self.assertIsNone(view["configuredModel"])
        self.assertEqual(view["items"][0]["analysis"]["status"], "waiting_model")
        self.assertEqual(store.db.execute("SELECT count(*) FROM snapshot_inference_jobs").fetchone()[0], 0)

    def test_independent_result_pins_model_metadata_and_original_digest(self):
        model = self.adapter()
        store = self.store(model)
        ack = self.prepare(store)
        original = self.latest(store)["window"]
        with mock.patch.object(rf66.RF66Model, "load", side_effect=AssertionError("legacy fallback")):
            self.assertTrue(store.inference.tick())
        item = self.latest(store)
        result = item["analysis"]
        self.assertEqual((result["status"], result["score"], result["threshold"], result["verdict"]), ("completed", .75, .5, True))
        self.assertEqual(result["inference"]["model"], metadata())
        self.assertEqual(result["inference"]["inputDigest"], item["digest"])
        self.assertEqual(result["inference"]["attempt"], 1)
        self.assertEqual(item["window"], original)
        self.assertFalse(result["affectsAlerts"])
        self.assertNotIn("confirmation", result)
        self.assertEqual(self.predict.call_args.args[0]["shape"], [512, 3])
        self.assertNotIn("transmission", self.predict.call_args.args[1])
        self.assertEqual(self.send(store)[0]["acknowledged"], ack["acknowledged"])
        self.assertFalse(store.inference.tick())
        self.predict.assert_called_once()

    def test_zero_boundary_and_comparison_are_explicit_not_rf66_defaults(self):
        for comparison, threshold, expected in ((">", 0, False), (">=", 0, True), ("<", .1, True), ("<=", 0, True)):
            with self.subTest(comparison=comparison):
                store = self.store(self.adapter({"score": 0}, comparison=comparison, threshold=threshold))
                self.prepare(store)
                store.inference.tick()
                self.assertEqual(self.latest(store)["analysis"]["verdict"], expected)

    def test_all_delivery_reasons_are_analyzed_independently_of_board_state(self):
        from tests.test_edge_state_snapshots import EdgeSnapshotTest, REPORTS
        store = self.store(self.adapter({"score": 0}))
        for index, reason in enumerate(REPORTS):
            payload = EdgeSnapshotTest.payload(self, index, reason)
            store.ingest(self.principal, DEVICE, payload)
            store.tick()
            store.inference.tick()
            item = self.latest(store)
            self.assertEqual(item["transmission"], payload["transmission"])
            self.assertFalse(item["analysis"]["verdict"])
            self.assertFalse(item["analysis"]["affectsAlerts"])
            self.assertNotIn("confirmation", item["analysis"])
        self.assertEqual(self.predict.call_count, 4)

    def test_invalid_or_missing_model_outputs_never_become_normal(self):
        for output in (None, {}, {"score": True}, {"score": float("nan")}, {"score": float("inf")},
                       {"score": "0.9"}, {"score": 0, "verdict": True}, {"unavailableReason": "bad reason"}):
            with self.subTest(output=output):
                store = self.store(self.adapter())
                self.predict.return_value = output
                self.prepare(store)
                store.inference.tick()
                result = self.latest(store)["analysis"]
                self.assertEqual((result["status"], result["reason"]), ("unavailable", "MODEL_OUTPUT_INVALID"))
                self.assertIsNone(result["score"])
                self.assertIsNone(result["verdict"])

    def test_model_unavailable_and_exceptions_are_stored_without_secret_details(self):
        for failed in (False, True):
            store = self.store(self.adapter({"unavailableReason": "INSUFFICIENT_INPUT"}))
            if failed:
                self.predict.side_effect = RuntimeError("private model path and credentials")
            self.prepare(store)
            store.inference.tick()
            result = self.latest(store)["analysis"]
            self.assertEqual(result["reason"], "MODEL_EXECUTION_FAILED" if failed else "INSUFFICIENT_INPUT")
            self.assertIsNone(result["verdict"])
            self.assertNotIn("credentials", json.dumps(result))

    def test_invalid_or_clipped_input_is_never_scheduled_for_prediction(self):
        store = self.store(self.adapter())
        self.send(store, quality="sensor_unavailable", samples=None, sampleCount=0)
        self.assertFalse(store.tick())
        self.assertFalse(store.inference.tick())
        from tests.test_raw_vibration import encoded
        self.prepare(store, 1, samples=encoded([(4095, 0, 0)] * 512))
        self.assertFalse(store.inference.tick())
        self.predict.assert_not_called()
        self.assertEqual(store.db.execute("SELECT status FROM snapshot_inference_jobs").fetchone()[0], "unavailable")

    def test_mutated_prepared_input_or_job_metadata_is_not_used(self):
        for target in ("prepared", "metadata"):
            with self.subTest(target=target):
                store = self.store(self.adapter())
                self.prepare(store)
                with store.db:
                    if target == "prepared":
                        result = self.latest(store)["analysis"]
                        result["preparedInput"]["values"][0][0] = 123
                        store.db.execute("UPDATE periodic_snapshots SET result=?", (json.dumps(result),))
                    else:
                        store.db.execute("UPDATE snapshot_inference_jobs SET metadata='{}'")
                self.assertTrue(store.inference.tick())
                self.predict.assert_not_called()
                self.assertEqual(self.latest(store)["analysis"]["reason"], "SNAPSHOT_INPUT_INTEGRITY_FAILED")

    def test_scope_mismatch_cannot_use_or_expose_another_assets_model(self):
        store = self.store(self.adapter(scope={"deviceId": "DEV-01-MOT-02", "siteId": "SITE-01", "assetId": "SITE-01-MOT-02"}))
        self.prepare(store)
        self.assertFalse(store.inference.tick())
        view = store.list_device(self.admin, DEVICE)
        self.assertIsNone(view["configuredModel"])
        self.assertFalse(view["inferenceEnabled"])
        self.assertIsNone(view["items"][0]["inferenceJob"])

    def test_contract_requires_single_snapshot_and_rejects_legacy_objects(self):
        for changes in ({"inputContract": {**INPUT_CONTRACT, "shape": [12, 9]}},
                        {"threshold": float("nan")}, {"threshold": True}, {"threshold": 10**400},
                        {"comparison": "auto"}, {"scope": {}}, {"preprocessingVersion": ""},
                        {"contractId": "rf66-consecutive-3-v1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.adapter(**changes)
        with self.assertRaises(ValueError):
            PeriodicSnapshotStore(model=object())

    def test_adapter_cannot_mutate_metadata_or_saved_preparation(self):
        spec = metadata()
        def predict(prepared, context):
            prepared["values"][0][0] = 100
            context["assetId"] = "OTHER"
            return {"score": .75}
        model = SnapshotModelAdapter(spec, predict)
        spec["threshold"] = 99
        model.metadata()["threshold"] = 99
        store = self.store(model)
        self.prepare(store)
        prepared = copy.deepcopy(self.latest(store)["analysis"]["preparedInput"])
        store.inference.tick()
        self.assertEqual(self.latest(store)["analysis"]["preparedInput"], prepared)
        self.assertTrue(self.latest(store)["analysis"]["verdict"])

    def test_model_attachment_does_not_reprocess_preexisting_waiting_or_queued_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inbox.sqlite3"
            with closing(PeriodicSnapshotStore(path)) as store:
                self.prepare(store)
                historical = self.latest(store)["analysis"]
                self.send(store, 1)
            with closing(PeriodicSnapshotStore(path, model=self.adapter())) as store:
                store.tick()
                self.assertFalse(store.inference.tick())
                self.assertEqual(store.list_device(self.admin, DEVICE)["items"][1]["analysis"], historical)
                self.prepare(store, 2)
                self.assertTrue(store.inference.tick())
                self.predict.assert_called_once()

    def test_receipt_and_job_commit_together_and_duplicates_do_not_reschedule(self):
        store = self.store(self.adapter())
        store.db.executescript("CREATE TRIGGER fail_job BEFORE INSERT ON snapshot_inference_jobs BEGIN SELECT RAISE(ABORT,'disk error'); END;")
        with self.assertRaises(data.ApiError):
            self.send(store)
        self.assertEqual(store.db.execute("SELECT count(*) FROM periodic_snapshots").fetchone()[0], 0)
        store.db.execute("DROP TRIGGER fail_job")
        self.prepare(store)
        self.send(store)
        self.assertEqual(store.db.execute("SELECT count(*) FROM snapshot_inference_jobs").fetchone()[0], 1)

    def test_two_connections_claim_one_job_without_duplicate_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inbox.sqlite3"
            model = self.adapter()
            with closing(PeriodicSnapshotStore(path, model=model)) as one, closing(PeriodicSnapshotStore(path, model=model)) as two:
                self.prepare(one)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(s.inference.tick) for s in (one, two)]
                    self.assertEqual(sorted(f.result(timeout=10) for f in futures), [False, True])
                self.predict.assert_called_once()

    def test_expired_claim_can_resume_but_late_old_result_cannot_overwrite(self):
        store = self.store(self.adapter())
        self.prepare(store)
        row, token, started = store.inference._claim()
        self.assertFalse(store.inference.tick())
        with store.db:
            store.db.execute("UPDATE snapshot_inference_jobs SET lease_until=0")
        self.assertTrue(store.inference.tick())
        result = self.latest(store)["analysis"]
        self.assertEqual(result["inference"]["attempt"], 2)
        self.assertFalse(store.inference._finish(row, token, started,
            {"status": "completed", "score": 0, "verdict": False}, 1))
        self.assertEqual(self.latest(store)["analysis"], result)

    def test_expired_execution_result_is_unavailable_not_a_late_verdict(self):
        store = self.store(self.adapter())
        self.prepare(store)
        now = time.time()
        clock = [now]
        def delayed(prepared, context):
            clock[0] += 61
            return {"score": 0}
        self.predict.side_effect = delayed
        with mock.patch("motor_diagnosis.snapshot_inference.time.time", side_effect=lambda: clock[0]):
            self.assertTrue(store.inference.tick())
        result = self.latest(store)["analysis"]
        self.assertEqual(result["reason"], "INFERENCE_DEADLINE_EXCEEDED")
        self.assertIsNone(result["verdict"])

    def test_shutdown_does_not_wait_forever_or_write_after_database_close(self):
        entered, release = threading.Event(), threading.Event()
        model = self.adapter()
        def blocked(prepared, context):
            entered.set()
            release.wait(8)
            return {"score": .75}
        self.predict.side_effect = blocked
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inbox.sqlite3"
            store = PeriodicSnapshotStore(path, model=model)
            self.prepare(store)
            store.start()
            try:
                self.assertTrue(entered.wait(2))
                worker = store.inference.worker
                start = time.monotonic()
                store.close()
                self.assertLess(time.monotonic() - start, 4)
                release.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT status FROM periodic_snapshots").fetchone()[0], "queued_inference")
            finally:
                release.set()
                if not store.stop.is_set():
                    store.close()

    def test_commit_failure_retains_claim_for_bounded_recovery(self):
        store = self.store(self.adapter())
        self.prepare(store)
        row, token, started = store.inference._claim()
        store.db.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
                                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.Error):
                store.inference._finish(row, token, started,
                    {"status": "completed", "reason": None, "score": 0, "verdict": False}, 1)
        finally:
            store.db.set_authorizer(None)
        self.assertEqual(self.latest(store)["analysis"]["status"], "queued_inference")
        with store.db:
            store.db.execute("UPDATE snapshot_inference_jobs SET lease_until=0,attempts=3")
        self.assertTrue(store.inference.tick())
        self.predict.assert_not_called()
        self.assertEqual(self.latest(store)["analysis"]["reason"], "INFERENCE_RETRY_EXHAUSTED")

    def test_changed_model_cannot_take_old_jobs_or_overwrite_completed_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inbox.sqlite3"
            old = self.adapter()
            with closing(PeriodicSnapshotStore(path, model=old)) as store:
                self.prepare(store)
                store.inference.tick()
                original = self.latest(store)["analysis"]
                self.prepare(store, 1)
            with closing(PeriodicSnapshotStore(path, model=self.adapter(modelVersion="test-v2"))) as store:
                self.assertFalse(store.inference.tick())
                self.assertFalse(self.latest(store)["inferenceJob"]["runtimeAvailable"])
                self.assertEqual(store.list_device(self.admin, DEVICE)["items"][1]["analysis"], original)
            with closing(PeriodicSnapshotStore(path, model=old)) as store:
                self.assertTrue(store.inference.tick())
                self.assertFalse(store.inference.tick())

    def test_schema1_upgrade_keeps_originals_and_does_not_assign_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inbox.sqlite3"
            with closing(PeriodicSnapshotStore(path)) as store:
                ack, _ = self.send(store)
                before = [tuple(r) for r in store.db.execute("SELECT * FROM periodic_snapshots")]
                store.db.executescript("DROP TABLE snapshot_inference_jobs; UPDATE periodic_snapshot_schema SET version=1;")
            with closing(PeriodicSnapshotStore(path, model=self.adapter())) as store:
                self.assertEqual(store.db.execute("SELECT version FROM periodic_snapshot_schema").fetchone()[0], 3)
                self.assertEqual([tuple(r) for r in store.db.execute("SELECT * FROM periodic_snapshots")], before)
                self.assertEqual(self.send(store)[0]["acknowledged"], ack["acknowledged"])
                store.tick()
                self.assertFalse(store.inference.tick())

    def test_backup_copy_keeps_final_result_and_pending_model_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            model = self.adapter()
            store = self.store(model)
            self.prepare(store)
            store.inference.tick()
            result = self.latest(store)["analysis"]
            self.prepare(store, 1)
            path = Path(folder) / "backup.sqlite3"
            with closing(sqlite3.connect(path)) as target:
                store.db.backup(target)
            with closing(PeriodicSnapshotStore(path, model=model)) as restored:
                self.assertTrue(restored.inference.tick())
                self.assertEqual(restored.list_device(self.admin, DEVICE)["items"][1]["analysis"], result)

    def test_inspect_distinguishes_inference_backlog_and_prediction_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            with closing(PeriodicSnapshotStore(project / operations.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"], model=self.adapter({"unavailableReason": "TEST_UNAVAILABLE"}))) as store:
                self.prepare(store)
                report = operations.inspect(project, {}, DEVICE, now=time.time() + 121)
                self.assertIn("PERIODIC_SNAPSHOT_DB_PATH:INFERENCE_BACKLOG", [i["code"] for i in report["issues"]])
                self.assertIsNone(report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]["inferenceEnabled"])
                store.inference.tick()
                report = operations.inspect(project, {}, DEVICE)
                self.assertIn("PERIODIC_SNAPSHOT_INFERENCE_UNAVAILABLE", [i["code"] for i in report["issues"]])
                self.assertNotIn("PERIODIC_SNAPSHOT_INPUT_UNAVAILABLE", [i["code"] for i in report["issues"]])

    def test_http_ack_and_preparation_continue_while_prediction_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def predict(prepared, context):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test predictor release timed out")
            return {"score": .75}
        server = create_server("127.0.0.1", 0, auto_alerts=False,
                               snapshot_model=SnapshotModelAdapter(metadata(), predict), rf66_artifact="ignored.zip")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(slot):
            conn = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                conn.request("POST", f"/api/devices/{DEVICE}/periodic-snapshots",
                    json.dumps(fixtures.SnapshotTest.payload(self, slot)),
                    {"Authorization": "Bearer demo-telemetry-ingest-token", "Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.load(response)
            finally:
                conn.close()
        try:
            self.assertEqual(post(0)[0], 202)
            self.assertTrue(entered.wait(3))
            self.assertEqual(post(1)[0], 202)
            self.assertEqual(post(0)[0], 200)
            view = server.periodic_snapshots.list_device(self.admin, DEVICE)
            self.assertTrue(view["inferenceEnabled"])
            self.assertEqual(len(view["items"]), 2)
            self.assertIsNone(server.raw_vibration.model)
            self.assertIsNone(server.raw_vibration.worker)
            release.set()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.latest(server.periodic_snapshots)["analysis"]["status"] != "completed":
                threading.Event().wait(.02)
            self.assertEqual(self.latest(server.periodic_snapshots)["analysis"]["status"], "completed")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()
