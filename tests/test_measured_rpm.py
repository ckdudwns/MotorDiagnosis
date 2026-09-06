"""Equipment-independent RPM contract; fixtures are not hardware measurements."""

from __future__ import annotations

import csv
import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from motor_diagnosis import data
from motor_diagnosis.mqtt_service import forward_mqtt_message
from motor_diagnosis.server import create_server
from motor_diagnosis.telemetry_bulk import ingest_telemetry_bulk
from tests.test_backend_production_integrations import telemetry_payload
from tests import test_week3_backend as week3_helpers


class RpmSetup(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        login = data.authenticate({"username": "admin", "password": "admin123"})
        self.token = login["session"]["token"]
        self.admin = data.current_user_for_token(self.token)
        self.principal = data.telemetry_principal_for_token(
            "demo-telemetry-ingest-token"
        )
        self.started = datetime.now(timezone.utc) - timedelta(minutes=2)

    def tearDown(self):
        data.close_runtime_state()
        data.reset_runtime_state()

    def payload(self, sequence=1, **changes):
        timestamp = data.format_rfc3339(self.started + timedelta(seconds=sequence))
        return {
            **telemetry_payload(sequence, timestamp),
            "rpm": 1450.25,
            "rpmStatus": "valid",
            "rpmMeasuredAt": timestamp,
            "rpmSource": "test-fixture:shaft-feedback",
            **changes,
        }

    def ingest(self, payload):
        result, status = data.ingest_telemetry(self.principal, payload)
        self.assertEqual(status, 201, result)
        return data.TELEMETRY_RECORDS[-1]

    def export(self, **kwargs):
        return data.dataset_export_for(
            self.admin, "SITE-01", "SITE-01-GEN-01", **kwargs
        )


class MeasuredRpmContractTest(RpmSetup):
    def test_valid_stop_and_fractional_rpm_are_not_rated_rpm(self):
        for rpm in (0, 1450.25, 1.0):
            with self.subTest(rpm=rpm):
                record = data.normalize_telemetry_payload(self.payload(rpm=rpm))
                self.assertEqual(record["rpm"], rpm)
                self.assertEqual(record["rpmStatus"], "valid")

    def test_legacy_normalization_adds_no_measurement_claim(self):
        for rpm in (None, 0, 1796.0):
            payload = telemetry_payload(1, self.payload()["timestamp"])
            payload["rpm"] = rpm
            record = data.normalize_telemetry_payload(payload)
            self.assertEqual(record["rpm"], rpm)
            self.assertFalse(
                {"rpmStatus", "rpmMeasuredAt", "rpmSource"} & record.keys()
            )

    def test_missing_and_bad_observations_preserve_null_not_zero(self):
        for status in ("unavailable", "stale", "invalid"):
            payload = self.payload(rpm=None, rpmStatus=status)
            if status == "unavailable":
                payload.update(rpmMeasuredAt=None, rpmSource=None)
            record = data.normalize_telemetry_payload(payload)
            self.assertIsNone(record["rpm"])
            self.assertEqual(record["rpmStatus"], status)
        record = data.normalize_telemetry_payload(
            self.payload(rpm=None, rpmStatus="invalid", rpmMeasuredAt=None)
        )
        self.assertIsNone(record["rpmMeasuredAt"])

    def test_partial_extension_is_rejected(self):
        for missing in ("rpm", "rpmStatus", "rpmSource", "rpmMeasuredAt"):
            payload = self.payload()
            del payload[missing]
            with (
                self.subTest(missing=missing),
                self.assertRaises(data.ApiError) as error,
            ):
                data.normalize_telemetry_payload(payload)
            self.assertEqual(error.exception.status, 400)

    def test_numeric_strings_nonfinite_boolean_and_negative_are_not_valid_feedback(
        self,
    ):
        for rpm in (
            None,
            True,
            False,
            "1450",
            "",
            [],
            {},
            -1,
            float("nan"),
            float("inf"),
            10**400,
        ):
            with self.subTest(rpm=repr(rpm)), self.assertRaises(data.ApiError) as error:
                data.normalize_telemetry_payload(self.payload(rpm=rpm))
            self.assertEqual(error.exception.status, 400)

    def test_invalid_combinations_are_rejected(self):
        changes = [
            {"rpmStatus": value} for value in (None, True, [], "VALID", "estimated")
        ] + [
            {"rpmSource": None},
            {"rpmMeasuredAt": None},
            {"isSynthetic": True},
            {"rpmStatus": "stale"},
            {"rpmStatus": "invalid"},
            {"rpmStatus": "unavailable", "rpm": None},
            {"rpmStatus": "stale", "rpm": None, "rpmMeasuredAt": None},
            {"rpmStatus": "stale", "rpm": None, "rpmSource": None},
            {"rpmStatus": "invalid", "rpm": None, "rpmSource": None},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(data.ApiError):
                data.normalize_telemetry_payload(self.payload(**change))

    def test_source_identifier_validation(self):
        for source in (
            "",
            "  ",
            5,
            False,
            [],
            {},
            "x" * 161,
            "a\nb",
            "\txyz",
            "x\x7fy",
            "x\x85y",
            "x\ud800y",
        ):
            with self.subTest(source=repr(source)), self.assertRaises(data.ApiError):
                data.normalize_telemetry_payload(self.payload(rpmSource=source))
        record = data.normalize_telemetry_payload(
            self.payload(rpmSource="  fixture:shaft  ")
        )
        self.assertEqual(record["rpmSource"], "fixture:shaft")

    def test_measurement_timestamp_uses_strict_calendar_and_known_timezone(self):
        for timestamp in (
            True,
            12,
            {},
            "",
            "20260902T100000Z",
            "2026-W36-3T10:00:00Z",
            "2026-09-02T10:00:00,123Z",
            "2026-09-02 10:00:00Z",
            "2026-09-02T10:00:00",
            "2026-02-30T10:00:00Z",
            "2026-09-02T24:00:00Z",
            "2026-09-02T10:00:60Z",
            "2026-09-02T10:00:00+01:60",
            "2026-09-02T10:00:00+24:00",
            "2026-09-02T10:00:00-00:00",
            "2026-09-02T10:00:00.1234567Z",
            "0001-01-01T00:00:00+01:00",
        ):
            with (
                self.subTest(timestamp=timestamp),
                self.assertRaises(data.ApiError) as error,
            ):
                data.normalize_telemetry_payload(self.payload(rpmMeasuredAt=timestamp))
            self.assertEqual(error.exception.status, 400)

    def test_measurement_time_is_normalized_to_utc_and_kept_during_delayed_ingest(self):
        measured = self.started - timedelta(minutes=10)
        offset = measured.astimezone(timezone(timedelta(hours=9))).isoformat()
        record = self.ingest(self.payload(rpmMeasuredAt=offset))
        self.assertEqual(record["rpmMeasuredAt"], data.format_rfc3339(measured))
        self.assertNotEqual(record["rpmMeasuredAt"], record["receivedAt"])

    def test_measurement_after_telemetry_capture_is_rejected(self):
        timestamp = data.format_rfc3339(self.started + timedelta(seconds=2))
        with self.assertRaises(data.ApiError):
            data.normalize_telemetry_payload(self.payload(rpmMeasuredAt=timestamp))

    def test_observation_metadata_participates_in_idempotency(self):
        payload = self.payload()
        self.ingest(payload)
        self.assertEqual(data.ingest_telemetry(self.principal, payload)[1], 200)
        for change in (
            {"rpmSource": "another-feedback"},
            {"rpm": 0},
            {"rpmMeasuredAt": data.format_rfc3339(self.started)},
        ):
            with self.subTest(change=change), self.assertRaises(data.ApiError) as error:
                data.ingest_telemetry(self.principal, {**payload, **change})
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(data.TELEMETRY_RECORDS), 1)

    def test_bulk_uses_the_same_normalization_and_validation(self):
        result = ingest_telemetry_bulk(
            self.principal,
            {
                "items": [
                    self.payload(1, rpm=0),
                    self.payload(
                        2, rpm=None, rpmStatus="unavailable", rpmMeasuredAt=None
                    ),
                    self.payload(3, rpm="1450"),
                ]
            },
        )
        self.assertEqual(result["accepted"], 2, result)
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(data.TELEMETRY_RECORDS[0]["rpm"], 0)
        self.assertEqual(data.TELEMETRY_RECORDS[1]["rpmStatus"], "unavailable")

    def test_query_and_export_keep_observations_without_promoting_labels(self):
        self.ingest(self.payload())
        points = data.telemetry_for("SITE-01", "SITE-01-GEN-01")
        self.assertEqual(points[0]["rpmSource"], self.payload()["rpmSource"])
        row = self.export()["rows"][0]
        self.assertEqual(row["rpm"], 1450.25)
        self.assertEqual(row["rpm_status"], "valid")
        self.assertEqual(row["rpm_source"], self.payload()["rpmSource"])
        self.assertEqual(row["rpm_measured_at"], self.payload()["rpmMeasuredAt"])
        self.assertFalse(row["training_eligible"])
        self.assertIsNone(row["ground_truth_label"])

    def test_restart_preserves_metadata_and_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "runtime.sqlite3")
            data.configure_runtime_state(database)
            payload = self.payload(rpm=0)
            self.ingest(payload)
            data.close_runtime_state()
            data.reset_runtime_state()
            data.configure_runtime_state(database)
            try:
                point = data.telemetry_for("SITE-01", "SITE-01-GEN-01")[0]
                for key in ("rpm", "rpmStatus", "rpmSource", "rpmMeasuredAt"):
                    self.assertEqual(point[key], payload[key])
                self.assertEqual(data.ingest_telemetry(self.principal, payload)[1], 200)
                self.assertEqual(len(data.TELEMETRY_RECORDS), 1)
            finally:
                data.close_runtime_state()

    def test_frozen_legacy_and_new_rows_do_not_follow_later_observation_changes(self):
        legacy = telemetry_payload(1, self.payload()["timestamp"])
        self.ingest(legacy)
        self.ingest(self.payload(2))
        helper = week3_helpers.Week3DataBoundaryTest()
        dataset = data.create_dataset_version(
            self.admin,
            helper.dataset_payload(
                checksum="measured-rpm-freeze",
                source_filters={"siteId": "SITE-01", "assetId": "SITE-01-GEN-01"},
                label_mapping={"needs_review": "NORMAL"},
            ),
        )
        before = self.export(dataset_id=dataset["id"])
        self.assertNotIn("rpm_status", before["rows"][0])
        self.assertEqual(before["rows"][1]["rpm_status"], "valid")
        data.TELEMETRY_RECORDS[1]["rpmSource"] = "later-source"
        data.ASSETS[0]["ratedRpm"] = 9999
        self.assertEqual(before, self.export(dataset_id=dataset["id"]))


class MeasuredRpmHttpTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.server = create_server(
            "127.0.0.1", 0, demo_enabled=False, auto_alerts=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        super().tearDown()

    def request(self, path, body=None, *, token=None, raw=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(
                "POST" if body is not None else "GET",
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Authorization": "Bearer "
                    + (self.token if token is None else token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            content = response.read()
            return response.status, content if raw else json.loads(content)
        finally:
            connection.close()

    def test_http_ingest_bulk_query_and_mixed_exports(self):
        legacy = telemetry_payload(1, self.payload()["timestamp"])
        status, result = self.request(
            "/api/telemetry/ingest", legacy, token="demo-telemetry-ingest-token"
        )
        self.assertEqual(status, 201, result)
        measured = self.payload(2, rpm=0)
        status, result = self.request(
            "/api/telemetry/bulk",
            {"items": [measured]},
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accepted"], 1)
        status, result = self.request(
            "/api/telemetry?siteId=SITE-01&assetId=SITE-01-GEN-01"
        )
        self.assertEqual(status, 200, result)
        point = next(point for point in result["points"] if point["sequence"] == 2)
        self.assertEqual(point["rpm"], 0)
        self.assertEqual(point["rpmStatus"], "valid")
        path = "/api/datasets/export?siteId=SITE-01&assetId=SITE-01-GEN-01"
        status, content = self.request(path + "&format=csv", raw=True)
        self.assertEqual(status, 200, content)
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["rpm_status"], "")
        self.assertEqual(rows[1]["rpm_status"], "valid")
        self.assertEqual(float(rows[1]["rpm"]), 0)
        self.assertEqual(rows[1]["rpm_measured_at"], measured["rpmMeasuredAt"])
        status, content = self.request(path + "&format=xlsx", raw=True)
        self.assertEqual(status, 200)
        with ZipFile(io.BytesIO(content)) as archive:
            xml = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet_rows = xml.findall("s:sheetData/s:row", ns)
        headers = [cell.find("s:is/s:t", ns).text for cell in sheet_rows[0]]
        cells = {header: cell for header, cell in zip(headers, sheet_rows[2])}
        self.assertEqual(cells["rpm_status"].find("s:is/s:t", ns).text, "valid")
        self.assertEqual(
            cells["rpm_source"].find("s:is/s:t", ns).text, measured["rpmSource"]
        )
        self.assertEqual(float(cells["rpm"].find("s:v", ns).text), 0)

    def test_invalid_feedback_and_missing_authorization_cannot_be_stored(self):
        status, _ = self.request("/api/telemetry/ingest", self.payload(), token="")
        self.assertEqual(status, 401)
        status, result = self.request(
            "/api/telemetry/ingest",
            self.payload(rpm=-1),
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 400, result)
        self.assertEqual(data.TELEMETRY_RECORDS, [])

    def test_mqtt_http_bridge_preserves_original_observation(self):
        payload = self.payload(rpm=None, rpmStatus="stale")
        result, status = forward_mqtt_message(
            "devices/DEV-01-GEN-01/telemetry",
            json.dumps(payload).encode("utf-8"),
            endpoint=f"http://127.0.0.1:{self.server.server_port}/api/telemetry/ingest",
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 201, result)
        record = data.TELEMETRY_RECORDS[0]
        for key in ("rpm", "rpmStatus", "rpmMeasuredAt", "rpmSource"):
            self.assertEqual(record[key], payload[key])


if __name__ == "__main__":
    unittest.main()
