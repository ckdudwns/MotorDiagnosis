"""Failure, timing and restart contracts from the PR19 review."""

from __future__ import annotations

import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from motor_diagnosis import data
from motor_diagnosis.server import AppHandler, create_server
from motor_diagnosis.telemetry_bulk import ingest_telemetry_bulk
from motor_diagnosis.xlsx_export import dataset_xlsx_bytes
from tests.test_backend_production_integrations import telemetry_payload


class Pr19RegressionTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(data.reset_runtime_state)
        self.addCleanup(data.close_runtime_state)
        self.database = str(Path(directory.name) / "runtime.sqlite3")
        data.configure_runtime_state(self.database)
        login = data.authenticate({"username": "admin", "password": "admin123"})
        self.token = login["session"]["token"]
        self.admin = data.current_user_for_token(self.token)
        self.ingest = data.telemetry_principal_for_token("demo-telemetry-ingest-token")
        self.health = data.telemetry_principal_for_token("demo-device-health-token")
        self.started = datetime.now(timezone.utc) - timedelta(seconds=120)

    def restart(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        data.configure_runtime_state(self.database)

    def point(self, sequence, seconds, vibration=1.0):
        return telemetry_payload(
            sequence,
            data.format_rfc3339(self.started + timedelta(seconds=seconds)),
            vibration=vibration,
        )

    def note(self, text="original"):
        return data.create_event_note(
            self.admin, "EV-241", {"category": "inspection", "text": text}
        )

    def events(self):
        return [e for e in data.EVENTS if e.get("source") == "backend-integration-test"]

    def open_event(self):
        for seconds in range(11):
            data.ingest_telemetry(self.ingest, self.point(seconds + 1, seconds))
        self.assertEqual(len(self.events()), 1)
        return self.events()[0]["id"]

    def event(self, event_id):
        return next(event for event in data.EVENTS if event["id"] == event_id)

    def health_report(self, status, seconds):
        return data.update_device_health(
            self.health, "DEV-01-GEN-01",
            {
                "reportedAt": data.format_rfc3339(self.started + timedelta(seconds=seconds)),
                "sensorFaults": [{"code": "adxl345_timeout", "status": status}],
            },
        )

    def test_failed_note_save_never_leaks_into_later_commit(self):
        before_notes = data.copy_payload(data.EVENT_NOTES)
        before_history = data.copy_payload(data.EVENT_NOTE_HISTORY)
        before_audit = data.copy_payload(data.AUDIT_LOGS)
        with patch.object(data._RUNTIME_STATE_STORE, "save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.note("must not survive")
        self.assertEqual(data.EVENT_NOTES, before_notes)
        self.assertEqual(data.EVENT_NOTE_HISTORY, before_history)
        self.assertEqual(data.AUDIT_LOGS, before_audit)
        saved = self.note("successful retry")
        self.restart()
        self.assertEqual([n["id"] for n in data.EVENT_NOTES], [saved["id"]])
        self.assertEqual(data.EVENT_NOTES[0]["text"], "successful retry")

    def test_failed_lifecycle_save_restores_checkpoint_and_idempotency(self):
        data.ingest_telemetry(self.ingest, self.point(1, 0))
        before = data.copy_payload(data.LIFECYCLE_CHECKPOINTS)
        with patch.object(data._RUNTIME_STATE_STORE, "save", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                data.ingest_telemetry(self.ingest, self.point(2, 10))
        self.assertEqual(data.LIFECYCLE_CHECKPOINTS, before)
        self.assertEqual(self.events(), [])
        self.assertNotIn(("DEV-01-GEN-01", 2), data.TELEMETRY_IDEMPOTENCY)
        self.assertEqual(len(data.TELEMETRY_RECORDS), 1)
        result, status = data.ingest_telemetry(self.ingest, self.point(2, 10))
        self.assertEqual(status, 201)
        self.assertFalse(result["duplicate"])
        self.assertEqual(len(self.events()), 1)
        self.restart()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(data.TELEMETRY_RECORDS), 2)

    def test_duration_uses_measurement_time_and_survives_restart(self):
        for index in range(10):
            data.ingest_telemetry(self.ingest, self.point(index + 1, index / 10))
        self.assertEqual(self.events(), [])  # 0.9 seconds is not 10 seconds.
        self.restart()
        data.ingest_telemetry(self.ingest, self.point(11, 9.999))
        self.assertEqual(self.events(), [])
        data.ingest_telemetry(self.ingest, self.point(12, 10))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["durationSec"], 10)
        self.assertEqual(self.events()[0]["occurredAt"], self.point(1, 0)["timestamp"])

    def test_sparse_and_buffered_points_use_same_elapsed_time(self):
        response = ingest_telemetry_bulk(
            self.ingest, {"items": [self.point(2, 11), self.point(1, 0)]}
        )
        self.assertEqual(response["accepted"], 2)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["sampleCount"], 2)
        self.assertEqual(self.events()[0]["durationSec"], 11)

    def test_legacy_lifecycle_checkpoint_remains_readable(self):
        lifecycle = data.AnomalyEventLifecycle()
        for seconds in (0, 1):
            lifecycle.process_point({
                "assetId": "SITE-01-GEN-01",
                "timestamp": self.point(seconds + 1, seconds)["timestamp"],
                "anomalyScore": 100,
            })
        legacy = lifecycle.snapshot()
        legacy["config"].pop("min_duration_enter_sec")
        legacy["config"].pop("min_duration_exit_sec")
        legacy["assets"]["SITE-01-GEN-01"].pop("sensorFaultBoundaryAt")
        restored = data.AnomalyEventLifecycle.from_snapshot(legacy)
        self.assertEqual(restored.snapshot()["assets"]["SITE-01-GEN-01"]["openEvent"]["status"], "open")

    def test_exit_duration_and_broken_entry_streak_use_elapsed_time(self):
        data.ingest_telemetry(self.ingest, self.point(1, 0))
        data.ingest_telemetry(self.ingest, self.point(2, 9, vibration=0.074))
        data.ingest_telemetry(self.ingest, self.point(3, 10))
        data.ingest_telemetry(self.ingest, self.point(4, 19.999))
        self.assertEqual(self.events(), [])
        data.ingest_telemetry(self.ingest, self.point(5, 20))
        self.assertEqual(len(self.events()), 1)
        for index in range(10):
            data.ingest_telemetry(self.ingest, self.point(6 + index, 21 + index / 10, vibration=0.074))
        self.assertEqual(self.events()[0]["status"], "open")
        self.restart()
        data.ingest_telemetry(self.ingest, self.point(16, 31, vibration=0.074))
        self.assertEqual(self.events()[0]["status"], "closed")
        self.assertEqual(self.events()[0]["endReason"], "score_recovered")

    def test_health_only_fault_closes_asset_event_and_survives_recovery(self):
        event_id = self.open_event()
        self.health_report("active", 12)
        event = self.event(event_id)
        self.assertEqual(event["status"], "closed")
        self.assertEqual(event["endReason"], "sensor_fault_detected")
        self.assertEqual(len(data.TELEMETRY_RECORDS), 11)
        self.restart()
        self.health_report("recovered", 15)
        self.restart()
        self.assertEqual(self.event(event_id)["status"], "closed")
        self.assertEqual(len(data.TELEMETRY_RECORDS), 11)
        self.assertTrue(all(e["status"] == "closed" for e in data.EVENTS if e.get("faultCode") == "adxl345_timeout"))
        # Recovery cannot reopen the old event through delayed/buffered data.
        data.ingest_telemetry(self.ingest, self.point(12, 14))
        self.assertEqual(len(self.events()), 1)
        data.ingest_telemetry(self.ingest, self.point(13, 16))
        data.ingest_telemetry(self.ingest, self.point(14, 26))
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(sum(e["status"] == "open" for e in self.events()), 1)

    def test_failed_health_save_rolls_back_event_and_boundary(self):
        event_id = self.open_event()
        checkpoint = data.copy_payload(data.LIFECYCLE_CHECKPOINTS)
        with patch.object(data._RUNTIME_STATE_STORE, "save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.health_report("active", 12)
        self.assertEqual(data.LIFECYCLE_CHECKPOINTS, checkpoint)
        self.assertEqual(self.event(event_id)["status"], "open")
        self.assertFalse(any(e.get("faultCode") == "adxl345_timeout" for e in data.EVENTS))
        self.health_report("active", 12)
        self.restart()
        self.assertEqual(self.event(event_id)["status"], "closed")

    def test_generated_asset_event_id_can_be_queried_and_reviewed(self):
        event_id = self.open_event()
        detail = data.event_detail_for(self.admin, event_id)
        self.assertEqual(detail["event"]["id"], event_id)
        self.assertEqual(detail["appliedRule"]["version"], "RULE-SITE-01-GEN-01-v1")
        reviewed = data.review_event(
            self.admin, event_id, {"label": "needs_review", "note": "Check the bearing"}
        )
        self.assertEqual(reviewed["event"]["id"], event_id)

    def test_sensor_event_detail_uses_sensor_rule_after_restart(self):
        self.health_report("active", 1)
        sensor_id = next(e["id"] for e in data.EVENTS if e.get("faultCode") == "adxl345_timeout")
        self.restart()
        detail = data.event_detail_for(self.admin, sensor_id)
        self.assertEqual(detail["appliedRule"]["version"], "sensor-health-v1")
        self.assertEqual(detail["appliedRule"]["type"], "device_sensor_health")
        self.assertEqual(detail["deviceSnapshot"]["id"], "DEV-01-GEN-01")
        reviewed = data.review_event(self.admin, sensor_id, {"label": "sensor_issue", "note": "Confirmed sensor fault"})
        self.assertEqual(reviewed["event"]["label"], "sensor_issue")

    def test_invalid_note_patch_is_unchanged_with_and_without_database(self):
        for durable in (True, False):
            with self.subTest(durable=durable):
                if not durable:
                    data.close_runtime_state()
                note = self.note()
                for payload in (
                    {"category": "action", "text": "   "},
                    {"category": "action", "text": "changed", "attachmentRefs": [False]},
                ):
                    before_history = data.copy_payload(data.EVENT_NOTE_HISTORY)
                    before_audit = data.copy_payload(data.AUDIT_LOGS)
                    with self.assertRaises(data.ApiError) as error:
                        data.update_event_note(self.admin, "EV-241", note["id"], payload)
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(data._event_note_for("EV-241", note["id"]), note)
                    self.assertEqual(data.EVENT_NOTE_HISTORY, before_history)
                    self.assertEqual(data.AUDIT_LOGS, before_audit)

    def test_xlsx_encodes_controls_and_preserves_literal_escape_strings(self):
        payload = self.point(1, 0)
        text = "raw\x01\x00\r\t\n_x0001_ & <sensor> \ufffe"
        payload["vibrationUnitNote"] = text
        _, status = data.ingest_telemetry(self.ingest, payload)
        self.assertEqual(status, 201)
        exported = data.dataset_export_for(self.admin, "SITE-01", "SITE-01-GEN-01")
        self.assertEqual(exported["rows"][0]["vibration_unit_note"], text)
        with ZipFile(io.BytesIO(dataset_xlsx_bytes(exported))) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ElementTree.fromstring(archive.read(name))
            xml = archive.read("xl/worksheets/sheet2.xml").decode()
        self.assertIn("raw_x0001__x0000__x000D_", xml)
        self.assertIn("_x005F_x0001_", xml)
        self.assertIn("_xFFFE_", xml)
        self.assertIn("&amp; &lt;sensor&gt;", xml)
        self.assertEqual(exported["rows"][0]["vibration_unit_note"], text)

    def test_rejection_quarantine_and_conflict_metrics_survive_immediate_restart(self):
        data.ingest_telemetry(self.ingest, self.point(1, 0))
        invalid = self.point(2, 1)
        invalid["rpm"] = "invalid"
        for payload in (invalid, self.point(1, 0, vibration=0.5)):
            with self.assertRaises(data.ApiError):
                data.ingest_telemetry(self.ingest, payload)
        before_metrics = data.copy_payload(data.TELEMETRY_METRICS)
        before_quarantine = data.copy_payload(data.QUARANTINED_DEVICE_MESSAGES)
        self.restart()
        self.assertEqual(data.TELEMETRY_METRICS, before_metrics)
        self.assertEqual(data.QUARANTINED_DEVICE_MESSAGES, before_quarantine)
        self.assertEqual(data.TELEMETRY_METRICS["requests"], 3)
        self.assertEqual(data.TELEMETRY_METRICS["rejected"], 2)
        self.assertEqual(data.TELEMETRY_METRICS["conflicts"], 1)
        self.assertEqual(len(data.TELEMETRY_RECORDS), 1)

    def test_site_summary_batches_transitions_and_reads_do_not_save(self):
        # Include telemetry in the snapshot: repeated full-state commits must
        # not scale with the 210-device summary.
        with data.STORE_LOCK:
            data.TELEMETRY_RECORDS.extend(
                {**self.point(i, i / 10), "receivedAt": data.now_iso()}
                for i in range(1000)
            )
        store = data._RUNTIME_STATE_STORE
        with patch.object(store, "save", wraps=store.save) as save:
            rows = data.dashboard_sites_summary(self.admin)
            self.assertEqual(len(rows), 65)
            self.assertLessEqual(save.call_count, 1)
        with patch.object(store, "save", wraps=store.save) as save:
            data.dashboard_sites_summary(self.admin)
            for _ in range(3):
                data.event_notes_for(self.admin, "EV-241")
                data.service_health_dependencies()
            self.assertEqual(save.call_count, 0)

    def test_storage_recovers_only_after_a_successful_commit(self):
        with patch.object(data._RUNTIME_STATE_STORE, "save", side_effect=sqlite3.OperationalError("disk full")) as save:
            for _ in range(2):
                with self.assertRaises(sqlite3.OperationalError):
                    self.note("failed")
            self.assertEqual(save.call_count, 2)
        storage_events = [e["status"] for e in data.SERVICE_HEALTH_EVENTS if e["dependencyId"] == "storage"]
        self.assertEqual(storage_events, ["failure", "failure"])
        self.note("saved")
        self.restart()
        storage_events = [e["status"] for e in data.SERVICE_HEALTH_EVENTS if e["dependencyId"] == "storage"]
        self.assertEqual(storage_events, ["failure", "failure", "recovered"])

    def test_same_second_note_order_follows_writes_not_random_ids(self):
        with patch.object(data, "now_iso", return_value="2026-09-05T12:00:00Z"):
            with patch.object(data.secrets, "token_hex", return_value="F" * 20):
                first = self.note("first")
            with patch.object(data.secrets, "token_hex", return_value="0" * 20):
                second = self.note("second")
            latest = data.event_notes_for(self.admin, "EV-241")["latestByCategory"]["inspection"]
            self.assertEqual(latest["id"], second["id"])
            data.update_event_note(self.admin, "EV-241", first["id"], {"text": "edited last"})
        self.restart()
        notes = data.event_notes_for(self.admin, "EV-241")
        self.assertEqual(notes["items"][0]["id"], first["id"])
        self.assertEqual(notes["latestByCategory"]["inspection"]["text"], "edited last")

    def test_shutdown_drains_inflight_successful_post_before_closing_database(self):
        server = create_server("127.0.0.1", 0, state_database=self.database, auto_alerts=False)
        serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
        serve_thread.start()
        entered = threading.Event()
        release = threading.Event()
        closing = threading.Event()
        db_closed = threading.Event()
        result = []
        failures = []
        original_read = AppHandler.read_json
        original_close = data.close_runtime_state

        def held_read(handler):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release request")
            return original_read(handler)

        def track_database_close():
            db_closed.set()
            original_close()

        def post():
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(
                    "POST", "/api/events/EV-241/notes",
                    json.dumps({"category": "inspection", "text": "shutdown survivor"}),
                    {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                result.append((response.status, json.loads(response.read())))
            except Exception as error:
                failures.append(error)
            finally:
                connection.close()

        def close():
            server.shutdown()
            closing.set()
            server.server_close()

        writer = threading.Thread(target=post)
        closer = threading.Thread(target=close)
        with patch.object(AppHandler, "read_json", held_read), patch(
            "motor_diagnosis.server.close_runtime_state", track_database_close
        ):
            try:
                writer.start()
                self.assertTrue(entered.wait(3))
                closer.start()
                self.assertTrue(closing.wait(3))
                self.assertFalse(db_closed.wait(0.1))
            finally:
                release.set()
                writer.join(5)
                if closer.ident is None:
                    closer.start()
                closer.join(5)
                serve_thread.join(5)
        self.assertFalse(failures)
        self.assertFalse(writer.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(result[0][0], 201)
        self.assertTrue(db_closed.is_set())
        self.restart()
        self.assertEqual(data.EVENT_NOTES[0]["text"], "shutdown survivor")
        self.assertEqual(data.EVENT_NOTES[0]["id"], result[0][1]["id"])


if __name__ == "__main__":
    unittest.main()
