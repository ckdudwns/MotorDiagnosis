"""Device quality ingestion, attribution, retention and HTTP contracts."""

import copy
import http.client
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from motor_diagnosis import communication_quality, data
from motor_diagnosis.communication_quality import (
    CommunicationQualityStore,
    COUNTERS,
    _iso,
    _timestamp,
)
from motor_diagnosis.runtime_store import RuntimeStateStore
from motor_diagnosis.server import create_server

DEVICE = "DEV-01-MOT-02"
TOKEN = "quality-fixture-" + "A" * 40
OTHER_TOKEN = "quality-fixture-" + "B" * 40
BASE = int(time.time()) // 3600 * 3_600_000
FIXTURE = os.environ.get("IOT_QUALITY_FIXTURE_EXE", "")


def report(window=1, start=BASE, duration=60_000, boot="0" * 31 + "1", **metrics):
    counts = dict.fromkeys(COUNTERS, 0)
    counts.update(
        attempts=2,
        acknowledged=1,
        transportFailures=1,
        retries=1,
        replayAttempts=1,
        ackLatencyTotalMs=100,
        ackLatencyMaxMs=100,
        bufferSamples=2,
        bufferDepthSum=5,
        bufferDepthMax=3,
        bufferDepthLast=2,
        bufferCapacity=24999,
        wifiSamples=2,
        offlineSamples=1,
    )
    counts.update(metrics)
    return {
        "schemaVersion": 1,
        "transport": "http",
        "deviceId": DEVICE,
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "bootId": boot,
        "windowId": window,
        "startUptimeMs": start - BASE,
        "endUptimeMs": start - BASE + duration,
        "startedAt": _iso(start),
        "endedAt": _iso(start + duration),
        "metrics": counts,
    }


def idle_report():
    return report(
        attempts=0,
        acknowledged=0,
        transportFailures=0,
        retries=0,
        replayAttempts=0,
        ackLatencyTotalMs=0,
        ackLatencyMaxMs=0,
    )


def registration(device_id=DEVICE):
    return {
        "id": device_id,
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "certificateId": "CERT-REPLACEMENT",
        "certificateFingerprint": "replacement-fingerprint",
        "certificateIssuedAt": "2026-01-01T00:00:00Z",
        "certificateExpiresAt": "2099-01-01T00:00:00Z",
    }


class QualitySetup(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.addCleanup(data.reset_runtime_state)
        self.addCleanup(data.close_runtime_state)
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DEVICE_QUALITY_TOKENS_JSON": json.dumps(
                    {DEVICE: TOKEN, "DEV-01-GEN-01": OTHER_TOKEN}
                ),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.clock = patch.object(
            CommunicationQualityStore, "now_ms", return_value=BASE + 3_600_000
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        login = data.authenticate({"username": "admin", "password": "admin123"})
        self.admin_token = login["session"]["token"]
        self.admin = data.current_user_for_token(self.admin_token)

    def query_args(self, start=BASE, end=BASE + 3_600_000, bucket=60):
        return {
            "from": [_iso(start)],
            "to": [_iso(end)],
            "bucketSeconds": [str(bucket)],
        }

    def assert_error(self, code, function, *args):
        with self.assertRaises(data.ApiError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)
        return error.exception


class QualityStoreTest(QualitySetup):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = str(Path(self.temporary.name) / "quality.sqlite3")
        self.store = CommunicationQualityStore(self.database)
        self.addCleanup(self.store.close)

    def view(self, **kwargs):
        return self.store.query(self.admin, DEVICE, self.query_args(**kwargs))

    def test_first_report_duplicate_ack_and_content_conflict(self):
        response, status = self.store.ingest(TOKEN, DEVICE, report())
        self.assertEqual(status, 201)
        self.assertFalse(response["duplicate"])
        response, status = self.store.ingest(TOKEN, DEVICE, report())
        self.assertEqual(status, 200)
        self.assertTrue(response["duplicate"])
        self.assert_error(
            "COMMUNICATION_QUALITY_CONFLICT",
            self.store.ingest,
            TOKEN,
            DEVICE,
            report(bufferDropped=1),
        )
        self.assertEqual(self.view()["summary"]["windowCount"], 1)

    def test_weighted_latency_counts_and_explicit_missing_buckets(self):
        self.store.ingest(TOKEN, DEVICE, report())
        self.store.ingest(
            TOKEN,
            DEVICE,
            report(
                2,
                BASE + 60_000,
                attempts=4,
                acknowledged=3,
                ackLatencyTotalMs=900,
                ackLatencyMaxMs=500,
            ),
        )
        result = self.view()
        summary = result["summary"]
        self.assertEqual(summary["attempts"], 6)
        self.assertEqual(summary["failures"], 2)
        self.assertEqual(summary["ackLatencyMeanMs"], 250)
        self.assertEqual(summary["ackLatencyMaxMs"], 500)
        self.assertEqual(summary["bufferDepthSampleMean"], 2.5)
        self.assertEqual(sum(item["attempts"] for item in result["items"]), 6)
        self.assertFalse(result["items"][2]["hasData"])
        self.assertIsNone(result["items"][2]["failureRatePct"])
        self.assertIsNone(result["items"][2]["ackLatencyMeanMs"])
        self.assertIsNone(result["items"][2]["bufferDepthLast"])

    def test_no_ack_is_not_zero_latency_and_zero_depth_is_valid(self):
        self.store.ingest(
            TOKEN,
            DEVICE,
            report(
                attempts=1,
                acknowledged=0,
                ackLatencyTotalMs=0,
                ackLatencyMaxMs=0,
                bufferDepthSum=0,
                bufferDepthMax=0,
                bufferDepthLast=0,
            ),
        )
        summary = self.view()["summary"]
        self.assertIsNone(summary["ackLatencyMeanMs"])
        self.assertIsNone(summary["ackLatencyMaxMs"])
        self.assertEqual(summary["bufferDepthSampleMean"], 0)
        self.assertEqual(summary["failureRatePct"], 100)

    def test_long_offline_window_is_not_fabricated_into_minutes(self):
        self.store.ingest(TOKEN, DEVICE, report(duration=180_000))
        result = self.view()
        self.assertFalse(result["items"][0]["hasData"])
        self.assertFalse(result["items"][1]["hasData"])
        self.assertEqual(result["items"][2]["attempts"], 2)
        self.assertEqual(result["items"][2]["crossBoundaryWindows"], 1)
        self.assertEqual(result["attribution"], "whole_window_at_end")
        cut = self.view(start=BASE + 150_000, end=BASE + 240_000)
        self.assertEqual(cut["summary"]["attempts"], 2)
        self.assertEqual(sum(row["crossBoundaryWindows"] for row in cut["items"]), 1)

    def test_out_of_order_distinct_windows_and_reboot_are_not_counter_subtraction(self):
        self.store.ingest(TOKEN, DEVICE, report(2, BASE + 60_000))
        self.store.ingest(TOKEN, DEVICE, report())
        self.store.ingest(TOKEN, DEVICE, report(1, BASE + 120_000, boot="f" * 32))
        self.assertEqual(self.view()["summary"]["attempts"], 6)
        overlap = report(3, BASE + 30_000)
        self.assert_error(
            "COMMUNICATION_QUALITY_OVERLAP", self.store.ingest, TOKEN, DEVICE, overlap
        )

    def test_tokens_are_separate_device_scoped_and_never_persisted(self):
        for token in (
            "",
            self.admin_token,
            "demo-device-health-token",
            "demo-telemetry-ingest-token",
            "x\ud800",
        ):
            self.assert_error(
                "AUTH_REQUIRED", self.store.ingest, token, DEVICE, report()
            )
        self.assert_error(
            "COMMUNICATION_QUALITY_FORBIDDEN",
            self.store.ingest,
            OTHER_TOKEN,
            DEVICE,
            report(),
        )
        with patch.dict(
            os.environ,
            {
                "DEVICE_QUALITY_TOKENS_JSON": "{}",
                "DEVICE_CONFIG_TOKENS_JSON": json.dumps({DEVICE: TOKEN}),
            },
        ):
            self.assert_error(
                "AUTH_REQUIRED", self.store.ingest, TOKEN, DEVICE, report()
            )
        self.store.ingest(TOKEN, DEVICE, report())
        stored = self.store._db.execute(
            "SELECT payload FROM communication_windows"
        ).fetchone()[0]
        self.assertNotIn(TOKEN, stored)

    def test_malformed_or_duplicate_credentials_fail_closed(self):
        for raw in (
            "[]",
            "{",
            json.dumps({DEVICE: "short"}),
            json.dumps({DEVICE: TOKEN, "OTHER": TOKEN}),
            '{"' + DEVICE + '":"' + TOKEN + '","' + DEVICE + '":"' + OTHER_TOKEN + '"}',
        ):
            with patch.dict(os.environ, {"DEVICE_QUALITY_TOKENS_JSON": raw}):
                error = self.assert_error(
                    "COMMUNICATION_QUALITY_AUTH_UNAVAILABLE",
                    self.store.ingest,
                    TOKEN,
                    DEVICE,
                    report(),
                )
                self.assertNotIn(TOKEN, str(error))

    def test_mapping_and_read_permissions_preserve_provenance(self):
        before = data._runtime_state_payload()
        self.store.ingest(TOKEN, DEVICE, report())
        self.assertEqual(
            data._runtime_state_payload(), before
        )  # No events/health/labels/checkpoints.
        denied = {**self.admin, "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError):
            self.store.query(denied, DEVICE, self.query_args())
        device = data.get_device(DEVICE)
        device["assetId"] = "SITE-01-GEN-01"
        self.assertFalse(self.view()["summary"]["hasData"])
        self.assert_error(
            "COMMUNICATION_QUALITY_SCOPE_MISMATCH",
            self.store.ingest,
            TOKEN,
            DEVICE,
            report(),
        )
        device["mappingStatus"] = "inactive"
        self.assert_error(
            "COMMUNICATION_QUALITY_INACTIVE", self.store.ingest, TOKEN, DEVICE, report()
        )

    def test_strict_metric_types_fields_and_relations_have_no_side_effects(self):
        bad_reports = []
        for key in COUNTERS:
            for value in (True, None, "1", 1.0, -1, 2**53):
                bad = report()
                bad["metrics"][key] = value
                bad_reports.append(bad)
        for changes in (
            {"attempts": 99},
            {"retries": 3},
            {"replayAttempts": 3},
            {"ackLatencyTotalMs": 99},
            {"ackLatencyMaxMs": 99},
            {"bufferCapacity": 0},
            {"bufferDepthMax": 1},
            {"bufferDepthSum": 999},
            {"offlineSamples": 3},
        ):
            bad = report()
            bad["metrics"].update(changes)
            bad_reports.append(bad)
        for bad in bad_reports:
            with self.subTest(bad=bad):
                self.assert_error(
                    "INVALID_COMMUNICATION_QUALITY",
                    self.store.ingest,
                    TOKEN,
                    DEVICE,
                    bad,
                )
        for field in report():
            bad = report()
            bad.pop(field)
            self.assert_error(
                "INVALID_COMMUNICATION_QUALITY", self.store.ingest, TOKEN, DEVICE, bad
            )
        self.assertFalse(self.view()["summary"]["hasData"])

    def test_invalid_clock_scope_protocol_and_identity(self):
        for field, value in (
            ("transport", "mqtt"),
            ("schemaVersion", 2),
            ("schemaVersion", True),
            ("bootId", "xyz"),
            ("windowId", 0),
            ("windowId", True),
            ("endUptimeMs", 59_999),
            ("startedAt", "20260906T000000Z"),
            ("endedAt", _iso(BASE + 4_000_000)),
        ):
            bad = report()
            bad[field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(data.ApiError),
            ):
                self.store.ingest(TOKEN, DEVICE, bad)
        self.assert_error(
            "COMMUNICATION_QUALITY_SCOPE_MISMATCH",
            self.store.ingest,
            TOKEN,
            DEVICE,
            {**report(), "siteId": "SITE-02"},
        )

    def test_timestamp_milliseconds_roundtrip_exactly(self):
        for millisecond in range(1000):
            value = BASE + millisecond
            self.assertEqual(_timestamp(_iso(value), "timestamp"), value)

    def test_concurrent_connections_deduplicate_atomically(self):
        other = CommunicationQualityStore(self.database)
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)

        def send(store):
            barrier.wait()
            return store.ingest(TOKEN, DEVICE, report())[1]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, (self.store, other)))
        self.assertCountEqual(results, [200, 201])
        self.assertEqual(self.view()["summary"]["windowCount"], 1)

    def test_save_failure_is_not_an_ack_and_exact_retry_recovers(self):
        locker = sqlite3.connect(self.database)
        try:
            locker.execute("BEGIN IMMEDIATE")
            error = self.assert_error(
                "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
                self.store.ingest,
                TOKEN,
                DEVICE,
                report(),
            )
            self.assertEqual(error.status, 503)
        finally:
            locker.rollback()
            locker.close()
        self.assertFalse(self.view()["summary"]["hasData"])
        self.assertEqual(self.store.ingest(TOKEN, DEVICE, report())[1], 201)

    def test_restart_retains_dedup_and_aggregates(self):
        self.store.ingest(TOKEN, DEVICE, report())
        before = self.view()
        self.store.close()
        self.store = CommunicationQualityStore(self.database)
        self.addCleanup(self.store.close)
        self.assertEqual(self.view(), before)
        self.assertEqual(self.store.ingest(TOKEN, DEVICE, report())[1], 200)

    def test_direct_delete_is_blocked_after_restart_and_raw_retention(self):
        self.store.ingest(TOKEN, DEVICE, idle_report())
        self.store.close()
        with patch.object(
            CommunicationQualityStore, "now_ms", return_value=BASE + 31 * 86_400_000
        ):
            self.store = CommunicationQualityStore(self.database)
        self.addCleanup(self.store.close)
        self.assertFalse(self.view()["summary"]["hasData"])
        before = data._runtime_state_payload()
        error = self.assert_error(
            "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY",
            data.delete_device,
            self.admin,
            DEVICE,
        )
        self.assertEqual(error.status, 409)
        self.assertEqual(data._runtime_state_payload(), before)

    def test_v1_history_is_migrated_before_startup_pruning(self):
        self.store.ingest(TOKEN, DEVICE, idle_report())
        self.store.close()
        # Reconstruct the previous release's schema in this disposable fixture.
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("DROP TABLE communication_devices")
            database.execute("PRAGMA user_version=1")
        with patch.object(
            CommunicationQualityStore, "now_ms", return_value=BASE + 31 * 86_400_000
        ):
            self.store = CommunicationQualityStore(self.database)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store._db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertFalse(self.view()["summary"]["hasData"])
        self.assert_error(
            "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY",
            data.delete_device,
            self.admin,
            DEVICE,
        )

    def test_history_marker_and_report_commit_atomically(self):
        with self.store._db:
            self.store._db.execute("""CREATE TRIGGER fail_history_marker
                BEFORE INSERT ON communication_devices
                BEGIN SELECT RAISE(ABORT, 'simulated history marker failure'); END""")
        self.assert_error(
            "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
            self.store.ingest,
            TOKEN,
            DEVICE,
            idle_report(),
        )
        self.assertFalse(self.view()["summary"]["hasData"])
        self.assertEqual(
            self.store._db.execute(
                "SELECT count(*) FROM communication_devices"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(data.delete_device(self.admin, DEVICE)["deleted"])

    def test_history_check_failure_is_closed_and_does_not_hold_master_lock(self):
        before = data._runtime_state_payload()

        def unavailable(*args):
            self.assertFalse(data.STORE_LOCK._is_owned())
            raise sqlite3.OperationalError("history unavailable")

        failing = Mock(wraps=self.store._db)
        failing.execute.side_effect = unavailable
        with patch.object(self.store, "_db", failing):
            error = self.assert_error(
                "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
                data.delete_device,
                self.admin,
                DEVICE,
            )
            self.assertEqual(error.status, 503)
            self.assert_error(
                "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
                data.create_device,
                self.admin,
                "SITE-01",
                registration("DEV-NEW-QUALITY"),
            )
        self.assertEqual(data._runtime_state_payload(), before)

    def test_delete_checks_history_across_store_connections(self):
        other = CommunicationQualityStore(self.database)
        self.addCleanup(other.close)
        other.ingest(TOKEN, DEVICE, idle_report())
        other.close()
        self.assert_error(
            "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY",
            data.delete_device,
            self.admin,
            DEVICE,
        )

    def test_retention_queries_hide_expired_without_a_new_write(self):
        self.store.ingest(TOKEN, DEVICE, report())
        with patch.object(
            CommunicationQualityStore, "now_ms", return_value=BASE + 31 * 86_400_000
        ):
            self.assertFalse(self.view()["summary"]["hasData"])
            self.assertEqual(self.store.prune(), 1)
            response, status = self.store.ingest(TOKEN, DEVICE, report())
            self.assertEqual((status, response["disposition"]), (200, "expired"))
            self.assertFalse(self.view()["summary"]["hasData"])

    def test_restart_prunes_expired_and_future_schema_is_not_overwritten(self):
        self.store.ingest(TOKEN, DEVICE, report())
        self.store.close()
        with patch.object(
            CommunicationQualityStore, "now_ms", return_value=BASE + 31 * 86_400_000
        ):
            self.store = CommunicationQualityStore(self.database)
        self.addCleanup(self.store.close)
        self.assertFalse(self.view()["summary"]["hasData"])
        with self.store._db:
            self.store._db.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            CommunicationQualityStore(self.database)
        self.assertEqual(
            self.store._db.execute("PRAGMA user_version").fetchone()[0], 99
        )

    def test_quality_db_cannot_share_general_runtime_state_file(self):
        filename = str(Path(self.temporary.name) / "runtime.sqlite3")
        runtime = RuntimeStateStore(filename)
        self.addCleanup(runtime.close)
        runtime.save({"deviceConfigs": {}}, "fixture")
        with self.assertRaisesRegex(ValueError, "separate database"):
            CommunicationQualityStore(filename)
        self.assertEqual(runtime.load(), {"deviceConfigs": {}})

    def test_idle_retention_worker_runs_without_alerts_or_general_state_db(self):
        self.store.ingest(TOKEN, DEVICE, report())
        removed = threading.Event()
        original_prune = CommunicationQualityStore.prune

        def track_prune(store, *args, **kwargs):
            count = original_prune(store, *args, **kwargs)
            if count:
                removed.set()
            return count

        with (
            patch("motor_diagnosis.server.RETENTION_MAINTENANCE_INTERVAL_SEC", 0.02),
            patch.object(CommunicationQualityStore, "prune", track_prune),
        ):
            server = create_server(
                "127.0.0.1", 0, auto_alerts=False, communication_database=self.database
            )
            try:
                with patch.object(
                    CommunicationQualityStore,
                    "now_ms",
                    return_value=BASE + 31 * 86_400_000,
                ):
                    self.assertTrue(
                        removed.wait(2), "Idle quality retention did not run"
                    )
                self.assertEqual(
                    self.store._db.execute(
                        "SELECT count(*) FROM communication_windows"
                    ).fetchone()[0],
                    0,
                )
            finally:
                server.server_close()

    def test_query_bounds_and_empty_state_are_explicit(self):
        self.assertEqual(len(self.view()["items"]), 60)
        for args in (
            {"bucketSeconds": ["1"]},
            {"bucketSeconds": ["60", "300"]},
            {"extra": ["1"]},
            self.query_args(end=BASE),
            self.query_args(end=BASE + 31 * 86_400_000),
            self.query_args(end=BASE + 2 * 86_400_000, bucket=60),
        ):
            self.assert_error(
                "INVALID_COMMUNICATION_QUALITY",
                self.store.query,
                self.admin,
                DEVICE,
                args,
            )


class QualityHttpTest(QualitySetup):
    def setUp(self):
        super().setUp()
        self.server = create_server(
            "127.0.0.1", 0, auto_alerts=False, demo_enabled=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method="GET", body=None, token=None, query=None, path=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        path = path or f"/api/devices/{DEVICE}/communication-quality"
        if query is not None:
            path += "?" + urlencode(query, doseq=True)
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Authorization": "Bearer "
                    + (self.admin_token if token is None else token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_http_separates_device_write_and_user_read(self):
        self.assertEqual(self.request("POST", report())[0], 401)
        self.assertEqual(self.request("POST", report(), TOKEN)[0], 201)
        self.assertEqual(self.request("POST", report(), TOKEN)[0], 200)
        self.assertNotEqual(self.request(token=TOKEN)[0], 200)
        status, response = self.request(query=self.query_args())
        self.assertEqual(status, 200)
        self.assertEqual(response["summary"]["attempts"], 2)

    def test_quality_only_device_cannot_be_deleted_or_replaced_via_http(self):
        original = copy.deepcopy(data.get_device(DEVICE))
        self.assertNotIn(DEVICE, data.DEVICE_CONFIGS)
        self.assertFalse(
            any(row.get("deviceId") == DEVICE for row in data.TELEMETRY_RECORDS)
        )
        self.assertEqual(self.request("POST", idle_report(), TOKEN)[0], 201)
        before = data._runtime_state_payload()
        status, response = self.request("DELETE", path=f"/api/devices/{DEVICE}")
        self.assertEqual(status, 409, response)
        self.assertEqual(
            response["error"]["code"], "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY"
        )
        self.assertEqual(data._runtime_state_payload(), before)
        self.assertEqual(
            self.request("POST", registration(), path="/api/devices")[0], 400
        )
        self.assertEqual(data.get_device(DEVICE), original)
        self.assertEqual(self.request("POST", idle_report(), TOKEN)[0], 200)
        self.assertEqual(
            self.request(query=self.query_args())[1]["summary"]["windowCount"], 1
        )
        # Deactivation remains the supported operation for an old device.
        self.assertEqual(
            self.request(
                "PATCH", {"mappingStatus": "inactive"}, path=f"/api/devices/{DEVICE}"
            )[0],
            200,
        )

    def test_device_without_quality_or_other_history_can_still_be_deleted(self):
        self.assertEqual(self.request("DELETE", path=f"/api/devices/{DEVICE}")[0], 200)
        self.assertEqual(
            self.request("POST", registration(), path="/api/devices")[0], 201
        )
        self.assertEqual(self.request("POST", idle_report(), TOKEN)[0], 201)

    def test_orphaned_historical_id_cannot_be_registered_via_http(self):
        self.assertEqual(self.request("POST", idle_report(), TOKEN)[0], 201)
        # Simulate an ID removed by the prior unguarded release.
        with data.STORE_LOCK:
            data.DEVICES[:] = [item for item in data.DEVICES if item["id"] != DEVICE]
        before = data._runtime_state_payload()
        status, response = self.request("POST", registration(), path="/api/devices")
        self.assertEqual(status, 409, response)
        self.assertEqual(
            response["error"]["code"], "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY"
        )
        self.assertEqual(data._runtime_state_payload(), before)

    def test_quality_ingest_wins_race_with_device_delete(self):
        entered, release = threading.Event(), threading.Event()
        normalize = communication_quality.normalize_report

        def paused(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Quality fixture was not released")
            return normalize(*args)

        with patch.object(communication_quality, "normalize_report", paused):
            with ThreadPoolExecutor(max_workers=2) as executor:
                ingest = executor.submit(self.request, "POST", idle_report(), TOKEN)
                try:
                    self.assertTrue(entered.wait(2))
                    # A blocked quality transaction must not lock normal state reads.
                    with data.STORE_LOCK:
                        self.assertEqual(data.get_device(DEVICE)["id"], DEVICE)
                    deletion = executor.submit(
                        self.request, "DELETE", path=f"/api/devices/{DEVICE}"
                    )
                    with self.assertRaises(TimeoutError):
                        deletion.result(timeout=0.1)
                finally:
                    release.set()
                self.assertEqual(ingest.result(timeout=5)[0], 201)
                self.assertEqual(deletion.result(timeout=5)[0], 409)
        self.assertEqual(
            self.request(query=self.query_args())[1]["summary"]["windowCount"], 1
        )

    def test_device_delete_wins_race_without_accepting_an_orphaned_report(self):
        entered, release = threading.Event(), threading.Event()
        store = self.server.communication_quality
        check = store.check_device_deletion

        def paused(device_id):
            check(device_id)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Deletion fixture was not released")

        with patch.object(store, "check_device_deletion", paused):
            with ThreadPoolExecutor(max_workers=2) as executor:
                deletion = executor.submit(
                    self.request, "DELETE", path=f"/api/devices/{DEVICE}"
                )
                try:
                    self.assertTrue(entered.wait(2))
                    ingest = executor.submit(self.request, "POST", idle_report(), TOKEN)
                    with self.assertRaises(TimeoutError):
                        ingest.result(timeout=0.1)
                finally:
                    release.set()
                self.assertEqual(deletion.result(timeout=5)[0], 200)
                self.assertEqual(ingest.result(timeout=5)[0], 404)
        self.assertEqual(
            store._db.execute("SELECT count(*) FROM communication_windows").fetchone()[
                0
            ],
            0,
        )

    def test_shutdown_drains_successful_report_before_closing_quality_database(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        store = self.server.communication_quality
        ingest = store.ingest
        close = store.close
        count_at_close = []

        def delayed_ingest(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Fixture was not released")
            return ingest(*args)

        def track_close():
            count_at_close.append(
                store._db.execute(
                    "SELECT count(*) FROM communication_windows"
                ).fetchone()[0]
            )
            closed.set()
            close()

        with (
            patch.object(store, "ingest", delayed_ingest),
            patch.object(store, "close", track_close),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                request = executor.submit(self.request, "POST", report(), TOKEN)
                try:
                    self.assertTrue(entered.wait(2))
                    shutdown = executor.submit(self.finish)
                    self.assertFalse(closed.wait(0.1))
                finally:
                    release.set()
                self.assertEqual(request.result(timeout=5)[0], 201)
                shutdown.result(timeout=5)
        self.assertTrue(closed.is_set())
        self.assertEqual(count_at_close, [1])

    @unittest.skipUnless(
        FIXTURE and Path(FIXTURE).is_file(),
        "Build quality native fixture and set IOT_QUALITY_FIXTURE_EXE",
    )
    def test_native_collector_payload_http_dedup_and_ack_roundtrip(self):
        result = subprocess.run(
            [FIXTURE, "--payload", str(BASE)],
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        body = json.loads(result.stdout)
        for expected_status in (201, 200):
            status, response = self.request("POST", body, TOKEN)
            self.assertEqual(status, expected_status, response)
            accepted = subprocess.run(
                [FIXTURE, "--ack", str(BASE), str(status)],
                input=json.dumps(response),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        _, summary = self.request(query=self.query_args())
        self.assertEqual(summary["summary"]["attempts"], body["metrics"]["attempts"])


if __name__ == "__main__":
    unittest.main()
