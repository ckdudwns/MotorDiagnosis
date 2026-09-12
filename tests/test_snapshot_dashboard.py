"""Render actual persisted snapshot responses with the shipped browser script."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis import data
from motor_diagnosis.alerts import AlertService
from motor_diagnosis.snapshot_model import SnapshotModelAdapter
from motor_diagnosis.pump_event_model import PumpEventModel
from tests.test_measured_rpm import RpmSetup
from tests import test_periodic_snapshots as fixtures
from tests import test_edge_feature_snapshots as feature_fixtures
from tests import test_pump_event_model as pump_fixtures
from tests.test_snapshot_inference import metadata


class SnapshotDashboardTest(RpmSetup):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the shipped UI")
    def test_feature_store_compatibility_and_alert_flags_render_in_all_modes(self):
        self.base = int(datetime.now(timezone.utc).timestamp() - 120) // 60 * 60
        cases = []
        for mode in ("shadow", "events", "alerts"):
            for compatibility in ("input_contract_mismatch", "scope_mismatch", "not_configured", "ready"):
                with self.subTest(mode=mode, compatibility=compatibility):
                    model = None
                    if compatibility in ("input_contract_mismatch", "scope_mismatch"):
                        model = PumpEventModel(pump_fixtures.ARTIFACT, pump_fixtures.CHECKSUM,
                                               pump_fixtures.STREAM, pump_fixtures.SCOPE)
                    elif compatibility == "ready":
                        model = SnapshotModelAdapter(feature_fixtures.model_metadata(), lambda *_: {"score": .8})
                    with closing(PeriodicSnapshotStore(model=model, event_mode=mode)) as store:
                        request = feature_fixtures.EdgeFeatureSnapshotTest.payload(self)
                        if compatibility == "scope_mismatch":
                            request["window"]["sensorId"] = "SENSOR-03"
                        ack, status = store.ingest(self.principal, pump_fixtures.DEVICE, request)
                        self.assertEqual(status, 202)
                        self.assertTrue(ack["acknowledged"][0]["durablyStored"])
                        store.tick()
                        store.inference.tick()
                        response = store.list_device(self.admin, pump_fixtures.DEVICE)
                        self.assertEqual(response["modelCompatibility"]["status"], compatibility)
                        self.assertEqual(response["inferenceEnabled"], compatibility == "ready")
                        self.assertEqual(response["affectsAlerts"], compatibility == "ready" and mode == "alerts")
                        self.assertEqual(response["items"][0]["receivedAt"], ack["acknowledged"][0]["receivedAt"])
                        if compatibility != "ready":
                            self.assertIsNone(response["items"][0]["analysis"]["verdict"])
                            self.assertEqual(store.db.execute("SELECT count(*) FROM snapshot_inference_jobs").fetchone()[0], 0)
                            self.assertEqual(store.db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0], 0)
                        cases.append({"response": response, "compatibility": compatibility})
        result = subprocess.run(
            [shutil.which("node"), "tests/snapshot_feature_dashboard_fixture.mjs"],
            cwd=Path(__file__).resolve().parents[1], input=json.dumps(cases),
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_server_query_clock_advances_without_rewriting_receipt(self):
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as folder:
            store = PeriodicSnapshotStore(Path(folder) / "snapshot.sqlite3")
            try:
                payload = fixtures.SnapshotTest.payload(self)
                ack, _ = store.ingest(self.principal, fixtures.DEVICE, payload)
                result = store.list_device(self.admin, fixtures.DEVICE)
                queried = datetime.fromisoformat(result["queriedAt"].replace("Z", "+00:00"))
                self.assertLess(abs((datetime.now(timezone.utc) - queried).total_seconds()), 5)
                self.assertEqual(result["items"][0]["receivedAt"], ack["acknowledged"][0]["receivedAt"])
            finally:
                store.close()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the shipped UI")
    def test_store_contract_and_browser_states(self):
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.sqlite3"
            store = PeriodicSnapshotStore(path)
            try:
                store.ingest(self.principal, fixtures.DEVICE, fixtures.SnapshotTest.payload(self))
                store.tick()  # Historical waiting_model is not reassigned at restart.
            finally:
                store.close()
            store = PeriodicSnapshotStore(path, model=SnapshotModelAdapter(metadata(), lambda *_: {"score": .75}))
            try:
                for slot, changes in [(1, {}), (2, {"quality": "fifo_overrun"}), (3, {})]:
                    store.ingest(self.principal, fixtures.DEVICE, fixtures.SnapshotTest.payload(self, slot, **changes))
                    if slot == 1:
                        store.tick()
                        store.inference.tick()
                payload = store.list_device(self.admin, fixtures.DEVICE)
            finally:
                store.close()
        result = subprocess.run(
            [shutil.which("node"), "tests/test_snapshot_dashboard.mjs", "--store-fixture"],
            cwd=Path(__file__).resolve().parents[1], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the shipped UI")
    def test_real_event_detail_and_sent_alerts_render_without_rf66_assumptions(self):
        self.started = datetime.now(timezone.utc) - timedelta(hours=1)
        score, clock = [.8], [self.started.timestamp() + .5]
        store = PeriodicSnapshotStore(model=SnapshotModelAdapter(metadata(), lambda *_: {"score": score[0]}), event_mode="alerts")
        store.events.clock = lambda: clock[0]
        alerts = AlertService(clock=lambda: clock[0], snapshot_guard=store.events.notification_allowed)
        data.update_alert_policy(self.admin, "ALERT-POLICY-DEFAULT", {"enabled": True, "channels": ["web"],
            "severity": "critical", "cooldownSec": 0, "workHours": {"start": "00:00", "end": "23:59"}})
        try:
            for slot in (0, 1):
                score[0] = .8 if slot == 0 else 0
                clock[0] = self.started.timestamp() + slot * 300 + .5
                store.ingest(self.principal, fixtures.DEVICE, fixtures.SnapshotTest.payload(self, slot))
                store.tick(); store.inference.tick(); store.events.tick()
                alerts.observe_events(); alerts.process_due()
            event = next(e for e in data.EVENTS if e.get("source") == "snapshot")
            payload = {"snapshots": store.list_device(self.admin, fixtures.DEVICE),
                "detail": data.event_detail_for(self.admin, event["id"]),
                "reviews": data.event_reviews_for(self.admin, event["id"], page=1, size=20),
                "notes": data.event_notes_for(self.admin, event["id"]),
                "alerts": alerts.list_for(self.admin)["items"]}
        finally:
            alerts.close(); store.close()
        result = subprocess.run([shutil.which("node"), "tests/snapshot_event_dashboard_fixture.mjs"],
            cwd=Path(__file__).resolve().parents[1], input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
