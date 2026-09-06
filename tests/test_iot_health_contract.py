"""Optional C++ producer -> actual local HTTP API contract integration.

Set IOT_HEALTH_FIXTURE_EXE to the compiled test_device_health executable.
No board, production credentials, persistent database or remote API is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from motor_diagnosis import data
from motor_diagnosis.server import create_server

FIXTURE_EXE = os.environ.get("IOT_HEALTH_FIXTURE_EXE", "")


@unittest.skipUnless(
    FIXTURE_EXE and Path(FIXTURE_EXE).is_file(),
    "Build the native device-health fixture and set IOT_HEALTH_FIXTURE_EXE",
)
class IotHealthHttpContractTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"APP_ENV": "production", "DEVICE_HEALTH_TOKEN": "iot-test-health-only"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        data.reset_runtime_state()
        self.server = create_server(
            "127.0.0.1", 0, demo_enabled=False, auto_alerts=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        generated = subprocess.run(
            [FIXTURE_EXE, "--emit-fixture"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.active, self.recovered, self.telemetry = map(
            json.loads, generated.stdout.splitlines()
        )

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, path, payload, token="iot-test-health-only"):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def sensor_events(self):
        return [
            event
            for event in data.EVENTS
            if event.get("deviceId") == "DEV-01-MOT-02"
            and event.get("faultCode") == "adxl345_channel_error"
        ]

    def test_generated_metrics_fault_recovery_and_ack_match_backend(self):
        path = "/api/devices/DEV-01-MOT-02/health"
        status, body = self.post(path, self.active)
        self.assertEqual(status, 200)
        self.assertEqual(body["rssiDbm"], -61)
        self.assertEqual(body["rebootCount"], 4)
        self.assertEqual(body["bufferUsagePct"], 50)
        self.assertEqual(body["sensorHealth"], "fault")
        acknowledged = subprocess.run(
            [FIXTURE_EXE, "--validate-backend-ack", json.dumps(body)],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(acknowledged.returncode, 0)
        self.assertEqual(self.post(path, self.active)[0], 200)  # ACK-loss retry
        self.assertEqual(len(self.sensor_events()), 1)
        self.assertEqual(self.post(path, self.recovered)[1]["sensorHealth"], "healthy")
        self.assertEqual(self.post(path, self.recovered)[0], 200)
        event = self.sensor_events()[0]
        self.assertEqual(len(self.sensor_events()), 1)
        self.assertEqual(event["status"], "closed")
        self.assertEqual(event["endReason"], "sensor_recovered")
        self.assertEqual(event["durationSec"], 1)
        self.assertTrue(event["assetEventExcluded"])

    def test_health_credential_has_no_telemetry_or_admin_authority(self):
        principal = data.telemetry_principal_for_token("iot-test-health-only")
        self.assertEqual(principal["permissions"], ["device-health:write"])
        self.assertIn(self.post("/api/telemetry/ingest", self.telemetry)[0], (401, 403))
        self.assertIn(self.post("/api/assets", {})[0], (401, 403))

    def test_unauthorized_report_does_not_consume_fault_or_create_event(self):
        path = "/api/devices/DEV-01-MOT-02/health"
        self.assertEqual(self.post(path, self.active, token="invalid")[0], 401)
        self.assertEqual(len(self.sensor_events()), 0)
        self.assertEqual(self.post(path, self.active)[0], 200)
        self.assertEqual(len(self.sensor_events()), 1)


if __name__ == "__main__":
    unittest.main()
