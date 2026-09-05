from __future__ import annotations

import hashlib
import json
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from motor_diagnosis import data
from motor_diagnosis.mqtt_service import (
    MqttBridgeError,
    is_permanent_ingest_error,
    process_mqtt_message,
)
from motor_diagnosis.telemetry_bulk import ingest_telemetry_bulk


def timestamp(offset_seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def telemetry_payload(sequence: int = 1, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": timestamp(-5),
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
        "scenarioLabel": None,
        "knownVibrationLabel": None,
        "knownAcousticLabel": None,
        "source": "contract-regression",
        "isSynthetic": True,
        "vibrationUnitNote": "raw accelerometer output; not mm/s",
        "acousticUnitNote": "raw waveform RMS; not dB SPL",
    }
    payload.update(changes)
    return payload


class TelemetryLabelAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        data.reset_runtime_state()
        self.general = data.telemetry_principal_for_token("demo-telemetry-ingest-token")
        self.mqtt = data.telemetry_principal_for_token("demo-mqtt-ingest-token")
        self.validation = data.telemetry_principal_for_token(
            "demo-telemetry-validation-token"
        )

    def test_scenario_label_is_required_nullable_and_enum_bounded(self) -> None:
        accepted, status = data.ingest_telemetry(
            self.general, telemetry_payload(scenarioLabel=None)
        )
        self.assertEqual(status, 201)
        self.assertFalse(accepted["duplicate"])
        self.assertIsNone(data.TELEMETRY_RECORDS[0]["scenarioLabel"])

        invalid_values = ("missing", "", "unknown", 3, True, [])
        for index, value in enumerate(invalid_values, start=2):
            with self.subTest(value=value):
                payload = telemetry_payload(index)
                if value == "missing":
                    payload.pop("scenarioLabel")
                else:
                    payload["scenarioLabel"] = value
                with self.assertRaises(data.ApiError) as invalid:
                    data.ingest_telemetry(self.general, payload)
                self.assertEqual(invalid.exception.status, 400)
                self.assertEqual(invalid.exception.code, "INVALID_TELEMETRY_PAYLOAD")

    def test_demo_validation_token_is_disabled_in_production(self) -> None:
        with patch.dict("os.environ", {"APP_ENV": "production"}):
            with self.assertRaises(data.ApiError) as disabled:
                data.telemetry_principal_for_token("demo-telemetry-validation-token")
            self.assertEqual(disabled.exception.status, 401)
            self.assertEqual(disabled.exception.code, "AUTH_REQUIRED")
            general = data.telemetry_principal_for_token("demo-telemetry-ingest-token")
        self.assertNotIn("telemetry:label", general["permissions"])

    def test_only_explicit_label_principal_can_store_labels_with_provenance(
        self,
    ) -> None:
        labeled_payloads = (
            telemetry_payload(1, scenarioLabel="normal"),
            telemetry_payload(2, knownVibrationLabel="bearing_outer_race"),
            telemetry_payload(3, knownAcousticLabel="fan_noise"),
        )
        for principal in (self.general, self.mqtt):
            for payload in labeled_payloads:
                with self.subTest(
                    principal=principal["id"], sequence=payload["sequence"]
                ):
                    with self.assertRaises(data.ApiError) as forbidden:
                        data.ingest_telemetry(principal, payload)
                    self.assertEqual(forbidden.exception.status, 403)
                    self.assertEqual(
                        forbidden.exception.code, "TELEMETRY_LABEL_FORBIDDEN"
                    )
        self.assertEqual(data.TELEMETRY_RECORDS, [])
        self.assertTrue(data.QUARANTINED_DEVICE_MESSAGES)
        self.assertTrue(
            all(
                item["reason"] == "TELEMETRY_LABEL_FORBIDDEN"
                for item in data.QUARANTINED_DEVICE_MESSAGES
            )
        )

        wildcard_admin = {
            "id": "wildcard-system-user",
            "type": "user",
            "permissions": ["*"],
            "allowedDeviceIds": ["*"],
        }
        with self.assertRaises(data.ApiError) as wildcard_forbidden:
            data.ingest_telemetry(
                wildcard_admin, telemetry_payload(4, scenarioLabel="normal")
            )
        self.assertEqual(wildcard_forbidden.exception.code, "TELEMETRY_LABEL_FORBIDDEN")

        accepted, status = data.ingest_telemetry(
            self.validation,
            telemetry_payload(
                5,
                scenarioLabel="normal",
                knownVibrationLabel="bearing_outer_race",
                knownAcousticLabel="fan_noise",
            ),
        )
        self.assertEqual(status, 201)
        stored = data.TELEMETRY_RECORDS[0]
        self.assertEqual(stored["receivedAt"], accepted["receivedAt"])
        self.assertEqual(
            stored["labelProvenance"],
            {
                "principalId": "service-telemetry-validation",
                "principalType": "service",
                "submittedAt": accepted["receivedAt"],
                "fields": [
                    "knownAcousticLabel",
                    "knownVibrationLabel",
                    "scenarioLabel",
                ],
                "verifiedByServer": True,
            },
        )
        queried = data.telemetry_for("SITE-01", "SITE-01-GEN-01")
        self.assertEqual(queried[0]["scenarioLabel"], "normal")
        self.assertEqual(queried[0]["labelProvenance"], stored["labelProvenance"])

    def test_authorization_precedes_mapping_validation(self) -> None:
        mismatch = telemetry_payload(
            1,
            assetId="SITE-01-MOT-02",
            scenarioLabel="normal",
        )
        restricted = {
            "id": "restricted-device",
            "type": "service",
            "permissions": ["telemetry:ingest", "telemetry:label"],
            "allowedDeviceIds": ["DEV-99"],
        }
        with self.assertRaises(data.ApiError) as device_forbidden:
            data.ingest_telemetry(restricted, mismatch)
        self.assertEqual(device_forbidden.exception.code, "TELEMETRY_INGEST_FORBIDDEN")

        with self.assertRaises(data.ApiError) as label_forbidden:
            data.ingest_telemetry(self.general, mismatch)
        self.assertEqual(label_forbidden.exception.code, "TELEMETRY_LABEL_FORBIDDEN")

        with self.assertRaises(data.ApiError) as mapping_mismatch:
            data.ingest_telemetry(self.validation, mismatch)
        self.assertEqual(mapping_mismatch.exception.code, "DEVICE_MAPPING_MISMATCH")
        self.assertEqual(data.TELEMETRY_RECORDS, [])

    def test_bulk_preflights_all_labels_before_any_write(self) -> None:
        payload = {
            "items": [
                telemetry_payload(1),
                telemetry_payload(2, scenarioLabel="normal"),
            ]
        }
        with self.assertRaises(data.ApiError) as forbidden:
            ingest_telemetry_bulk(self.general, payload)
        self.assertEqual(forbidden.exception.status, 403)
        self.assertEqual(forbidden.exception.code, "TELEMETRY_LABEL_FORBIDDEN")
        self.assertEqual(data.TELEMETRY_RECORDS, [])
        self.assertEqual(data.TELEMETRY_METRICS["requests"], 0)

    def test_bulk_and_single_ingest_normalize_blank_labels_consistently(self) -> None:
        single, single_status = data.ingest_telemetry(
            self.general,
            telemetry_payload(1, knownVibrationLabel="   "),
        )
        self.assertEqual(single_status, 201)
        self.assertTrue(single["accepted"])
        self.assertIsNone(data.TELEMETRY_RECORDS[0]["knownVibrationLabel"])

        data.reset_runtime_state()
        bulk = ingest_telemetry_bulk(
            self.general,
            {"items": [telemetry_payload(1, knownVibrationLabel="   ")]},
        )
        self.assertEqual((bulk["accepted"], bulk["rejected"]), (1, 0))
        self.assertIsNone(data.TELEMETRY_RECORDS[0]["knownVibrationLabel"])

        data.reset_runtime_state()
        with self.assertRaises(data.ApiError) as single_invalid:
            data.ingest_telemetry(
                self.general,
                telemetry_payload(1, scenarioLabel="   "),
            )
        self.assertEqual(single_invalid.exception.code, "INVALID_TELEMETRY_PAYLOAD")

        bulk_invalid = ingest_telemetry_bulk(
            self.general,
            {"items": [telemetry_payload(1, scenarioLabel="   ")]},
        )
        self.assertEqual(bulk_invalid["items"][0]["status"], 400)
        self.assertEqual(
            bulk_invalid["items"][0]["error"]["code"],
            "INVALID_TELEMETRY_PAYLOAD",
        )
        self.assertEqual(data.TELEMETRY_RECORDS, [])

    def test_mqtt_label_forbidden_is_terminal_and_acknowledged(self) -> None:
        error = MqttBridgeError(
            403,
            "TELEMETRY_LABEL_FORBIDDEN",
            "labels require a validation principal",
        )
        self.assertTrue(is_permanent_ingest_error(error))

        class FakeClient:
            def __init__(self) -> None:
                self.ack_calls: list[tuple[int, int]] = []

            def ack(self, mid: int, qos: int) -> int:
                self.ack_calls.append((mid, qos))
                return 0

        client = FakeClient()
        message = SimpleNamespace(
            topic="devices/DEV-01-GEN-01/telemetry",
            payload=json.dumps(telemetry_payload()).encode("utf-8"),
            mid=17,
            qos=1,
        )
        with patch(
            "motor_diagnosis.mqtt_service.forward_mqtt_message",
            side_effect=error,
        ):
            outcome = process_mqtt_message(
                client,
                message,
                endpoint="http://backend/api/telemetry/ingest",
                token="demo-mqtt-ingest-token",
            )
        self.assertEqual(outcome, "quarantined")
        self.assertEqual(client.ack_calls, [(17, 1)])


class DatasetLabelPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        data.reset_runtime_state()
        login = data.authenticate({"username": "admin", "password": "admin123"})
        self.admin = data.current_user_for_token(login["session"]["token"])

    def dataset_payload(self, label_mapping: dict[str, str]) -> dict[str, object]:
        return {
            "name": "telemetry-label-policy-v2",
            "source": {
                "type": "internal",
                "uri": "api://telemetry",
                "license": "project-internal",
                "checksum": "sha256:"
                + hashlib.sha256(b"telemetry-label-policy").hexdigest(),
            },
            "compatibility": {
                "signalType": ["vibration", "acoustic"],
                "samplingRateHz": 12000,
                "units": {"vibration": "g", "acoustic": "raw-rms"},
                "operatingConditions": {"rpm": 1800},
            },
            "sourceFilters": {
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
            },
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": label_mapping,
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "reason": "Verify controlled telemetry labels",
        }

    def point(self, timestamp_value: str, sequence: int, **changes: object) -> dict:
        point = {
            "timestamp": timestamp_value,
            "sequence": sequence,
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "deviceId": "DEV-01-MOT-02",
            "rpm": 1800,
            "vibrationRmsRaw": 0.08,
            "vibrationRmsMmS": None,
            "acousticRmsRaw": 0.007,
            "acousticDb": None,
            "scenarioLabel": None,
            "knownVibrationLabel": None,
            "knownAcousticLabel": None,
            "labelProvenance": None,
        }
        point.update(changes)
        return point

    @staticmethod
    def provenance(submitted_at: str, *fields: str) -> dict:
        return {
            "principalId": "service-telemetry-validation",
            "principalType": "service",
            "submittedAt": submitted_at,
            "fields": list(fields),
            "verifiedByServer": True,
        }

    def create_four_state_dataset(self) -> dict:
        data.EVENTS.clear()
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        times = [
            data.format_rfc3339(base + timedelta(seconds=10 * i)) for i in range(4)
        ]
        data.TELEMETRY_RECORDS.extend(
            [
                self.point(
                    times[0],
                    1,
                    scenarioLabel="normal",
                    labelProvenance=self.provenance(times[0], "scenarioLabel"),
                ),
                self.point(times[1], 2),
                self.point(
                    times[2],
                    3,
                    scenarioLabel="normal",
                    source="claimed-validation-source",
                    isSynthetic=True,
                ),
                self.point(
                    times[3],
                    4,
                    knownVibrationLabel="unmapped-bearing-label",
                    labelProvenance=self.provenance(times[3], "knownVibrationLabel"),
                ),
            ]
        )
        data.EVENTS.append(
            {
                "id": "EV-UNREVIEWED-CANDIDATE",
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
                "deviceId": "DEV-01-MOT-02",
                "severity": "warning",
                "eventType": "anomaly",
                "title": "Candidate only",
                "occurredAt": times[1],
                "time": times[1],
                "durationSec": 0,
                "label": "needs_review",
                "reviewed": False,
            }
        )
        return data.create_dataset_version(
            self.admin, self.dataset_payload({"normal": "NORMAL"})
        )

    def test_export_classifies_label_state_and_manifest_counts(self) -> None:
        dataset = self.create_four_state_dataset()
        exported = data.dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )
        rows = exported["rows"]
        self.assertEqual(
            [row["label_status"] for row in rows],
            ["verified", "weak", "unlabeled", "unmapped"],
        )
        self.assertEqual(
            [row["training_eligible"] for row in rows],
            [True, False, False, False],
        )
        self.assertEqual(rows[1]["event_label"], "needs_review")
        self.assertIsNone(rows[1]["label_taxonomy_version"])
        self.assertIsNone(rows[1]["ground_truth_label"])
        self.assertFalse(rows[1]["event_reviewed"])
        self.assertIsNone(rows[2]["scenario_label"])
        self.assertIsNone(rows[2]["ground_truth_label"])
        self.assertIsNone(rows[2]["ground_truth_source"])
        self.assertIsNone(rows[2]["target_label"])
        self.assertIsNone(rows[2]["target_label_taxonomy_version"])
        self.assertEqual(rows[3]["ground_truth_label"], "unmapped-bearing-label")
        self.assertIsNone(rows[3]["target_label"])

        manifest = exported["manifest"]
        self.assertEqual(
            manifest["labelCounts"],
            {"verified": 1, "weak": 1, "unlabeled": 1, "unmapped": 1},
        )
        self.assertEqual(manifest["trainingEligibleCount"], 1)
        self.assertEqual(
            manifest["trainingEligibleSplitCounts"],
            {"train": 1, "validation": 0, "test": 0},
        )
        self.assertNotIn("event_candidate", manifest["labelPriority"])
        self.assertEqual(
            manifest["labelPolicyVersion"], data.DATASET_LABEL_POLICY_VERSION
        )
        self.assertEqual(
            manifest["snapshotSchemaVersion"],
            data.DATASET_SNAPSHOT_SCHEMA_VERSION,
        )

    def test_live_export_requires_active_taxonomy_membership(self) -> None:
        data.EVENTS.clear()
        sample_time = timestamp(-10)
        data.TELEMETRY_RECORDS.append(
            self.point(
                sample_time,
                1,
                knownVibrationLabel="arbitrary-unregistered-label",
                labelProvenance=self.provenance(sample_time, "knownVibrationLabel"),
            )
        )
        normal_time = timestamp(-9)
        data.TELEMETRY_RECORDS.append(
            self.point(
                normal_time,
                2,
                scenarioLabel="normal",
                labelProvenance=self.provenance(normal_time, "scenarioLabel"),
            )
        )

        exported = data.dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
        )
        row = exported["rows"][0]
        self.assertEqual(row["ground_truth_label"], "arbitrary-unregistered-label")
        self.assertIsNone(row["target_label"])
        self.assertIsNone(row["target_label_taxonomy_version"])
        self.assertEqual(row["label_status"], "unmapped")
        self.assertFalse(row["training_eligible"])
        normal_row = exported["rows"][1]
        self.assertEqual(normal_row["target_label"], "NORMAL")
        self.assertEqual(normal_row["target_label_taxonomy_version"], "ACOUSTIC-V1")
        self.assertEqual(normal_row["label_status"], "verified")
        self.assertTrue(normal_row["training_eligible"])
        self.assertEqual(exported["manifest"]["labelTaxonomyVersion"], "ACOUSTIC-V1")

    def test_needs_review_stays_weak_even_after_note_only_review(self) -> None:
        data.EVENTS.clear()
        sample_time = timestamp(-10)
        event = {
            "id": "EV-NOTE-ONLY",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "deviceId": "DEV-01-MOT-02",
            "severity": "warning",
            "eventType": "anomaly",
            "title": "Needs decision",
            "occurredAt": sample_time,
            "time": sample_time,
            "durationSec": 30,
            "label": "needs_review",
            "note": "",
            "reviewed": False,
        }
        data.EVENTS.append(event)

        reviewed = data.review_event(
            self.admin,
            event["id"],
            {"note": "Added context only", "reason": "No final label yet"},
        )
        self.assertFalse(reviewed["event"]["reviewed"])
        self.assertIsNone(reviewed["event"]["reviewedAt"])
        rows = data._dataset_rows_for_points(
            None,
            "SITE-01",
            "SITE-01-MOT-02",
            [self.point(sample_time, 1)],
        )
        self.assertEqual(rows[0]["event_label"], "needs_review")
        self.assertIsNone(rows[0]["ground_truth_label"])
        self.assertEqual(rows[0]["label_status"], "weak")
        self.assertFalse(rows[0]["training_eligible"])

        event["reviewed"] = True
        legacy_rows = data._dataset_rows_for_points(
            None,
            "SITE-01",
            "SITE-01-MOT-02",
            [self.point(sample_time, 2)],
        )
        self.assertEqual(legacy_rows[0]["label_status"], "weak")
        self.assertFalse(legacy_rows[0]["training_eligible"])

    def test_reviewed_event_wins_when_candidate_events_overlap(self) -> None:
        data.EVENTS.clear()
        base = datetime.now(timezone.utc) - timedelta(minutes=5)
        point_time = data.format_rfc3339(base + timedelta(seconds=30))
        reviewed_event = {
            "id": "EV-REVIEWED-OLDER",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "occurredAt": data.format_rfc3339(base),
            "durationSec": 120,
            "label": "confirmed_anomaly",
            "reviewed": True,
        }
        newer_candidate = {
            "id": "EV-CANDIDATE-NEWER",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "occurredAt": data.format_rfc3339(base + timedelta(seconds=20)),
            "durationSec": 120,
            "label": "needs_review",
            "reviewed": False,
        }
        data.EVENTS.extend([reviewed_event, newer_candidate])
        dataset = {
            "labelTaxonomyVersion": "ACOUSTIC-V1",
            "labelMapping": {"confirmed_anomaly": "BEARING_SUSPECT"},
        }

        rows = data._dataset_rows_for_points(
            dataset,
            "SITE-01",
            "SITE-01-MOT-02",
            [self.point(point_time, 1)],
        )
        self.assertEqual(rows[0]["event_id"], reviewed_event["id"])
        self.assertEqual(rows[0]["ground_truth_source"], "event_review")
        self.assertEqual(rows[0]["target_label"], "BEARING_SUSPECT")
        self.assertTrue(rows[0]["training_eligible"])

    def test_live_export_uses_one_taxonomy_snapshot_during_update(self) -> None:
        data.EVENTS.clear()
        base = datetime.now(timezone.utc) - timedelta(minutes=2)
        for sequence in (1, 2):
            sample_time = data.format_rfc3339(base + timedelta(seconds=sequence))
            data.TELEMETRY_RECORDS.append(
                self.point(
                    sample_time,
                    sequence,
                    scenarioLabel="normal",
                    labelProvenance=self.provenance(sample_time, "scenarioLabel"),
                )
            )

        first_row_started = threading.Event()
        continue_export = threading.Event()
        original_ground_truth = data._dataset_ground_truth
        first_call = True

        def pause_after_snapshot(*args, **kwargs):
            nonlocal first_call
            ground_truth = original_ground_truth(*args, **kwargs)
            if first_call:
                first_call = False
                first_row_started.set()
                self.assertTrue(continue_export.wait(timeout=2))
            return ground_truth

        result: dict[str, object] = {}
        failure: list[BaseException] = []

        def run_export() -> None:
            try:
                result.update(
                    data.dataset_export_for(
                        self.admin,
                        "SITE-01",
                        "SITE-01-MOT-02",
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                failure.append(error)

        with patch(
            "motor_diagnosis.data._dataset_ground_truth",
            side_effect=pause_after_snapshot,
        ):
            export_thread = threading.Thread(target=run_export)
            export_thread.start()
            self.assertTrue(first_row_started.wait(timeout=2))
            data.update_acoustic_taxonomy(
                self.admin,
                {
                    "version": "ACOUSTIC-V2",
                    "reason": "Exercise concurrent live export",
                    "labels": [
                        {
                            "code": "OTHER",
                            "name": "Other",
                            "criteria": "Replacement taxonomy label",
                            "sampleRefs": [],
                        }
                    ],
                },
            )
            continue_export.set()
            export_thread.join(timeout=2)

        self.assertFalse(export_thread.is_alive())
        if failure:
            raise failure[0]
        rows = result["rows"]
        self.assertEqual(
            [row["target_label_taxonomy_version"] for row in rows],
            ["ACOUSTIC-V1", "ACOUSTIC-V1"],
        )
        self.assertEqual(result["manifest"]["labelTaxonomyVersion"], "ACOUSTIC-V1")

    def test_live_export_uses_one_event_snapshot_during_review(self) -> None:
        data.EVENTS.clear()
        base = datetime.now(timezone.utc) - timedelta(minutes=2)
        event = {
            "id": "EV-CONCURRENT-REVIEW",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "deviceId": "DEV-01-MOT-02",
            "severity": "warning",
            "eventType": "anomaly",
            "title": "Pending concurrent review",
            "occurredAt": data.format_rfc3339(base),
            "time": data.format_rfc3339(base),
            "durationSec": 120,
            "label": "needs_review",
            "note": "",
            "reviewed": False,
        }
        data.EVENTS.append(event)
        for sequence in (1, 2):
            data.TELEMETRY_RECORDS.append(
                self.point(
                    data.format_rfc3339(base + timedelta(seconds=sequence)),
                    sequence,
                )
            )

        first_row_started = threading.Event()
        continue_export = threading.Event()
        original_ground_truth = data._dataset_ground_truth
        first_call = True

        def pause_after_snapshot(*args, **kwargs):
            nonlocal first_call
            ground_truth = original_ground_truth(*args, **kwargs)
            if first_call:
                first_call = False
                first_row_started.set()
                self.assertTrue(continue_export.wait(timeout=2))
            return ground_truth

        result: dict[str, object] = {}
        failure: list[BaseException] = []

        def run_export() -> None:
            try:
                result.update(
                    data.dataset_export_for(
                        self.admin,
                        "SITE-01",
                        "SITE-01-MOT-02",
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                failure.append(error)

        with patch(
            "motor_diagnosis.data._dataset_ground_truth",
            side_effect=pause_after_snapshot,
        ):
            export_thread = threading.Thread(target=run_export)
            export_thread.start()
            self.assertTrue(first_row_started.wait(timeout=2))
            data.review_event(
                self.admin,
                event["id"],
                {
                    "label": "confirmed_anomaly",
                    "note": "Reviewed while export is running",
                    "reason": "Exercise concurrent live export",
                },
            )
            continue_export.set()
            export_thread.join(timeout=2)

        self.assertFalse(export_thread.is_alive())
        if failure:
            raise failure[0]
        rows = result["rows"]
        self.assertEqual([row["label_status"] for row in rows], ["weak", "weak"])
        self.assertEqual([row["event_reviewed"] for row in rows], [False, False])
        self.assertEqual(result["manifest"]["labelCounts"]["weak"], 2)
        self.assertEqual(result["manifest"]["trainingEligibleCount"], 0)

    def test_live_export_uses_one_asset_snapshot_during_rpm_update(self) -> None:
        data.EVENTS.clear()
        data.TELEMETRY_RECORDS.clear()
        data.update_asset(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            {"ratedRpm": 1450},
        )
        # Use an actual accepted measurement, not the former display fallback.
        principal = data.telemetry_principal_for_token("demo-telemetry-ingest-token")
        data.ingest_telemetry(principal, telemetry_payload(
            assetId="SITE-01-MOT-02", deviceId="DEV-01-MOT-02",
            rpm=1450, isSynthetic=False,
        ))

        rows_ready = threading.Event()
        continue_export = threading.Event()
        original_rows_for_points = data._dataset_rows_for_points

        def pause_after_snapshot(*args, **kwargs):
            rows = original_rows_for_points(*args, **kwargs)
            rows_ready.set()
            self.assertTrue(continue_export.wait(timeout=2))
            return rows

        result: dict[str, object] = {}
        failure: list[BaseException] = []

        def run_export() -> None:
            try:
                result.update(
                    data.dataset_export_for(
                        self.admin,
                        "SITE-01",
                        "SITE-01-MOT-02",
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                failure.append(error)

        with patch(
            "motor_diagnosis.data._dataset_rows_for_points",
            side_effect=pause_after_snapshot,
        ):
            export_thread = threading.Thread(target=run_export)
            export_thread.start()
            self.assertTrue(rows_ready.wait(timeout=2))
            data.update_asset(
                self.admin,
                "SITE-01",
                "SITE-01-MOT-02",
                {"ratedRpm": 1950},
            )
            continue_export.set()
            export_thread.join(timeout=2)

        self.assertFalse(export_thread.is_alive())
        if failure:
            raise failure[0]
        self.assertEqual(
            result["manifest"]["compatibility"]["operatingConditions"]["ratedRpm"],
            1450,
        )
        self.assertEqual(data.get_asset("SITE-01", "SITE-01-MOT-02")["ratedRpm"], 1950)
        self.assertEqual([row["rpm"] for row in result["rows"]], [1450])
        self.assertEqual([row["sequence"] for row in result["rows"]], [1])

    def test_dataset_fingerprint_includes_policy_and_snapshot_versions(self) -> None:
        source = {
            "type": "external",
            "uri": "https://example.org/dataset",
            "license": "project-owned",
            "checksum": "sha256:" + "1" * 64,
        }
        compatibility = {
            "signalType": ["vibration"],
            "samplingRateHz": 12000,
            "units": {"vibration": "g"},
            "operatingConditions": {},
        }
        arguments = (
            source,
            compatibility,
            None,
            "ACOUSTIC-V1",
            {"normal": "NORMAL"},
            {"train": 1.0, "validation": 0.0, "test": 0.0},
        )
        first = data._dataset_version_fingerprint(*arguments, "policy-v1", "1")
        second = data._dataset_version_fingerprint(*arguments, "policy-v2", "2")
        self.assertNotEqual(first, second)

    def test_frozen_v1_snapshot_rows_manifest_and_checksum_remain_stable(self) -> None:
        dataset = self.create_four_state_dataset()
        snapshot = data.DATASET_SNAPSHOTS[dataset["id"]]
        for rows in snapshot.values():
            for row in rows:
                row.pop("label_status", None)
                row.pop("training_eligible", None)
                row.pop("event_reviewed", None)
        stored_dataset = next(
            item for item in data.DATASET_VERSIONS if item["id"] == dataset["id"]
        )
        stored_dataset.pop("labelPolicyVersion")
        stored_dataset.pop("snapshotSchemaVersion")

        before = data.dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )
        before_rows = deepcopy(before["rows"])
        before_manifest = deepcopy(before["manifest"])
        self.assertIn("event_candidate", before_manifest["labelPriority"])
        self.assertNotIn("labelCounts", before_manifest)
        self.assertNotIn("labelPolicyVersion", before_manifest)

        data.TELEMETRY_RECORDS.append(
            self.point(timestamp(-1), 99, scenarioLabel="normal")
        )
        data.EVENTS.clear()
        after = data.dataset_export_for(
            self.admin,
            "SITE-01",
            "SITE-01-MOT-02",
            dataset_id=dataset["id"],
        )
        self.assertEqual(after["rows"], before_rows)
        self.assertEqual(after["manifest"], before_manifest)
        self.assertEqual(after["manifest"]["checksum"], before_manifest["checksum"])


if __name__ == "__main__":
    unittest.main()
