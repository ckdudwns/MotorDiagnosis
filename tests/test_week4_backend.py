from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from motor_diagnosis import data
from motor_diagnosis.alerts import AlertService, DeliveryError, configured_adapters
from motor_diagnosis.model_registry import (
    create_baseline_version,
    create_model_version,
    versions_for,
)
from motor_diagnosis.server import create_server
from motor_diagnosis.telemetry_bulk import ingest_telemetry_bulk


def user(name):
    login = data.authenticate({"username": name, "password": f"{name}123"})
    return data.current_user_for_token(login["session"]["token"])


def dataset_payload(site="SITE-01", checksum="source"):
    return {
        "name": "Week 4 existing-data baseline",
        "source": {
            "type": "external",
            "uri": "https://example.org/dataset",
            "license": "project-owned",
            "checksum": "sha256:" + hashlib.sha256(checksum.encode()).hexdigest(),
        },
        "compatibility": {
            "signalType": ["vibration"],
            "samplingRateHz": 12000,
            "units": {"vibration": "g"},
            "operatingConditions": {"rpm": 1800},
        },
        "sourceFilters": {"siteId": site},
        "labelTaxonomyVersion": "ACOUSTIC-V1",
        "labelMapping": {"normal": "NORMAL"},
        "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
        "reason": "Week 4 metadata integration",
    }


def telemetry(sequence, *, timestamp=None):
    return {
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "deviceId": "DEV-01-MOT-02",
        "sequence": sequence,
        "timestamp": timestamp or data.now_iso(),
        "rpm": 1796,
        "vibrationRmsRaw": 0.08,
        "acousticRmsRaw": 0.007,
        "vibrationRmsMmS": None,
        "acousticDb": None,
        "scenarioLabel": "normal",
        "isSynthetic": True,
        "source": "week4-replay-test",
        "vibrationPeakHz": 29.9,
        "acousticPeakHz": 150,
        "vibrationUnitNote": "raw RMS",
        "acousticUnitNote": "raw RMS",
    }


class Week4AlertsTest(unittest.TestCase):
    def setUp(self):
        data.reset_runtime_state()
        self.admin, self.operator = user("admin"), user("operator")
        self.now = 1788177600.0
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()

    def service(self, database=":memory:", adapters=None):
        result = AlertService(database, adapters=adapters, clock=lambda: self.now)
        self.services.append(result)
        return result

    def configure(self, channels, **kwargs):
        return data.update_alert_policy(
            self.admin, "ALERT-POLICY-DEFAULT", {"channels": channels, **kwargs}
        )

    def event(self, site="SITE-01"):
        return data.inject_anomaly({"siteId": site, "assetId": f"{site}-MOT-02"})

    def test_channel_failure_retry_recovery_and_web_delivery_are_independent(self):
        calls = []

        def failing_then_recovered(row):
            calls.append(row["id"])
            if len(calls) < 3:
                raise DeliveryError("WEBHOOK_HTTP_503")

        self.configure(["web", "webhook", "email", "stub"])
        service = self.service(adapters={"webhook": failing_then_recovered})
        event = self.event()
        service.send(self.admin, {"eventId": event["id"]})
        service.process_due()
        rows = {row["channel"]: row for row in service.list_for(self.operator)["items"]}
        self.assertEqual(rows["web"]["status"], "sent")
        self.assertEqual(rows["stub"]["status"], "sent")
        self.assertEqual(rows["email"]["lastError"], "CHANNEL_NOT_CONFIGURED")
        self.assertEqual(rows["webhook"]["status"], "pending")
        self.now += 2
        service.process_due()
        self.now += 4
        service.process_due()
        row = service.list_for(self.admin, channel="webhook")["items"][0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(
            [attempt["success"] for attempt in row["attempts"]], [False, False, True]
        )
        self.assertEqual(len(set(calls)), 1)
        service.send(self.admin, {"eventId": event["id"]})
        service.process_due()
        self.assertEqual(len(calls), 3)

    def test_configured_webhook_uses_stable_key_and_classifies_errors(self):
        from unittest.mock import MagicMock

        with patch.dict(
            "os.environ",
            {"ALERT_WEBHOOK_URL": "https://example.org/alerts", "ALERT_SMTP_HOST": ""},
        ):
            adapter = configured_adapters()["webhook"]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 204
        with patch("motor_diagnosis.alerts.build_opener", return_value=opener):
            adapter({"id": "ALERT-stable", "eventId": "EV-1"})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Idempotency-key"), "ALERT-stable")
            for status, retryable in (
                (503, True),
                (429, True),
                (400, False),
                (302, False),
            ):
                opener.open.side_effect = HTTPError(
                    request.full_url, status, "error", {}, None
                )
                with self.assertRaises(DeliveryError) as error:
                    adapter({"id": "ALERT-stable"})
                self.assertEqual(error.exception.retryable, retryable)

    def test_configured_smtp_validates_tls_and_does_not_send_to_invalid_address(self):
        from unittest.mock import MagicMock

        settings = {
            "ALERT_WEBHOOK_URL": "",
            "ALERT_SMTP_HOST": "smtp.example.org",
            "ALERT_EMAIL_FROM": "sender@example.org",
            "ALERT_SMTP_USERNAME": "",
            "ALERT_SMTP_PASSWORD": "",
        }
        with patch.dict("os.environ", settings):
            adapter = configured_adapters()["email"]
        smtp = MagicMock()
        with patch(
            "motor_diagnosis.alerts.smtplib.SMTP_SSL", return_value=smtp
        ) as connect:
            row = {
                "id": "ALERT-stable",
                "eventId": "EV-1",
                "event": {"title": "test"},
                "recipient": "receiver@example.org",
                "isTest": True,
            }
            adapter(row)
            self.assertTrue(connect.call_args.kwargs["context"].check_hostname)
            self.assertEqual(connect.call_args.kwargs["timeout"], 5)
            message = smtp.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message["Message-ID"], "<ALERT-stable@bind-edge-ai.local>")
            with self.assertRaises(DeliveryError):
                adapter(
                    {**row, "recipient": "receiver@example.org\nBcc:other@example.org"}
                )
            self.assertEqual(connect.call_count, 1)

    def test_midnight_hours_disabled_policy_and_demo_ids_survive_reset(self):
        self.now = datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc).timestamp()
        self.configure(["web"], workHours={"start": "22:00", "end": "1:00"})
        service = self.service()
        event = self.event()
        self.assertEqual(
            len(service.send(self.admin, {"eventId": event["id"]})["deliveries"]), 1
        )
        self.configure(["web"], enabled=False)
        self.assertEqual(
            service.send(self.admin, {"eventId": event["id"], "isTest": True})[
                "suppressed"
            ][0]["reason"],
            "disabled",
        )
        data.reset_runtime_state()
        self.assertNotEqual(event["id"], self.event()["id"])

    def test_slow_external_channel_does_not_block_web_worker(self):
        entered, release = threading.Event(), threading.Event()

        def slow(row):
            entered.set()
            release.wait(3)

        self.configure(["web", "webhook"])
        service = self.service(adapters={"webhook": slow})
        service.send(self.admin, {"eventId": self.event()["id"]})
        try:
            service.tick()
            self.assertTrue(entered.wait(1))
            # Wait for the web executor only, while webhook remains blocked.
            service._pools["web"].submit(lambda: None).result(timeout=1)
            self.assertEqual(
                service.list_for(self.operator, channel="web")["items"][0]["status"],
                "sent",
            )
        finally:
            release.set()

    def test_restart_preserves_pending_retry_and_single_owner_is_enforced(self):
        self.configure(["webhook"])
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "alerts.sqlite3")
            first = self.service(
                database,
                {
                    "webhook": lambda row: (_ for _ in ()).throw(
                        DeliveryError("TEMPORARY")
                    )
                },
            )
            with self.assertRaises(ValueError):
                AlertService(database)
            event = self.event()
            first.send(self.admin, {"eventId": event["id"]})
            first.process_due()
            first.close()
            calls = []
            second = self.service(
                database, {"webhook": lambda row: calls.append(row["id"])}
            )
            self.now += 2
            second.process_due()
            second.observe_events()
            second.process_due()
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.list_for(self.admin)["items"][0]["attemptCount"], 2)
            second.close()

    def test_interrupted_send_reuses_delivery_id_after_restart(self):
        self.configure(["webhook"])
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "alerts.sqlite3")
            first = self.service(database)
            original = first.send(self.admin, {"eventId": self.event()["id"]})[
                "deliveries"
            ][0]
            self.assertEqual(len(first._claim()), 1)
            first.close()
            calls = []
            second = self.service(
                database, {"webhook": lambda row: calls.append(row["id"])}
            )
            second.process_due()
            self.assertEqual(calls, [original["id"]])
            second.close()

    def test_result_db_failure_retries_storage_without_resending(self):
        self.configure(["webhook"])
        calls = []
        service = self.service(
            adapters={"webhook": lambda row: calls.append(row["id"])}
        )
        service.send(self.admin, {"eventId": self.event()["id"]})
        row = service._claim()[0]
        with patch.object(
            service, "_save", side_effect=sqlite3.OperationalError("locked")
        ):
            service._deliver(row)
        service.process_due()
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.list_for(self.admin)["items"][0]["status"], "sent")

    def test_policy_scope_cooldown_review_work_hours_and_test_separation(self):
        service = self.service()
        self.configure(["web"], siteIds=["SITE-01"], cooldownSec=300)
        first = self.event()
        service.send(self.admin, {"eventId": first["id"]})
        second = self.event()
        self.assertEqual(
            service.send(self.admin, {"eventId": second["id"]})["suppressed"][0][
                "reason"
            ],
            "cooldown",
        )
        self.assertEqual(
            len(
                service.send(self.admin, {"eventId": second["id"], "isTest": True})[
                    "deliveries"
                ]
            ),
            1,
        )
        self.assertEqual(
            service.send(self.admin, {"eventId": self.event("SITE-05")["id"]})[
                "suppressed"
            ][0]["reason"],
            "scope_mismatch",
        )
        self.now += 301
        data.get_event(second["id"])["reviewed"] = True
        self.assertEqual(
            service.send(self.admin, {"eventId": second["id"]})["suppressed"][0][
                "reason"
            ],
            "reviewed",
        )
        self.configure(["web"], workHours={"start": "00:01", "end": "00:02"})
        self.assertEqual(
            service.send(self.admin, {"eventId": self.event()["id"]})["suppressed"][0][
                "reason"
            ],
            "outside_work_hours",
        )

    def test_delivery_and_audit_site_rights_and_no_recipient_leak(self):
        self.configure(["web", "stub"])
        service = self.service()
        for site in ("SITE-01", "SITE-05"):
            service.send(self.admin, {"eventId": self.event(site)["id"]})
        service.process_due()
        rows = service.list_for(self.operator)["items"]
        self.assertEqual({row["siteId"] for row in rows}, {"SITE-01"})
        self.assertTrue(
            all("policySnapshot" not in row and "recipient" not in row for row in rows)
        )
        logs = data.audit_logs_for(self.operator, action="alert.send", page=1, size=50)[
            "items"
        ]
        self.assertEqual({row["siteId"] for row in logs}, {"SITE-01"})
        # Audit must not re-expose recipient lists hidden by GET /api/alerts.
        self.assertNotIn("policySnapshot", json.dumps(logs))
        with self.assertRaises(data.ApiError):
            service.send(self.operator, {"eventId": self.event()["id"]})

    def test_concurrent_requests_create_one_delivery_and_max_retries_are_bounded(self):
        self.configure(["webhook"])
        service = self.service(
            adapters={
                "webhook": lambda row: (_ for _ in ()).throw(DeliveryError("DOWN"))
            }
        )
        payload = {"eventId": self.event()["id"]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: service.send(self.admin, payload), range(8)))
        self.assertEqual(service.list_for(self.admin)["total"], 1)
        for _ in range(4):
            service.process_due()
            self.now += 60
        row = service.list_for(self.admin)["items"][0]
        self.assertEqual(row["attemptCount"], 3)
        self.assertEqual(row["status"], "failed")


class Week4RegistryTest(unittest.TestCase):
    def setUp(self):
        data.reset_runtime_state()
        self.system, self.admin, self.operator = (
            user("system"),
            user("admin"),
            user("operator"),
        )
        self.dataset = data.create_dataset_version(self.admin, dataset_payload())

    def baseline_payload(self):
        return {
            "datasetId": self.dataset["id"],
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "features": {"rms": {"mean": 0.08, "std": 0.01}, "bandEnergy": [1, 2, 3]},
            "status": "draft",
        }

    def model_payload(self):
        baseline = create_baseline_version(self.system, self.baseline_payload())
        return {
            "version": "freq-baseline-v1",
            "artifactUri": "s3://models/baseline-v1.pkl",
            "datasetId": self.dataset["id"],
            "baselineVersion": baseline["version"],
            "metrics": {"f1": 0.8, "falsePositiveRate": 0.1},
            "domainGap": "Different sensor and motor",
            "fieldCalibrationPlan": "Collect target-motor normal runs before calibration",
            "errorCases": ["High-load normal run classified as anomaly"],
        }

    def test_immutable_snapshots_connect_dataset_baseline_model_and_disclaimers(self):
        payload = self.model_payload()
        model = create_model_version(self.system, payload)
        self.assertEqual(model["datasetSnapshot"]["source"], self.dataset["source"])
        self.assertEqual(model["approvalStatus"], "pending")
        self.assertFalse(model["artifactVerified"])
        self.assertEqual(model["deploymentStatus"], "not_deployed")
        self.assertIn("RUL", model["limitations"])
        payload["metrics"]["f1"] = 0.99
        model["metrics"]["f1"] = 0.99
        self.assertEqual(
            versions_for(self.operator, "model", version=payload["version"])["metrics"][
                "f1"
            ],
            0.8,
        )
        with self.assertRaises(data.ApiError) as error:
            create_model_version(
                self.system, {**payload, "version": payload["version"].upper()}
            )
        self.assertEqual(error.exception.code, "MODEL_VERSION_EXISTS")

    def test_dataset_baseline_mismatch_and_site_rights(self):
        payload = self.model_payload()
        other = data.create_dataset_version(
            self.admin, dataset_payload("SITE-05", "other")
        )
        with self.assertRaises(data.ApiError) as error:
            create_model_version(self.system, {**payload, "datasetId": other["id"]})
        self.assertEqual(error.exception.code, "MODEL_DATASET_MISMATCH")
        baseline = create_baseline_version(
            self.system,
            {
                **self.baseline_payload(),
                "datasetId": other["id"],
                "siteId": "SITE-05",
                "assetId": "SITE-05-MOT-02",
            },
        )
        model = create_model_version(
            self.system,
            {
                **payload,
                "datasetId": other["id"],
                "baselineVersion": baseline["version"],
            },
        )
        self.assertEqual(versions_for(self.operator, "model")["total"], 0)
        with self.assertRaises(data.ApiError):
            versions_for(self.operator, "model", version=model["version"])
        logs = data.audit_logs_for(
            self.operator, action="model.create", page=1, size=50
        )
        self.assertEqual(logs["total"], 0)

    def test_invalid_numbers_strings_roles_and_no_partial_creation(self):
        model_payload = self.model_payload()
        for value in (None, True, "0.8", float("nan"), float("inf"), 10**400):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(data.ApiError):
                    create_model_version(
                        self.system, {**model_payload, "metrics": {"f1": value}}
                    )
                with self.assertRaises(data.ApiError):
                    create_baseline_version(
                        self.system,
                        {**self.baseline_payload(), "features": {"rms": value}},
                    )
        for field in ("version", "artifactUri", "datasetId", "baselineVersion"):
            for value in (None, True, 123, " "):
                with self.assertRaises(data.ApiError):
                    create_model_version(self.system, {**model_payload, field: value})
        with self.assertRaises(data.ApiError):
            create_model_version(self.admin, model_payload)
        with self.assertRaises(data.ApiError):
            create_baseline_version(self.admin, self.baseline_payload())
        self.assertEqual(data.MODEL_VERSIONS, [])

    def test_baseline_asset_reference_survives_delete_attempt(self):
        asset = data.create_asset(
            self.admin,
            "SITE-01",
            {
                "assetCode": "W4-MOTOR",
                "name": "Week4 motor",
                "type": "motor",
                "ratedRpm": 1800,
            },
        )
        create_baseline_version(
            self.system, {**self.baseline_payload(), "assetId": asset["id"]}
        )
        with self.assertRaises(data.ApiError) as error:
            data.delete_asset(self.admin, "SITE-01", asset["id"])
        self.assertEqual(error.exception.code, "ASSET_HAS_IMMUTABLE_REFERENCES")


class Week4HttpTest(unittest.TestCase):
    def setUp(self):
        data.reset_runtime_state()
        self.server = create_server(
            "127.0.0.1", 0, demo_enabled=False, auto_alerts=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tokens = {
            name: self.request(
                "/api/auth/login", {"username": name, "password": name + "123"}
            )[1]["session"]["token"]
            for name in ("operator", "admin", "system")
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, payload=None, token=""):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
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

    def test_demo_default_off_site_auth_and_automatic_notification_flow(self):
        body = {"siteId": "SITE-01", "assetId": "SITE-01-MOT-02"}
        self.assertEqual(
            self.request("/api/demo/inject-anomaly", body, self.tokens["admin"])[0], 403
        )
        self.server.demo_enabled = True
        self.server.auto_alerts = True
        status, event = self.request(
            "/api/demo/inject-anomaly", body, self.tokens["admin"]
        )
        self.assertEqual(status, 201)
        self.assertTrue(event["isSynthetic"])
        deadline = time.monotonic() + 3
        while True:
            status, alerts = self.request(
                "/api/alerts?channel=web&status=sent", token=self.tokens["operator"]
            )
            if alerts.get("items") or time.monotonic() >= deadline:
                break
            threading.Event().wait(0.02)
        self.assertEqual(status, 200)
        self.assertEqual(alerts["items"][0]["eventId"], event["id"])
        self.assertEqual(alerts["items"][0]["event"]["occurredAt"], event["occurredAt"])
        status, detail = self.request(
            f"/api/anomaly/events/{event['id']}", token=self.tokens["operator"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["event"]["id"], event["id"])
        self.assertEqual(
            self.request(
                "/api/alerts/send", {"eventId": event["id"]}, self.tokens["admin"]
            )[0],
            202,
        )
        self.assertEqual(
            self.request(
                "/api/alerts/send", {"eventId": event["id"]}, self.tokens["operator"]
            )[0],
            403,
        )

    def test_bulk_replay_24_hour_gap_order_duplicates_partial_failure_and_auth(self):
        timestamp = data.format_rfc3339(
            datetime.now(timezone.utc) - timedelta(hours=24)
        )
        items = [telemetry(index, timestamp=timestamp) for index in (3, 1, 2)]
        bad = {**items[0], "sequence": 4, "rpm": True}
        path = "/api/telemetry/bulk"
        token = "demo-telemetry-ingest-token"
        self.assertEqual(
            self.request(path, {"items": items}, self.tokens["admin"])[0], 403
        )
        status, result = self.request(path, {"items": items + [bad]}, token)
        self.assertEqual(status, 207)
        self.assertEqual((result["accepted"], result["rejected"]), (3, 1))
        self.assertEqual([row["sequence"] for row in data.TELEMETRY_RECORDS], [1, 2, 3])
        status, replay = self.request(path, {"items": items}, token)
        self.assertEqual(
            (status, replay["duplicates"], replay["accepted"]), (200, 3, 0)
        )
        status, conflict = self.request(
            path, {"items": [{**items[0], "rpm": 123}]}, token
        )
        self.assertEqual(conflict["items"][0]["error"]["code"], "SEQUENCE_CONFLICT")
        for items_value in ([], [None], [items[0]] * 101):
            self.assertEqual(self.request(path, {"items": items_value}, token)[0], 400)

    def test_bulk_device_scope_preflight_prevents_partial_writes(self):
        principal = {
            "permissions": ["telemetry:ingest"],
            "allowedDeviceIds": ["DEV-01-MOT-02"],
        }
        with self.assertRaises(data.ApiError):
            ingest_telemetry_bulk(
                principal,
                {
                    "items": [
                        telemetry(1),
                        {**telemetry(2), "deviceId": "DEV-05-MOT-02"},
                    ]
                },
            )
        self.assertEqual(data.TELEMETRY_RECORDS, [])

    def test_bulk_rejects_invalid_nested_values_without_stopping_other_items(self):
        payload = {"items": [{**telemetry(1), "scenarioLabel": {}}, telemetry(2)]}
        status, result = self.request(
            "/api/telemetry/bulk", payload, "demo-telemetry-ingest-token"
        )
        self.assertEqual(status, 207)
        self.assertEqual((result["accepted"], result["rejected"]), (1, 1))
        self.assertEqual(
            result["items"][0]["error"]["code"], "INVALID_TELEMETRY_PAYLOAD"
        )

    def test_production_demo_guard_and_restricted_admin_site_authorization(self):
        with patch.dict(
            "os.environ", {"APP_ENV": "production", "DEMO_ENABLED": "true"}
        ):
            server = create_server("127.0.0.1", 0, demo_enabled=True)
            try:
                self.assertFalse(server.demo_enabled)
            finally:
                server.server_close()
        self.server.demo_enabled = True
        restricted = user("admin")
        restricted["allowedSiteIds"] = ["SITE-01"]
        before = len(data.EVENTS)
        with patch(
            "motor_diagnosis.server.current_user_for_token", return_value=restricted
        ):
            status, body = self.request(
                "/api/demo/inject-anomaly", {"siteId": "SITE-05"}, self.tokens["admin"]
            )
        self.assertEqual((status, body["error"]["code"]), (403, "SITE_FORBIDDEN"))
        self.assertEqual(len(data.EVENTS), before)

    def test_model_registration_http_contract_and_authorization(self):
        status, dataset = self.request(
            "/api/datasets", dataset_payload(), self.tokens["admin"]
        )
        self.assertEqual(status, 201)
        baseline_body = {
            "datasetId": dataset["id"],
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "features": {"rms": 0.08},
        }
        self.assertEqual(
            self.request("/api/baseline-versions", baseline_body, self.tokens["admin"])[
                0
            ],
            403,
        )
        status, baseline = self.request(
            "/api/baseline-versions", baseline_body, self.tokens["system"]
        )
        self.assertEqual(status, 201)
        model_body = {
            "version": "w4-v1",
            "datasetId": dataset["id"],
            "baselineVersion": baseline["version"],
            "artifactUri": "s3://models/w4",
            "metrics": {"f1": 0.8},
        }
        self.assertEqual(
            self.request("/api/model-versions", model_body, self.tokens["system"])[0],
            201,
        )
        for path in ("/api/model-versions", "/api/baseline-versions?siteId=SITE-01"):
            status, page = self.request(path, token=self.tokens["operator"])
            self.assertEqual((status, page["total"]), (200, 1))
        self.assertEqual(
            self.request("/api/model-versions/w4-v1", token=self.tokens["operator"])[0],
            200,
        )
        self.assertEqual(
            self.request("/api/model-versions/w4-v1/deploy", {}, self.tokens["system"])[
                0
            ],
            404,
        )


if __name__ == "__main__":
    unittest.main()
