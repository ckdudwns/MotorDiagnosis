"""Synthetic lifecycle and delivery tests; no hardware or external notifications."""
import json
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

from motor_diagnosis import data
from motor_diagnosis.alerts import AlertService, DeliveryError
from motor_diagnosis.raw_vibration import RawVibrationStore
from motor_diagnosis.rf66_events import MAX_AGE
from tests.test_measured_rpm import RpmSetup
from tests.test_rf66 import FakeRF
from tests import test_raw_vibration as raw_tests

DEVICE = raw_tests.DEVICE


class LifecycleTest(RpmSetup):
    window = raw_tests.RawWindowTest.window
    send = raw_tests.RawWindowTest.send

    def setUp(self):
        super().setUp()
        self.now = self.started.timestamp()
        self.model = FakeRF()
        self.store = RawVibrationStore(model=self.model, event_mode="events")
        self.store.events.clock = lambda: self.now
        self.addCleanup(self.store.close)
        data.update_alert_policy(self.admin, "ALERT-POLICY-DEFAULT", {
            "enabled": True, "channels": ["web"], "severity": "critical", "cooldownSec": 0,
            "workHours": {"start": "00:00", "end": "23:59"}})

    def step(self, index, score=.8, **changes):
        self.model.score = score
        w = self.window(index, **changes)
        self.now = data.parse_rfc3339("timestamp", w["timestamp"]).timestamp() + .64
        self.send(w)
        self.store.tick()
        self.store.events.dispatch()
        return self.store.list_device(self.admin, DEVICE)["items"][0]["analysis"]

    def events(self):
        return [e for e in data.EVENTS if e.get("source") == "rf66"]

    def alerts(self, path=":memory:", adapters=None):
        service = AlertService(path, clock=lambda: self.now, adapters=adapters,
                               rf66_guard=self.store.events.notification_allowed)
        self.addCleanup(service.close)
        return service

    def open_event(self):
        for i in range(3): self.step(i)
        return self.events()[0]

    def test_open_hold_recover_and_new_incident(self):
        self.step(0); self.step(1)
        self.assertEqual(self.events(), [])
        self.step(2)
        event_id = self.events()[0]["id"]
        self.step(3); self.step(4, score=0); self.step(5, score=0)
        self.assertEqual(self.events()[0]["status"], "open")
        self.step(6, score=0)
        self.assertEqual(self.events()[0]["status"], "closed")
        self.assertEqual(self.events()[0]["id"], event_id)
        self.assertEqual(self.events()[0]["endReason"], "three_normal_windows")
        for i in range(7,10): self.step(i)
        self.assertEqual(len(self.events()), 2)
        self.assertNotEqual(self.events()[0]["id"], event_id)
        self.assertIsNone(self.events()[0]["score"])

    def test_time_limits_match_confirmation_for_open_and_recovery(self):
        for interval, accepted in ((629999, False), (630000, True),
                                   (655401, True), (656241, True),
                                   (670000, True), (670001, False), (3000, False)):
            with self.subTest(interval=interval):
                self.store = RawVibrationStore(model=self.model, event_mode="events")
                self.store.events.clock = lambda: self.now
                self.addCleanup(self.store.close)
                for i in range(6):
                    result = self.step(i, score=.8 if i < 3 else 0,
                        startUptimeUs=i*interval,
                        timestamp=(self.started+timedelta(microseconds=i*interval)).isoformat())
                    self.assertEqual(result["confirmation"]["validWindows"],
                                     result["eventLifecycle"]["validWindows"])
                    if i == 2:
                        self.assertEqual(bool(result["eventLifecycle"]["activeEventId"]), accepted)
                records = self.store.db.execute("SELECT payload FROM rf66_incidents").fetchall()
                self.assertEqual(len(records), 1 if accepted else 0)
                if accepted:
                    event = json.loads(records[0][0])
                    self.assertEqual(event["status"], "closed")
                    self.assertEqual(event["rf66Policy"]["startIntervalRangeUs"], [630000, 670000])

    def test_invalid_gap_boot_and_missing_input_never_resolve(self):
        self.open_event()
        self.step(3, score=0); self.step(4, score=0)
        self.step(5, quality="sensor_unavailable", sampleCount=0, samples="")
        self.assertEqual(self.events()[0]["rf66Observation"], "unknown")
        self.step(6, score=0); self.step(8, score=0)
        self.assertEqual(self.events()[0]["status"], "open")
        self.step(0, score=0, bootId="d"*32)
        self.assertEqual(self.events()[0]["status"], "open")
        self.now += MAX_AGE+1
        self.store.events.dispatch()
        self.assertEqual(self.events()[0]["status"], "open")
        self.assertEqual(self.events()[0]["rf66Observation"], "unknown")

    def test_shadow_and_events_modes_never_send_even_manual_test(self):
        self.store.events.mode = "shadow"
        for i in range(3): self.step(i)
        self.assertEqual(self.events(), [])
        self.store.events.mode = "events"
        self.step(3); self.step(4)
        self.assertEqual(self.events(), [])
        self.step(5)
        service = self.alerts()
        event = self.events()[0]
        for is_test in (False, True):
            result = service.send(self.admin, {"eventId": event["id"], "isTest": is_test})
            self.assertEqual(result["deliveries"], [])
        service.observe_events(); service.process_due()
        self.assertEqual(service.list_for(self.admin)["items"], [])

    def test_alerts_open_and_recovery_once_each(self):
        self.store.events.mode = "alerts"
        event = self.open_event()
        service = self.alerts()
        for _ in range(2): service.observe_events(); service.process_due()
        rows = service.list_for(self.admin)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "sent")
        self.step(3); service.observe_events()
        for i in range(4,7): self.step(i, score=0)
        for _ in range(2): service.observe_events(); service.process_due()
        rows = service.list_for(self.admin)["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["event"]["rf66Transition"] for r in rows}, {"open", "closed"})
        self.assertTrue(all(r["eventId"] == event["id"] for r in rows))

    def test_delivery_failure_retries_same_id_and_mode_off_blocks_pending(self):
        self.store.events.mode = "alerts"
        calls = []
        def adapter(row):
            calls.append(row["id"])
            if len(calls) == 1: raise DeliveryError("TRANSIENT")
        data.update_alert_policy(self.admin, "ALERT-POLICY-DEFAULT", {"channels": ["webhook"], "recipients": ["test@example.com"]})
        event = self.open_event()
        service = self.alerts(adapters={"webhook": adapter})
        service.send(self.admin, {"eventId": event["id"]})
        service.process_due(); self.now += 2; service.process_due()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(service.list_for(self.admin)["items"][0]["status"], "sent")
        for i in range(3,6): self.step(i, score=0)
        service.observe_events()
        self.store.events.mode = "events"
        service.process_due()
        self.assertEqual(len(calls), 2)
        self.assertIn("RF66_DELIVERY_DISABLED_OR_STALE", [r["lastError"] for r in service.list_for(self.admin)["items"]])

    def test_stale_backlog_does_not_create_events(self):
        for i in range(3):
            self.send(self.window(i))
            self.now = self.started.timestamp()+100
            self.store.tick()
        self.store.events.dispatch()
        self.assertEqual(self.events(), [])

    def test_queued_alert_stale_or_model_changed_is_not_delivered(self):
        self.store.events.mode = "alerts"
        event = self.open_event()
        service = self.alerts()
        service.send(self.admin, {"eventId": event["id"]})
        self.model.checksum = "sha256:"+"b"*64
        self.assertFalse(self.store.events.notification_allowed(event))
        self.model.checksum = event["modelVersion"]
        self.now += MAX_AGE+1
        service.process_due()
        self.assertEqual(service.list_for(self.admin)["items"][0]["status"], "failed")

    def test_window_write_failure_rolls_back_incident_and_retries_once(self):
        self.step(0); self.step(1)
        self.send(self.window(2))
        self.store.db.execute("CREATE TEMP TRIGGER fail_rf BEFORE UPDATE OF result ON vibration_windows BEGIN SELECT RAISE(ABORT,'disk'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.tick()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0], 0)
        self.store.db.execute("DROP TRIGGER fail_rf")
        self.store.tick(); self.store.events.dispatch(); self.store.events.dispatch()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.send(self.window(2))[1], 200)
        self.assertFalse(self.store.tick())

    def test_projection_retry_and_review_preservation(self):
        event = self.open_event()
        data.configure_runtime_state(":memory:")
        data.review_event(self.admin, event["id"], {"label": "normal_false_positive", "note": "operator evidence"})
        self.step(3, score=0); self.step(4, score=0)
        self.model.score = 0
        self.send(self.window(5)); self.store.tick()
        with mock.patch.object(data._RUNTIME_STATE_STORE, "save", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError): self.store.events.dispatch()
        self.assertEqual(self.events()[0]["status"], "open")
        self.store.events.dispatch()
        self.assertEqual(self.events()[0]["status"], "closed")
        self.assertEqual(self.events()[0]["label"], "normal_false_positive")

    def test_event_detail_uses_rf66_evidence_not_telemetry_rule(self):
        event = self.open_event()
        detail = data.event_detail_for(self.admin, event["id"])
        self.assertEqual(detail["appliedRule"]["policyId"], "rf66-event-lifecycle-v1")
        self.assertEqual(len(detail["featureSnapshot"]["features66"]), 66)
        self.assertEqual(detail["context"]["points"], [])

    def test_restart_preserves_incident_and_normal_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"raw.db"
            first = RawVibrationStore(path, model=self.model, event_mode="events")
            first.events.clock = lambda: self.now
            try:
                for i in range(4):
                    self.model.score = .8 if i < 3 else 0
                    self.send(self.window(i), store=first); first.tick()
                first.events.dispatch()
                identifier = self.events()[0]["id"]
            finally: first.close()
            second = RawVibrationStore(path, model=self.model, event_mode="events")
            second.events.clock = lambda: self.now
            try:
                for i in (4,5): self.send(self.window(i), store=second); second.tick()
                second.events.dispatch()
                self.assertEqual(len(self.events()), 1)
                self.assertEqual(self.events()[0]["id"], identifier)
                self.assertEqual(self.events()[0]["status"], "closed")
            finally: second.close()

    def test_unknown_mode_fails_closed(self):
        with self.assertRaises(ValueError): RawVibrationStore(event_mode="true")

    def test_replaced_model_cannot_clear_old_incident_and_operator_can_resolve(self):
        event = self.open_event()
        self.model.checksum = "sha256:"+"b"*64
        for i in range(3,6): self.step(i, score=0)
        self.assertEqual(self.events()[0]["status"], "open")
        self.assertEqual(self.events()[0]["rf66Observation"], "unknown")
        outsider = {**self.admin, "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError):
            self.store.events.resolve(outsider, event["id"], {"reason": "review"})
        with self.assertRaises(data.ApiError):
            self.store.events.resolve(self.admin, event["id"], {"reason": ""})
        self.store.events.resolve(self.admin, event["id"], {"reason": "model changed; inspected"})
        self.store.events.dispatch()
        self.assertEqual(self.events()[0]["endReason"], "operator_resolution")
        self.assertFalse(self.store.events.notification_allowed(self.events()[0]))
        for i in range(6,9): self.step(i)
        self.assertEqual(len(self.events()), 2)
