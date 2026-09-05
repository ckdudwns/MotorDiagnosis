"""AI2 dashboard behavior and its real backend integration contracts."""

from __future__ import annotations

import csv
import http.client
import io
import json
import shutil
import subprocess
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from motor_diagnosis import data
from motor_diagnosis.server import create_server
from tests.test_backend_production_integrations import telemetry_payload


class Ai2DashboardScriptTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard tests")
    def test_shipped_script_behaviors(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/test_ai2_dashboard.mjs"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("30 behavior checks passed", result.stdout)


class Ai2DashboardHttpTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.server = create_server("127.0.0.1", 0, auto_alerts=False, demo_enabled=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tokens = {}
        for username in ("operator", "admin", "system"):
            _, response = self.request(
                "/api/auth/login", "POST",
                {"username": username, "password": username + "123"}, token="",
            )
            self.tokens[username] = response["session"]["token"]
        self.started = datetime.now(timezone.utc) - timedelta(minutes=1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        data.reset_runtime_state()

    def request(self, path, method="GET", body=None, *, token=None, raw=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Authorization": "Bearer " + (self.tokens.get("admin", "") if token is None else token)}
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, payload if raw else json.loads(payload)
        finally:
            connection.close()

    def ingest(self, sequence, offset, vibration=1.0):
        payload = telemetry_payload(
            sequence, data.format_rfc3339(self.started + timedelta(seconds=offset)),
            vibration=vibration,
        )
        status, response = self.request(
            "/api/telemetry/ingest", "POST", payload, token="demo-telemetry-ingest-token"
        )
        self.assertEqual(status, 201, response)

    def test_rule_seconds_score_sensor_fault_and_detail_share_the_existing_adapter(self):
        status, rule = self.request(
            "/api/anomaly/rules/SITE-01-GEN-01", "PUT",
            {"scoreThreshold": 70, "durationSec": 10, "hysteresis": 10,
             "mergeWindowSec": 5, "active": True, "reason": "AI2 dashboard contract"},
            token=self.tokens["system"],
        )
        self.assertEqual(status, 200, rule)
        for sequence, offset in enumerate((0, 0.1, 9.9), 1):
            self.ingest(sequence, offset)
        emitted = lambda: [event for event in data.EVENTS if event.get("source") == "backend-integration-test"]
        self.assertEqual(emitted(), [])
        self.ingest(4, 10)
        self.assertEqual(len(emitted()), 1)
        event_id = emitted()[0]["id"]
        status, detail = self.request("/api/anomaly/events/" + event_id)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["appliedRule"]["version"], rule["version"])
        self.assertEqual(detail["appliedRule"]["durationSec"], 10)
        self.assertFalse(detail["context"]["rawDataMissing"])
        self.assertTrue(detail["featureSnapshot"])
        self.assertIn("SITE-01-GEN-01", data.LIFECYCLE_CHECKPOINTS)
        status, health = self.request(
            "/api/devices/DEV-01-GEN-01/health", "POST",
            {"reportedAt": data.format_rfc3339(self.started + timedelta(seconds=11)),
             "sensorFaults": [{"code": "adxl345_timeout", "status": "active"}]},
            token="demo-device-health-token",
        )
        self.assertEqual(status, 200, health)
        self.ingest(5, 12)
        self.assertEqual(len(emitted()), 1)
        self.assertEqual(emitted()[0]["endReason"], "sensor_fault_detected")
        status, devices = self.request("/api/devices/DEV-01-GEN-01/health")
        self.assertEqual(status, 200)
        self.assertTrue(devices["activeSensorFaults"])

    def test_review_reason_notes_attachments_history_and_filters(self):
        path = "/api/events/EV-241"
        status, review = self.request(path + "/review", "POST", {
            "label": "confirmed_anomaly", "note": "Confirmed bearing issue",
            "reason": "Inspection result",
        }, token=self.tokens["operator"])
        self.assertEqual(status, 200, review)
        _, history = self.request(path + "/reviews?page=1&size=20")
        self.assertEqual(history["items"][0]["reason"], "Inspection result")
        _, filtered = self.request(
            "/api/events?siteId=SITE-01&assetId=SITE-01-MOT-02"
            "&label=confirmed_anomaly&reviewed=true&sort=occurredAt_desc&page=1&size=12"
        )
        self.assertIn("EV-241", [event["id"] for event in filtered["items"]])
        status, note = self.request(path + "/notes", "POST", {
            "category": "inspection", "text": "Check housing",
            "attachmentRefs": ["survey://SITE-01/photo-1"],
        })
        self.assertEqual(status, 201, note)
        status, updated = self.request(path + "/notes/" + note["id"], "PATCH", {
            "category": "action", "text": "Replace bearing", "attachmentRefs": []
        })
        self.assertEqual(status, 200, updated)
        _, notes = self.request(path + "/notes")
        self.assertEqual(notes["latestByCategory"]["action"]["id"], note["id"])
        _, history = self.request(path + "/notes/" + note["id"] + "/history")
        self.assertEqual(history["total"], 2)
        self.assertEqual(history["items"][0]["version"], 2)
        self.assertEqual(self.request(path + "/notes/" + note["id"], "DELETE")[0], 200)
        self.assertEqual(self.request(path + "/notes")[1]["total"], 0)

    def test_export_selected_time_range_csv_xlsx_and_frozen_dataset(self):
        self.ingest(1, 0, vibration=0.08)
        self.ingest(2, 20, vibration=0.08)
        from_time = data.format_rfc3339(self.started - timedelta(seconds=1))
        to_time = data.format_rfc3339(self.started + timedelta(seconds=1))
        path = "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01"
        path += "&from=" + from_time + "&to=" + to_time
        status, body = self.request(path + "&format=csv", raw=True)
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual([row["sequence"] for row in rows], ["1"])
        status, body = self.request(path + "&format=xlsx", raw=True)
        self.assertEqual(status, 200)
        with ZipFile(io.BytesIO(body)) as archive:
            xml = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        self.assertEqual(len(xml.findall("{*}sheetData/{*}row")), 2)
        status, dataset = self.request("/api/datasets", "POST", {
            "name": "AI2 export selection", "source": {
                "type": "internal", "uri": "api://telemetry", "license": "project-internal",
                "checksum": "sha256:" + "1" * 64,
            },
            "compatibility": {"signalType": ["vibration"], "samplingRateHz": 1000,
                              "units": {"vibration": "raw"}, "operatingConditions": {"ratedRpm": 1800}},
            "sourceFilters": {"assetIds": ["SITE-01-GEN-01"]},
            "labelTaxonomyVersion": "ACOUSTIC-V1", "labelMapping": {"normal": "NORMAL"},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0}, "reason": "AI2 selector",
        }, token=self.tokens["system"])
        self.assertEqual(status, 201, dataset)
        status, frozen_body = self.request(path + "&format=csv&datasetId=" + dataset["id"], raw=True)
        self.assertEqual(status, 200)
        frozen_rows = list(csv.DictReader(io.StringIO(frozen_body.decode("utf-8-sig"))))
        self.assertEqual([row["sequence"] for row in frozen_rows], ["1"])
        self.assertEqual(frozen_rows[0]["dataset_id"], dataset["id"])

    def test_management_permissions_and_firmware_read_only_contract(self):
        status, _ = self.request("/api/anomaly/rules/SITE-01-GEN-01", "PUT", {
            "scoreThreshold": 75, "reason": "Unauthorized edit"
        }, token=self.tokens["operator"])
        self.assertEqual(status, 403)
        status, response = self.request("/api/parameters/EDGE_BUFFER_HOURS", "PUT", {
            "value": 48, "reason": "Firmware-managed setting"
        }, token=self.tokens["system"])
        self.assertEqual(status, 409, response)
        for path in ("/api/sites", "/api/sites/SITE-01/assets",
                     "/api/sites/SITE-01/devices", "/api/assets/SITE-01-GEN-01/install-points",
                     "/api/sites/SITE-01/network-profile", "/api/alerts/policies",
                     "/api/parameters", "/api/audit-logs?page=1&size=12",
                     "/api/health/dependencies"):
            with self.subTest(path=path):
                status, response = self.request(path, token=self.tokens["system"])
                self.assertEqual(status, 200, response)

    def test_management_form_write_contracts(self):
        def write(path, method, payload, expected=200):
            status, result = self.request(
                path, method, {**payload, "reason": "AI2 management contract"},
                token=self.tokens["system"],
            )
            self.assertEqual(status, expected, result)
            return result

        site = write("/api/sites", "POST", {"id": "AI2-SITE", "name": "AI2 Site"}, 201)
        asset_path = f"/api/sites/{site['id']}/assets"
        asset = write(asset_path, "POST", {
            "assetCode": "MOT-01", "name": "AI2 Motor", "type": "motor", "ratedRpm": 1800,
            "installLocation": "Test bearing",
            "baseline": {"status": "ready", "capturedAt": data.now_iso(),
                         "vibrationRmsMmS": 1.0, "acousticDb": 50.0, "sampleCount": 10},
        }, 201)
        updated = write(f"{asset_path}/{asset['id']}", "PATCH", {"ratedRpm": 1450})
        self.assertEqual(updated["ratedRpm"], 1450)
        write(f"/api/sites/{site['id']}/rollout-plan", "PUT", {
            "networkProfileId": "NET-STORE-FWD", "targetAssetIds": [asset["id"]],
            "installPriority": "normal", "note": "Test registration",
        })
        device = write(f"/api/sites/{site['id']}/devices", "POST", {
            "id": "AI2-DEVICE", "assetId": asset["id"], "mappingStatus": "inactive",
            "certificateId": "AI2-TEST-CERT", "certificateFingerprint": "a" * 64,
        }, 201)
        write(f"/api/devices/{device['id']}", "PATCH", {"firmwareVersion": "ai2-test-1"})
        install_path = f"/api/assets/{asset['id']}/install-points"
        installation = write(install_path, "POST", {
            "position": "bearing", "orientation": "X", "mountingMethod": "bolt",
            "photoRefs": ["survey://ai2/installation"],
        }, 201)
        updated = write(f"{install_path}/{installation['id']}", "PATCH", {"active": False})
        self.assertFalse(updated["active"])
        write(f"/api/sites/{site['id']}/network-profile", "PUT", {"networkProfileId": "NET-STORE-FWD"})
        _, policies = self.request("/api/alerts/policies", token=self.tokens["system"])
        policy = write(f"/api/alerts/policies/{policies[0]['id']}", "PUT", {"cooldownSec": 120})
        self.assertEqual(policy["cooldownSec"], 120)
        updated = write("/api/parameters/RETENTION_RAW_DAYS", "PUT", {"value": 31})
        self.assertEqual(updated["value"], 31)
        status, audit = self.request("/api/audit-logs?action=parameter.update", token=self.tokens["system"])
        self.assertEqual(status, 200)
        self.assertGreater(audit["total"], 0)


if __name__ == "__main__":
    unittest.main()
