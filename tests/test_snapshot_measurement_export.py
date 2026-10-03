"""Measurement exports use synthetic accepted snapshots, never model forecasts."""
import copy
import csv
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import threading
import time
from urllib.parse import urlencode
from unittest import mock

from motor_diagnosis import data
from motor_diagnosis import edge_feature_snapshots as contract
from motor_diagnosis import periodic_snapshots
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup
from tests import test_periodic_snapshots as snapshot_fixtures


SCOPE = {"siteId": "SITE-01", "assetId": "SITE-01-MOT-02",
         "deviceId": "DEV-01-MOT-02", "sensorId": "SENSOR-02"}


class SnapshotMeasurementExportTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.base = int(time.time() - 7200) // 25 * 25
        self.store = PeriodicSnapshotStore()
        self.addCleanup(self.store.close)

    def stamp(self, seconds):
        return datetime.fromtimestamp(self.base + seconds, timezone.utc).isoformat()

    def feature_payload(self, index=1, *, valid=True, scope=None, profile=None):
        scope = scope or SCOPE
        profile = profile or contract.RAW_CF_HISTORY_PROFILE_ID
        features = dict(zip(contract.FEATURES, [2.1234567890123, 2., 2., -.3, .1, 0., 3., 3., 3.])) if valid else None
        captured = index * 25 - int(valid)
        window = {
            **scope, "schemaVersion": 3, "bootId": "e" * 32,
            "windowIndex": index * 40, "historySequence": index,
            "timestamp": self.stamp(captured), "startUptimeUs": (captured + 100) * 1000000,
            "sampleRateHz": 800, "sampleCount": 512 if valid else 0,
            "profileId": profile, "axes": ["X", "Y", "Z"], "unit": "dimensionless",
            "quality": "valid" if valid else "invalid", "reason": None if valid else "sensor_unavailable",
            "features": features, "periodicSlotEpoch": self.base + index * 25,
            "integrity": {"algorithm": "sha256", "digest": contract.feature_digest(features)},
        }
        return {"window": window, "transmission": {"policyId": contract.HISTORY_POLICY_ID,
                "eventType": "periodic", "state": "NORMAL", "anomalyCount": 0, "normalCount": 0}}

    def send_feature(self, index=1, *, store=None, **kwargs):
        payload = self.feature_payload(index, **kwargs)
        (store or self.store).ingest(self.principal, payload["window"]["deviceId"], payload)
        return payload

    def export(self, **changes):
        kwargs = {"user": self.admin, "site_id": SCOPE["siteId"], "asset_id": SCOPE["assetId"],
                  "from_timestamp": self.stamp(0), "to_timestamp": self.stamp(3600), **changes}
        return self.store.export_measurements(**kwargs)

    def test_all_measurements_beyond_display_limit_and_no_forecast_values(self):
        for index in range(1, 26):
            self.send_feature(index)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET result=?", (json.dumps({
                "forecast": {"features": dict.fromkeys(contract.FEATURES, 999999)}}),))
        self.assertEqual(len(self.store.list_device(self.admin, SCOPE["deviceId"])["items"]), 20)
        rows = self.export()["rows"]
        self.assertEqual(len(rows), 25)
        self.assertEqual([row["measured_at"] for row in rows], [self.stamp(i * 25 - 1) for i in range(1, 26)])
        self.assertTrue(all(row["cf_a_1"] == 2.1234567890123 for row in rows))
        self.assertTrue(all(row["sk_a_1"] == -.3 for row in rows))
        self.assertEqual(set(rows[0]), set(periodic_snapshots.MEASUREMENT_CSV_FIELDS))
        self.assertNotIn("forecast", json.dumps(rows))

    def test_time_range_is_inclusive_and_uses_measurement_not_receipt_time(self):
        for index in range(1, 5):
            self.send_feature(index)
        start = datetime.fromisoformat(self.stamp(49)).astimezone(timezone(timedelta(hours=9))).isoformat()
        rows = self.export(from_timestamp=start, to_timestamp=self.stamp(74))["rows"]
        self.assertEqual([row["measured_at"] for row in rows], [self.stamp(49), self.stamp(74)])
        self.assertEqual(len(self.export(from_timestamp=self.stamp(24), to_timestamp=self.stamp(24))["rows"]), 1)
        self.assertEqual(self.export(from_timestamp=self.stamp(0), to_timestamp=self.stamp(23))["rows"], [])

    def test_invalid_measurements_remain_blank_and_profiles_are_preserved(self):
        self.send_feature(1, valid=False)
        self.send_feature(2, scope={**SCOPE, "sensorId": "SENSOR-03"}, profile=contract.HISTORY_PROFILE_ID)
        rows = self.export()["rows"]
        self.assertEqual(rows[0]["quality"], "invalid")
        self.assertEqual(rows[0]["reason"], "sensor_unavailable")
        self.assertTrue(all(rows[0][feature] is None for feature in contract.FEATURES))
        self.assertEqual([row["profile_id"] for row in rows],
                         [contract.RAW_CF_HISTORY_PROFILE_ID, contract.HISTORY_PROFILE_ID])
        self.assertEqual(rows[1]["sensor_id"], "SENSOR-03")

    def test_raw_windows_are_not_exported_as_feature_measurements(self):
        raw = snapshot_fixtures.SnapshotTest.payload(self)
        raw["window"].update({key: SCOPE[key] for key in ("siteId", "assetId", "deviceId")})
        raw["window"]["timestamp"] = self.stamp(10)
        self.store.ingest(self.principal, SCOPE["deviceId"], raw)
        self.send_feature()
        rows = self.export()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["profile_id"], contract.RAW_CF_HISTORY_PROFILE_ID)

    def test_permissions_site_asset_and_current_device_mapping_are_enforced(self):
        self.send_feature()
        required = {"export:read", "telemetry:read", "device:read"}
        for missing in required:
            with self.subTest(missing=missing), mock.patch.object(
                data, "role_policy", return_value={"permissions": list(required - {missing})}
            ), self.assertRaises(data.ApiError) as error:
                self.export()
            self.assertEqual(error.exception.status, 403)
        with self.assertRaises(data.ApiError) as error:
            self.export(user={**self.admin, "allowedSiteIds": ["SITE-02"]})
        self.assertEqual(error.exception.code, "SITE_FORBIDDEN")
        with self.assertRaises(data.ApiError) as error:
            self.export(asset_id="SITE-02-MOT-02")
        self.assertEqual(error.exception.code, "ASSET_NOT_FOUND")
        self.assertEqual(self.export(asset_id="SITE-01-GEN-01")["rows"], [])
        device = data.get_device(SCOPE["deviceId"])
        with mock.patch.dict(device, {"assetId": "SITE-01-GEN-01"}):
            self.assertEqual(self.export()["rows"], [])
            self.assertEqual(self.export(asset_id="SITE-01-GEN-01")["rows"], [])

    def test_invalid_or_excessive_ranges_and_row_limits_fail_explicitly(self):
        for start, end, expected in (
            (None, self.stamp(25), "INVALID_TIMESTAMP"),
            ("2026-10-03T12:00:00", self.stamp(25), "INVALID_TIMESTAMP"),
            (self.stamp(25), "invalid", "INVALID_TIMESTAMP"),
            (self.stamp(25), self.stamp(24), "INVALID_TIME_RANGE"),
            (self.stamp(0), self.stamp(31 * 86400 + 1), "SNAPSHOT_EXPORT_RANGE_TOO_LARGE"),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(data.ApiError) as error:
                self.export(from_timestamp=start, to_timestamp=end)
            self.assertEqual(error.exception.code, expected)
        self.assertEqual(self.export(to_timestamp=self.stamp(31 * 86400))["rows"], [])
        for index in range(1, 4):
            self.send_feature(index)
        with mock.patch.object(periodic_snapshots, "EXPORT_LIMIT", 2):
            self.assertEqual(len(self.export(to_timestamp=self.stamp(49))["rows"]), 2)
            with self.assertRaises(data.ApiError) as error:
                self.export()
            self.assertEqual((error.exception.status, error.exception.code), (413, "SNAPSHOT_EXPORT_TOO_LARGE"))

    def test_multiple_current_devices_are_combined_and_limit_applies_to_entire_asset(self):
        self.send_feature(2)
        second = data.get_device("DEV-01-GEN-01")
        with mock.patch.dict(second, {"assetId": SCOPE["assetId"]}):
            self.send_feature(1, scope={**SCOPE, "deviceId": second["id"]})
            rows = self.export()["rows"]
            self.assertEqual([row["device_id"] for row in rows], [second["id"], SCOPE["deviceId"]])
            with mock.patch.object(periodic_snapshots, "EXPORT_LIMIT", 1), self.assertRaises(data.ApiError) as error:
                self.export()
            self.assertEqual(error.exception.code, "SNAPSHOT_EXPORT_TOO_LARGE")
        self.assertEqual(len(self.export()["rows"]), 1)

    def test_http_csv_download_authentication_headers_and_empty_features(self):
        server = create_server("127.0.0.1", 0, demo_enabled=False, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.send_feature(store=server.periodic_snapshots)
            self.send_feature(2, valid=False, store=server.periodic_snapshots)
            query = {"siteId": SCOPE["siteId"], "assetId": SCOPE["assetId"],
                     "from": self.stamp(0), "to": self.stamp(3600)}

            def request(params=None, token=self.token):
                conn = http.client.HTTPConnection(*server.server_address, timeout=10)
                try:
                    headers = {"Authorization": "Bearer " + token} if token else {}
                    conn.request("GET", "/api/periodic-snapshots/export?" + urlencode(query if params is None else params), headers=headers)
                    response = conn.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    conn.close()

            self.assertEqual(request(token=None)[0], 401)
            self.assertEqual(request(token="wrong-token")[0], 401)
            for missing in query:
                self.assertEqual(request({key: value for key, value in query.items() if key != missing})[0], 400)
            status, headers, body = request()
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "text/csv; charset=utf-8")
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["x-measurement-record-count"], "2")
            self.assertIn("content-disposition", headers["access-control-expose-headers"])
            self.assertRegex(headers["content-disposition"], r'^attachment; filename="measurements_\d{8}T\d{6}Z_\d{8}T\d{6}Z\.csv"$')
            self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
            text = body.decode("utf-8-sig")
            self.assertNotIn("\n", text.replace("\r\n", ""))
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(rows[0]["cf_a_1"], "2.1234567890123")
            self.assertEqual(rows[0]["sk_a_1"], "-0.3")
            self.assertEqual(rows[1]["reason"], "sensor_unavailable")
            self.assertTrue(all(rows[1][feature] == "" for feature in contract.FEATURES))
            self.assertFalse(any("forecast" in key for key in rows[0]))
            with mock.patch.object(periodic_snapshots, "EXPORT_LIMIT", 1):
                status, _, body = request()
                self.assertEqual(status, 413)
                self.assertEqual(json.loads(body)["error"]["code"], "SNAPSHOT_EXPORT_TOO_LARGE")
            with mock.patch.object(data, "role_policy", return_value={"permissions": ["device:read", "telemetry:read"]}):
                self.assertEqual(request()[0], 403)

            # Defense in depth: even a future free-text metadata column cannot inject a formula.
            export = server.periodic_snapshots.export_measurements(self.admin, SCOPE["siteId"], SCOPE["assetId"], query["from"], query["to"])
            unsafe = copy.deepcopy(export)
            unsafe["rows"][0]["sensor_id"] = ' =HYPERLINK("https://example.invalid")'
            unsafe["rows"][0]["reason"] = 'sensor, "quoted"'
            with mock.patch.object(server.periodic_snapshots, "export_measurements", return_value=unsafe):
                _, _, body = request()
            row = next(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
            self.assertEqual(row["sensor_id"], "'" + unsafe["rows"][0]["sensor_id"])
            self.assertEqual(row["reason"], 'sensor, "quoted"')
            self.assertEqual(row["sk_a_1"], "-0.3")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
