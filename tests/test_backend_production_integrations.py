from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from motor_diagnosis import data
from motor_diagnosis.alerts import AlertService
from motor_diagnosis.xlsx_export import dataset_xlsx_bytes
from motor_diagnosis.server import create_server


def telemetry_payload(sequence: int, timestamp: str, *, vibration: float = 0.08):
    return {
        "timestamp": timestamp,
        "sequence": sequence,
        "siteId": "SITE-01",
        "assetId": "SITE-01-GEN-01",
        "deviceId": "DEV-01-GEN-01",
        "rpm": 1796.0,
        "vibrationRmsRaw": vibration,
        "vibrationRmsMmS": None,
        "vibrationPeakHz": 1037.11,
        "acousticRmsRaw": 0.007019,
        "acousticDb": None,
        "acousticPeakHz": 216.4,
        "scenarioLabel": None,
        "knownVibrationLabel": None,
        "knownAcousticLabel": None,
        "source": "backend-integration-test",
        "isSynthetic": False,
        "vibrationUnitNote": "raw accelerometer output; not mm/s",
        "acousticUnitNote": "raw waveform RMS; not dB SPL",
    }


class BackendProductionIntegrationTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        login = data.authenticate({"username": "admin", "password": "admin123"})
        self.admin = data.current_user_for_token(login["session"]["token"])
        self.ingest = data.telemetry_principal_for_token(
            "demo-telemetry-ingest-token"
        )
        self.health = data.telemetry_principal_for_token("demo-device-health-token")

    def tearDown(self):
        data.close_runtime_state()
        data.reset_runtime_state()

    def test_health_timestamp_and_esp32_profile_contract(self):
        timestamp = data.now_iso()
        self.assertRegex(timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            data.parse_rfc3339("timestamp", timestamp).utcoffset(), timedelta(0)
        )
        profile = data.hardware_profile("HW-ESP32S3-ADXL345-INMP441-WIFI")
        self.assertEqual(profile["boardType"], "esp32-s3")
        self.assertEqual(profile["sensors"], ["adxl345", "inmp441"])

    def test_accepted_telemetry_drives_score_lifecycle_and_evidence(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=12)
        result = None
        for offset in range(11):
            result, status = data.ingest_telemetry(
                self.ingest,
                telemetry_payload(
                    offset + 1,
                    data.format_rfc3339(started + timedelta(seconds=offset)),
                    vibration=1.0,
                ),
            )
            self.assertEqual(status, 201)
        self.assertEqual(result["anomalyScore"], 100)
        self.assertTrue(result["lifecycleUpdates"])
        event = next(
            item
            for item in data.EVENTS
            if item.get("source") == "backend-integration-test"
        )
        self.assertEqual(event["thresholdVersion"], "RULE-SITE-01-GEN-01-v1")
        self.assertEqual(event["modelVersion"], "ai1_week2_vibration_rms_baseline_v1")
        self.assertTrue(event["triggerEvidence"])
        self.assertIn(event["id"], data.EVENT_EVIDENCE_SNAPSHOTS)
        self.assertIn("SITE-01-GEN-01", data.LIFECYCLE_CHECKPOINTS)
        alerts = AlertService(":memory:")
        try:
            alerts.observe_events()
            alerts.process_due()
            deliveries = alerts.list_for(
                self.admin, site_id="SITE-01", page=1, size=50
            )
        finally:
            alerts.close()
        self.assertIn(event["id"], {item["eventId"] for item in deliveries["items"]})
        dependency_statuses = {
            item["id"]: item["status"] for item in data.SERVICE_DEPENDENCIES
        }
        self.assertEqual(dependency_statuses["analysis"], "healthy")
        self.assertEqual(dependency_statuses["alerts"], "healthy")

    def test_device_health_persists_fault_and_recovery_without_training_label(self):
        active = data.update_device_health(
            self.health,
            "DEV-01-GEN-01",
            {
                "rssiDbm": -67,
                "rebootCount": 2,
                "bufferUsagePct": 31.5,
                "firmwareVersion": "esp32-edge-1.0.0",
                "sensorFaults": [
                    {
                        "code": "adxl345_timeout",
                        "status": "active",
                        "severity": "critical",
                        "detail": "Accelerometer acquisition timed out.",
                    }
                ],
            },
        )
        self.assertEqual(active["sensorHealth"], "fault")
        event = next(
            item
            for item in data.EVENTS
            if item.get("faultCode") == "adxl345_timeout"
        )
        self.assertFalse(event["reviewed"])
        self.assertTrue(event["assetEventExcluded"])

        recovered = data.update_device_health(
            self.health,
            "DEV-01-GEN-01",
            {
                "sensorFaults": [
                    {"code": "adxl345_timeout", "status": "recovered"}
                ]
            },
        )
        self.assertEqual(recovered["sensorHealth"], "healthy")
        self.assertEqual(event["status"], "closed")
        self.assertEqual(event["endReason"], "sensor_recovered")

    def test_runtime_state_survives_restart_with_idempotency_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "runtime.sqlite3")
            data.configure_runtime_state(database)
            data.PARAMETERS["RETENTION_RAW_DAYS"] = 1
            expired = telemetry_payload(
                999,
                data.format_rfc3339(
                    datetime.now(timezone.utc) - timedelta(days=2)
                ),
            )
            with data.STORE_LOCK:
                data.TELEMETRY_RECORDS.append(expired)
                data.TELEMETRY_IDEMPOTENCY[("DEV-01-GEN-01", 999)] = {
                    "payloadHash": "expired",
                    "receivedAt": expired["timestamp"],
                }
            self.assertNotIn(("DEV-01-GEN-01", 999), data.TELEMETRY_IDEMPOTENCY)
            started = datetime.now(timezone.utc) - timedelta(seconds=12)
            for offset in range(11):
                data.ingest_telemetry(
                    self.ingest,
                    telemetry_payload(
                        offset + 1,
                        data.format_rfc3339(started + timedelta(seconds=offset)),
                        vibration=1.0,
                    ),
                )
            expected_event_ids = {
                item["id"]
                for item in data.EVENTS
                if item.get("source") == "backend-integration-test"
            }
            data.close_runtime_state()
            data.reset_runtime_state()
            self.assertFalse(expected_event_ids.intersection(item["id"] for item in data.EVENTS))

            data.configure_runtime_state(database)
            self.assertEqual(len(data.TELEMETRY_RECORDS), 11)
            self.assertIn(("DEV-01-GEN-01", 10), data.TELEMETRY_IDEMPOTENCY)
            self.assertTrue(expected_event_ids.issubset(item["id"] for item in data.EVENTS))
            self.assertIn("SITE-01-GEN-01", data.LIFECYCLE_CHECKPOINTS)
            data.close_runtime_state()

    def test_event_notes_keep_category_attachments_author_and_history(self):
        note = data.create_event_note(
            self.admin,
            "EV-241",
            {
                "category": "inspection",
                "text": "Bearing housing was inspected.",
                "attachmentRefs": ["survey://SITE-01/photo-01.jpg"],
            },
        )
        updated = data.update_event_note(
            self.admin,
            "EV-241",
            note["id"],
            {"category": "action", "text": "Schedule bearing replacement."},
        )
        listing = data.event_notes_for(self.admin, "EV-241")
        history = data.event_note_history_for(self.admin, "EV-241", note["id"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(listing["latestByCategory"]["action"]["id"], note["id"])
        self.assertEqual(history["total"], 2)
        self.assertEqual(history["items"][0]["author"]["id"], self.admin["id"])
        deleted = data.delete_event_note(self.admin, "EV-241", note["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(data.event_notes_for(self.admin, "EV-241")["total"], 0)

    def test_xlsx_contains_manifest_and_same_export_rows(self):
        timestamp = data.format_rfc3339(datetime.now(timezone.utc) - timedelta(seconds=2))
        data.ingest_telemetry(self.ingest, telemetry_payload(1, timestamp))
        export = data.dataset_export_for(
            self.admin, "SITE-01", "SITE-01-GEN-01"
        )
        workbook = dataset_xlsx_bytes(export)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.xlsx"
            path.write_bytes(workbook)
            with ZipFile(path) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
                self.assertIn("xl/worksheets/sheet2.xml", archive.namelist())
                manifest_xml = archive.read("xl/worksheets/sheet1.xml").decode()
                rows_xml = archive.read("xl/worksheets/sheet2.xml").decode()
        self.assertIn("recordCount", manifest_xml)
        self.assertIn("vibration_rms_raw", rows_xml)
        self.assertIn(timestamp, rows_xml)

    def test_internal_dataset_reports_missing_installation_metadata(self):
        asset = data.create_asset(
            self.admin,
            "SITE-01",
            {
                "assetCode": "NEW-99",
                "name": "Unsurveyed motor",
                "ratedRpm": 1800,
            },
        )
        payload = {
            "name": "unsurveyed-internal-data",
            "source": {
                "type": "internal",
                "uri": "api://telemetry",
                "license": "project-internal",
                "checksum": "sha256:" + "1" * 64,
            },
            "compatibility": {
                "signalType": ["vibration"],
                "samplingRateHz": 1000,
                "units": {"vibration": "g"},
                "operatingConditions": {"ratedRpm": 1800},
            },
            "sourceFilters": {"assetIds": [asset["id"]]},
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": {"normal": "NORMAL"},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "reason": "Installation validation regression",
        }
        with self.assertRaises(data.ApiError) as missing:
            data.create_dataset_version(self.admin, payload)
        self.assertEqual(missing.exception.code, "DATASET_INSTALLATION_INCOMPLETE")
        self.assertEqual(missing.exception.details["status"], "missing")
        self.assertEqual(missing.exception.details["missing"][0]["assetId"], asset["id"])

    def test_device_owned_parameters_are_not_falsely_exposed_as_mutable(self):
        with self.assertRaises(data.ApiError) as immutable:
            data.update_parameter(
                self.admin,
                "EDGE_BUFFER_HOURS",
                {"value": 48, "reason": "No remote device configuration exists"},
            )
        self.assertEqual(immutable.exception.code, "PARAMETER_NOT_RUNTIME_MUTABLE")

        updated = data.update_parameter(
            self.admin,
            "ANOMALY_HOLD_SEC",
            {"value": 1, "reason": "Apply one-second runtime hold"},
        )
        self.assertEqual(updated["runtimeTarget"], "anomaly_rules.durationSec")
        self.assertTrue(all(rule["durationSec"] == 1 for rule in data.ANOMALY_RULES))
        timestamp = data.format_rfc3339(
            datetime.now(timezone.utc) - timedelta(seconds=2)
        )
        result, status = data.ingest_telemetry(
            self.ingest, telemetry_payload(1, timestamp, vibration=1.0)
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["lifecycleUpdates"], [])
        result, status = data.ingest_telemetry(
            self.ingest,
            telemetry_payload(
                2,
                data.format_rfc3339(data.parse_rfc3339("timestamp", timestamp) + timedelta(seconds=1)),
                vibration=1.0,
            ),
        )
        self.assertEqual(result["lifecycleUpdates"][0]["kind"], "asset_event_started")


class BackendProductionHttpContractTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.server = create_server("127.0.0.1", 0, auto_alerts=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, login, _ = self.request(
            "/api/auth/login",
            method="POST",
            payload={"username": "admin", "password": "admin123"},
        )
        self.assertEqual(status, 200)
        self.admin_token = login["session"]["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        data.reset_runtime_state()

    def request(self, path, *, method="GET", payload=None, token=""):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {}
        if body is not None:
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read()
                parsed = (
                    json.loads(raw.decode())
                    if response.headers.get_content_type() == "application/json"
                    else raw
                )
                return response.status, parsed, response.headers
        except HTTPError as error:
            return error.code, json.loads(error.read().decode()), error.headers

    def test_health_device_status_notes_and_xlsx_routes(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        status, health, _ = self.request("/api/health")
        after = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertEqual(status, 200)
        self.assertRegex(
            health["timestamp"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
        )
        parsed = data.parse_rfc3339("timestamp", health["timestamp"])
        self.assertLessEqual(before, parsed)
        self.assertLessEqual(parsed, after)

        status, device_health, _ = self.request(
            "/api/devices/DEV-01-GEN-01/health",
            method="POST",
            token="demo-device-health-token",
            payload={"rssiDbm": -72, "sensorFaults": []},
        )
        self.assertEqual(status, 200)
        self.assertEqual(device_health["rssiDbm"], -72)

        status, note, _ = self.request(
            "/api/events/EV-241/notes",
            method="POST",
            token=self.admin_token,
            payload={
                "category": "root_cause",
                "text": "Suspected lubrication loss.",
                "attachmentRefs": ["attachment://inspection/report-1"],
            },
        )
        self.assertEqual(status, 201)
        status, notes, _ = self.request(
            "/api/events/EV-241/notes", token=self.admin_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(notes["latestByCategory"]["root_cause"]["id"], note["id"])

        timestamp = data.format_rfc3339(datetime.now(timezone.utc) - timedelta(seconds=2))
        data.ingest_telemetry(
            data.telemetry_principal_for_token("demo-telemetry-ingest-token"),
            telemetry_payload(1, timestamp),
        )
        status, workbook, headers = self.request(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01&format=xlsx",
            token=self.admin_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(workbook.startswith(b"PK"))
        self.assertEqual(headers["x-dataset-record-count"], "1")


if __name__ == "__main__":
    unittest.main()
