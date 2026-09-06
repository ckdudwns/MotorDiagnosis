"""Independent waveform storage, explicit capture permission and AI1 handoff."""

import base64
import copy
import http.client
import json
import math
import os
import sqlite3
import subprocess
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ai.ai1.edge_handoff import convert, features
from motor_diagnosis import data
from motor_diagnosis.edge_analysis import AnalysisStore, CHANNELS, normalize_frame
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup

DEVICE = "DEV-01-GEN-01"


def frame(sequence=1, request_id=None):
    channels = {}
    for name, (rate, count, unit) in CHANNELS.items():
        samples = [
            float(
                math.sin(2 * math.pi * (32 if name != "acoustic" else 128) * i / rate)
            )
            for i in range(count)
        ]
        binary = struct.pack(f"<{count}f", *samples)
        samples = struct.unpack(f"<{count}f", binary)
        channels[name] = {
            "sampleRateHz": rate,
            "sampleCount": count,
            "unit": unit,
            "features": features(samples, 2048 if name == "acoustic" else 512),
        }
        if request_id:
            channels[name]["samplesFloat32LE"] = base64.b64encode(binary).decode(
                "ascii"
            )
    return {
        "schemaVersion": 1,
        "featureVersion": "edge-statistics-v1",
        "deviceId": DEVICE,
        "siteId": "SITE-01",
        "assetId": "SITE-01-GEN-01",
        "sequence": sequence,
        "timestamp": data.now_iso(),
        "requestId": request_id,
        "channels": channels,
    }


class EdgeAnalysisTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "analysis.sqlite3"
        self.store = AnalysisStore(self.path)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()
        super().tearDown()

    def request(self, **changes):
        return self.store.request(
            self.admin,
            DEVICE,
            {
                "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01",
                "reason": "Capture fixture",
                **changes,
            },
        )

    def test_analysis_contract_is_v1_and_does_not_add_speed_fields(self):
        payload = frame()
        normalized, _, _ = normalize_frame(payload)
        self.assertEqual(normalized["schemaVersion"], 1)
        self.assertEqual(set(normalized), set(payload))
        self.assertNotIn("rpm", normalized)
        for fields in ({"schemaVersion": 2}, {"schemaVersion": True}, {"rpm": 1500}):
            with self.subTest(fields=fields):
                with self.assertRaises(data.ApiError):
                    normalize_frame({**payload, **fields})

    def test_pagination_in_same_second_keeps_all_original_frames(self):
        payload = frame()
        for sequence in range(1, 104):
            payload["sequence"] = sequence
            self.store.ingest(self.principal, DEVICE, payload)
        first = self.store.list_device(self.admin, DEVICE)
        second = self.store.list_device(self.admin, DEVICE, cursor=first["nextCursor"])
        self.assertEqual((len(first["items"]), len(second["items"])), (100, 3))
        self.assertEqual(len({r["id"] for r in first["items"] + second["items"]}), 103)
        self.assertIsNone(second["nextCursor"])
        for cursor in ("broken", base64.b64encode(b'[true,"a"]').decode(), "x" * 201):
            with self.assertRaises(data.ApiError):
                self.store.list_device(self.admin, DEVICE, cursor=cursor)

    def test_expiry_remapping_and_numeric_rejection(self):
        request = self.request()
        payload = frame(request_id=request["requestId"])
        with self.store.db:
            self.store.db.execute(
                "UPDATE requests SET created=created-1000,expires=expires-1000"
            )
        self.assertIsNone(self.store.pending(self.principal, DEVICE)["requestId"])
        with self.assertRaises(data.ApiError):
            self.store.ingest(self.principal, DEVICE, payload)
        device = next(d for d in data.DEVICES if d["id"] == DEVICE)
        device["mappingStatus"] = "inactive"
        with self.assertRaises(data.ApiError):
            self.request()
        device["mappingStatus"] = "active"
        device["assetId"] = "SITE-01-MOT-01"
        with self.assertRaises(data.ApiError):
            self.store.ingest(self.principal, DEVICE, payload)
        for value in (True, float("nan"), 10**400):
            malformed = frame()
            malformed["channels"]["acoustic"]["features"]["rms"] = value
            with self.assertRaises(data.ApiError):
                normalize_frame(malformed)

    def test_independent_connections_do_not_overbook_capacity(self):
        other = AnalysisStore(self.path)
        payload = frame()
        try:
            with mock.patch("motor_diagnosis.edge_analysis.MAX_FRAMES_PER_DEVICE", 1):
                first, _ = self.store.ingest(self.principal, DEVICE, payload)
                same, code = other.ingest(self.principal, DEVICE, payload)
                self.assertEqual((same["frameId"], code), (first["frameId"], 200))
                payload["sequence"] = 2
                with self.assertRaises(data.ApiError):
                    other.ingest(self.principal, DEVICE, payload)
        finally:
            other.close()

    @unittest.skipUnless(
        os.environ.get("IOT_ANALYSIS_FIXTURE_EXE"),
        "Build the edge-analysis host fixture",
    )
    def test_production_cpp_frame_matches_backend_and_ai1_features(self):
        self.request(requestId="a" * 32)
        payload = json.loads(
            subprocess.check_output(
                [
                    os.environ["IOT_ANALYSIS_FIXTURE_EXE"],
                    "--emit-fixture",
                    data.now_iso(),
                ],
                text=True,
            )
        )
        accepted, code = self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(code, 201)
        converted = convert(self.store.detail(self.admin, accepted["frameId"]))
        for name, channel in payload["channels"].items():
            for key in ("rms", "peak", "kurtosis"):
                expected = channel["features"][key]
                observed = converted["features"][f"{name}.{key}"]
                if expected is None:
                    self.assertIsNone(observed)
                else:
                    self.assertAlmostEqual(
                        observed, expected, delta=max(1e-6, abs(expected) * 1e-6)
                    )
            calculated = features(
                converted["signals"][name]["samples"],
                2048 if name == "acoustic" else 512,
            )
            for observed, expected in zip(
                calculated["bandEnergy"], channel["features"]["bandEnergy"]
            ):
                self.assertAlmostEqual(
                    observed, expected, delta=max(1e-6, abs(expected) * 1e-6)
                )

    def test_feature_only_ingest_is_idempotent_without_raw_or_labels(self):
        payload = frame()
        accepted, status = self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(status, 201)
        duplicate, status = self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["frameId"], accepted["frameId"])
        detail = self.store.detail(self.admin, accepted["frameId"])
        self.assertFalse(detail["training"]["trainingEligible"])
        with self.assertRaises(ValueError):
            convert(detail)
        payload["channels"]["vibrationX"]["features"]["rms"] = 9
        with self.assertRaises(data.ApiError) as error:
            self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(error.exception.code, "ANALYSIS_SEQUENCE_CONFLICT")

    def test_request_ack_retry_and_capture_restart_keep_exact_identity(self):
        request = self.request(requestId="a" * 32)
        self.assertTrue(self.request(requestId="a" * 32)["duplicate"])
        self.assertEqual(
            self.store.pending(self.principal, DEVICE)["requestId"],
            request["requestId"],
        )
        payload = frame(request_id=request["requestId"])
        accepted, _ = self.store.ingest(self.principal, DEVICE, payload)
        self.assertIsNone(self.store.pending(self.principal, DEVICE)["requestId"])
        self.assertEqual(self.request(requestId="a" * 32)["status"], "completed")
        self.store.close()
        self.store = AnalysisStore(self.path)
        again, status = self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(status, 200)
        self.assertEqual(again["frameId"], accepted["frameId"])
        self.assertEqual(
            self.store.detail(self.admin, accepted["frameId"])["frame"],
            normalize_frame(payload)[0],
        )

    def test_raw_without_matching_request_and_stale_selected_asset_are_rejected(self):
        with self.assertRaises(data.ApiError):
            self.store.ingest(self.principal, DEVICE, frame(request_id="b" * 32))
        with self.assertRaises(data.ApiError) as error:
            self.request(assetId="SITE-01-MOT-01")
        self.assertEqual(error.exception.code, "DEVICE_MAPPING_MISMATCH")
        self.request()
        with self.assertRaises(data.ApiError) as error:
            self.request()
        self.assertEqual(error.exception.code, "WAVEFORM_REQUEST_PENDING")

    def test_failed_commit_cannot_complete_capture_or_change_capacity(self):
        request = self.request()
        payload = frame(request_id=request["requestId"])
        self.store.db.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON frames BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.ingest(self.principal, DEVICE, payload)
        self.assertEqual(
            self.store.db.execute("SELECT bytes FROM usage").fetchone()[0], 0
        )
        self.assertEqual(
            self.store.pending(self.principal, DEVICE)["requestId"],
            request["requestId"],
        )

    def test_retention_runs_on_read_and_restart_without_new_ingest(self):
        accepted, _ = self.store.ingest(self.principal, DEVICE, frame())
        with self.store.db:
            self.store.db.execute("UPDATE frames SET captured=0")
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"], [])
        self.assertEqual(
            self.store.db.execute("SELECT bytes FROM usage").fetchone()[0], 0
        )
        with self.assertRaises(data.ApiError):
            self.store.detail(self.admin, accepted["frameId"])
        with self.assertRaises(data.ApiError):
            self.store.check_device_deletion(DEVICE)

    def test_limits_and_permissions_fail_without_discarding_existing_data(self):
        accepted, _ = self.store.ingest(self.principal, DEVICE, frame())
        with mock.patch("motor_diagnosis.edge_analysis.MAX_FRAMES_PER_DEVICE", 1):
            with self.assertRaises(data.ApiError) as error:
                self.store.ingest(self.principal, DEVICE, frame(sequence=2))
        self.assertEqual(error.exception.code, "ANALYSIS_STORAGE_FULL")
        denied = {**self.admin, "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError):
            self.store.detail(denied, accepted["frameId"])
        self.assertEqual(len(self.store.list_device(self.admin, DEVICE)["items"]), 1)

    def test_raw_schema_and_handoff_integrity_are_explicit(self):
        request = self.request()
        payload = frame(request_id=request["requestId"])
        bad = copy.deepcopy(payload)
        bad["channels"]["vibrationX"]["samplesFloat32LE"] = base64.b64encode(
            struct.pack("<512f", *([float("nan")] * 512))
        ).decode()
        with self.assertRaises(data.ApiError):
            normalize_frame(bad)
        accepted, _ = self.store.ingest(self.principal, DEVICE, payload)
        envelope = self.store.detail(self.admin, accepted["frameId"])
        converted = convert(envelope)
        self.assertEqual(len(converted["signals"]["acoustic"]["samples"]), 10240)
        self.assertFalse(converted["training_eligible"])
        self.assertIsNone(converted["target_label"])
        self.assertEqual(len(converted["featureNames"]), 24)
        self.assertAlmostEqual(
            converted["features"]["acoustic.rms"],
            payload["channels"]["acoustic"]["features"]["rms"],
            places=6,
        )
        envelope["frame"]["sequence"] += 1
        with self.assertRaises(ValueError):
            convert(envelope)


class EdgeAnalysisHttpTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.server = create_server("127.0.0.1", 0, auto_alerts=False)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        super().tearDown()

    def call(self, path, method="GET", body=None, token=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        connection.request(
            method,
            path,
            json.dumps(body) if body is not None else None,
            {
                "Authorization": f"Bearer {token or self.token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        status, result = response.status, json.loads(response.read())
        connection.close()
        return status, result

    def test_real_http_capture_and_user_only_download(self):
        path = f"/api/devices/{DEVICE}/analysis"
        status, request = self.call(
            path + "/requests",
            "POST",
            {
                "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01",
                "reason": "HTTP fixture",
            },
        )
        self.assertEqual(status, 201, request)
        status, pending = self.call(
            path + "/pending", token="demo-telemetry-ingest-token"
        )
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["requestId"], request["requestId"])
        status, accepted = self.call(
            path,
            "POST",
            frame(request_id=request["requestId"]),
            token="demo-telemetry-ingest-token",
        )
        self.assertEqual(status, 201, accepted)
        self.assertEqual(self.call("/api/analysis/" + accepted["frameId"])[0], 200)
        self.assertEqual(
            self.call(
                "/api/analysis/" + accepted["frameId"],
                token="demo-telemetry-ingest-token",
            )[0],
            401,
        )
        status, listing = self.call(path)
        self.assertEqual(status, 200, listing)
        self.assertTrue(listing["items"][0]["hasWaveform"])
        self.assertNotIn(
            "samplesFloat32LE", listing["items"][0]["channels"]["acoustic"]
        )


if __name__ == "__main__":
    unittest.main()
