"""Synthetic server decisions only; no firmware, real model or outbound services."""
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

from motor_diagnosis import data, operations
from motor_diagnosis.alerts import AlertService, DeliveryError
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.server import create_server
from motor_diagnosis.snapshot_events import MAX_TRANSITION_AGE
from motor_diagnosis.snapshot_model import SnapshotModelAdapter
from tests.test_measured_rpm import RpmSetup
from tests.test_snapshot_inference import metadata
from tests.test_periodic_snapshots import DEVICE
from tests import test_periodic_snapshots as fixtures, test_edge_state_snapshots as edge_fixtures


class SnapshotEventsTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)
        self.now = self.started.timestamp() + .5
        self.predict = mock.Mock(return_value={"score": .8})
        self.model = SnapshotModelAdapter(metadata(), self.predict)
        self.store = self.make_store()
        data.update_alert_policy(self.admin, "ALERT-POLICY-DEFAULT", {
            "enabled": True, "channels": ["web"], "severity": "critical", "cooldownSec": 0,
            "workHours": {"start": "00:00", "end": "23:59"}})

    def make_store(self, path=":memory:", *, mode="events", model=True):
        store = PeriodicSnapshotStore(path, model=self.model if model is True else model, event_mode=mode)
        store.events.clock = lambda: self.now
        self.addCleanup(store.close)
        return store

    def payload(self, slot=0, **changes):
        return fixtures.SnapshotTest.payload(self, slot, **changes)

    def step(self, slot=0, score=.8, *, payload=None, process=True, **changes):
        self.predict.return_value = {"score": score}
        payload = payload or self.payload(slot, **changes)
        self.now = data.parse_rfc3339("timestamp", payload["window"]["timestamp"]).timestamp() + .5
        ack = self.store.ingest(self.principal, DEVICE, payload)
        if process:
            self.store.tick()
            self.store.inference.tick()
            self.store.events.tick()
        return ack

    def incidents(self):
        return [e for e in data.EVENTS if e.get("source") == "snapshot"]

    def alerts(self, path=":memory:", **kwargs):
        service = AlertService(path, clock=lambda: self.now,
                               snapshot_guard=self.store.events.notification_allowed, **kwargs)
        self.addCleanup(service.close)
        return service

    def test_sparse_open_maintain_recover_and_new_incident(self):
        self.step(0)
        event = self.incidents()[0]
        original_id, original_evidence = event["id"], copy.deepcopy(event["snapshotEvidence"])
        self.step(1)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(event["status"], "open")
        self.step(2, score=0)
        self.assertEqual((event["status"], event["endReason"]), ("closed", "single_normal_snapshot"))
        self.assertEqual(event["snapshotRecoveryEvidence"]["windowIndex"], 938)
        self.assertEqual(event["snapshotEvidence"], original_evidence)
        self.assertIsNone(event["score"])
        detail = data.event_detail_for(self.admin, original_id)
        self.assertEqual(detail["context"]["points"], [])
        self.assertEqual(detail["featureSnapshot"], original_evidence)
        self.assertFalse(detail["appliedRule"]["continuousWindowsRequired"])
        self.step(3)
        self.assertEqual(len(self.incidents()), 2)
        self.assertNotEqual(self.incidents()[0]["id"], original_id)

    def test_board_anomaly_never_opens_without_server_anomaly(self):
        for index, reason in enumerate(("anomaly_enter", "anomaly_periodic", "normal_recovered", "normal_periodic")):
            self.step(score=0, payload=edge_fixtures.EdgeSnapshotTest.payload(self, index, reason))
        self.assertEqual(self.incidents(), [])
        self.step(4, payload=self.payload(4))  # NORMAL/legacy report is not an override of the server verdict.
        self.assertEqual(len(self.incidents()), 1)

    def test_duplicates_do_not_create_more_jobs_incidents_or_alerts(self):
        self.store.events.mode = "alerts"
        first, _ = self.step()
        alerts = self.alerts()
        for _ in range(3):
            ack, code = self.step()
            self.assertEqual((code, ack["accepted"], ack["acknowledged"]), (200, 0, first["acknowledged"]))
            alerts.observe_events()
            alerts.process_due()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_event_jobs").fetchone()[0], 1)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(len(alerts.list_for(self.admin)["items"]), 1)

    def test_shadow_events_and_missing_model_never_send_including_manual_test(self):
        self.store.events.mode = "shadow"
        self.step()
        self.assertEqual(self.incidents(), [])
        self.store.events.mode = "events"
        self.step(1)
        service = self.alerts()
        for is_test in (False, True):
            result = service.send(self.admin, {"eventId": self.incidents()[0]["id"], "isTest": is_test})
            self.assertEqual(result["deliveries"], [])
        service.observe_events(); service.process_due()
        self.assertEqual(service.list_for(self.admin)["items"], [])
        self.store = self.make_store(model=None, mode="alerts")
        self.step(2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0], 0)

    def test_alerts_open_and_recovery_once_while_hold_never_resends(self):
        self.store.events.mode = "alerts"
        self.step()
        service = self.alerts()
        service.observe_events(); service.process_due()
        self.step(1)
        service.observe_events(); service.process_due()
        self.assertEqual(len(service.list_for(self.admin)["items"]), 1)
        self.step(2, score=0)
        for _ in range(2): service.observe_events(); service.process_due()
        rows = service.list_for(self.admin)["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["event"]["snapshotTransition"] for r in rows}, {"open", "closed"})
        self.assertTrue(all(r["status"] == "sent" for r in rows))
        self.assertEqual(len({r["eventId"] for r in rows}), 1)

    def test_quality_failures_pending_and_missing_input_mark_unknown_not_recovery(self):
        self.step()
        event = self.incidents()[0]
        self.step(1, score=0, quality="fifo_overrun", samples=None, sampleCount=0)
        self.assertEqual((event["status"], event["snapshotObservation"]), ("open", "unknown"))
        self.step(2, process=False)
        self.store.events.tick()
        self.assertEqual(event["snapshotObservation"], "unknown")
        self.store.tick(); self.store.inference.tick(); self.store.events.tick()
        self.assertEqual(event["snapshotObservation"], "observing")
        self.now += 361
        self.store.events.tick()
        self.assertEqual((event["status"], event["snapshotObservation"]), ("open", "unknown"))

    def test_late_normal_backlog_cannot_close_newer_anomaly(self):
        self.step(2)
        self.step(1, score=0)
        self.assertEqual(self.incidents()[0]["status"], "open")
        ignored = self.store.db.execute("SELECT status,reason FROM snapshot_event_jobs ORDER BY ordinal DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(ignored), ("ignored", "SUPERSEDED_OR_LATE_INPUT"))

    def test_old_and_future_completed_backlogs_cannot_open_events(self):
        self.step(process=False)
        self.store.tick(); self.store.inference.tick()
        self.now += MAX_TRANSITION_AGE
        self.store.events.tick()
        self.assertEqual(self.incidents(), [])
        self.step(1, process=False)
        self.store.tick(); self.store.inference.tick()
        self.now -= 7
        self.store.events.tick()
        self.assertEqual(self.incidents(), [])

    def test_newer_unprocessed_snapshot_blocks_old_transition_and_delivery(self):
        self.store.events.mode = "alerts"
        self.step()
        event = self.incidents()[0]
        service = self.alerts()
        service.observe_events()  # Durable pending outbox entry, not yet delivered.
        self.step(1, score=0, process=False)
        service.process_due()  # Guard must inspect the inbox even before events.tick.
        self.assertEqual(service.list_for(self.admin)["items"][0]["lastError"], "SNAPSHOT_DELIVERY_DISABLED_OR_STALE")
        self.store.events.tick()
        self.assertEqual(event["status"], "open")
        self.assertEqual(event["snapshotObservation"], "unknown")

    def test_model_change_does_not_close_old_model_incident(self):
        self.step()
        old = self.incidents()[0]
        self.store.inference.model = SnapshotModelAdapter(metadata(modelVersion="test-v2"), self.predict)
        self.step(1, score=0)
        self.assertEqual((old["status"], old["snapshotObservation"]), ("open", "unknown"))
        self.step(2)
        self.assertEqual(len(self.incidents()), 2)
        self.step(3, score=0)
        self.assertEqual(old["status"], "open")
        self.assertEqual(self.incidents()[0]["status"], "closed")

    def test_new_boot_is_a_single_observation_not_continuity_requirement(self):
        self.step()
        self.step(1, score=0, bootId="e" * 32, windowIndex=0, startUptimeUs=123)
        self.assertEqual(self.incidents()[0]["status"], "closed")
        self.assertEqual(self.incidents()[0]["snapshotRecoveryEvidence"]["bootId"], "e" * 32)

    def test_mapping_change_blocks_pending_delivery_and_incident_recovery(self):
        self.store.events.mode = "alerts"
        self.step()
        service = self.alerts()
        service.observe_events()
        with data.STORE_LOCK:
            device = next(d for d in data.DEVICES if d["id"] == DEVICE)
            device["mappingStatus"] = "inactive"
        service.process_due()
        self.store.events.tick()
        self.assertEqual(self.incidents()[0]["snapshotObservation"], "unknown")
        self.assertEqual(service.list_for(self.admin)["items"][0]["status"], "failed")

    def test_receipt_mode_and_binding_are_pinned_and_not_promoted(self):
        self.step(process=False)
        self.store.events.mode = "alerts"
        self.store.tick(); self.store.inference.tick(); self.store.events.tick()
        self.assertEqual(self.incidents(), [])
        self.store.events.mode = "events"
        self.step(1)
        self.store.events.mode = "alerts"
        self.step(2)
        service = self.alerts()
        service.observe_events(); service.process_due()
        self.assertEqual(service.list_for(self.admin)["items"], [])

    def test_retry_uses_same_delivery_id_and_mode_off_stops_it(self):
        self.store.events.mode = "alerts"
        self.step()
        calls = []
        def adapter(row):
            calls.append(row["id"])
            if len(calls) == 1: raise DeliveryError("TRANSIENT")
        data.update_alert_policy(self.admin, "ALERT-POLICY-DEFAULT", {"channels": ["webhook"], "recipients": ["synthetic@example.com"]})
        service = self.alerts(adapters={"webhook": adapter})
        service.observe_events(); service.process_due()
        self.now += 2
        service.process_due()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1)
        self.step(1, score=0)
        service.observe_events()
        self.store.events.mode = "events"
        service.process_due()
        self.assertEqual(len(calls), 2)

    def test_manual_resolution_is_audited_not_normal_and_preserves_review(self):
        self.store.events.mode = "alerts"
        self.step()
        event = self.incidents()[0]
        event.update(note="사용자 검수 메모", reviewed=True, label="normal")
        self.step(1)
        self.assertEqual(event["note"], "사용자 검수 메모")
        resolved = self.store.events.resolve(self.admin, event["id"], {"reason": "모델 교체 점검"})
        self.assertEqual(resolved["snapshotTransition"], "manual_closed")
        self.assertEqual(event["snapshotObservation"], "operator_resolved")
        self.assertFalse(self.store.events.notification_allowed(event))
        self.store.events.resolve(self.admin, event["id"], {"reason": "중복 요청"})
        audit = [a for a in data.AUDIT_LOGS if a["action"] == "snapshot_event_resolved"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["reason"], "모델 교체 점검")
        self.assertEqual(event["note"], "사용자 검수 메모")

    def test_manual_resolution_permissions_site_scope_and_reason(self):
        self.step()
        event_id = self.incidents()[0]["id"]
        for user, reason in (({**self.admin, "role": "viewer", "permissions": []}, "점검"),
                             ({**self.admin, "allowedSiteIds": ["SITE-02"]}, "점검"),
                             (self.admin, ""), (self.admin, "x" * 501)):
            with self.subTest(reason=reason[:10]), self.assertRaises(data.ApiError):
                self.store.events.resolve(user, event_id, {"reason": reason})
        self.assertEqual(self.incidents()[0]["status"], "open")

    def test_projection_retry_preserves_review_and_does_not_duplicate(self):
        self.step(process=False)
        self.store.tick(); self.store.inference.tick()
        with mock.patch.object(self.store.events, "dispatch", side_effect=RuntimeError("runtime state failure")):
            with self.assertRaises(RuntimeError): self.store.events.tick()
        saved = self.store.db.execute("SELECT projected,revision FROM snapshot_incidents").fetchone()
        self.assertEqual(tuple(saved), (0, 1))
        self.assertEqual(self.incidents(), [])
        self.store.events.tick()
        self.assertEqual(len(self.incidents()), 1)
        self.incidents()[0]["note"] = "검수 유지"
        with self.store.db: self.store.db.execute("UPDATE snapshot_incidents SET projected=0")
        self.store.events.dispatch()
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0]["note"], "검수 유지")

    def test_atomic_event_transaction_rolls_back_and_retries(self):
        self.step(process=False)
        self.store.tick(); self.store.inference.tick()
        save = self.store.events._save
        def fail(event):
            save(event)
            raise sqlite3.OperationalError("commit simulation")
        with mock.patch.object(self.store.events, "_save", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError): self.store.events.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM snapshot_event_jobs").fetchone()[0], "pending")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0], 0)
        self.store.events.tick()
        self.assertEqual(len(self.incidents()), 1)

    def test_corrupted_result_does_not_become_an_event(self):
        self.step(process=False)
        self.store.tick(); self.store.inference.tick()
        result = json.loads(self.store.db.execute("SELECT result FROM periodic_snapshots").fetchone()[0])
        result["verdict"] = False
        with self.store.db: self.store.db.execute("UPDATE periodic_snapshots SET result=?", (json.dumps(result),))
        self.store.events.tick()
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.store.db.execute("SELECT reason FROM snapshot_event_jobs").fetchone()[0], "MODEL_RESULT_INVALID")

    def test_schema2_upgrade_does_not_schedule_historical_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.sqlite3"
            with closing(PeriodicSnapshotStore(path, model=self.model)) as old:
                old.ingest(self.principal, DEVICE, self.payload())
                old.tick(); old.inference.tick()
                original = old.db.execute("SELECT result FROM periodic_snapshots").fetchone()[0]
                old.db.executescript("DROP TABLE snapshot_event_jobs; DROP TABLE snapshot_incidents; UPDATE periodic_snapshot_schema SET version=2;")
            with closing(PeriodicSnapshotStore(path, model=self.model, event_mode="alerts")) as restored:
                restored.events.clock = lambda: self.now
                restored.events.tick()
                self.assertEqual(restored.db.execute("SELECT count(*) FROM snapshot_event_jobs").fetchone()[0], 0)
                self.assertEqual(restored.db.execute("SELECT result FROM periodic_snapshots").fetchone()[0], original)
                self.assertEqual(restored.ingest(self.principal, DEVICE, self.payload())[1], 200)
                self.assertEqual(self.incidents(), [])

    def test_snapshot_backup_and_restart_preserve_incident_and_event_jobs(self):
        self.step()
        event_id = self.incidents()[0]["id"]
        self.step(1, score=0, process=False)
        self.store.tick(); self.store.inference.tick()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "backup.sqlite3"
            with closing(sqlite3.connect(path)) as target: self.store.db.backup(target)
            with closing(PeriodicSnapshotStore(path, model=self.model)) as restored:
                restored.events.clock = lambda: self.now
                restored.events.tick()
                self.assertEqual(len(self.incidents()), 1)
                self.assertEqual((self.incidents()[0]["id"], self.incidents()[0]["status"]), (event_id, "closed"))

    def test_invalid_event_mode_is_rejected_before_database_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "no-create.sqlite3"
            with self.assertRaises(ValueError): PeriodicSnapshotStore(path, event_mode="enabled")
            self.assertFalse(path.exists())

    def test_no_alert_guard_fails_closed_for_snapshot_source(self):
        self.store.events.mode = "alerts"
        self.step()
        with closing(AlertService(clock=lambda: self.now)) as service:
            service.observe_events(); service.process_due()
            self.assertEqual(service.list_for(self.admin)["items"], [])

    def test_alert_outbox_restart_does_not_resend_a_sent_transition(self):
        self.store.events.mode = "alerts"
        self.step()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "alerts.sqlite3")
            for _ in range(2):
                with closing(AlertService(path, clock=lambda: self.now,
                        snapshot_guard=self.store.events.notification_allowed)) as service:
                    service.observe_events(); service.process_due()
                    rows = service.list_for(self.admin)["items"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["attemptCount"], 1)

    def test_event_capacity_is_explicit_and_inspect_reports_projection_backlog(self):
        with mock.patch("motor_diagnosis.snapshot_events.MAX_INCIDENTS", 0):
            self.step()
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.store.db.execute("SELECT reason FROM snapshot_event_jobs").fetchone()[0], "INCIDENT_CAPACITY")
        self.step(1)
        with self.store.db: self.store.db.execute("UPDATE snapshot_incidents SET projected=0")
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            target = project / operations.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"]
            target.parent.mkdir(parents=True)
            with closing(sqlite3.connect(target)) as db: self.store.db.backup(db)
            report = operations.inspect(project, {"SNAPSHOT_EVENT_MODE": "events"}, DEVICE, now=self.now)
            entry = report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]
            self.assertEqual(entry["incidents"], {"open": 1})
            self.assertEqual(entry["eventJobs"], {"ignored": 1, "processed": 1})
            self.assertEqual(entry["unprojectedIncidents"], 1)
            self.assertIn("PERIODIC_SNAPSHOT_EVENT_PROJECTION_PENDING", [issue["code"] for issue in report["issues"]])

    def test_alert_state_lookup_has_a_device_measured_time_index(self):
        plan = self.store.db.execute("EXPLAIN QUERY PLAN SELECT * FROM periodic_snapshots WHERE device=? "
            "ORDER BY captured DESC,late ASC,ordinal DESC LIMIT 1", (DEVICE,)).fetchall()
        details = " ".join(r[3] for r in plan)
        self.assertIn("snapshot_device_latest", details)
        self.assertNotIn("TEMP B-TREE", details)

    def test_server_worker_projects_events_even_when_delivery_worker_disabled(self):
        self.started = datetime.now(timezone.utc) - timedelta(seconds=2)
        server = create_server("127.0.0.1", 0, auto_alerts=False, snapshot_model=self.model)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(http.client.HTTPConnection(*server.server_address, timeout=4)) as conn:
                conn.request("POST", f"/api/devices/{DEVICE}/periodic-snapshots", json.dumps(self.payload()),
                    {"Authorization": "Bearer demo-telemetry-ingest-token", "Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 202); response.read()
                deadline = time.monotonic() + 5
                while not self.incidents() and time.monotonic() < deadline: time.sleep(.02)
                self.assertEqual(len(self.incidents()), 1)
                event_id = self.incidents()[0]["id"]
                conn.request("GET", f"/api/anomaly/events/{event_id}", headers={"Authorization": "Bearer " + self.token})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                detail = json.load(response)
                self.assertEqual(detail["event"]["source"], "snapshot")
                self.assertEqual(detail["featureSnapshot"]["digest"], detail["event"]["snapshotEvidence"]["digest"])
                self.assertEqual(detail["context"]["points"], [])
                conn.request("POST", f"/api/events/{event_id}/snapshot-resolve", json.dumps({"reason": "API 경로 검증"}),
                    {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["snapshotTransition"], "manual_closed")
        finally:
            server.shutdown(); server.server_close(); thread.join()
