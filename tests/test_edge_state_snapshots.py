"""IoT-reported state changes are delivery metadata, never server RF verdicts."""
import copy
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading

from motor_diagnosis import data, operations
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.server import create_server
from motor_diagnosis.transmission_policy import EDGE_SNAPSHOT_POLICY_ID, SNAPSHOT_POLICY_ID
from tests.test_measured_rpm import RpmSetup
from tests import test_periodic_snapshots as fixtures

DEVICE = fixtures.DEVICE
# Explicit expectations rather than copying the production mapping.
REPORTS = {
    "normal_periodic": ("NORMAL", "periodic", 300, 0, 5),
    "anomaly_enter": ("ANOMALY_ACTIVE", "immediate", 0, 3, 0),
    "anomaly_periodic": ("ANOMALY_ACTIVE", "periodic", 10, 19, 0),
    "normal_recovered": ("NORMAL", "immediate", 0, 0, 5),
}


class EdgeSnapshotTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.started = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        self.store = PeriodicSnapshotStore()
        self.addCleanup(self.store.close)

    def payload(self, index=0, reason="normal_periodic", *, transmission=None, **changes):
        payload = fixtures.SnapshotTest.payload(self, 0, **{
            "windowIndex": index, "startUptimeUs": index * 660000,
            "timestamp": (self.started + timedelta(seconds=index * .66)).isoformat(),
            **changes,
        })
        state, mode, interval, anomaly, normal = REPORTS[reason]
        payload["transmission"] = {
            "policyId": EDGE_SNAPSHOT_POLICY_ID, "state": state, "mode": mode,
            "intervalSec": interval, "reason": reason,
            "anomalyCount": anomaly, "normalCount": normal,
            **(transmission or {}),
        }
        return payload

    def send(self, index=0, reason="normal_periodic", *, payload=None, **changes):
        return self.store.ingest(self.principal, DEVICE,
                                 payload if payload is not None else self.payload(index, reason, **changes))

    def listing(self):
        return self.store.list_device(self.admin, DEVICE)

    def assert_rejected(self, payload):
        with self.assertRaises(data.ApiError) as error:
            self.send(payload=payload)
        self.assertEqual((error.exception.status, error.exception.code), (400, "INVALID_PERIODIC_SNAPSHOT"))

    def test_all_four_reports_saved_unchanged_without_model_decision(self):
        for index, reason in ((0, "normal_periodic"), (3, "anomaly_enter"),
                              (19, "anomaly_periodic"), (24, "normal_recovered"),
                              (455, "normal_periodic")):
            payload = self.payload(index, reason)
            ack, status = self.send(payload=payload)
            self.assertEqual((status, ack["accepted"], ack["policyId"]), (202, 1, EDGE_SNAPSHOT_POLICY_ID))
            stored = self.store.db.execute("SELECT body FROM periodic_snapshots WHERE idx=?", (index,)).fetchone()[0]
            self.assertEqual(json.loads(stored), payload)
        result = self.listing()
        self.assertEqual(result["statuses"], {"queued": 5})
        self.assertFalse(result["processingEnabled"])
        self.assertFalse(result["boardStateVerifiedByServer"])
        for item in result["items"]:
            self.assertEqual(item["analysis"], {
                "status": "queued", "reason": "INFERENCE_NOT_ENABLED", "verdict": None, "affectsAlerts": False,
            })

    def test_report_cadence_is_not_zero_after_immediate_transitions(self):
        for index, reason, cadence in ((0, "normal_periodic", 300), (3, "anomaly_enter", 10),
                                       (19, "anomaly_periodic", 10), (24, "normal_recovered", 300)):
            self.send(index, reason)
            result = self.listing()
            self.assertEqual(result["intervalSec"], cadence)
            self.assertEqual(result["latestTransmission"], self.payload(index, reason)["transmission"])
            self.assertEqual(result["boardStateSource"], "device_report")

    def test_empty_listing_advertises_both_contracts_without_claiming_board_state(self):
        result = self.listing()
        self.assertIsNone(result["latestTransmission"])
        self.assertEqual(result["preferredPolicyId"], EDGE_SNAPSHOT_POLICY_ID)
        policies = {p["policyId"]: p for p in result["transmissionPolicies"]}
        self.assertIn(SNAPSHOT_POLICY_ID, policies)
        edge = policies[EDGE_SNAPSHOT_POLICY_ID]
        self.assertEqual((edge["enterConsecutiveWindows"], edge["recoveryConsecutiveWindows"]), (3, 5))
        self.assertEqual((edge["normalIntervalSec"], edge["anomalyIntervalSec"]), (300, 10))
        self.assertFalse(edge["serverVerified"])

    def test_malformed_metadata_types_missing_and_extra_fields_are_rejected(self):
        for field in self.payload()["transmission"]:
            payload = self.payload()
            del payload["transmission"][field]
            self.assert_rejected(payload)
        for change in ({"reason": []}, {"state": []}, {"mode": []}, {"policyId": []},
                       {"reason": "other"}, {"extra": True}, {"intervalSec": 300.0},
                       {"intervalSec": True}, {"mode": "priority"}, {"mode": "replay"},
                       {"anomalyCount": True}, {"normalCount": 5.0}, {"anomalyCount": -1},
                       {"normalCount": 2**31}, {"normalCount": None}):
            with self.subTest(change=change):
                self.assert_rejected(self.payload(transmission=change))
        self.assertEqual(self.listing()["items"], [])

    def test_state_mode_interval_cannot_disagree_with_reason(self):
        for reason in REPORTS:
            for change in ({"state": "UNKNOWN"}, {"intervalSec": 5},
                           {"mode": "periodic" if REPORTS[reason][1] == "immediate" else "immediate"}):
                with self.subTest(reason=reason, change=change):
                    self.assert_rejected(self.payload(reason=reason, transmission=change))
        self.assert_rejected(self.payload(reason="anomaly_enter", transmission={"state": "NORMAL"}))
        self.assert_rejected(self.payload(reason="normal_recovered", transmission={"state": "ANOMALY_ACTIVE"}))

    def test_transition_counters_and_latched_state_boundaries(self):
        for reason, change in (
            ("anomaly_enter", {"anomalyCount": 2}), ("anomaly_enter", {"anomalyCount": 4}),
            ("normal_recovered", {"normalCount": 4}), ("normal_recovered", {"normalCount": 6}),
            ("normal_periodic", {"anomalyCount": 3, "normalCount": 0}),
            ("anomaly_periodic", {"normalCount": 5, "anomalyCount": 0}),
            ("normal_periodic", {"anomalyCount": 1, "normalCount": 1}),
        ):
            with self.subTest(reason=reason, change=change):
                self.assert_rejected(self.payload(reason=reason, transmission=change))
        self.send(0, transmission={"anomalyCount": 2, "normalCount": 0})
        self.send(19, "anomaly_periodic", transmission={"anomalyCount": 0, "normalCount": 4})
        self.assertEqual(self.listing()["statuses"], {"queued": 2})

    def test_periodic_quality_failure_is_evidence_not_recovery_or_anomaly(self):
        for index, reason in enumerate(("normal_periodic", "anomaly_periodic")):
            ack, status = self.send(index, reason, quality="sensor_unavailable", samples=None, sampleCount=0,
                                    transmission={"anomalyCount": 0, "normalCount": 0})
            self.assertEqual((status, ack["processingStatus"]), (202, "unavailable"))
        for item in self.listing()["items"]:
            self.assertIsNone(item["window"]["samples"])
            self.assertEqual(item["analysis"]["reason"], "sensor_unavailable")
            self.assertIsNone(item["analysis"]["verdict"])
        for reason in REPORTS:
            self.assert_rejected(self.payload(reason=reason, quality="fifo_overrun"))

    def test_first_packet_can_report_active_without_server_observing_entry(self):
        self.send(1000, "anomaly_periodic")
        self.assertEqual(self.listing()["latestTransmission"]["state"], "ANOMALY_ACTIVE")
        self.assertFalse(self.listing()["boardStateVerifiedByServer"])

    def test_exact_retry_keeps_first_receipt_and_policy_after_recovery(self):
        first, _ = self.send(3, "anomaly_enter")
        self.send(24, "normal_recovered")
        retry, status = self.send(3, "anomaly_enter")
        self.assertEqual((status, retry["accepted"], retry["policyId"]), (200, 0, EDGE_SNAPSHOT_POLICY_ID))
        self.assertEqual(retry["acknowledged"], first["acknowledged"])
        self.assertEqual(self.listing()["latestTransmission"]["state"], "NORMAL")

    def test_late_transition_or_old_boot_cannot_rewind_latest_report(self):
        self.send(24, "normal_recovered")
        ack, _ = self.send(3, "anomaly_enter")
        self.assertTrue(ack["lateArrival"])
        self.send(19, "anomaly_periodic")
        self.send(0, "anomaly_periodic", bootId="d" * 32)
        self.assertEqual(self.listing()["latestTransmission"]["reason"], "normal_recovered")
        self.assertEqual(self.listing()["intervalSec"], 300)

    def test_same_window_cannot_be_relabelled_for_transition_or_retry(self):
        self.send(3, "anomaly_enter")
        for payload in (self.payload(3, "anomaly_periodic"),
                        self.payload(3, "normal_periodic"),
                        fixtures.SnapshotTest.payload(self, 0, **self.payload(3)["window"])):
            with self.subTest(transmission=payload["transmission"]), self.assertRaises(data.ApiError) as error:
                self.send(payload=payload)
            self.assertEqual((error.exception.status, error.exception.code), (409, "SNAPSHOT_CONFLICT"))
        self.assertEqual(len(self.listing()["items"]), 1)

    def test_legacy_policy_and_new_policy_coexist_and_ack_own_version(self):
        old = fixtures.SnapshotTest.payload(self)
        self.assertEqual(self.send(payload=old)[0]["policyId"], SNAPSHOT_POLICY_ID)
        self.assertEqual(self.listing()["policyId"], SNAPSHOT_POLICY_ID)
        self.send(3, "anomaly_enter")
        ack, status = self.send(payload=old)
        self.assertEqual((status, ack["policyId"]), (200, SNAPSHOT_POLICY_ID))
        self.assertEqual(self.listing()["policyId"], EDGE_SNAPSHOT_POLICY_ID)

    def test_restart_retains_transmission_and_inspect_reports_actual_cadence(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            path = project / operations.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"]
            payload = self.payload(3, "anomaly_enter")
            store = PeriodicSnapshotStore(path)
            try:
                first, _ = store.ingest(self.principal, DEVICE, payload)
            finally:
                store.close()
            store = PeriodicSnapshotStore(path)
            try:
                retry, status = store.ingest(self.principal, DEVICE, payload)
                self.assertEqual((status, retry["acknowledged"]), (200, first["acknowledged"]))
                self.assertEqual(store.list_device(self.admin, DEVICE)["latestTransmission"], payload["transmission"])
                for reason, expected in (("anomaly_enter", 10), ("normal_recovered", 300)):
                    if reason == "normal_recovered":
                        store.ingest(self.principal, DEVICE, self.payload(24, reason))
                    component = operations.inspect(project, {}, DEVICE)["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]
                    latest = component["latest"]
                    self.assertEqual(latest["transmission"]["reason"], reason)
                    self.assertEqual(latest["expectedReportIntervalSec"], expected)
                    self.assertFalse(latest["boardStateVerifiedByServer"])
            finally:
                store.close()

    def test_auth_and_raw_integrity_are_not_bypassed_by_immediate_report(self):
        payload = self.payload(3, "anomaly_enter")
        with self.assertRaises(data.ApiError) as error:
            self.store.ingest({"permissions": ["device-health:write"]}, DEVICE, payload)
        self.assertEqual(error.exception.status, 403)
        payload["window"]["integrity"]["digest"] = "0" * 64
        with self.assertRaises(data.ApiError) as error:
            self.send(payload=payload)
        self.assertEqual(error.exception.code, "SNAPSHOT_INTEGRITY_MISMATCH")
        self.assertEqual(self.listing()["items"], [])

    def test_http_accepts_reports_but_never_sends_them_to_rf66_or_alerts(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False, rf66_event_mode="events")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"/api/devices/{DEVICE}/periodic-snapshots"
        def request(method, payload=None, token="demo-telemetry-ingest-token"):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request(method, endpoint, json.dumps(payload) if payload else None,
                             {"Content-Type": "application/json", "Authorization": "Bearer " + token})
                response = conn.getresponse()
                return response.status, json.load(response)
            finally:
                conn.close()
        try:
            before_events = copy.deepcopy(data.EVENTS)
            for index, reason in ((0, "normal_periodic"), (3, "anomaly_enter"),
                                  (19, "anomaly_periodic"), (24, "normal_recovered")):
                status, ack = request("POST", self.payload(index, reason))
                self.assertEqual((status, ack["policyId"]), (202, EDGE_SNAPSHOT_POLICY_ID))
            self.assertEqual(request("POST", self.payload(3, "anomaly_enter"))[0], 200)
            self.assertEqual(request("POST", self.payload(3, "anomaly_enter"), token="wrong")[0], 401)
            self.assertEqual(request("POST", self.payload(3, "anomaly_enter", transmission={"anomalyCount": 2}))[0], 400)
            status, result = request("GET", token=self.token)
            self.assertEqual((status, result["statuses"]), (200, {"queued": 4}))
            self.assertEqual(result["latestTransmission"]["reason"], "normal_recovered")
            for store in (server.raw_vibration, server.vibration_windows):
                self.assertFalse(store.tick())
                self.assertEqual(store.db.execute("SELECT count(*) FROM vibration_windows").fetchone()[0], 0)
            self.assertEqual(server.raw_vibration.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0], 0)
            self.assertEqual(data.EVENTS, before_events)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
