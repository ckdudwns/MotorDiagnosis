from __future__ import annotations

import csv
import hashlib
import json
import threading
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from motor_diagnosis.data import (
    ApiError,
    DATASET_SNAPSHOTS,
    DATASET_VERSIONS,
    DEVICES,
    EVENTS,
    EVENT_EVIDENCE_SNAPSHOTS,
    EVENT_REVIEW_HISTORY,
    TELEMETRY_RECORDS,
    alert_policies_for,
    audit_logs_for,
    authenticate,
    create_asset,
    create_dataset_version,
    create_environment_inspection,
    create_site,
    current_user_for_token,
    dataset_export_for,
    dataset_version_for,
    delete_asset,
    delete_device,
    delete_site,
    event_detail_for,
    event_reviews_for,
    environment_inspections_for,
    format_rfc3339,
    parse_rfc3339,
    reset_runtime_state,
    recover_device_from_telemetry,
    review_event,
    sensor_faults_for_device,
    update_acoustic_taxonomy,
    update_alert_policy,
    update_anomaly_rule,
)
from motor_diagnosis.server import create_server, paginated_events


def checksum_for(label: str) -> str:
    value = label.removeprefix("sha256:")
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return f"sha256:{value}"
    return f"sha256:{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


class Week3DataBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_state()
        self.admin = self.user("admin", "admin123")
        self.operator = self.user("operator", "operator123")

    def user(self, username: str, password: str) -> dict:
        login = authenticate({"username": username, "password": password})
        return current_user_for_token(login["session"]["token"])

    def telemetry_record(
        self,
        timestamp: str,
        sequence: int,
        *,
        site_id: str = "SITE-01",
        asset_id: str = "SITE-01-MOT-02",
        device_id: str = "DEV-01-MOT-02",
        rpm: float = 1780.0,
    ) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "sequence": sequence,
            "siteId": site_id,
            "assetId": asset_id,
            "deviceId": device_id,
            "rpm": rpm,
            "vibrationRmsRaw": 0.08,
            "vibrationRmsMmS": None,
            "acousticRmsRaw": 0.007,
            "acousticDb": None,
        }

    def add_split_ready_telemetry(
        self,
        timestamp,
        *,
        site_id: str = "SITE-01",
        asset_id: str = "SITE-01-MOT-02",
        device_id: str = "DEV-01-MOT-02",
        sequence_start: int = 1,
    ) -> None:
        for offset, rpm in enumerate((1500.0, 1800.0, 2100.0)):
            TELEMETRY_RECORDS.append(
                self.telemetry_record(
                    format_rfc3339(timestamp + timedelta(seconds=offset * 5)),
                    sequence_start + offset,
                    site_id=site_id,
                    asset_id=asset_id,
                    device_id=device_id,
                    rpm=rpm,
                )
            )

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
                "checksum": checksum_for(checksum),
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
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
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

    def test_base_event_feature_snapshot_is_frozen_before_the_first_detail_read(
        self,
    ) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        frozen_snapshot = json.loads(
            json.dumps(EVENT_EVIDENCE_SNAPSHOTS[event["id"]]["featureSnapshot"])
        )
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

        self.assertIsNotNone(frozen_snapshot)
        self.assertEqual(detail["featureSnapshot"], frozen_snapshot)
        self.assertNotEqual(detail["featureSnapshot"].get("sequence"), 2)

    def test_event_evidence_snapshots_do_not_change_after_late_updates(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        frozen_feature = json.loads(
            json.dumps(EVENT_EVIDENCE_SNAPSHOTS[event["id"]]["featureSnapshot"])
        )
        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(seconds=20)), 1)
        )
        before = event_detail_for(self.operator, event["id"])

        device = next(item for item in DEVICES if item["id"] == "DEV-01-MOT-02")
        device["firmware"] = "edge-9.9.9"
        device["firmwareVersion"] = "edge-9.9.9"
        TELEMETRY_RECORDS.append(
            self.telemetry_record(format_rfc3339(event_time + timedelta(seconds=1)), 2)
        )

        after = event_detail_for(self.operator, event["id"])

        self.assertEqual(after["deviceSnapshot"], before["deviceSnapshot"])
        self.assertEqual(after["featureSnapshot"], before["featureSnapshot"])
        self.assertNotEqual(after["deviceSnapshot"]["firmware"], device["firmware"])
        self.assertEqual(after["featureSnapshot"], frozen_feature)

    def test_recovery_event_freezes_the_recovered_device_state(self) -> None:
        device = next(item for item in DEVICES if item["id"] == "DEV-01-GEN-01")
        device["health"] = "offline"
        device["offlineSince"] = "2026-08-24T02:50:00Z"

        recover_device_from_telemetry(device, "2026-08-24T03:00:00Z")

        recovery_event = EVENTS[0]
        evidence = EVENT_EVIDENCE_SNAPSHOTS[recovery_event["id"]]
        self.assertEqual(recovery_event["eventType"], "device_recovered")
        self.assertEqual(evidence["deviceSnapshot"]["health"], "online")
        self.assertIsNone(evidence["deviceSnapshot"]["offlineSince"])
        self.assertEqual(
            evidence["deviceSnapshot"]["lastReceivedAt"], "2026-08-24T03:00:00Z"
        )

    def test_reviews_with_same_timestamp_return_newest_id_first(self) -> None:
        first = review_event(
            self.operator,
            "EV-241",
            {"label": "needs_review", "reason": "First same-time review"},
        )["review"]
        second = review_event(
            self.operator,
            "EV-241",
            {"label": "confirmed_anomaly", "reason": "Second same-time review"},
        )["review"]
        for review in EVENT_REVIEW_HISTORY:
            review["changedAt"] = "2026-08-24T03:00:00.000Z"

        history = event_reviews_for(self.operator, "EV-241", page=1, size=10)

        self.assertEqual(
            [item["id"] for item in history["items"]], [second["id"], first["id"]]
        )

    def test_event_pagination_sorts_rfc3339_values_by_instant(self) -> None:
        first = next(item for item in EVENTS if item["id"] == "EV-241")
        second = next(item for item in EVENTS if item["id"] == "EV-238")
        first["occurredAt"] = "2026-08-24T10:00:00+09:00"
        second["occurredAt"] = "2026-08-24T02:00:00.500Z"
        first["reviewed"] = False
        second["reviewed"] = False

        descending = paginated_events(
            self.admin, {"sort": ["occurredAt_desc"], "page": ["1"], "size": ["50"]}
        )
        unreviewed = paginated_events(
            self.admin, {"sort": ["unreviewed_desc"], "page": ["1"], "size": ["50"]}
        )

        self.assertLess(
            [item["id"] for item in descending["items"]].index(second["id"]),
            [item["id"] for item in descending["items"]].index(first["id"]),
        )
        self.assertLess(
            [item["id"] for item in unreviewed["items"]].index(second["id"]),
            [item["id"] for item in unreviewed["items"]].index(first["id"]),
        )

    def test_new_asset_has_an_updatable_default_anomaly_rule(self) -> None:
        asset = create_asset(
            self.admin,
            "SITE-01",
            {
                "assetCode": "NEW-99",
                "name": "New test motor",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )

        updated = update_anomaly_rule(
            self.admin,
            asset["id"],
            {"scoreThreshold": 80, "reason": "Initialize the new asset rule"},
        )

        self.assertEqual(updated["version"], f"RULE-{asset['id']}-v2")
        self.assertEqual(updated["scoreThreshold"], 80)

    def test_alert_policy_asset_scope_protects_policy_and_audit_details(self) -> None:
        updated = update_alert_policy(
            self.admin,
            "ALERT-POLICY-DEFAULT",
            {
                "siteIds": [],
                "assetIds": ["SITE-05-MOT-02"],
                "recipients": ["site-05-secret-recipient"],
                "reason": "Scope the policy to site 05",
            },
        )

        self.assertEqual(updated["assetIds"], ["SITE-05-MOT-02"])
        self.assertEqual(alert_policies_for(self.operator), [])
        operator_audits = audit_logs_for(self.operator, page=1, size=50)
        self.assertNotIn(
            "alert-policy.update",
            {item["action"] for item in operator_audits["items"]},
        )
        admin_audits = audit_logs_for(self.admin, page=1, size=50)
        policy_audit = next(
            item
            for item in admin_audits["items"]
            if item["action"] == "alert-policy.update"
        )
        self.assertEqual(policy_audit["siteIds"], ["SITE-05"])

    def test_deleted_policy_asset_scope_remains_readable_and_can_be_recovered(
        self,
    ) -> None:
        asset = create_asset(
            self.admin,
            "SITE-01",
            {
                "id": "LEGACY-ASSET",
                "assetCode": "LEGACY-01",
                "name": "Legacy policy target",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )
        scoped = update_alert_policy(
            self.admin,
            "ALERT-POLICY-DEFAULT",
            {
                "siteIds": [],
                "assetIds": [asset["id"]],
                "reason": "Store the resolved site scope",
            },
        )
        self.assertEqual(scoped["scopeSiteIds"], ["SITE-01"])

        delete_asset(self.admin, "SITE-01", asset["id"])

        visible = alert_policies_for(self.admin)
        self.assertEqual(visible[0]["scopeSiteIds"], ["SITE-01"])
        recovered = update_alert_policy(
            self.admin,
            "ALERT-POLICY-DEFAULT",
            {
                "siteIds": [],
                "assetIds": [],
                "reason": "Remove the deleted asset from policy scope",
            },
        )
        self.assertEqual(recovered["assetIds"], [])
        self.assertEqual(recovered["scopeSiteIds"], [])

    def test_deleted_policy_asset_uses_frozen_scope_before_id_prefix(self) -> None:
        asset = create_asset(
            self.admin,
            "SITE-01",
            {
                "id": "SITE-02-LEGACY-POLICY-ASSET",
                "assetCode": "PREFIX-COLLISION",
                "name": "Cross-prefix policy target",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )
        update_alert_policy(
            self.admin,
            "ALERT-POLICY-DEFAULT",
            {
                "siteIds": [],
                "assetIds": [asset["id"]],
                "reason": "Freeze the actual site 01 policy scope",
            },
        )
        delete_asset(self.admin, "SITE-01", asset["id"])
        site_01_operator = {**self.operator, "allowedSiteIds": ["SITE-01"]}
        site_02_operator = {**self.operator, "allowedSiteIds": ["SITE-02"]}

        self.assertEqual(len(alert_policies_for(site_01_operator)), 1)
        self.assertEqual(alert_policies_for(site_02_operator), [])

    def test_older_environment_inspection_does_not_replace_current_status(self) -> None:
        device = next(item for item in DEVICES if item["id"] == "DEV-01-GEN-01")
        common = {
            "waterIngress": "ok",
            "saltCorrosion": "ok",
            "glandStatus": "ok",
            "enclosureStatus": "ok",
        }
        create_environment_inspection(
            self.admin,
            device["id"],
            {"inspectedAt": "2026-08-20T00:00:00Z", "dust": "critical", **common},
        )
        create_environment_inspection(
            self.admin,
            device["id"],
            {"inspectedAt": "2026-08-01T00:00:00Z", "dust": "ok", **common},
        )

        self.assertEqual(device["environmentStatus"], "critical")
        self.assertEqual(device["lastEnvironmentInspectionAt"], "2026-08-20T00:00:00Z")

    def test_environment_inspections_use_id_as_same_timestamp_tiebreaker(self) -> None:
        common = {
            "inspectedAt": "2026-08-24T03:00:00Z",
            "waterIngress": "ok",
            "saltCorrosion": "ok",
            "glandStatus": "ok",
            "enclosureStatus": "ok",
        }
        first = create_environment_inspection(
            self.admin,
            "DEV-01-GEN-01",
            {"dust": "attention", **common},
        )
        second = create_environment_inspection(
            self.admin,
            "DEV-01-GEN-01",
            {"dust": "critical", **common},
        )

        page = environment_inspections_for(self.admin, "DEV-01-GEN-01", page=1, size=10)

        self.assertEqual(page["latest"]["id"], second["id"])
        self.assertEqual(
            [item["id"] for item in page["items"]], [second["id"], first["id"]]
        )

    def test_device_with_environment_inspections_cannot_be_deleted(self) -> None:
        create_environment_inspection(
            self.admin,
            "DEV-01-GEN-01",
            {
                "inspectedAt": "2026-08-24T03:00:00Z",
                "dust": "ok",
                "waterIngress": "ok",
                "saltCorrosion": "ok",
                "glandStatus": "ok",
                "enclosureStatus": "ok",
            },
        )

        with self.assertRaises(ApiError) as referenced:
            delete_device(self.admin, "DEV-01-GEN-01")

        self.assertEqual(
            referenced.exception.code, "DEVICE_HAS_ENVIRONMENT_INSPECTIONS"
        )
        self.assertTrue(any(item["id"] == "DEV-01-GEN-01" for item in DEVICES))

    def test_no_signal_fault_uses_last_received_at_without_health_api_call(
        self,
    ) -> None:
        device = next(item for item in DEVICES if item["id"] == "DEV-01-GEN-01")
        device["health"] = "online"
        device["lastSeenSecAgo"] = 0
        device["lastReceivedAt"] = format_rfc3339(
            datetime.now(timezone.utc) - timedelta(seconds=180)
        )

        faults = sensor_faults_for_device(self.operator, device["id"], page=1, size=50)

        self.assertIn("no_signal", {item["faultType"] for item in faults["items"]})

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
        self.add_split_ready_telemetry(event_time + timedelta(seconds=10))
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
        self.assertEqual(exported["manifest"]["sourceRecordCount"], 3)
        self.assertEqual(exported["manifest"]["normalizedRecordCount"], 3)
        self.assertEqual(sum(exported["manifest"]["splitCounts"].values()), 3)
        self.assertEqual(
            exported["manifest"]["splitCounts"],
            {"train": 3, "validation": 0, "test": 0},
        )
        self.assertEqual(exported["manifest"]["assignedSplit"], "train")
        self.assertEqual(exported["manifest"]["splitPolicy"], dataset["splitPolicy"])

    def test_label_mapping_rejects_casefold_key_collisions(self) -> None:
        payload = self.dataset_payload(
            checksum="sha256:casefold-label-collision",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={
                "needs_review": "NORMAL",
                "NEEDS_REVIEW": "BEARING_SUSPECT",
            },
        )

        with self.assertRaises(ApiError) as collision:
            create_dataset_version(self.admin, payload)

        self.assertEqual(collision.exception.status, 400)
        self.assertEqual(collision.exception.code, "INVALID_LABEL_MAPPING")

    def test_same_source_checksum_allows_a_new_normalized_version(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(event_time + timedelta(seconds=10))
        first_payload = self.dataset_payload(
            checksum="sha256:shared-raw-source",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "NORMAL"},
        )
        second_payload = json.loads(json.dumps(first_payload))
        second_payload["labelMapping"] = {"needs_review": "BEARING_SUSPECT"}

        first = create_dataset_version(self.admin, first_payload)
        second = create_dataset_version(self.admin, second_payload)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["source"]["checksum"], second["source"]["checksum"])
        self.assertNotEqual(first["versionFingerprint"], second["versionFingerprint"])
        with self.assertRaises(ApiError) as duplicate:
            create_dataset_version(self.admin, second_payload)
        self.assertEqual(duplicate.exception.code, "DATASET_VERSION_EXISTS")

    def test_empty_internal_snapshot_is_not_registered_as_frozen(self) -> None:
        payload = self.dataset_payload(
            checksum="sha256:empty-future-window",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
                "from": "2099-01-01T00:00:00Z",
                "to": "2099-01-01T01:00:00Z",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )

        with self.assertRaises(ApiError) as empty_snapshot:
            create_dataset_version(self.admin, payload)

        self.assertEqual(empty_snapshot.exception.status, 400)
        self.assertEqual(empty_snapshot.exception.code, "EMPTY_DATASET_SNAPSHOT")

    def test_internal_snapshot_does_not_freeze_demo_telemetry(self) -> None:
        payload = self.dataset_payload(
            checksum="sha256:no-demo-fallback",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )

        with self.assertRaises(ApiError) as empty_snapshot:
            create_dataset_version(self.admin, payload)

        self.assertEqual(TELEMETRY_RECORDS, [])
        self.assertEqual(empty_snapshot.exception.status, 400)
        self.assertEqual(empty_snapshot.exception.code, "EMPTY_DATASET_SNAPSHOT")

    def test_internal_source_contract_rejects_invalid_type_uri_and_checksum(
        self,
    ) -> None:
        payload = self.dataset_payload(
            checksum="sha256:placeholder",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        invalid_sources = [
            {**payload["source"], "type": "archive"},
            {**payload["source"], "uri": "s3://not-internal-telemetry"},
            {**payload["source"], "checksum": "not-a-sha256"},
            {**payload["source"], "checksum": "sha256:x"},
        ]

        for invalid_source in invalid_sources:
            invalid_payload = json.loads(json.dumps(payload))
            invalid_payload["source"] = invalid_source
            with self.subTest(source=invalid_source):
                with self.assertRaises(ApiError) as invalid:
                    create_dataset_version(self.admin, invalid_payload)
                self.assertEqual(invalid.exception.status, 400)
                self.assertEqual(invalid.exception.code, "INVALID_DATASET_SOURCE")

        self.assertEqual(DATASET_VERSIONS, [])

    def test_dataset_source_rejects_null_contract_fields(self) -> None:
        payload = self.dataset_payload(
            checksum="source-null",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/source-null.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("source-null"),
        }

        for field in ("type", "uri", "license", "checksum"):
            invalid_payload = json.loads(json.dumps(payload))
            invalid_payload["source"][field] = None
            with self.subTest(field=field):
                with self.assertRaises(ApiError) as invalid:
                    create_dataset_version(self.admin, invalid_payload)
                self.assertEqual(invalid.exception.code, "INVALID_DATASET_SOURCE")

    def test_dataset_units_reject_non_string_values(self) -> None:
        payload = self.dataset_payload(
            checksum="units-null",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/units-null.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("units-null"),
        }
        payload["compatibility"]["units"]["acoustic"] = None

        with self.assertRaises(ApiError) as invalid:
            create_dataset_version(self.admin, payload)

        self.assertEqual(invalid.exception.code, "INVALID_DATASET_UNITS")

    def test_dataset_units_reject_normalized_key_collisions(self) -> None:
        payload = self.dataset_payload(
            checksum="unit-key-collision",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/unit-key-collision.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("unit-key-collision"),
        }
        payload["compatibility"]["units"] = {
            "vibration": "g",
            " VIBRATION ": "mm/s",
        }

        with self.assertRaises(ApiError) as invalid:
            create_dataset_version(self.admin, payload)

        self.assertEqual(invalid.exception.code, "INVALID_DATASET_UNITS")

    def test_operating_conditions_are_validated_and_normalized(self) -> None:
        payload = self.dataset_payload(
            checksum="normalized-operating-conditions",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/normalized-operating-conditions.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("normalized-operating-conditions"),
        }
        payload["compatibility"]["operatingConditions"] = {
            " rpmRange ": [1700.0, 1800.0],
            " load ": " mixed ",
        }

        dataset = create_dataset_version(self.admin, payload)

        self.assertEqual(
            dataset["compatibility"]["operatingConditions"],
            {"rpmRange": [1700, 1800], "load": "mixed"},
        )

        for invalid_conditions in (
            {"rpmRange": [1800, 1700]},
            {"rpmRange": [1700, float("inf")]},
            {"load": None},
            {"load": "mixed", " LOAD ": "full"},
        ):
            invalid_payload = json.loads(json.dumps(payload))
            invalid_payload["source"]["checksum"] = checksum_for(
                repr(invalid_conditions)
            )
            invalid_payload["compatibility"]["operatingConditions"] = invalid_conditions
            with self.subTest(conditions=invalid_conditions):
                with self.assertRaises(ApiError) as invalid:
                    create_dataset_version(self.admin, invalid_payload)
                self.assertEqual(invalid.exception.code, "INVALID_OPERATING_CONDITIONS")

    def test_operating_conditions_preserve_large_integers_and_limit_depth(
        self,
    ) -> None:
        large_integer = 2**80 + 123
        payload = self.dataset_payload(
            checksum="large-operating-integer",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/large-operating-integer.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("large-operating-integer"),
        }
        payload["compatibility"]["operatingConditions"] = {"cycleCount": large_integer}

        dataset = create_dataset_version(self.admin, payload)

        self.assertEqual(
            dataset["compatibility"]["operatingConditions"]["cycleCount"],
            large_integer,
        )

        nested: object = "leaf"
        for index in range(12):
            nested = {f"level{index}": nested}
        deep_payload = json.loads(json.dumps(payload))
        deep_payload["source"]["checksum"] = checksum_for("deep-operating-input")
        deep_payload["compatibility"]["operatingConditions"] = {"nested": nested}

        with self.assertRaises(ApiError) as invalid:
            create_dataset_version(self.admin, deep_payload)

        self.assertEqual(invalid.exception.code, "INVALID_OPERATING_CONDITIONS")

    def test_compatibility_key_casing_has_one_version_fingerprint(self) -> None:
        payload = self.dataset_payload(
            checksum="compatibility-key-casing",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/compatibility-key-casing.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("compatibility-key-casing"),
        }
        first = create_dataset_version(self.admin, payload)
        duplicate = json.loads(json.dumps(payload))
        duplicate["compatibility"]["signalType"] = ["VIBRATION", "ACOUSTIC"]
        duplicate["compatibility"]["units"] = {
            "VIBRATION": "g",
            "ACOUSTIC": "raw-rms",
        }
        duplicate["compatibility"]["operatingConditions"] = {
            "RPMRANGE": [1700, 1800],
            "LOAD": "mixed",
        }

        with self.assertRaises(ApiError) as exists:
            create_dataset_version(self.admin, duplicate)

        self.assertTrue(first["versionFingerprint"].startswith("sha256:"))
        self.assertEqual(exists.exception.code, "DATASET_VERSION_EXISTS")

    def test_external_dataset_validates_the_assets_actual_site(self) -> None:
        misleading_asset = create_asset(
            self.admin,
            "SITE-02",
            {
                "id": "SITE-01-MISLEADING-ASSET",
                "assetCode": "MISLEADING-01",
                "name": "Actual site 02 asset",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )
        payload = self.dataset_payload(
            checksum="external-actual-asset-site",
            source_filters={
                "siteId": "SITE-01",
                "assetId": misleading_asset["id"],
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/external-actual-asset-site.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("external-actual-asset-site"),
        }

        with self.assertRaises(ApiError) as mismatch:
            create_dataset_version(self.admin, payload)

        self.assertEqual(mismatch.exception.code, "INVALID_SOURCE_FILTERS")

    def test_site_only_frozen_dataset_prevents_site_deletion(self) -> None:
        site = create_site(
            self.admin,
            {
                "id": "SITE-DATASET-LIFE",
                "code": "DATASET-LIFE",
                "name": "Dataset lifecycle site",
                "networkType": "D",
            },
        )
        payload = self.dataset_payload(
            checksum="site-only-frozen-dataset",
            source_filters={"siteId": site["id"]},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/site-only-frozen-dataset.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("site-only-frozen-dataset"),
        }
        dataset = create_dataset_version(self.admin, payload)

        with self.assertRaises(ApiError) as referenced:
            delete_site(self.admin, site["id"])

        self.assertEqual(referenced.exception.code, "SITE_HAS_IMMUTABLE_REFERENCES")
        self.assertEqual(
            dataset_version_for(self.admin, dataset["id"])["id"], dataset["id"]
        )

    def test_negative_zero_split_cannot_create_a_duplicate_version(self) -> None:
        payload = self.dataset_payload(
            checksum="negative-zero-split",
            source_filters={"siteId": "SITE-01"},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/negative-zero.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("negative-zero-split"),
        }
        create_dataset_version(self.admin, payload)
        duplicate = json.loads(json.dumps(payload))
        duplicate["split"]["validation"] = -0.0

        with self.assertRaises(ApiError) as exists:
            create_dataset_version(self.admin, duplicate)

        self.assertEqual(exists.exception.code, "DATASET_VERSION_EXISTS")

    def test_equivalent_source_filter_forms_share_one_version_fingerprint(self) -> None:
        payload = self.dataset_payload(
            checksum="equivalent-source-filters",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/equivalent-filters.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("equivalent-source-filters"),
        }
        create_dataset_version(self.admin, payload)
        duplicate = json.loads(json.dumps(payload))
        duplicate["sourceFilters"] = {
            "siteIds": ["SITE-01"],
            "assetIds": ["SITE-01-MOT-02"],
        }

        with self.assertRaises(ApiError) as exists:
            create_dataset_version(self.admin, duplicate)

        self.assertEqual(exists.exception.code, "DATASET_VERSION_EXISTS")

    def test_asset_referenced_by_frozen_dataset_cannot_be_deleted(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(event_time)
        payload = self.dataset_payload(
            checksum="frozen-delete-guard",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        create_dataset_version(self.admin, payload)
        device = next(item for item in DEVICES if item["id"] == "DEV-01-MOT-02")
        device["mappingStatus"] = "inactive"

        with self.assertRaises(ApiError) as referenced:
            delete_asset(self.admin, "SITE-01", "SITE-01-MOT-02")

        self.assertEqual(referenced.exception.code, "ASSET_HAS_IMMUTABLE_REFERENCES")

    def test_external_dataset_reference_prevents_asset_deletion(self) -> None:
        asset = create_asset(
            self.admin,
            "SITE-01",
            {
                "assetCode": "EXT-REF-01",
                "name": "External dataset reference",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )
        payload = self.dataset_payload(
            checksum="external-delete-guard",
            source_filters={"siteId": "SITE-01", "assetId": asset["id"]},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/external-delete-guard.csv",
            "license": "verified-for-mvp",
            "checksum": checksum_for("external-delete-guard"),
        }
        create_dataset_version(self.admin, payload)

        with self.assertRaises(ApiError) as referenced:
            delete_asset(self.admin, "SITE-01", asset["id"])

        self.assertEqual(referenced.exception.code, "ASSET_HAS_IMMUTABLE_REFERENCES")

    def test_event_reference_prevents_asset_deletion(self) -> None:
        asset = create_asset(
            self.admin,
            "SITE-01",
            {
                "assetCode": "EVENT-REF-01",
                "name": "Event reference",
                "assetType": "motor",
                "ratedRpm": 1800,
            },
        )
        EVENTS.append(
            {
                "id": "EV-DELETE-GUARD",
                "siteId": "SITE-01",
                "assetId": asset["id"],
                "occurredAt": "2026-08-24T03:00:00Z",
            }
        )

        with self.assertRaises(ApiError) as referenced:
            delete_asset(self.admin, "SITE-01", asset["id"])

        self.assertEqual(referenced.exception.code, "ASSET_HAS_IMMUTABLE_REFERENCES")

    def test_internal_version_fingerprint_tracks_the_canonical_snapshot(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(event_time)
        payload = self.dataset_payload(
            checksum="sha256:reported-source-a",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={
                "needs_review": "BEARING_SUSPECT",
                "confirmed_anomaly": "NORMAL",
            },
        )

        first = create_dataset_version(self.admin, payload)
        self.assertNotEqual(first["source"]["checksum"], payload["source"]["checksum"])
        self.assertEqual(len(first["source"]["checksum"]), 71)
        self.assertTrue(
            all(
                character in "0123456789abcdef"
                for character in first["source"]["checksum"][7:]
            )
        )
        changed_reported_checksum = json.loads(json.dumps(payload))
        changed_reported_checksum["source"]["checksum"] = checksum_for(
            "reported-source-b"
        )
        with self.assertRaises(ApiError) as same_snapshot:
            create_dataset_version(self.admin, changed_reported_checksum)
        self.assertEqual(same_snapshot.exception.code, "DATASET_VERSION_EXISTS")

        TELEMETRY_RECORDS.append(
            self.telemetry_record(
                format_rfc3339(event_time + timedelta(seconds=15)),
                4,
                rpm=2300.0,
            )
        )
        after_telemetry = create_dataset_version(self.admin, payload)
        self.assertNotEqual(
            first["snapshotChecksum"], after_telemetry["snapshotChecksum"]
        )
        self.assertNotEqual(
            first["versionFingerprint"], after_telemetry["versionFingerprint"]
        )

        event["label"] = "confirmed_anomaly"
        after_review = create_dataset_version(self.admin, payload)
        self.assertNotEqual(
            after_telemetry["snapshotChecksum"], after_review["snapshotChecksum"]
        )
        self.assertNotEqual(
            after_telemetry["versionFingerprint"], after_review["versionFingerprint"]
        )

    def test_dataset_export_preserves_ground_truth_and_provenance(self) -> None:
        record = self.telemetry_record("2026-08-24T03:00:00Z", 1)
        record.update(
            {
                "vibrationPeakHz": 1037.11,
                "acousticPeakHz": 216.4,
                "scenarioLabel": "normal",
                "knownVibrationLabel": "NORMAL",
                "knownAcousticLabel": "=1+1",
                "source": "@CWRU_only_synthetic",
                "isSynthetic": True,
                "vibrationUnitNote": "+raw accelerometer output; not mm/s",
                "acousticUnitNote": "raw waveform RMS; not dB SPL",
            }
        )
        TELEMETRY_RECORDS.append(record)
        dataset = create_dataset_version(
            self.admin,
            self.dataset_payload(
                checksum="sha256:provenance-placeholder",
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
        row = exported["rows"][0]

        self.assertIsNone(row["event_label"])
        self.assertEqual(row["known_vibration_label"], "NORMAL")
        self.assertEqual(row["scenario_label"], "normal")
        self.assertEqual(row["ground_truth_label"], "NORMAL")
        self.assertEqual(row["ground_truth_source"], "known_vibration_label")
        self.assertEqual(row["target_label"], "NORMAL")
        self.assertEqual(row["target_label_taxonomy_version"], "ACOUSTIC-V1")
        self.assertEqual(row["known_acoustic_label"], "'=1+1")
        self.assertEqual(row["telemetry_source"], "'@CWRU_only_synthetic")
        self.assertTrue(row["is_synthetic"])
        self.assertEqual(row["vibration_peak_hz"], 1037.11)
        self.assertEqual(row["acoustic_peak_hz"], 216.4)
        self.assertEqual(
            row["vibration_unit_note"], "'+raw accelerometer output; not mm/s"
        )
        canonical_checksum = hashlib.sha256(
            json.dumps(exported["rows"], sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        self.assertEqual(
            exported["manifest"]["checksum"], f"sha256:{canonical_checksum}"
        )
        self.assertEqual(
            exported["manifest"]["labelPriority"],
            [
                "event_review",
                "known_vibration_label",
                "known_acoustic_label",
                "scenario_label",
                "event_candidate",
            ],
        )

    def test_dataset_split_keeps_assets_intact_and_uses_largest_remainder(
        self,
    ) -> None:
        asset_ids = [f"SITE-{index:02d}-GEN-01" for index in range(1, 11)]
        for site_index, asset_id in enumerate(asset_ids, start=1):
            for sequence, rpm in ((1, 1500.0), (2, 2100.0)):
                TELEMETRY_RECORDS.append(
                    self.telemetry_record(
                        "2026-08-24T03:00:00Z",
                        sequence,
                        site_id=f"SITE-{site_index:02d}",
                        asset_id=asset_id,
                        device_id=f"DEV-{site_index:02d}-GEN-01",
                        rpm=rpm,
                    )
                )
        payload = self.dataset_payload(
            checksum="sha256:ten-asset-split",
            source_filters={"assetIds": asset_ids},
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["split"] = {"train": 0.7, "validation": 0.2, "test": 0.1}

        dataset = create_dataset_version(self.admin, payload)
        rows = [
            row
            for scope_rows in DATASET_SNAPSHOTS[dataset["id"]].values()
            for row in scope_rows
        ]
        asset_splits = {
            asset_id: {
                row["dataset_split"] for row in rows if row["asset_id"] == asset_id
            }
            for asset_id in asset_ids
        }

        self.assertTrue(all(len(splits) == 1 for splits in asset_splits.values()))
        self.assertEqual(
            Counter(next(iter(splits)) for splits in asset_splits.values()),
            Counter({"train": 7, "validation": 2, "test": 1}),
        )
        self.assertEqual(dataset["splitPolicy"], "asset_grouped")

    def test_snapshot_checksum_is_stable_for_equal_timestamp_records(self) -> None:
        records = [
            self.telemetry_record(
                "2026-08-24T03:00:00Z",
                sequence,
                rpm=1500.0 + sequence * 100,
            )
            for sequence in (1, 2, 3)
        ]
        payload = self.dataset_payload(
            checksum="sha256:stable-order-placeholder",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        TELEMETRY_RECORDS.extend(records)
        forward = create_dataset_version(self.admin, payload)

        reset_runtime_state()
        TELEMETRY_RECORDS.extend(reversed(records))
        reverse = create_dataset_version(self.admin, payload)

        self.assertEqual(forward["source"]["checksum"], reverse["source"]["checksum"])
        self.assertEqual(forward["snapshotChecksum"], reverse["snapshotChecksum"])

    def test_dataset_split_requires_enough_asset_groups(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        TELEMETRY_RECORDS.extend(
            [
                self.telemetry_record(format_rfc3339(event_time), 1),
                self.telemetry_record(
                    format_rfc3339(event_time + timedelta(seconds=5)), 2
                ),
            ]
        )
        payload = self.dataset_payload(
            checksum="sha256:single-operating-condition",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["split"] = {"train": 0.7, "validation": 0.2, "test": 0.1}

        with self.assertRaises(ApiError) as invalid_split:
            create_dataset_version(self.admin, payload)

        self.assertEqual(invalid_split.exception.status, 400)
        self.assertEqual(invalid_split.exception.code, "INVALID_DATASET_SPLIT")

    def test_dataset_split_rejects_unsupported_keys(self) -> None:
        payload = self.dataset_payload(
            checksum="sha256:unsupported-split-key",
            source_filters={
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["split"] = {
            "train": 0.5,
            "validation": 0.15,
            "test": 0.1,
            "holdout": 0.25,
        }

        with self.assertRaises(ApiError) as invalid_split:
            create_dataset_version(self.admin, payload)

        self.assertEqual(invalid_split.exception.status, 400)
        self.assertEqual(invalid_split.exception.code, "INVALID_DATASET_SPLIT")

    def test_dataset_read_and_create_audit_enforce_site_scope(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(event_time)
        dataset = create_dataset_version(
            self.admin,
            self.dataset_payload(
                checksum="sha256:site-scoped-dataset",
                source_filters={
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-MOT-02",
                },
                label_mapping={"needs_review": "BEARING_SUSPECT"},
            ),
        )
        site_02_operator = {**self.operator, "allowedSiteIds": ["SITE-02"]}

        with self.assertRaises(ApiError) as forbidden:
            dataset_version_for(site_02_operator, dataset["id"])
        self.assertEqual(forbidden.exception.status, 403)
        self.assertEqual(forbidden.exception.code, "SITE_FORBIDDEN")

        hidden_audits = audit_logs_for(site_02_operator, page=1, size=20)
        self.assertNotIn(
            "dataset.create", {item["action"] for item in hidden_audits["items"]}
        )
        visible_audits = audit_logs_for(self.operator, page=1, size=20)
        dataset_audit = next(
            item
            for item in visible_audits["items"]
            if item["action"] == "dataset.create"
        )
        self.assertEqual(dataset_audit["siteId"], "SITE-01")
        self.assertEqual(dataset_audit["siteIds"], ["SITE-01"])

    def test_external_dataset_source_filters_enforce_site_scope(self) -> None:
        create_asset(
            self.admin,
            "SITE-05",
            {
                "assetCode": "FAN-03",
                "name": "Restricted site fan",
                "assetType": "fan",
                "ratedRpm": 1800,
            },
        )
        payload = self.dataset_payload(
            checksum="sha256:external-site-05",
            source_filters={
                "siteId": "SITE-05",
                "assetId": "SITE-05-FAN-03",
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )
        payload["source"] = {
            "type": "external",
            "uri": "s3://training/site-05/fan-03.csv",
            "license": "project-internal",
            "checksum": checksum_for("external-site-05"),
        }
        dataset = create_dataset_version(self.admin, payload)

        with self.assertRaises(ApiError) as forbidden:
            dataset_version_for(self.operator, dataset["id"])

        self.assertEqual(forbidden.exception.status, 403)
        self.assertEqual(forbidden.exception.code, "SITE_FORBIDDEN")

    def test_taxonomy_audit_preserves_the_previous_active_state(self) -> None:
        update_acoustic_taxonomy(
            self.admin,
            {
                "version": "ACOUSTIC-V2",
                "labels": [
                    {
                        "code": "normal",
                        "name": "Normal",
                        "criteria": "Verified normal sound",
                        "sampleRefs": [],
                    }
                ],
                "reason": "Verify taxonomy audit snapshots",
            },
        )

        audits = audit_logs_for(
            self.admin,
            action="label-taxonomy.update",
            page=1,
            size=10,
        )
        self.assertEqual(audits["total"], 1)
        self.assertTrue(audits["items"][0]["before"]["active"])
        self.assertTrue(audits["items"][0]["after"]["active"])

    def test_source_filter_assets_must_belong_to_selected_sites(self) -> None:
        payload = self.dataset_payload(
            checksum="sha256:cross-site-filter-mismatch",
            source_filters={
                "siteIds": ["SITE-01"],
                "assetIds": ["SITE-01-MOT-02", "SITE-02-GEN-01"],
            },
            label_mapping={"needs_review": "BEARING_SUSPECT"},
        )

        with self.assertRaises(ApiError) as mismatch:
            create_dataset_version(self.admin, payload)

        self.assertEqual(mismatch.exception.status, 400)
        self.assertEqual(mismatch.exception.code, "INVALID_SOURCE_FILTERS")

    def test_frozen_dataset_export_is_unchanged_after_new_telemetry(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-241")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(event_time + timedelta(seconds=10))
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
            self.telemetry_record(
                format_rfc3339(event_time + timedelta(seconds=30)),
                4,
                rpm=2300.0,
            )
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
        self.assertEqual(first_export["manifest"]["recordCount"], 3)
        self.assertEqual(dataset["snapshotRecordCount"], 3)
        self.assertEqual(
            dataset["snapshotChecksum"], first_export["manifest"]["checksum"]
        )

    def test_dataset_export_rejects_site_outside_source_filters(self) -> None:
        event = next(item for item in EVENTS if item["id"] == "EV-238")
        event_time = parse_rfc3339("occurredAt", event["occurredAt"])
        self.add_split_ready_telemetry(
            event_time,
            site_id="SITE-02",
            asset_id="SITE-02-GEN-01",
            device_id="DEV-02-GEN-01",
        )
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

    def test_dataset_list_get_returns_authorized_paginated_versions(self) -> None:
        payload = {
            "name": "dataset-list-contract",
            "source": {
                "type": "external",
                "uri": "s3://training/dataset-list-contract.csv",
                "license": "verified-for-mvp",
                "checksum": checksum_for("dataset-list-contract"),
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
            "sourceFilters": {
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": {"needs_review": "BEARING_SUSPECT"},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "reason": "Verify documented dataset list route",
        }
        status, created = self.request(
            "/api/datasets",
            method="POST",
            payload=payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 201)

        status, listing = self.request(
            "/api/datasets?page=1&size=10", token=self.operator_token
        )

        self.assertEqual(status, 200)
        self.assertEqual(listing["page"], 1)
        self.assertEqual(listing["size"], 10)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], created["id"])

    def test_deep_operating_conditions_return_400_instead_of_500(self) -> None:
        nested: object = "leaf"
        for index in range(600):
            nested = {f"level{index}": nested}
        payload = {
            "name": "deep-operating-conditions",
            "source": {
                "type": "external",
                "uri": "s3://training/deep-operating-conditions.csv",
                "license": "verified-for-mvp",
                "checksum": checksum_for("deep-operating-conditions"),
            },
            "compatibility": {
                "signalType": ["vibration"],
                "samplingRateHz": 12000,
                "units": {"vibration": "g"},
                "operatingConditions": {"nested": nested},
            },
            "sourceFilters": {"siteId": "SITE-01"},
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": {"needs_review": "BEARING_SUSPECT"},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "reason": "Reject pathological nesting safely",
        }

        status, response = self.request(
            "/api/datasets",
            method="POST",
            payload=payload,
            token=self.admin_token,
        )

        self.assertEqual(status, 400)
        self.assertIn(
            response["error"]["code"],
            {"INVALID_OPERATING_CONDITIONS", "INVALID_JSON"},
        )

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
                "checksum": checksum_for("week3-existing-dataset-v1"),
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
        self.assertEqual(dataset["splitPolicy"], "asset_grouped")

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
        self.assertEqual(duplicate["error"]["code"], "DATASET_VERSION_EXISTS")

        invalid_mapping_payload = json.loads(json.dumps(dataset_payload))
        invalid_mapping_payload["source"]["checksum"] = checksum_for(
            "invalid-label-map"
        )
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

        TELEMETRY_RECORDS.extend(
            [
                {
                    "timestamp": f"2026-08-24T03:00:{second:02d}Z",
                    "sequence": index,
                    "siteId": "SITE-01",
                    "assetId": "SITE-01-GEN-01",
                    "deviceId": "DEV-01-GEN-01",
                    "rpm": rpm,
                    "vibrationRmsRaw": 0.08,
                    "vibrationRmsMmS": None,
                    "acousticRmsRaw": 0.007,
                    "acousticDb": None,
                }
                for index, (second, rpm) in enumerate(
                    ((0, 1500.0), (5, 1800.0), (10, 2100.0)), start=1
                )
            ]
        )
        TELEMETRY_RECORDS[0].update(
            {
                "vibrationPeakHz": 1037.11,
                "acousticPeakHz": 216.4,
                "scenarioLabel": "normal",
                "knownVibrationLabel": "NORMAL",
                "knownAcousticLabel": "=1+1",
                "source": "@SUM(1,1)",
                "isSynthetic": True,
                "vibrationUnitNote": "+unsafe spreadsheet value",
                "acousticUnitNote": "raw waveform RMS; not dB SPL",
            }
        )
        internal_payload = json.loads(json.dumps(dataset_payload))
        internal_payload["name"] = "internal-filtered-telemetry-v1"
        internal_payload["source"] = {
            "type": "internal",
            "uri": "api://telemetry",
            "license": "project-internal",
            "checksum": checksum_for("week3-internal-filtered-v1"),
        }
        internal_payload["sourceFilters"] = {
            "siteId": "SITE-01",
            "assetId": "SITE-01-GEN-01",
        }
        internal_payload["labelMapping"] = {"needs_review": "BEARING_SUSPECT"}
        internal_payload["split"] = {
            "train": 1.0,
            "validation": 0.0,
            "test": 0.0,
        }
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
        self.assertIn("source_record_count", reader.fieldnames or [])
        self.assertIn("normalized_record_count", reader.fieldnames or [])
        self.assertIn("split_counts", reader.fieldnames or [])
        self.assertIn("ground_truth_label", reader.fieldnames or [])
        self.assertIn("ground_truth_source", reader.fieldnames or [])
        self.assertIn("telemetry_source", reader.fieldnames or [])
        self.assertIn("is_synthetic", reader.fieldnames or [])
        self.assertIn("vibration_peak_hz", reader.fieldnames or [])
        self.assertIn("acoustic_peak_hz", reader.fieldnames or [])
        self.assertIn("label_priority", reader.fieldnames or [])
        self.assertEqual(first_row["ground_truth_label"], "NORMAL")
        self.assertEqual(first_row["ground_truth_source"], "known_vibration_label")
        self.assertEqual(first_row["known_acoustic_label"], "'=1+1")
        self.assertEqual(first_row["telemetry_source"], "'@SUM(1,1)")
        self.assertEqual(first_row["vibration_unit_note"], "'+unsafe spreadsheet value")
        self.assertEqual(first_row["is_synthetic"], "True")
        self.assertEqual(
            int(first_row["source_record_count"]),
            int(first_row["normalized_record_count"]),
        )
        self.assertEqual(
            sum(json.loads(first_row["split_counts"]).values()),
            int(first_row["normalized_record_count"]),
        )

        unfiltered_payload = json.loads(json.dumps(internal_payload))
        unfiltered_payload["name"] = "internal-unfiltered-telemetry-v1"
        unfiltered_payload["source"]["checksum"] = checksum_for(
            "week3-internal-unfiltered-v1"
        )
        unfiltered_payload.pop("sourceFilters")
        unfiltered_payload["labelMapping"] = {
            "needs_review": "BEARING_SUSPECT",
            "sensor_issue": "SENSOR_NOISE",
        }
        status, unfiltered_dataset = self.request(
            "/api/datasets",
            method="POST",
            payload=unfiltered_payload,
            token=self.admin_token,
        )
        self.assertEqual(status, 201)

        status, reversed_range = self.request(
            "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01"
            f"&datasetId={unfiltered_dataset['id']}"
            "&from=2026-08-25T01:00:00Z&to=2026-08-25T00:00:00Z&format=csv",
            token=self.operator_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(reversed_range["error"]["code"], "INVALID_TIME_RANGE")

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
