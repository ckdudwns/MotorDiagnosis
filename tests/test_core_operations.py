"""Real HTTP contracts consumed by the device operations workspace (UI-06)."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from motor_diagnosis import data, remote_config
from motor_diagnosis.communication_quality import COUNTERS, _iso
from motor_diagnosis.server import create_server

DEVICE = "DEV-01-MOT-02"
CONFIG_TOKEN = "operations-config-fixture-" + "A" * 32
QUALITY_TOKEN = "operations-quality-fixture-" + "B" * 32
ROOT = f"/api/devices/{DEVICE}"


class CoreOperationsHttpTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.addCleanup(data.reset_runtime_state)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DEVICE_CONFIG_TOKENS_JSON": json.dumps({DEVICE: CONFIG_TOKEN}),
                "DEVICE_QUALITY_TOKENS_JSON": json.dumps({DEVICE: QUALITY_TOKEN}),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = create_server(
            "127.0.0.1",
            0,
            auto_alerts=False,
            demo_enabled=False,
            state_database=str(Path(self.temp.name) / "state.sqlite3"),
            communication_database=str(Path(self.temp.name) / "quality.sqlite3"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)
        self.tokens = {}
        for name in ("admin", "operator", "system"):
            status, login = self.request(
                "/api/auth/login",
                "POST",
                {"username": name, "password": name + "123"},
                token="",
            )
            self.assertEqual(status, 200, login)
            self.tokens[name] = login["session"]["token"]

    def finish(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, method="GET", body=None, token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {
                    "Authorization": "Bearer "
                    + (self.tokens["admin"] if token is None else token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            content = (
                json.loads(raw)
                if "application/json" in response.getheader("Content-Type", "")
                else raw
            )
            return response.status, content
        finally:
            connection.close()

    def publish(self, version=0, interval=6000, replay=2):
        return self.request(
            ROOT + "/configuration",
            "PUT",
            {
                "expectedVersion": version,
                "settings": {
                    "measurementIntervalMs": interval,
                    "replayBatchSize": replay,
                },
                "reason": "로컬 화면 검증용 설정",
            },
        )

    def report_result(self, command, status="applied"):
        return self.request(
            ROOT + "/configuration/result",
            "POST",
            {
                "version": command["version"],
                "commandId": command["commandId"],
                "status": status,
                "settings": command["settings"] if status == "applied" else None,
                "errorCode": None if status == "applied" else "storage_failure",
            },
            token=CONFIG_TOKEN,
        )

    def report_quality(self, *, idle=False):
        end = int(time.time()) * 1000 - 1000
        metrics = dict.fromkeys(COUNTERS, 0)
        metrics.update(
            attempts=0 if idle else 2,
            acknowledged=0 if idle else 1,
            transportFailures=0 if idle else 1,
            retries=0 if idle else 1,
            ackLatencyTotalMs=0 if idle else 125,
            ackLatencyMaxMs=0 if idle else 125,
            bufferSamples=2,
            bufferDepthSum=0,
            bufferDepthMax=0,
            bufferDepthLast=0,
            bufferCapacity=100,
            wifiSamples=2,
            offlineSamples=0,
        )
        return self.request(
            ROOT + "/communication-quality",
            "POST",
            {
                "schemaVersion": 1,
                "transport": "http",
                "deviceId": DEVICE,
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
                "bootId": "a" * 32,
                "windowId": 1,
                "startUptimeMs": 0,
                "endUptimeMs": 60000,
                "startedAt": _iso(end - 60000),
                "endedAt": _iso(end),
                "metrics": metrics,
            },
            token=QUALITY_TOKEN,
        )

    def quality_query(self):
        end = int(time.time()) * 1000
        return (
            ROOT
            + "/communication-quality?"
            + urlencode(
                {"from": _iso(end - 86400000), "to": _iso(end), "bucketSeconds": 3600}
            )
        )

    def test_page_and_read_contracts_share_device_scope_without_device_secrets(self):
        status, page = self.request("/", token="")
        self.assertEqual(status, 200)
        for element in (
            "navDeviceOps",
            "opsDevice",
            "opsConfigPublish",
            "opsQualityRows",
        ):
            self.assertIn(f'id="{element}"', page)
        for secret in (CONFIG_TOKEN, QUALITY_TOKEN, self.tokens["admin"]):
            self.assertNotIn(secret, page)
        status, devices = self.request("/api/sites/SITE-01/devices")
        self.assertEqual(status, 200, devices)
        device = next(row for row in devices if row["id"] == DEVICE)
        self.assertEqual(device["assetId"], "SITE-01-MOT-02")
        status, health = self.request(ROOT + "/health")
        self.assertEqual(status, 200, health)
        self.assertEqual(health["deviceId"], DEVICE)
        self.assertEqual(health["assetId"], device["assetId"])
        status, config = self.request(ROOT + "/configuration")
        self.assertEqual(status, 200, config)
        self.assertEqual(config["version"], 0)
        self.assertEqual(config["defaults"], remote_config.DEFAULT_SETTINGS)
        self.assertIsNone(config["desired"])
        self.assertIsNone(config["lastApplied"])
        status, history = self.request(ROOT + "/configuration/history")
        self.assertEqual(status, 200, history)
        self.assertEqual(history, {"items": [], "retainedLimit": 100})

    def test_quality_distinguishes_no_observations_from_measured_zero(self):
        status, empty = self.request(self.quality_query())
        self.assertEqual(status, 200, empty)
        self.assertFalse(empty["summary"]["hasData"])
        self.assertIsNone(empty["summary"]["bufferDepthLast"])
        self.assertEqual(self.report_quality(idle=True)[0], 201)
        status, quality = self.request(self.quality_query())
        self.assertEqual(status, 200, quality)
        self.assertEqual(quality["deviceId"], DEVICE)
        self.assertEqual(quality["siteId"], "SITE-01")
        self.assertEqual(quality["assetId"], "SITE-01-MOT-02")
        self.assertEqual(quality["transport"], "http")
        self.assertEqual(quality["attribution"], "whole_window_at_end")
        self.assertTrue(quality["summary"]["hasData"])
        self.assertEqual(quality["summary"]["attempts"], 0)
        self.assertEqual(quality["summary"]["bufferDepthLast"], 0)
        self.assertIsNone(quality["summary"]["ackLatencyMeanMs"])
        self.assertTrue(any(not row["hasData"] for row in quality["items"]))

    def test_quality_failure_ack_and_buffer_summary_match_ui_contract(self):
        self.assertEqual(self.report_quality()[0], 201)
        status, response = self.request(self.quality_query())
        self.assertEqual(status, 200, response)
        summary = response["summary"]
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["failureRatePct"], 50)
        self.assertEqual(summary["ackLatencyMeanMs"], 125)
        self.assertEqual(summary["ackLatencyMaxMs"], 125)
        self.assertEqual(summary["bufferDepthSampleMean"], 0)
        self.assertEqual(summary["observedDurationMs"], 60000)

    def test_pending_applied_and_failed_commands_remain_distinct(self):
        status, command = self.publish()
        self.assertEqual(status, 200, command)
        self.assertEqual(command["state"], "pending")
        self.assertEqual(command["version"], 1)
        self.assertRegex(command["commandId"], r"^[0-9a-f]{32}$")
        config = self.request(ROOT + "/configuration")[1]
        self.assertEqual(config["desired"], command)
        self.assertIsNone(config["lastApplied"])
        self.assertEqual(self.report_result(command)[0], 200)
        status, next_command = self.publish(1, interval=9000)
        self.assertEqual(status, 200, next_command)
        self.assertEqual(self.report_result(next_command, "failed")[0], 200)
        config = self.request(ROOT + "/configuration")[1]
        self.assertEqual(config["desired"]["state"], "failed")
        self.assertEqual(config["lastApplied"]["version"], 1)
        self.assertEqual(config["lastApplied"]["settings"], command["settings"])
        history = self.request(ROOT + "/configuration/history")[1]
        self.assertEqual(
            [row["state"] for row in history["items"]], ["failed", "applied"]
        )
        self.assertTrue(
            all(row["assetId"] == "SITE-01-MOT-02" for row in history["items"])
        )

    def test_version_conflict_and_readback_do_not_issue_another_configuration(self):
        status, command = self.publish()
        self.assertEqual(status, 200, command)
        status, conflict = self.publish()
        self.assertEqual(status, 409, conflict)
        config = self.request(ROOT + "/configuration")[1]
        self.assertEqual(config["desired"], command)
        self.assertEqual(config["version"], 1)
        self.assertEqual(
            len(self.request(ROOT + "/configuration/history")[1]["items"]), 1
        )

    def test_operator_can_read_but_cannot_publish_or_use_device_result_route(self):
        token = self.tokens["operator"]
        for suffix in (
            "/health",
            "/configuration",
            "/configuration/history",
            "/communication-quality",
        ):
            self.assertEqual(self.request(ROOT + suffix, token=token)[0], 200, suffix)
        body = {
            "expectedVersion": 0,
            "settings": remote_config.DEFAULT_SETTINGS,
            "reason": "fixture",
        }
        self.assertEqual(
            self.request(ROOT + "/configuration", "PUT", body, token)[0], 403
        )
        self.assertEqual(
            self.request(ROOT + "/configuration/result", "POST", {}, token)[0], 401
        )
        self.assertEqual(
            self.request(ROOT + "/configuration", token=QUALITY_TOKEN)[0], 401
        )
        self.assertEqual(
            self.request(ROOT + "/communication-quality", token=CONFIG_TOKEN)[0], 401
        )
        self.assertEqual(self.request(ROOT + "/configuration")[1]["version"], 0)


if __name__ == "__main__":
    unittest.main()
