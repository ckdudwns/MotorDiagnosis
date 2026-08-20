from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from motor_diagnosis.data import (
    EVENTS,
    QUARANTINED_DEVICE_MESSAGES,
    TELEMETRY_RECORDS,
    ApiError,
    authenticate,
    connectivity_tests_for_device,
    create_connectivity_test,
    current_user_for_token,
    dashboard_sites_summary,
    device_health_for,
    get_device,
    hardware_profiles,
    ingest_mqtt_message,
    ingest_telemetry,
    reset_runtime_state,
    service_health_dependencies,
    telemetry_for,
    telemetry_principal_for_token,
    telemetry_units,
    update_device_hardware_profile,
)
from motor_diagnosis.server import create_server


def utc_text(offset_seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def telemetry_payload(sequence: int = 1, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": utc_text(-2),
        "sequence": sequence,
        "siteId": "SITE-01",
        "assetId": "SITE-01-GEN-01",
        "deviceId": "DEV-01-GEN-01",
        "rpm": 1796.0,
        "vibrationRmsRaw": 0.079035,
        "vibrationRmsMmS": None,
        "vibrationPeakHz": 1037.11,
        "acousticRmsRaw": 0.007019,
        "acousticDb": None,
        "acousticPeakHz": 216.4,
        "scenarioLabel": "normal",
        "knownVibrationLabel": "NORMAL",
        "knownAcousticLabel": None,
        "source": "CWRU_only_synthetic",
        "isSynthetic": True,
        "vibrationUnitNote": "raw accelerometer output; not mm/s",
        "acousticUnitNote": "raw waveform RMS; not dB SPL",
    }
    payload.update(changes)
    return payload


class Week2BackendTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()
        self.principal = telemetry_principal_for_token("demo-telemetry-ingest-token")

    def user(self, username: str, password: str) -> dict:
        login = authenticate({"username": username, "password": password})
        return current_user_for_token(login["session"]["token"])

    def test_ingest_is_idempotent_and_query_uses_only_stored_raw_data(self) -> None:
        accepted, accepted_status = ingest_telemetry(
            self.principal, telemetry_payload()
        )
        duplicate, duplicate_status = ingest_telemetry(
            self.principal, telemetry_payload()
        )

        self.assertEqual(accepted_status, 201)
        self.assertFalse(accepted["duplicate"])
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["receivedAt"], accepted["receivedAt"])
        self.assertEqual(len(TELEMETRY_RECORDS), 1)

        points = telemetry_for("SITE-01", "SITE-01-GEN-01")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["sequence"], 1)
        self.assertIsNone(points[0]["vibrationRmsMmS"])
        self.assertIn("vibrationRmsRaw", telemetry_units("SITE-01", "SITE-01-GEN-01"))
        self.assertNotIn(
            "vibrationRmsMmS", telemetry_units("SITE-01", "SITE-01-GEN-01")
        )

        with self.assertRaises(ApiError) as conflict:
            ingest_telemetry(self.principal, telemetry_payload(rpm=1801.0))
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(conflict.exception.code, "SEQUENCE_CONFLICT")
        self.assertEqual(len(TELEMETRY_RECORDS), 1)
        self.assertEqual(QUARANTINED_DEVICE_MESSAGES[-1]["reason"], "SEQUENCE_CONFLICT")

    def test_ingest_rejects_invalid_mapping_raw_fields_and_non_finite_numbers(
        self,
    ) -> None:
        invalid_payloads = [
            (telemetry_payload(deviceId="DEV-UNKNOWN"), "DEVICE_NOT_FOUND"),
            (telemetry_payload(assetId="SITE-01-MOT-02"), "DEVICE_MAPPING_MISMATCH"),
            (telemetry_payload(vibrationRmsMmS=1.2), "INVALID_RAW_ONLY_FIELD"),
            (
                telemetry_payload(vibrationRmsRaw=float("inf")),
                "INVALID_TELEMETRY_PAYLOAD",
            ),
        ]
        for payload, error_code in invalid_payloads:
            with self.subTest(error_code=error_code):
                with self.assertRaises(ApiError) as context:
                    ingest_telemetry(self.principal, payload)
                self.assertEqual(context.exception.code, error_code)
        self.assertEqual(len(TELEMETRY_RECORDS), 0)
        self.assertEqual(len(QUARANTINED_DEVICE_MESSAGES), 4)

    def test_mqtt_uses_the_same_validation_and_idempotency_path(self) -> None:
        body = json.dumps(telemetry_payload())
        accepted, status = ingest_mqtt_message("devices/DEV-01-GEN-01/telemetry", body)
        duplicate, duplicate_status = ingest_mqtt_message(
            "devices/DEV-01-GEN-01/telemetry", body
        )
        self.assertEqual(status, 201)
        self.assertFalse(accepted["duplicate"])
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["duplicate"])

        with self.assertRaises(ApiError) as mismatch:
            ingest_mqtt_message("devices/DEV-OTHER/telemetry", body)
        self.assertEqual(mismatch.exception.code, "DEVICE_MAPPING_MISMATCH")

    def test_telemetry_query_supports_a_shared_time_range(self) -> None:
        old_timestamp = utc_text(-120)
        recent_timestamp = utc_text(-5)
        ingest_telemetry(
            self.principal, telemetry_payload(sequence=1, timestamp=old_timestamp)
        )
        ingest_telemetry(
            self.principal, telemetry_payload(sequence=2, timestamp=recent_timestamp)
        )

        points = telemetry_for(
            "SITE-01",
            "SITE-01-GEN-01",
            from_timestamp=utc_text(-60),
            to_timestamp=utc_text(),
        )
        self.assertEqual([point["sequence"] for point in points], [2])

    def test_device_offline_and_recovery_history_are_recorded(self) -> None:
        device = get_device("DEV-01-GEN-01")
        device["lastReceivedAt"] = utc_text(-300)
        device["lastSeenSecAgo"] = 300
        device["health"] = "online"

        offline = device_health_for(device["id"])
        self.assertEqual(offline["health"], "offline")
        self.assertIsNotNone(offline["offlineSince"])
        self.assertEqual(EVENTS[0]["eventType"], "device_offline")

        ingest_telemetry(self.principal, telemetry_payload())
        recovered = device_health_for(device["id"])
        self.assertEqual(recovered["health"], "online")
        self.assertIsNotNone(recovered["lastRecoveredAt"])
        self.assertEqual(len(recovered["missingIntervals"]), 1)
        self.assertEqual(EVENTS[0]["eventType"], "device_recovered")

    def test_dashboard_summary_applies_role_scope_and_dynamic_counts(self) -> None:
        operator = self.user("operator", "operator123")
        rows = dashboard_sites_summary(operator)

        self.assertEqual(
            {row["siteId"] for row in rows},
            {"SITE-01", "SITE-02", "SITE-03", "SITE-04"},
        )
        required = {
            "totalDevices",
            "onlineDevices",
            "normalAssets",
            "warningAssets",
            "criticalAssets",
            "unreviewedEvents",
            "lastReceivedAt",
        }
        self.assertTrue(all(required <= row.keys() for row in rows))
        west_rows = dashboard_sites_summary(operator, region="west_coast")
        self.assertTrue(all(row["region"] == "west_coast" for row in west_rows))

    def test_connectivity_history_and_hardware_replacement_preserve_asset(self) -> None:
        admin = self.user("admin", "admin123")
        created = create_connectivity_test(
            admin,
            "DEV-01-GEN-01",
            {
                "testedAt": utc_text(-10),
                "phase": "before",
                "rssiDbm": -67,
                "packetLossPct": 0.4,
                "retryRatePct": 1.2,
                "latencyMs": 85,
                "verdict": "pass",
            },
        )
        history = connectivity_tests_for_device(
            admin, "DEV-01-GEN-01", phase="before", page=1, size=10
        )
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["latest"]["id"], created["id"])
        self.assertEqual(device_health_for("DEV-01-GEN-01")["rssiDbm"], -67)

        original_asset_id = get_device("DEV-01-GEN-01")["assetId"]
        updated = update_device_hardware_profile(
            admin,
            "DEV-01-GEN-01",
            {
                "profileId": "HW-PICO2-DUAL-SENSOR-GATEWAY",
                "replacedComponents": ["board", "connectivity"],
                "reason": "Gateway field validation",
                "effectiveAt": utc_text(),
            },
        )
        self.assertEqual(updated["assetId"], original_asset_id)
        self.assertEqual(updated["profileId"], "HW-PICO2-DUAL-SENSOR-GATEWAY")
        self.assertEqual(
            updated["hardwareProfileHistory"][0]["assetId"], original_asset_id
        )
        self.assertEqual(
            len(hardware_profiles(board_type="pico-2", connectivity_type="gateway")), 1
        )

    def test_service_health_exposes_ingest_metrics_and_failure_history(self) -> None:
        ingest_telemetry(self.principal, telemetry_payload())
        with self.assertRaises(ApiError):
            ingest_telemetry(
                self.principal, telemetry_payload(sequence=2, acousticDb=51.2)
            )

        health = service_health_dependencies()
        self.assertEqual(health["ingestMetrics"]["requests"], 2)
        self.assertEqual(health["ingestMetrics"]["accepted"], 1)
        self.assertEqual(health["ingestMetrics"]["rejected"], 1)
        self.assertGreaterEqual(health["quarantinedMessageCount"], 1)
        self.assertEqual(health["events"][-1]["status"], "failure")


class Week2HttpSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()
        self.server = create_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        token: str = "",
    ) -> tuple[int, dict | list]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if payload is not None:
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def login(self, username: str, password: str) -> str:
        status, body = self.request(
            "/api/auth/login",
            method="POST",
            payload={"username": username, "password": password},
        )
        self.assertEqual(status, 200)
        return body["session"]["token"]

    def test_week2_http_contract(self) -> None:
        operator_token = self.login("operator", "operator123")
        admin_token = self.login("admin", "admin123")
        system_token = self.login("system", "system123")

        ingest_payload = telemetry_payload()
        status, missing_auth = self.request(
            "/api/telemetry/ingest",
            method="POST",
            payload=ingest_payload,
        )
        self.assertEqual(status, 401)
        self.assertEqual(missing_auth["error"]["code"], "AUTH_REQUIRED")
        status, forbidden_ingest = self.request(
            "/api/telemetry/ingest",
            method="POST",
            payload=ingest_payload,
            token=admin_token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            forbidden_ingest["error"]["code"], "TELEMETRY_INGEST_FORBIDDEN"
        )
        status, accepted = self.request(
            "/api/telemetry/ingest",
            method="POST",
            payload=ingest_payload,
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 201)
        self.assertFalse(accepted["duplicate"])
        status, duplicate = self.request(
            "/api/telemetry/ingest",
            method="POST",
            payload=ingest_payload,
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])

        status, telemetry = self.request(
            "/api/telemetry?siteId=SITE-01&assetId=SITE-01-GEN-01",
            token=operator_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(telemetry["points"]), 1)
        self.assertIn("vibrationRmsRaw", telemetry["units"])

        status, summary = self.request(
            "/api/dashboard/sites-summary", token=operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(summary), 4)

        status, forbidden = self.request("/api/health/dependencies", token=admin_token)
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "FORBIDDEN")
        status, dependencies = self.request(
            "/api/health/dependencies", token=system_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(dependencies["ingestMetrics"]["accepted"], 1)

        status, connectivity = self.request(
            "/api/devices/DEV-01-GEN-01/connectivity-tests",
            method="POST",
            token=admin_token,
            payload={
                "testedAt": utc_text(),
                "phase": "after",
                "rssiDbm": -60,
                "packetLossPct": 0.1,
                "retryRatePct": 0.2,
                "latencyMs": 55,
                "verdict": "pass",
            },
        )
        self.assertEqual(status, 201)
        status, connectivity_history = self.request(
            "/api/devices/DEV-01-GEN-01/connectivity-tests?phase=after",
            token=operator_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(connectivity_history["latest"]["id"], connectivity["id"])

        status, profiles = self.request(
            "/api/device-hardware-profiles?boardType=pico-2", token=operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(profiles), 1)
        status, updated = self.request(
            "/api/devices/DEV-01-GEN-01/hardware-profile",
            method="PUT",
            token=admin_token,
            payload={
                "profileId": profiles[0]["id"],
                "replacedComponents": ["board", "connectivity"],
                "reason": "HTTP integration test",
                "effectiveAt": utc_text(),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["assetId"], "SITE-01-GEN-01")


if __name__ == "__main__":
    unittest.main()
