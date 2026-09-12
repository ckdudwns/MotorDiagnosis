"""Offline stage-1 contracts: no firmware, live server, or model changes."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import copy
import http.client
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest import mock

from motor_diagnosis import data, device_lifecycle, operations
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.raw_vibration import RawVibrationStore
from motor_diagnosis.server import create_server
from motor_diagnosis.transmission_policy import SNAPSHOT_POLICY_ID
from tests.test_measured_rpm import RpmSetup
from tests import test_raw_vibration as raw_fixtures
from tests import test_vibration_windows as feature_fixtures

DEVICE = raw_fixtures.DEVICE


class SnapshotTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)
        self.store = PeriodicSnapshotStore()
        self.addCleanup(self.store.close)

    def payload(self, slot=0, **changes):
        # Continuous acquisition index intentionally skips hundreds of windows.
        window = raw_fixtures.RawWindowTest.window(self, slot * 469, **{
            "startUptimeUs": slot * 300000000,
            "timestamp": (self.started + timedelta(seconds=slot*300)).isoformat(),
            **changes,
        })
        try:
            digest = None if window["samples"] is None else hashlib.sha256(
                base64.b64decode(window["samples"], validate=True)).hexdigest()
        except ValueError:
            digest = "0"*64  # Malformed-base64 fixtures must reach the API validator.
        window.setdefault("integrity", {"algorithm": "sha256", "digest": digest})
        return {"window": window, "transmission": {
            "policyId": SNAPSHOT_POLICY_ID, "mode": "periodic", "intervalSec": 300,
        }}

    def send(self, slot=0, *, store=None, payload=None, principal=None, device=DEVICE, **changes):
        return (store or self.store).ingest(
            self.principal if principal is None else principal, device,
            self.payload(slot, **changes) if payload is None else payload)

    def items(self):
        return self.store.list_device(self.admin, DEVICE)["items"]

    def test_single_window_original_and_full_policy_persisted_before_ack(self):
        payload = self.payload()
        ack, status = self.send(payload=payload)
        self.assertEqual((status, ack["accepted"]), (202, 1))
        self.assertEqual(ack["processingStatus"], "queued")
        self.assertFalse(ack["processingEnabled"])
        self.assertFalse(ack["duplicate"])
        row = self.store.db.execute("SELECT * FROM periodic_snapshots").fetchone()
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(json.loads(row["body"]), payload)
        self.assertEqual(ack["acknowledged"][0]["digest"], row["digest"])
        item = self.items()[0]
        self.assertEqual(item["window"], payload["window"])
        self.assertEqual(item["transmission"], payload["transmission"])
        self.assertIsNone(item["analysis"]["verdict"])
        self.assertNotIn("modelInput", item["analysis"])
        self.assertFalse(item["analysis"]["affectsAlerts"])

    def test_five_minute_sparse_selection_has_no_continuous_confirmation(self):
        for slot in range(4):
            self.send(slot)
        items = self.items()
        self.assertEqual([r["window"]["windowIndex"] for r in items], [1407, 938, 469, 0])
        self.assertTrue(all(r["analysis"]["status"] == "queued" for r in items))
        self.assertTrue(all("confirmation" not in r["analysis"] and "missingWindowsBefore" not in r for r in items))
        self.assertFalse(hasattr(self.store, "worker"))

    def test_duplicate_is_200_and_does_not_change_original_receipt(self):
        first, _ = self.send()
        second, status = self.send(payload=dict(reversed(list(self.payload().items()))))
        self.assertEqual((status, second["accepted"], second["duplicate"]), (200, 0, True))
        self.assertEqual(first["acknowledged"], second["acknowledged"])
        self.assertEqual(len(self.items()), 1)

    def test_identical_retry_can_ack_after_new_input_age_limit(self):
        first, _ = self.send()
        with mock.patch("time.time", return_value=time.time()+3*86400):
            ack, status = self.send()
            self.assertEqual(status, 200)
            self.assertEqual(first["acknowledged"], ack["acknowledged"])
            with self.assertRaises(data.ApiError):
                self.send(1)

    def test_conflicting_content_is_409_without_overwrite(self):
        first, _ = self.send()
        with self.assertRaises(data.ApiError) as error:
            self.send(quality="fifo_overrun")
        self.assertEqual((error.exception.status, error.exception.code), (409, "SNAPSHOT_CONFLICT"))
        self.assertEqual(self.items()[0]["digest"], first["acknowledged"][0]["digest"])
        self.assertEqual(self.items()[0]["window"]["quality"], "valid")

    def test_no_batch_priority_replay_or_unversioned_input(self):
        valid = self.payload()
        bad = [[], {}, {"window": valid["window"]},
               {"windows": [valid["window"]], "transmission": valid["transmission"]},
               {**valid, "window": [valid["window"], valid["window"]]},
               {**valid, "reason": "abnormal"}]
        for fields in ({"mode": "priority"}, {"mode": "replay"}, {"mode": "immediate"},
                       {"mode": "normal"}, {"policyId": "edge-trigger-batch-v1"},
                       {"intervalSec": 1}, {"intervalSec": 300.0}, {"intervalSec": True},
                       {"reason": "rms_high"}):
            bad.append({**valid, "transmission": {**valid["transmission"], **fields}})
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(data.ApiError) as error:
                self.send(payload=payload)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.items(), [])

    def test_profile_shape_encoding_units_quality_and_nonfinite_validation(self):
        for fields in ({"profileId": "new-model-tbd"}, {"profileId": []}, {"unit": "g"},
                       {"quality": "normal"}, {"quality": []}, {"sampleRateHz": 5},
                       {"sampleCount": 1}, {"samples": "not-base64"}, {"axes": ["Z", "Y", "X"]},
                       {"gPerCount": float("nan")}, {"startUptimeUs": -1},
                       {"bootId": "208"}, {"windowIndex": True}, {"windowIndex": 2**80},
                       {"extra": "ignored?"}):
            with self.subTest(fields=fields), self.assertRaises(data.ApiError) as error:
                self.send(**fields)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.items(), [])

    def test_unavailable_quality_preserves_null_or_partial_samples(self):
        for slot, samples in enumerate((None, raw_fixtures.encoded(raw_fixtures.counts()[:3]))):
            ack, status = self.send(slot, quality="sample_gap", samples=samples, sampleCount=3)
            self.assertEqual((status, ack["processingStatus"]), (202, "unavailable"))
        for item in self.items():
            self.assertEqual(item["analysis"]["reason"], "sample_gap")
            self.assertIsNone(item["analysis"]["verdict"])
        self.assertIsNone(self.items()[-1]["window"]["samples"])

    def test_feature_vectors_are_not_raw_snapshot_payloads(self):
        payload = self.payload()
        payload["window"] = feature_fixtures.WindowTest.window(self)
        with self.assertRaises(data.ApiError):
            self.send(payload=payload)
        self.assertEqual(self.items(), [])

    def test_integrity_is_over_decoded_sample_bytes_and_required(self):
        payload = self.payload()
        wrong = hashlib.sha256(payload["window"]["samples"].encode()).hexdigest()
        with self.assertRaises(data.ApiError) as error:
            self.send(integrity={"algorithm": "sha256", "digest": wrong})
        self.assertEqual(error.exception.code, "SNAPSHOT_INTEGRITY_MISMATCH")
        for integrity in (None, {}, {"algorithm": "crc32", "digest": "0"*64},
                          {"algorithm": "sha256", "digest": None},
                          {"algorithm": "sha256", "digest": []},
                          {"algorithm": "sha256", "digest": "ABC"},
                          {"algorithm": "sha256", "digest": "0"*64, "extra": True}):
            with self.subTest(integrity=integrity), self.assertRaises(data.ApiError):
                self.send(integrity=integrity)
        del payload["window"]["integrity"]
        with self.assertRaises(data.ApiError):
            self.send(payload=payload)
        self.assertEqual(self.items(), [])
        self.send()

    def test_changed_sample_with_old_checksum_is_not_persisted(self):
        payload = self.payload()
        counts = raw_fixtures.counts()
        counts[0] = (100, 100, 100)
        payload["window"]["samples"] = raw_fixtures.encoded(counts)
        with self.assertRaises(data.ApiError) as error:
            self.send(payload=payload)
        self.assertEqual(error.exception.code, "SNAPSHOT_INTEGRITY_MISMATCH")
        self.assertEqual(self.items(), [])

    def test_missing_samples_must_not_have_fabricated_empty_checksum(self):
        with self.assertRaises(data.ApiError):
            self.send(quality="sensor_unavailable", samples=None, sampleCount=0,
                      integrity={"algorithm": "sha256", "digest": hashlib.sha256(b"").hexdigest()})
        self.assertEqual(self.items(), [])

    def test_scope_and_mapping_must_be_authorized_before_dedupe(self):
        self.send()
        for principal in ({}, {"permissions": ["device-health:write"]},
                          {"permissions": ["telemetry:ingest"], "allowedDeviceIds": ["DEV-OTHER"]}):
            with self.subTest(principal=principal), self.assertRaises(data.ApiError) as error:
                self.send(principal=principal)
            self.assertEqual(error.exception.status, 403)
        with self.assertRaises(data.ApiError):
            self.send(device="DEV-01-MOT-02")
        device = data.get_device(DEVICE)
        for fields in ({"mappingStatus": "inactive"}, {"certificateStatus": "revoked"}):
            with mock.patch.dict(device, fields), self.assertRaises(data.ApiError) as error:
                self.send()
            self.assertEqual(error.exception.status, 409)

    def test_new_input_mapping_mismatch_is_not_stored(self):
        with self.assertRaises(data.ApiError) as error:
            self.send(assetId="SITE-01-MOT-02")
        self.assertEqual(error.exception.code, "DEVICE_MAPPING_MISMATCH")
        self.assertEqual(self.items(), [])

    def test_registration_id_with_dots_is_supported(self):
        original = copy.deepcopy(data.get_device(DEVICE))
        original["id"] = "DEV.REVIEW.01"
        data.DEVICES.append(original)
        self.send(device=original["id"], deviceId=original["id"])
        self.assertEqual(len(self.store.list_device(self.admin, original["id"])["items"]), 1)

    def test_late_arrival_and_boot_retries_do_not_rewind_latest_measurement(self):
        self.send(2)
        ack, status = self.send(0)
        self.assertEqual((status, ack["lateArrival"]), (202, True))
        self.send(1)
        self.send(3, bootId="d"*32, windowIndex=0, startUptimeUs=0)
        self.send(1, bootId="e"*32, windowIndex=0, startUptimeUs=0)
        self.assertEqual(self.items()[0]["window"]["bootId"], "d"*32)
        self.assertEqual(len(self.items()), 5)

    def test_clock_freeze_and_context_mutation_rollback(self):
        self.send(0)
        for fields in ({"timestamp": self.payload()["window"]["timestamp"]},
                       {"startUptimeUs": 0}):
            with self.subTest(fields=fields), self.assertRaises(data.ApiError) as error:
                self.send(1, **fields)
            self.assertEqual(error.exception.code, "TIMESTAMP_UPTIME_MISMATCH")
        with mock.patch.dict(data.get_device(DEVICE), {"assetId": "SITE-01-MOT-02"}), self.assertRaises(data.ApiError) as error:
            self.send(1, assetId="SITE-01-MOT-02")
        self.assertEqual(error.exception.code, "SNAPSHOT_SEQUENCE_CONFLICT")
        self.assertEqual(len(self.items()), 1)

    def test_equal_measurement_time_from_another_boot_does_not_replace_head(self):
        self.send(2)
        ack, _ = self.send(2, bootId="d"*32, windowIndex=0, startUptimeUs=0)
        self.assertTrue(ack["lateArrival"])
        self.assertEqual(self.items()[0]["window"]["bootId"], "c"*32)

    def test_late_arrival_must_fit_neighbor_index_and_time(self):
        self.send(0)
        self.send(2)
        with self.assertRaises(data.ApiError) as error:
            self.send(1, startUptimeUs=700000000,
                      timestamp=(self.started+timedelta(seconds=700)).isoformat())
        self.assertEqual(error.exception.code, "SNAPSHOT_SEQUENCE_CONFLICT")

    def test_new_stale_or_future_measurement_is_rejected(self):
        for stamp in (datetime.now(timezone.utc)-timedelta(days=3), datetime.now(timezone.utc)+timedelta(hours=1)):
            with self.subTest(stamp=stamp), self.assertRaises(data.ApiError):
                self.send(timestamp=stamp.isoformat())

    def test_backpressure_preserves_queue_and_still_acknowledges_retries(self):
        self.store.max_rows = 1
        self.send()
        with self.assertRaises(data.ApiError) as error:
            self.send(1)
        self.assertEqual((error.exception.status, error.exception.code), (503, "SNAPSHOT_BACKPRESSURE"))
        self.assertEqual(self.send()[1], 200)
        self.assertEqual(len(self.items()), 1)
        self.store.max_rows = 3
        self.store.max_streams = 1
        with self.assertRaises(data.ApiError):
            self.send(1, bootId="d"*32)

    def test_insert_failure_rolls_back_stream_and_never_acknowledges(self):
        self.store.db.executescript("CREATE TRIGGER fail BEFORE INSERT ON periodic_snapshots BEGIN SELECT RAISE(ABORT, 'disk error'); END;")
        with self.assertRaises(data.ApiError) as error:
            self.send()
        self.assertEqual(error.exception.code, "SNAPSHOT_STORAGE_UNAVAILABLE")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_streams").fetchone()[0], 0)
        self.assertEqual(self.items(), [])

    def test_commit_failure_does_not_return_ack_or_keep_uncommitted_record(self):
        self.store.db.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(data.ApiError) as error:
                self.send()
            self.assertEqual(error.exception.code, "SNAPSHOT_STORAGE_UNAVAILABLE")
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.send()[1], 202)

    def test_restart_and_independent_connection_observe_committed_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"snapshots.sqlite3"
            store = PeriodicSnapshotStore(path)
            ack, _ = self.send(store=store)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT status FROM periodic_snapshots").fetchone()[0], "queued")
            store.close()
            store = PeriodicSnapshotStore(path)
            try:
                self.assertEqual(self.send(store=store)[0]["acknowledged"], ack["acknowledged"])
                self.assertEqual(store.db.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(store.list_device(self.admin, DEVICE)["statuses"], {"queued": 1})
                with self.assertRaises(data.ApiError):
                    store.check_device_deletion(DEVICE)
            finally:
                store.close()

    def test_simultaneous_duplicate_connections_create_one_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"snapshots.sqlite3"
            one, two = PeriodicSnapshotStore(path), PeriodicSnapshotStore(path)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self.send, store=s) for s in (one, two)]
                    results = [f.result(timeout=10) for f in futures]
                self.assertEqual(sorted(status for _, status in results), [200, 202])
                self.assertEqual(sum(ack["accepted"] for ack, _ in results), 1)
            finally:
                one.close()
                two.close()

    def test_wrong_database_kind_and_overlapping_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"raw.sqlite3"
            raw = RawVibrationStore(path)
            raw.close()
            with self.assertRaisesRegex(ValueError, "separate database"):
                PeriodicSnapshotStore(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='periodic_snapshots'").fetchone())
            with self.assertRaisesRegex(ValueError, "separate database"):
                create_server("127.0.0.1", 0, raw_window_database=path, snapshot_database=path)

    def test_request_size_limit_applies_to_direct_ingestion(self):
        with self.assertRaises(data.ApiError) as error:
            self.send(samples="A"*70000)
        self.assertEqual(error.exception.status, 413)

    def test_read_permissions_and_site_scope_are_enforced(self):
        self.send()
        with self.assertRaises(data.ApiError) as error:
            self.store.list_device({**self.admin, "allowedSiteIds": ["SITE-02"]}, DEVICE)
        self.assertEqual(error.exception.code, "SITE_FORBIDDEN")
        with mock.patch.object(data, "role_policy", return_value={"permissions": ["device:read"]}):
            with self.assertRaises(data.ApiError) as error:
                self.store.list_device(self.admin, DEVICE)
            self.assertEqual(error.exception.status, 403)

    def test_real_snapshot_wal_backup_restore_preserves_queue_and_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            for key, default in operations.DB_DEFAULTS.items():
                if key in {"PERIODIC_SNAPSHOT_DB_PATH", "SHADOW_MODEL_DB_PATH"}:
                    continue
                path = project / default
                path.parent.mkdir(exist_ok=True)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE test_fixture(value INTEGER)")
            auth = project / "test-auth.json"
            auth.write_text("{}", encoding="utf-8")
            env = {"AUTH_USERS_FILE": str(auth)}
            snapshot_path = project / operations.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"]
            store = PeriodicSnapshotStore(snapshot_path)
            try:
                first, _ = self.send(store=store)
                self.send(1, store=store, quality="sensor_unavailable", samples=None, sampleCount=0)
                # Stage-1 pending is visible, not mistaken for an overdue RF66 worker.
                report = operations.inspect(project, env, DEVICE)
                component = report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]
                self.assertEqual(component["statuses"], {"queued": 1, "unavailable": 1})
                self.assertFalse(component["processingEnabled"])
                self.assertNotIn("PERIODIC_SNAPSHOT_DB_PATH:PROCESSING_BACKLOG",
                                 [item["code"] for item in report["issues"]])
                with mock.patch.object(operations, "run", return_value="a"*40):
                    backup = operations.make_backup(project, env, {"User": "test"}, project/"backup",
                                                    stopped=lambda: True)
            finally:
                store.close()
            manifest = operations.verify_backup(backup)
            self.assertEqual(manifest["formatVersion"], 2)
            self.assertIn("PERIODIC_SNAPSHOT_DB_PATH", [item["key"] for item in manifest["files"]])
            restored = project / "restored"
            self.assertTrue(operations.restore_drill(backup, restored)["verified"])
            store = PeriodicSnapshotStore(restored/"PERIODIC_SNAPSHOT_DB_PATH.sqlite3")
            try:
                ack, status = self.send(store=store)
                self.assertEqual(status, 200)
                self.assertEqual(ack["acknowledged"], first["acknowledged"])
                self.assertEqual(store.list_device(self.admin, DEVICE)["statuses"], {"queued": 1, "unavailable": 1})
            finally:
                store.close()

    def test_legacy_backup_verification_survives_new_database_requirement(self):
        # The new file is mandatory in v2 but did not exist in real v1 backups.
        from tests.test_operations import OperationsTest
        fixture = OperationsTest()
        fixture.setUp()
        try:
            backup = fixture.backup()
            path = backup / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["files"] = [f for f in manifest["files"] if f["key"] != "PERIODIC_SNAPSHOT_DB_PATH"]
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Incomplete/duplicate"):
                operations.verify_backup(backup)
            manifest["formatVersion"] = 1
            path.write_text(json.dumps(manifest))
            self.assertEqual(operations.verify_backup(backup)["formatVersion"], 1)
            self.assertTrue(operations.restore_drill(backup, Path(fixture.temp.name)/"v1-drill")["verified"])
        finally:
            fixture.doCleanups()

    def test_http_receipt_auth_read_scope_and_legacy_path_separation(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False, rf66_event_mode="events")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"/api/devices/{DEVICE}/periodic-snapshots"
        def request(method, body=None, token="demo-telemetry-ingest-token", path=endpoint):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                conn.request(method, path, json.dumps(body) if body is not None else None, headers)
                response = conn.getresponse()
                return response.status, json.load(response)
            finally:
                conn.close()
        try:
            for token in (None, "wrong-token"):
                self.assertEqual(request("POST", self.payload(), token=token)[0], 401)
            status, ack = request("POST", self.payload())
            self.assertEqual((status, ack["accepted"]), (202, 1))
            self.assertEqual(request("POST", self.payload())[0], 200)
            self.assertEqual(request("GET", token=None)[0], 401)
            status, result = request("GET", token=self.token)
            self.assertEqual(status, 200)
            self.assertEqual(result["statuses"], {"queued": 1})
            self.assertEqual(result["items"][0]["window"], self.payload()["window"])
            for store in (server.raw_vibration, server.vibration_windows):
                self.assertFalse(store.tick())
                self.assertEqual(store.db.execute("SELECT count(*) FROM vibration_windows").fetchone()[0], 0)
            self.assertEqual(server.raw_vibration.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0], 0)
            self.assertEqual(request("POST", self.payload(), path=f"/api/devices/{DEVICE}/raw-vibration-windows")[0], 400)
            legacy = {"windows": [self.payload()["window"]]}
            del legacy["windows"][0]["integrity"]
            self.assertEqual(request("POST", legacy)[0], 400)
            self.assertEqual(request("POST", legacy, path=f"/api/devices/{DEVICE}/raw-vibration-windows")[0], 202)
            self.assertEqual(request("POST", self.payload(samples="A"*70000))[0], 413)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
