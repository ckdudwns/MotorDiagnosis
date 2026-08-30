from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai.ai2.week1.prepare_ai1_handoff import build_replay_record
from ai.ai2.week1.replay_telemetry import (
    DEFAULT_BACKEND_ASSET_ID,
    DEFAULT_BACKEND_DEVICE_ID,
    DEFAULT_BACKEND_SITE_ID,
    load_replay_payloads,
    post_payload,
    remap_payload,
)
from ai.ai2.week2.anomaly_score import latest_asset_statuses

from motor_diagnosis.data import (
    EVENTS,
    QUARANTINED_DEVICE_MESSAGES,
    TELEMETRY_METRICS,
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
    ingest_telemetry,
    reset_runtime_state,
    review_event,
    service_health_dependencies,
    telemetry_for,
    telemetry_principal_for_token,
    telemetry_units,
    update_device_hardware_profile,
)
from motor_diagnosis.mqtt_service import (
    MqttBridgeError,
    MqttRetryQueue,
    configure_mqtt_callbacks,
    forward_mqtt_message,
    is_permanent_ingest_error,
    process_mqtt_message,
    quarantine_local_mqtt_message,
    report_mqtt_status,
    subscription_is_granted,
)
from motor_diagnosis.server import authorized_events, create_server
from motor_diagnosis.web import render_page


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
        payload = telemetry_payload()
        accepted, accepted_status = ingest_telemetry(self.principal, payload)
        duplicate, duplicate_status = ingest_telemetry(self.principal, payload)

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

        conflict_payload = {**payload, "rpm": 1801.0}
        with self.assertRaises(ApiError) as conflict:
            ingest_telemetry(self.principal, conflict_payload)
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
            (
                telemetry_payload(vibrationRmsRaw=-0.01),
                "INVALID_TELEMETRY_PAYLOAD",
            ),
        ]
        for payload, error_code in invalid_payloads:
            with self.subTest(error_code=error_code):
                with self.assertRaises(ApiError) as context:
                    ingest_telemetry(self.principal, payload)
                self.assertEqual(context.exception.code, error_code)
        self.assertEqual(len(TELEMETRY_RECORDS), 0)
        self.assertEqual(len(QUARANTINED_DEVICE_MESSAGES), 5)

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

    def test_latest_raw_score_updates_site_summary_asset_counts(self) -> None:
        ingest_telemetry(
            self.principal,
            telemetry_payload(vibrationRmsRaw=0.10),
        )

        summaries = dashboard_sites_summary(
            self.user("admin", "admin123"),
            live_asset_statuses=latest_asset_statuses(TELEMETRY_RECORDS),
        )
        site = next(row for row in summaries if row["siteId"] == "SITE-01")

        self.assertEqual(site["criticalAssets"], 2)
        self.assertEqual(site["warningAssets"], 0)

    def test_non_asset_anomaly_event_no_longer_sets_asset_status(self) -> None:
        for label in (
            "normal_false_positive",
            "repair_completed",
            "sensor_issue",
        ):
            with self.subTest(label=label):
                reset_runtime_state()
                review_event(
                    self.user("admin", "admin123"),
                    "EV-241",
                    {"label": label, "note": "reviewed"},
                )
                summaries = dashboard_sites_summary(self.user("admin", "admin123"))
                site = next(row for row in summaries if row["siteId"] == "SITE-01")

                self.assertEqual(site["criticalAssets"], 0)
                self.assertEqual(site["unreviewedEvents"], 0)

    def test_events_sort_by_parsed_rfc3339_time(self) -> None:
        EVENTS.extend(
            [
                {
                    "id": "EV-OFFSET",
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-GEN-01",
                    "occurredAt": "2026-08-24T10:00:00+09:00",
                },
                {
                    "id": "EV-FRACTION",
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-GEN-01",
                    "occurredAt": "2026-08-24T02:00:00.500Z",
                },
            ]
        )

        event_ids = [
            event["id"] for event in authorized_events(self.user("admin", "admin123"))
        ]

        self.assertLess(event_ids.index("EV-FRACTION"), event_ids.index("EV-OFFSET"))

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

        required_metrics = (
            "rssiDbm",
            "packetLossPct",
            "retryRatePct",
            "latencyMs",
        )
        base_payload = {
            "testedAt": utc_text(),
            "phase": "after",
            "rssiDbm": -61,
            "packetLossPct": 0.2,
            "retryRatePct": 0.3,
            "latencyMs": 40,
            "verdict": "pass",
        }
        for missing_field in required_metrics:
            with self.subTest(missing_field=missing_field):
                invalid_payload = dict(base_payload)
                invalid_payload.pop(missing_field)
                with self.assertRaises(ApiError) as context:
                    create_connectivity_test(admin, "DEV-01-GEN-01", invalid_payload)
                self.assertEqual(context.exception.status, 400)
                self.assertEqual(context.exception.code, "INVALID_CONNECTIVITY_TEST")
        invalid_numbers = {
            "rssiDbm": "not-a-number",
            "packetLossPct": float("inf"),
            "retryRatePct": "NaN",
            "latencyMs": 10**1000,
        }
        for invalid_field, invalid_value in invalid_numbers.items():
            with self.subTest(invalid_field=invalid_field):
                invalid_payload = dict(base_payload)
                invalid_payload[invalid_field] = invalid_value
                with self.assertRaises(ApiError) as context:
                    create_connectivity_test(admin, "DEV-01-GEN-01", invalid_payload)
                self.assertEqual(context.exception.status, 400)
                self.assertEqual(context.exception.code, "INVALID_NUMBER")
        self.assertEqual(
            connectivity_tests_for_device(admin, "DEV-01-GEN-01", page=1, size=10)[
                "total"
            ],
            1,
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

        ingest_telemetry(self.principal, telemetry_payload(sequence=3))
        still_degraded = service_health_dependencies()
        self.assertEqual(still_degraded["status"], "degraded")
        self.assertFalse(
            any(event["status"] == "recovered" for event in still_degraded["events"])
        )

        for sequence in range(4, 12):
            ingest_telemetry(self.principal, telemetry_payload(sequence=sequence))
        recovered = service_health_dependencies()
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(
            sum(event["status"] == "recovered" for event in recovered["events"]),
            1,
        )

    def test_mqtt_ack_policy_distinguishes_success_retry_and_quarantine(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        client = FakeClient()
        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=b"{}",
            mid=7,
            qos=1,
        )
        accepted_response = {
            "deviceId": "DEV-01-GEN-01",
            "sequence": 1,
            "duplicate": False,
        }
        with patch(
            "motor_diagnosis.mqtt_service.forward_mqtt_message",
            return_value=(accepted_response, 201),
        ):
            result = process_mqtt_message(
                client,
                message,
                endpoint="http://backend/api/telemetry/ingest",
                token="token",
            )
        self.assertEqual(result, "accepted")
        self.assertEqual(client.ack_calls, [(7, 1)])

        message.mid = 8
        with patch(
            "motor_diagnosis.mqtt_service.forward_mqtt_message",
            side_effect=MqttBridgeError(503, "INGEST_UNAVAILABLE", "temporary"),
        ):
            result = process_mqtt_message(
                client,
                message,
                endpoint="http://backend/api/telemetry/ingest",
                token="token",
            )
        self.assertEqual(result, "retry")
        self.assertEqual(client.ack_calls, [(7, 1)])

        message.mid = 9
        message.qos = 2
        with patch(
            "motor_diagnosis.mqtt_service.forward_mqtt_message",
            side_effect=MqttBridgeError(400, "INVALID_TELEMETRY_PAYLOAD", "bad"),
        ):
            result = process_mqtt_message(
                client,
                message,
                endpoint="http://backend/api/telemetry/ingest",
                token="token",
            )
        self.assertEqual(result, "quarantined")
        self.assertEqual(client.ack_calls, [(7, 1), (9, 2)])

        for status, code in (
            (401, "AUTH_REQUIRED"),
            (403, "TELEMETRY_INGEST_FORBIDDEN"),
            (404, "NOT_FOUND"),
            (413, "REQUEST_TOO_LARGE"),
        ):
            with self.subTest(status=status, code=code):
                message.mid += 1
                with patch(
                    "motor_diagnosis.mqtt_service.forward_mqtt_message",
                    side_effect=MqttBridgeError(status, code, "not quarantined"),
                ):
                    result = process_mqtt_message(
                        client,
                        message,
                        endpoint="http://backend/api/telemetry/ingest",
                        token="token",
                    )
                self.assertEqual(result, "retry")
                self.assertEqual(client.ack_calls, [(7, 1), (9, 2)])

        for status, code in (
            (400, "INVALID_TELEMETRY_PAYLOAD"),
            (404, "DEVICE_NOT_FOUND"),
            (409, "DEVICE_MAPPING_MISMATCH"),
            (409, "SEQUENCE_CONFLICT"),
        ):
            with self.subTest(quarantined_status=status, quarantined_code=code):
                self.assertTrue(
                    is_permanent_ingest_error(MqttBridgeError(status, code, "stored"))
                )

    def test_mqtt_retry_queue_performs_a_second_http_delivery_before_ack(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []
                self.acked = threading.Event()

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                self.acked.set()
                return 0

        client = FakeClient()
        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=json.dumps(telemetry_payload()).encode("utf-8"),
            mid=21,
            qos=1,
        )
        accepted_response = {
            "deviceId": "DEV-01-GEN-01",
            "sequence": 1,
            "duplicate": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            retry_queue = MqttRetryQueue(
                database_path=Path(directory) / "mqtt-retry.sqlite3",
                ingest_endpoint="http://backend/api/telemetry/ingest",
                quarantine_endpoint="http://backend/api/telemetry/quarantine",
                token="token",
                initial_delay=0,
            )
            with patch(
                "motor_diagnosis.mqtt_service.forward_mqtt_message",
                side_effect=[
                    MqttBridgeError(503, "INGEST_UNAVAILABLE", "temporary"),
                    (accepted_response, 201),
                ],
            ) as forward:
                result = process_mqtt_message(
                    client,
                    message,
                    endpoint="http://backend/api/telemetry/ingest",
                    quarantine_endpoint="http://backend/api/telemetry/quarantine",
                    token="token",
                    retry_queue=retry_queue,
                )
                self.assertEqual(result, "retry")
                self.assertEqual(retry_queue.pending_count(), 1)
                self.assertEqual(client.ack_calls, [])
                reopened_queue = MqttRetryQueue(
                    database_path=Path(directory) / "mqtt-retry.sqlite3",
                    ingest_endpoint="http://backend/api/telemetry/ingest",
                    quarantine_endpoint="http://backend/api/telemetry/quarantine",
                    token="token",
                    initial_delay=0,
                )
                self.assertEqual(reopened_queue.pending_count(), 1)

                retry_queue.start()
                try:
                    self.assertTrue(client.acked.wait(timeout=2))
                finally:
                    retry_queue.stop()
                self.assertEqual(forward.call_count, 2)
                self.assertEqual(client.ack_calls, [(21, 1)])
                self.assertEqual(retry_queue.pending_count(), 0)

    def test_mqtt_health_waits_for_successful_suback(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.next_mid = 30

            def subscribe(self, _topic: str, *, qos: int) -> tuple[int, int]:
                self.next_mid += 1
                return 0, self.next_mid

        args = SimpleNamespace(
            topic="devices/+/telemetry",
            qos=1,
            health_endpoint="http://backend/api/health/dependencies/mqtt",
            ingest_endpoint="http://backend/api/telemetry/ingest",
            quarantine_endpoint="http://backend/api/telemetry/quarantine",
            ingest_token="token",
        )
        self.assertTrue(subscription_is_granted([1], requested_qos=1))
        self.assertTrue(subscription_is_granted([2], requested_qos=1))
        self.assertFalse(subscription_is_granted([0], requested_qos=1))
        self.assertTrue(subscription_is_granted([2], requested_qos=2))
        self.assertFalse(subscription_is_granted([1], requested_qos=2))
        self.assertFalse(subscription_is_granted([128], requested_qos=1))
        self.assertFalse(subscription_is_granted([], requested_qos=1))

        with tempfile.TemporaryDirectory() as directory:
            retry_queue = MqttRetryQueue(
                database_path=Path(directory) / "mqtt-retry.sqlite3",
                ingest_endpoint=args.ingest_endpoint,
                quarantine_endpoint=args.quarantine_endpoint,
                token=args.ingest_token,
            )
            client = FakeClient()
            with patch(
                "motor_diagnosis.mqtt_service.report_mqtt_status_safely"
            ) as report:
                pending = configure_mqtt_callbacks(client, args, retry_queue)
                client.on_connect(client, None, None, 0, None)
                self.assertEqual(pending, {31})
                report.assert_not_called()

                client.on_subscribe(client, None, 31, [1], None)
                self.assertEqual(pending, set())
                self.assertEqual(report.call_args.kwargs["status"], "healthy")

                report.reset_mock()
                client.on_connect(client, None, None, 0, None)
                self.assertEqual(pending, {32})
                report.assert_not_called()
                client.on_subscribe(client, None, 32, [128], None)
                self.assertEqual(report.call_args.kwargs["status"], "degraded")
                self.assertEqual(
                    report.call_args.kwargs["error_code"],
                    "MQTT_SUBSCRIBE_REJECTED",
                )

                report.reset_mock()
                client.on_connect(client, None, None, 0, None)
                self.assertEqual(pending, {33})
                report.assert_not_called()
                client.on_subscribe(client, None, 33, [0], None)
                self.assertEqual(report.call_args.kwargs["status"], "degraded")
                self.assertEqual(
                    report.call_args.kwargs["error_code"],
                    "MQTT_SUBSCRIBE_QOS_DOWNGRADED",
                )


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

    def request_text(self, path: str, token: str) -> tuple[int, str]:
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8-sig")
        except HTTPError as error:
            return error.code, error.read().decode("utf-8")

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
        self.assertIn("anomalyScore", telemetry["points"][0])
        self.assertIn("anomalyStatus", telemetry["points"][0])

        event_from = utc_text(-60 * 60)
        status, filtered_events = self.request(
            f"/api/events?siteId=SITE-01&assetId=SITE-01-MOT-02&from={event_from}",
            token=operator_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(filtered_events["page"], 1)
        self.assertGreater(filtered_events["total"], 0)
        self.assertTrue(filtered_events["items"])
        self.assertTrue(
            all(
                event["siteId"] == "SITE-01" and event["assetId"] == "SITE-01-MOT-02"
                for event in filtered_events["items"]
            )
        )
        self.assertTrue(
            all("occurredAt" in event for event in filtered_events["items"])
        )
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

    def test_raw_telemetry_csv_export_uses_timestamp_fields(self) -> None:
        admin_token = self.login("admin", "admin123")
        status, accepted = self.request(
            "/api/telemetry/ingest",
            method="POST",
            payload=telemetry_payload(),
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 201)
        self.assertEqual(accepted["sequence"], 1)

        status, csv_body = self.request_text(
            "/api/export?siteId=SITE-01&assetId=SITE-01-GEN-01",
            admin_token,
        )

        self.assertEqual(status, 200)
        self.assertIn("timestamp,sequence,site_id,asset_id,device_id", csv_body)
        self.assertIn("vibration_rms_raw", csv_body)
        self.assertIn("SITE-01-GEN-01", csv_body)

    def test_mqtt_bridge_forwards_into_http_server_storage(self) -> None:
        payload = telemetry_payload()
        accepted, status = forward_mqtt_message(
            "devices/DEV-01-GEN-01/telemetry",
            json.dumps(payload),
            endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            token="demo-mqtt-ingest-token",
        )
        self.assertEqual(status, 201)
        self.assertFalse(accepted["duplicate"])
        duplicate, duplicate_status = forward_mqtt_message(
            "devices/DEV-01-GEN-01/telemetry",
            json.dumps(payload),
            endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            token="demo-mqtt-ingest-token",
        )
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["duplicate"])

        operator_token = self.login("operator", "operator123")
        query_status, telemetry = self.request(
            "/api/telemetry?siteId=SITE-01&assetId=SITE-01-GEN-01",
            token=operator_token,
        )
        self.assertEqual(query_status, 200)
        self.assertEqual([point["sequence"] for point in telemetry["points"]], [1])

        with self.assertRaises(MqttBridgeError) as mismatch:
            forward_mqtt_message(
                "devices/DEV-OTHER/telemetry",
                json.dumps(payload),
                endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
                token="demo-mqtt-ingest-token",
            )
        self.assertEqual(mismatch.exception.code, "DEVICE_MAPPING_MISMATCH")

        class AckClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        ack_client = AckClient()
        invalid_message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=json.dumps(telemetry_payload(sequence=2, acousticDb=51.2)),
            mid=12,
            qos=1,
        )
        delivery = process_mqtt_message(
            ack_client,
            invalid_message,
            endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            token="demo-mqtt-ingest-token",
        )
        self.assertEqual(delivery, "quarantined")
        self.assertEqual(ack_client.ack_calls, [(12, 1)])
        self.assertEqual(
            QUARANTINED_DEVICE_MESSAGES[-1]["reason"], "INVALID_RAW_ONLY_FIELD"
        )

    def test_local_mqtt_errors_are_stored_before_ack_and_counted(self) -> None:
        class AckClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        client = AckClient()
        endpoint = f"http://127.0.0.1:{self.port}/api/telemetry/ingest"
        quarantine_endpoint = f"http://127.0.0.1:{self.port}/api/telemetry/quarantine"
        malformed = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=b"{not-json",
            mid=51,
            qos=1,
        )
        mismatched = SimpleNamespace(
            topic="devices/DEV-OTHER/telemetry",
            payload=json.dumps(telemetry_payload(sequence=52)).encode("utf-8"),
            mid=52,
            qos=1,
        )

        first = process_mqtt_message(
            client,
            malformed,
            endpoint=endpoint,
            quarantine_endpoint=quarantine_endpoint,
            token="demo-mqtt-ingest-token",
        )
        second = process_mqtt_message(
            client,
            mismatched,
            endpoint=endpoint,
            quarantine_endpoint=quarantine_endpoint,
            token="demo-mqtt-ingest-token",
        )

        self.assertEqual((first, second), ("quarantined", "quarantined"))
        self.assertEqual(client.ack_calls, [(51, 1), (52, 1)])
        self.assertEqual(
            [record["reason"] for record in QUARANTINED_DEVICE_MESSAGES],
            ["INVALID_JSON", "DEVICE_MAPPING_MISMATCH"],
        )
        self.assertTrue(
            all(
                record["source"] == "mqtt_bridge"
                for record in QUARANTINED_DEVICE_MESSAGES
            )
        )
        self.assertEqual(TELEMETRY_METRICS["localRejected"], 2)
        self.assertEqual(TELEMETRY_METRICS["rejected"], 2)

        forbidden_status, forbidden = self.request(
            "/api/telemetry/quarantine",
            method="POST",
            token="demo-telemetry-ingest-token",
            payload={
                "topic": malformed.topic,
                "payload": "bad",
                "reason": "INVALID_JSON",
                "message": "invalid",
            },
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden["error"]["code"], "TELEMETRY_QUARANTINE_FORBIDDEN")

    def test_mqtt_lone_surrogate_is_quarantined_before_ack(self) -> None:
        class AckClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        client = AckClient()
        payload = telemetry_payload(sequence=53, source="\ud800")
        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=json.dumps(payload).encode("utf-8"),
            mid=53,
            qos=1,
        )

        delivery = process_mqtt_message(
            client,
            message,
            endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            quarantine_endpoint=(
                f"http://127.0.0.1:{self.port}/api/telemetry/quarantine"
            ),
            token="demo-mqtt-ingest-token",
        )

        self.assertEqual(delivery, "quarantined")
        self.assertEqual(client.ack_calls, [(53, 1)])
        self.assertEqual(QUARANTINED_DEVICE_MESSAGES[-1]["reason"], "INVALID_JSON")
        self.assertEqual(TELEMETRY_METRICS["localRejected"], 1)
        self.assertEqual(TELEMETRY_METRICS["rejected"], 1)

    def test_raw_str_surrogate_is_safely_quarantined_or_queued(self) -> None:
        class AckClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        raw_payload = json.dumps(
            telemetry_payload(sequence=54, source="\ud800"), ensure_ascii=False
        )
        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=raw_payload,
            mid=54,
            qos=1,
        )
        client = AckClient()

        delivery = process_mqtt_message(
            client,
            message,
            endpoint=f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            quarantine_endpoint=(
                f"http://127.0.0.1:{self.port}/api/telemetry/quarantine"
            ),
            token="demo-mqtt-ingest-token",
        )

        self.assertEqual(delivery, "quarantined")
        self.assertEqual(client.ack_calls, [(54, 1)])
        self.assertEqual(QUARANTINED_DEVICE_MESSAGES[-1]["reason"], "INVALID_JSON")
        self.assertIn("\\ud800", QUARANTINED_DEVICE_MESSAGES[-1]["payload"])

        queued_client = AckClient()
        queued_message = SimpleNamespace(**{**vars(message), "mid": 55})
        with tempfile.TemporaryDirectory() as directory:
            quarantine_endpoint = (
                f"http://127.0.0.1:{self.port}/api/telemetry/quarantine"
            )
            retry_queue = MqttRetryQueue(
                database_path=Path(directory) / "raw-str-retry.sqlite3",
                ingest_endpoint="http://backend/api/telemetry/ingest",
                quarantine_endpoint=quarantine_endpoint,
                token="demo-mqtt-ingest-token",
                initial_delay=0,
            )

            failures_remaining = 2

            def flaky_quarantine(*args, **kwargs):
                nonlocal failures_remaining
                if failures_remaining:
                    failures_remaining -= 1
                    raise MqttBridgeError(503, "HTTP_SERVICE_UNAVAILABLE", "temporary")
                return quarantine_local_mqtt_message(*args, **kwargs)

            with patch(
                "motor_diagnosis.mqtt_service.quarantine_local_mqtt_message",
                side_effect=flaky_quarantine,
            ) as quarantine:
                queued_delivery = process_mqtt_message(
                    queued_client,
                    queued_message,
                    endpoint="http://backend/api/telemetry/ingest",
                    quarantine_endpoint=quarantine_endpoint,
                    token="demo-mqtt-ingest-token",
                    retry_queue=retry_queue,
                )
                self.assertEqual(queued_delivery, "retry")
                self.assertEqual(retry_queue.pending_count(), 1)
                self.assertEqual(
                    retry_queue.process_due_once(now=float("inf")), "retry"
                )
                with retry_queue._connection() as connection:
                    pending = connection.execute(
                        "SELECT * FROM mqtt_retry_queue"
                    ).fetchone()
                self.assertEqual(pending["error_code"], "INVALID_JSON")
                self.assertEqual(pending["last_error_code"], "HTTP_SERVICE_UNAVAILABLE")
                self.assertEqual(
                    retry_queue.process_due_once(now=float("inf")), "quarantined"
                )

            self.assertEqual(quarantine.call_count, 3)
            self.assertEqual(quarantine.call_args_list[-1].args[4].code, "INVALID_JSON")
            self.assertEqual(QUARANTINED_DEVICE_MESSAGES[-1]["reason"], "INVALID_JSON")
            self.assertEqual(retry_queue.pending_count(), 0)
            self.assertEqual(queued_client.ack_calls, [(55, 1)])

    def test_quarantine_ack_failure_retries_ack_without_duplicate_storage(self) -> None:
        class FlakyAckClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []
                self.results = iter((1, 0))

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return next(self.results)

        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload="{invalid-json",
            mid=56,
            qos=1,
        )
        client = FlakyAckClient()
        initial_records = len(QUARANTINED_DEVICE_MESSAGES)
        initial_rejected = int(TELEMETRY_METRICS["rejected"])
        initial_local_rejected = int(TELEMETRY_METRICS["localRejected"])
        quarantine_endpoint = f"http://127.0.0.1:{self.port}/api/telemetry/quarantine"

        with tempfile.TemporaryDirectory() as directory:
            retry_queue = MqttRetryQueue(
                database_path=Path(directory) / "ack-only-retry.sqlite3",
                ingest_endpoint="http://backend/api/telemetry/ingest",
                quarantine_endpoint=quarantine_endpoint,
                token="demo-mqtt-ingest-token",
                initial_delay=0,
            )
            with patch(
                "motor_diagnosis.mqtt_service.quarantine_local_mqtt_message",
                wraps=quarantine_local_mqtt_message,
            ) as quarantine:
                delivery = process_mqtt_message(
                    client,
                    message,
                    endpoint="http://backend/api/telemetry/ingest",
                    quarantine_endpoint=quarantine_endpoint,
                    token="demo-mqtt-ingest-token",
                    retry_queue=retry_queue,
                )
                self.assertEqual(delivery, "retry")
                self.assertEqual(retry_queue.pending_count(), 1)
                self.assertEqual(
                    retry_queue.process_due_once(now=float("inf")), "quarantined"
                )

            self.assertEqual(quarantine.call_count, 1)
            self.assertEqual(retry_queue.pending_count(), 0)

        self.assertEqual(client.ack_calls, [(56, 1), (56, 1)])
        self.assertEqual(len(QUARANTINED_DEVICE_MESSAGES), initial_records + 1)
        self.assertEqual(TELEMETRY_METRICS["rejected"], initial_rejected + 1)
        self.assertEqual(TELEMETRY_METRICS["localRejected"], initial_local_rejected + 1)

    def test_actual_ai2_replay_output_is_accepted_and_queryable(self) -> None:
        source_asset_id = "SYN-ASSET-01"
        generated = build_replay_record(
            {
                "asset_id": source_asset_id,
                "rpm": "1796.0",
                "vibration_rms_raw": "0.079035",
                "vibration_rms_mm_s": "",
                "vibration_peak_hz": "1037.11",
                "acoustic_rms_raw": "0.007019",
                "acoustic_db": "",
                "acoustic_peak_hz": "216.4",
                "scenario_label": "normal",
                "known_vibration_label": "NORMAL",
                "known_acoustic_label": "",
                "source": "CWRU_only_synthetic",
                "is_synthetic": "true",
                "vibration_unit_note": "raw accelerometer output; not mm/s",
                "acoustic_unit_note": "raw waveform RMS; not dB SPL",
            },
            sequence=41,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=2),
            site_id="SYN-SITE-01",
            device_map={source_asset_id: "SYN-DEV-01"},
        )

        with tempfile.TemporaryDirectory() as directory:
            replay_path = Path(directory) / "ai1_telemetry_replay.jsonl"
            replay_path.write_text(
                json.dumps(generated, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            replay_payload = load_replay_payloads(replay_path)[0]

        self.assertEqual(replay_payload["siteId"], "SYN-SITE-01")
        mapped_payload = remap_payload(
            replay_payload,
            site_id=DEFAULT_BACKEND_SITE_ID,
            asset_id=DEFAULT_BACKEND_ASSET_ID,
            device_id=DEFAULT_BACKEND_DEVICE_ID,
        )
        status, accepted = post_payload(
            f"http://127.0.0.1:{self.port}/api/telemetry/ingest",
            "demo-telemetry-ingest-token",
            mapped_payload,
        )
        self.assertEqual(status, 201)
        self.assertEqual(accepted["deviceId"], DEFAULT_BACKEND_DEVICE_ID)

        admin_token = self.login("admin", "admin123")
        query_status, telemetry = self.request(
            "/api/telemetry?siteId=SITE-01&assetId=SITE-01-GEN-01",
            token=admin_token,
        )
        self.assertEqual(query_status, 200)
        self.assertEqual([point["sequence"] for point in telemetry["points"]], [41])

    def test_mqtt_connection_status_is_reflected_in_dependency_health(self) -> None:
        endpoint = f"http://127.0.0.1:{self.port}/api/health/dependencies/mqtt"
        failed = report_mqtt_status(
            endpoint,
            "demo-mqtt-ingest-token",
            "degraded",
            detail="Broker connection attempt failed.",
            error_code="MQTT_CONNECT_FAILED",
        )
        self.assertEqual(failed["status"], "degraded")
        self.assertIsNotNone(failed["lastFailureAt"])
        self.assertEqual(failed["errorRatePct"], 100.0)

        system_token = self.login("system", "system123")
        status, health = self.request("/api/health/dependencies", token=system_token)
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "degraded")
        mqtt = next(item for item in health["dependencies"] if item["id"] == "mqtt")
        self.assertEqual(mqtt["detail"], "Broker connection attempt failed.")
        self.assertEqual(health["events"][-1]["errorCode"], "MQTT_CONNECT_FAILED")

        recovered = report_mqtt_status(
            endpoint,
            "demo-mqtt-ingest-token",
            "healthy",
            detail="Subscribed to devices/+/telemetry with QoS 1",
        )
        self.assertEqual(recovered["status"], "healthy")
        self.assertIsNotNone(recovered["lastRecoveryAt"])
        self.assertEqual(recovered["errorRatePct"], 50.0)
        status, health = self.request("/api/health/dependencies", token=system_token)
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["events"][-1]["status"], "recovered")

        forbidden_status, forbidden = self.request(
            "/api/health/dependencies/mqtt",
            method="POST",
            token="demo-telemetry-ingest-token",
            payload={"status": "degraded"},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden["error"]["code"], "SERVICE_HEALTH_WRITE_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
