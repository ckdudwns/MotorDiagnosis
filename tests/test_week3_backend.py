from __future__ import annotations

import csv
import json
import threading
import unittest
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from motor_diagnosis.data import (
    ApiError,
    EVENTS,
    TELEMETRY_RECORDS,
    authenticate,
    create_dataset_version,
    create_environment_inspection,
    current_user_for_token,
    dataset_export_for,
    event_detail_for,
    format_rfc3339,
    parse_rfc3339,
    reset_runtime_state,
    update_anomaly_rule,
)
from motor_diagnosis.server import create_server


class Week3DataBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()
        self.admin = self.user("admin", "admin123")
        self.operator = self.user("operator", "operator123")

    def user(self, username: str, password: str) -> dict:
        login = authenticate({"username": username, "password": password})
        return current_user_for_token(login["session"]["token"])

    def telemetry_record(self, timestamp: str, sequence: int) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "sequence": sequence,
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "deviceId": "DEV-01-MOT-02",
            "rpm": 1780.0,
            "vibrationRmsRaw": 0.08,
            "vibrationRmsMmS": None,
            "acousticRmsRaw": 0.007,
            "acousticDb": None,
        }

    def dataset_payload(
        self,
        *,
        checksum: str,
        source_filters: dict[str, object],
        label_mapping: dict[str, str],
    ) -> dict[str, object]:
        return {
            "name": "internal-week3-training-export",
            "source": {
                "type": "internal",
                "uri": "api://telemetry",
                "license": "project-internal",
                "checksum": checksum,
            },
            "compatibility": {
                "signalType": ["vibration", "acoustic"],
                "samplingRateHz": 12000,
                "units": {"vibration": "g", "acoustic": "raw-rms"},
                "operatingConditions": {
                    "rpmRange": [1700, 1800],
                    "load": "mixed",
                },
            },
            "sourceFilters": source_filters,
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": label_mapping,
            "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
            "reason": "Week 3 dataset boundary regression test",
        }

    def test_event_context_marks_raw_missing_when_stored_data_is_outside_window(
        self,
    ) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(minutes=6)), 1)
        )

        detail = event_detail_for(self.operator, event["id"])

        self.assertEqual(detail["context"]["points"], [])
        self.assertEqual(detail["context"]["source"], "unavailable")
        self.assertTrue(detail["context"]["rawDataMissing"])

    def test_event_detail_keeps_the_rule_version_used_when_event_was_created(
        self,
    ) -> None:
        updated = update_anomaly_rule(
            self.admin,
            "SITE-01-MOT-02",
            {
                "scoreThreshold": 82,
                "durationSec": 20,
                "hysteresis": 7,
                "reason": "Boundary regression test",
            },
        )

        detail = event_detail_for(self.operator, "EV-241")

        self.assertEqual(updated["version"], "RULE-SITE-01-MOT-02-v2")
        self.assertEqual(detail["event"]["thresholdVersion"], "RULE-SITE-01-MOT-02-v1")
        self.assertEqual(detail["appliedRule"]["version"], "RULE-SITE-01-MOT-02-v1")
        self.assertEqual(detail["appliedRule"]["scoreThreshold"], 75.0)

    def test_event_feature_snapshot_is_closest_to_the_event_time(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.extend(
            [
                self.telemetry_record(
                    format_rfc3339(event_time - timedelta(seconds=60)), 1
                ),
                self.telemetry_record(
                    format_rfc3339(event_time + timedelta(seconds=5)), 2
                ),
                self.telemetry_record(
                    format_rfc3339(event_time + timedelta(seconds=240)), 3
                ),
            ]
        )

        detail = event_detail_for(self.operator, event["id"])

        self.assertEqual(detail["featureSnapshot"]["sequence"], 2)
        self.assertEqual(
            detail["featureSnapshot"]["timestamp"],
            format_rfc3339(event_time + timedelta(seconds=5)),
        )

    def test_all_not_checked_inspection_is_not_reported_as_ok(self) -> None:
        inspection = create_environment_inspection(
            self.admin,
            "DEV-01-GEN-01",
            {
                "inspectedAt": "2026-08-24T03:00:00Z",
                "dust": "not_checked",
                "waterIngress": "not_checked",
                "saltCorrosion": "not_checked",
                "glandStatus": "not_checked",
                "enclosureStatus": "not_checked",
            },
        )

        self.assertEqual(inspection["overallStatus"], "not_checked")

    def test_dataset_rows_only_link_events_covering_the_sample_timestamp(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.extend(
            [
                self.telemetry_record(
                    format_rfc3339(event_time - timedelta(seconds=60)), 1
                ),
                self.telemetry_record(
                    format_rfc3339(event_time + timedelta(seconds=10)), 2
                ),
            ]
        )

        exported = dataset_export_for(self.admin, "SITE-01", "SITE-01-MOT-02")
        rows = {row["sequence"]: row for row in exported["rows"]}

        self.assertIsNone(rows[1]["event_id"])
        self.assertIsNone(rows[1]["event_label"])
        self.assertEqual(rows[2]["event_id"], "EV-241")
        self.assertEqual(rows[2]["event_label"], "needs_review")
        self.assertIsNone(rows[2]["label_taxonomy_version"])
        self.assertIsNone(exported["manifest"]["labelTaxonomyVersion"])
        self.assertEqual(exported["manifest"]["exportType"], "internal_telemetry")
        self.assertEqual(exported["manifest"]["source"]["uri"], "api://telemetry")
        self.assertEqual(
            exported["manifest"]["source"]["checksum"],
            exported["manifest"]["checksum"],
        )

    def test_dataset_export_applies_registered_label_mapping(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(seconds=10)), 1)
        )
        dataset = create_dataset_version(
            self.admin,
            self.dataset_payload(
                checksum="sha256:internal-label-mapping",
                source_filters={
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-MOT-02",
                },
                label_mapping={"needs_review": "BEARING_SUSPECT"},
            ),
        )

        exported = dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )

        self.assertEqual(exported["rows"][0]["event_label"], "BEARING_SUSPECT")
        self.assertEqual(exported["rows"][0]["label_taxonomy_version"], "ACOUSTIC-V1")
        self.assertEqual(
            exported["manifest"]["labelMapping"]["needs_review"],
            "BEARING_SUSPECT",
        )
        self.assertEqual(
            exported["manifest"]["sourceFilters"],
            {"siteId": "SITE-01", "assetId": "SITE-01-MOT-02"},
        )

    def test_frozen_dataset_export_is_unchanged_after_new_telemetry(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(seconds=10)), 1)
        )
        dataset = create_dataset_version(
            self.admin,
            self.dataset_payload(
                checksum="sha256:frozen-dataset-regression",
                source_filters={
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-MOT-02",
                },
                label_mapping={"needs_review": "BEARING_SUSPECT"},
            ),
        )
        first_export = dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )

        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(seconds=20)), 2)
        )
        event["label"] = "sensor_issue"
        second_export = dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )

        self.assertEqual(first_export["rows"], second_export["rows"])
        self.assertEqual(first_export["manifest"], second_export["manifest"])
        self.assertEqual(first_export["manifest"]["recordCount"], 1)
        self.assertEqual(dataset["snapshotRecordCount"], 1)
        self.assertEqual(
            dataset["snapshotChecksum"], first_export["manifest"]["checksum"]
        )

    def test_dataset_export_rejects_site_outside_source_filters(self) -> None:
        dataset = create_dataset_version(
            self.admin,
            self.dataset_payload(
                checksum="sha256:site-02-only",
                source_filters={
                    "siteId": "SITE-02",
                    "assetId": "SITE-02-GEN-01",
                },
                label_mapping={"needs_review": "BEARING_SUSPECT"},
            ),
        )

        with self.assertRaises(ApiError) as mismatch:
            dataset_export_for(
                self.admin,
                "SITE-01",
                "SITE-01-MOT-02",
                dataset_id=dataset["id"],
            )

        self.assertEqual(mismatch.exception.status, 409)
        self.assertEqual(mismatch.exception.code, "DATASET_SOURCE_FILTER_MISMATCH")


class Week3HttpContractTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()
        self.server = create_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.operator_token = self.login("operator", "operator123")
        self.admin_token = self.login("admin", "admin123")

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
        status, body, _ = self.request_raw(
            path, method=method, payload=payload, token=token
        )
        return status, json.loads(body.decode("utf-8"))

    def request_raw(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        token: str = "",
    ) -> tuple[int, bytes, object]:
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
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def login(self, username: str, password: str) -> str:
        status, body = self.request(
            "/api/auth/login",
            method="POST",
            payload={"username": username, "password": password},
        )
        self.assertEqual(status, 200)
        return body["session"]["token"]

    def test_event_lookup_detail_review_and_history(self) -> None:
        status, page = self.request(
            "/api/events?siteId=SITE-01&page=1&size=1",
            token=self.operator_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["size"], 1)
        self.assertGreaterEqual(page["total"], 1)
        self.assertFalse(page["items"][0]["reviewed"])

        status, detail_before = self.request(
            "/api/anomaly/events/EV-241", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail_before["event"]["id"], "EV-241")
        self.assertEqual(
            detail_before["event"]["thresholdVersion"], "RULE-SITE-01-MOT-02-v1"
        )
        self.assertTrue(detail_before["context"]["rawDataMissing"])
        self.assertEqual(detail_before["context"]["source"], "demo")
        self.assertTrue(detail_before["context"]["points"])
        self.assertIsNotNone(detail_before["featureSnapshot"])
        self.assertEqual(detail_before["appliedRule"]["assetId"], "SITE-01-MOT-02")

        status, review_result = self.request(
            "/api/events/EV-241/review",
            method="POST",
            payload={
                "label": "normal_false_positive",
                "note": "Confirmed as a maintenance test.",
                "reason": "Week 3 operator verification",
            },
            token=self.operator_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(review_result["event"]["reviewed"])
        self.assertEqual(
            review_result["event"]["title"], detail_before["event"]["title"]
        )
        self.assertEqual(
            review_result["event"]["occurredAt"], detail_before["event"]["occurredAt"]
        )
        self.assertEqual(review_result["review"]["before"]["label"], "needs_review")
        self.assertEqual(
            review_result["review"]["after"]["label"], "normal_false_positive"
        )
        self.assertEqual(review_result["review"]["actor"]["id"], "user-operator")

        status, history = self.request(
            "/api/events/EV-241/reviews", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["reason"], "Week 3 operator verification")

        status, unreviewed = self.request(
            "/api/events?siteId=SITE-01&reviewed=false", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertNotIn("EV-241", {item["id"] for item in unreviewed["items"]})

    def test_rule_policy_parameter_and_audit_contract(self) -> None:
        status, rule = self.request(
            "/api/anomaly/rules/SITE-01-MOT-02",
            method="PUT",
            payload={
                "scoreThreshold": 82,
                "durationSec": 20,
                "hysteresis": 7,
                "mergeWindowSec": 45,
                "active": True,
                "reason": "Reduce transient alarms",
            },
            token=self.admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(rule["version"], "RULE-SITE-01-MOT-02-v2")
        self.assertEqual(rule["durationSec"], 20)
        self.assertEqual(len(rule["history"]), 1)

        status, event_detail = self.request(
            "/api/anomaly/events/EV-241", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            event_detail["appliedRule"]["version"], "RULE-SITE-01-MOT-02-v1"
        )

        status, readable_rule = self.request(
            "/api/anomaly/rules/SITE-01-MOT-02", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(readable_rule["version"], rule["version"])

        status, invalid_rule = self.request(
            "/api/anomaly/rules/SITE-01-MOT-02",
            method="PUT",
            payload={
                "scoreThreshold": 30,
                "hysteresis": 30,
                "reason": "Invalid boundary check",
            },
            token=self.admin_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_rule["error"]["code"], "INVALID_HYSTERESIS")

        status, policy = self.request(
            "/api/alerts/policies/ALERT-POLICY-DEFAULT",
            method="PUT",
            payload={
                "siteIds": ["SITE-01"],
                "assetIds": ["SITE-01-MOT-02"],
                "channels": ["web", "email"],
                "recipients": ["operations", "maintenance"],
                "cooldownSec": 600,
            },
            token=self.admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(policy["cooldownSec"], 600)
        self.assertEqual(policy["channels"], ["web", "email"])

        status, parameter = self.request(
            "/api/parameters/DEVICE_OFFLINE_SEC",
            method="PUT",
            payload={"value": 180, "reason": "Align with field heartbeat"},
            token=self.admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(parameter["value"], 180)
        self.assertIn("auditId", parameter)

        status, forbidden = self.request(
            "/api/parameters/DEVICE_OFFLINE_SEC",
            method="PUT",
            payload={"value": 240, "reason": "Operator must not change it"},
            token=self.operator_token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "FORBIDDEN")

        status, parameters = self.request(
            "/api/parameters?category=device", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            next(item for item in parameters if item["key"] == "DEVICE_OFFLINE_SEC")[
                "value"
            ],
            180,
        )
        status, audits = self.request(
            "/api/audit-logs?actorId=user-admin", token=self.operator_token
        )
        self.assertEqual(status, 200)
        actions = {item["action"] for item in audits["items"]}
        self.assertTrue(
            {
                "anomaly-rule.update",
                "alert-policy.update",
                "parameter.update",
            }.issubset(actions)
        )
        self.assertTrue(
            all(
                item["reason"] and "before" in item and "after" in item
                for item in audits["items"]
            )
        )

    def test_environment_inspection_and_sensor_fault_contract(self) -> None:
        status, inspection = self.request(
            "/api/devices/DEV-01-GEN-01/environment-inspections",
            method="POST",
            payload={
                "inspectedAt": "2026-08-24T03:00:00Z",
                "dust": "attention",
                "waterIngress": "ok",
                "saltCorrosion": "ok",
                "glandStatus": "ok",
                "enclosureStatus": "ok",
                "maintenanceAction": "Cleaned the enclosure filter.",
            },
            token=self.admin_token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(inspection["overallStatus"], "attention")
        self.assertEqual(inspection["inspector"]["id"], "user-admin")

        status, not_checked = self.request(
            "/api/devices/DEV-01-GEN-01/environment-inspections",
            method="POST",
            payload={
                "inspectedAt": "2026-08-24T03:30:00Z",
                "dust": "not_checked",
                "waterIngress": "not_checked",
                "saltCorrosion": "not_checked",
                "glandStatus": "not_checked",
                "enclosureStatus": "not_checked",
            },
            token=self.admin_token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(not_checked["overallStatus"], "not_checked")

        status, inspections = self.request(
            "/api/devices/DEV-01-GEN-01/environment-inspections",
            token=self.operator_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(inspections["total"], 2)
        self.assertEqual(inspections["latest"]["id"], not_checked["id"])

        status, _ = self.request(
            "/api/devices/DEV-01-GEN-01",
            method="PATCH",
            payload={"health": "offline", "lastSeenSecAgo": 600},
            token=self.admin_token,
        )
        self.assertEqual(status, 200)
        status, faults = self.request(
            "/api/devices/DEV-01-GEN-01/faults", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertTrue(faults["items"])
        self.assertTrue(
            all(
                item["classification"] == "sensor_fault" and item["assetEventExcluded"]
                for item in faults["items"]
            )
        )

        status, forbidden = self.request(
            "/api/devices/DEV-01-GEN-01/environment-inspections",
            method="POST",
            payload={
                "inspectedAt": "2026-08-24T04:00:00Z",
                "dust": "ok",
                "waterIngress": "ok",
                "saltCorrosion": "ok",
                "glandStatus": "ok",
                "enclosureStatus": "ok",
            },
            token=self.operator_token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "FORBIDDEN")

    def test_dataset_registry_taxonomy_and_csv_export_contract(self) -> None:
        status, taxonomies = self.request(
            "/api/label-taxonomies/acoustic", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(taxonomies[0]["version"], "ACOUSTIC-V1")

        dataset_payload = {
            "name": "existing-motor-vibration-v1",
            "source": {
                "type": "external",
                "uri": "dataset://existing/motor-vibration-v1",
                "license": "verified-for-mvp",
                "checksum": "sha256:week3-existing-dataset-v1",
            },
            "compatibility": {
                "signalType": ["vibration", "acoustic"],
                "samplingRateHz": 12000,
                "units": {"vibration": "g", "acoustic": "raw-rms"},
                "operatingConditions": {"rpmRange": [1700, 1800], "load": "mixed"},
            },
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": {
                "Normal": "NORMAL",
                "Fault": "BEARING_SUSPECT",
            },
            "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
            "reason": "Register the frozen week 3 training source",
        }
        status, dataset = self.request(
            "/api/datasets",
            method="POST",
            payload=dataset_payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(dataset["status"], "frozen")
        self.assertEqual(dataset["splitPolicy"], "asset_or_operating_condition_grouped")

        status, registered = self.request(
            f"/api/datasets/{dataset['id']}", token=self.operator_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            registered["source"]["checksum"], dataset_payload["source"]["checksum"]
        )

        status, duplicate = self.request(
            "/api/datasets",
            method="POST",
            payload=dataset_payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(duplicate["error"]["code"], "DATASET_CHECKSUM_EXISTS")

        invalid_mapping_payload = json.loads(json.dumps(dataset_payload))
        invalid_mapping_payload["source"]["checksum"] = "sha256:invalid-label-map"
        invalid_mapping_payload["labelMapping"]["Fault"] = "UNKNOWN_LABEL"
        status, invalid_mapping = self.request(
            "/api/datasets",
            method="POST",
            payload=invalid_mapping_payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_mapping["error"]["code"], "INVALID_LABEL_MAPPING")

        status, source_mismatch = self.request(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01"
            f"&datasetId={dataset['id']}&format=csv",
            token=self.operator_token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(source_mismatch["error"]["code"], "DATASET_SOURCE_MISMATCH")

        status, csv_body, headers = self.request_raw(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01&format=csv",
            token=self.operator_token,
        )
        self.assertEqual(status, 200)
        csv_text = csv_body.decode("utf-8-sig")
        self.assertIn("label_taxonomy_version", csv_text.splitlines()[0])
        self.assertIn("dataset_split", csv_text.splitlines()[0])
        self.assertIn("operating_conditions", csv_text.splitlines()[0])
        self.assertIn("api://telemetry", csv_text)
        self.assertNotIn(dataset_payload["source"]["uri"], csv_text)
        self.assertTrue(headers["x-dataset-checksum"].startswith("sha256:"))
        self.assertGreater(int(headers["x-dataset-record-count"]), 0)

        internal_payload = json.loads(json.dumps(dataset_payload))
        internal_payload["name"] = "internal-filtered-telemetry-v1"
        internal_payload["source"] = {
            "type": "internal",
            "uri": "api://telemetry",
            "license": "project-internal",
            "checksum": "sha256:week3-internal-filtered-v1",
        }
        internal_payload["sourceFilters"] = {
            "siteId": "SITE-01",
            "assetId": "SITE-01-GEN-01",
        }
        internal_payload["labelMapping"] = {"needs_review": "BEARING_SUSPECT"}
        status, internal_dataset = self.request(
            "/api/datasets",
            method="POST",
            payload=internal_payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 201)

        status, filtered_csv_body, _ = self.request_raw(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01"
            f"&datasetId={internal_dataset['id']}&format=csv",
            token=self.operator_token,
        )
        self.assertEqual(status, 200)
        filtered_csv = filtered_csv_body.decode("utf-8-sig")
        reader = csv.DictReader(filtered_csv.splitlines())
        first_row = next(reader)
        self.assertIn("dataset_id", reader.fieldnames or [])
        self.assertIn("source_filters", reader.fieldnames or [])
        self.assertEqual(first_row["dataset_id"], internal_dataset["id"])
        self.assertEqual(
            json.loads(first_row["source_filters"]),
            internal_payload["sourceFilters"],
        )

        status, not_implemented = self.request(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01&format=xlsx",
            token=self.operator_token,
        )
        self.assertEqual(status, 501)
        self.assertEqual(
            not_implemented["error"]["code"], "EXPORT_FORMAT_NOT_IMPLEMENTED"
        )


if __name__ == "__main__":
    unittest.main()
