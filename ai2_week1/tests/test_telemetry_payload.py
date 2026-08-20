"""Regression tests for the AI-2 external telemetry payload contract."""

from __future__ import annotations

import sys
import tempfile
import unittest
import csv
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_payload import to_external_payload
from prepare_ai1_handoff import REQUIRED_COLUMNS, read_and_validate_rows


class TelemetryPayloadTest(unittest.TestCase):
    def test_camel_case_input_remains_sendable(self) -> None:
        payload = to_external_payload(
            {
                "timestamp": "2026-08-20T00:00:00Z",
                "sequence": 1,
                "siteId": "SYN-SITE-01",
                "assetId": "SYN-ASSET-01",
                "deviceId": "SYN-DEV-01",
                "rpm": 1796.0,
                "vibrationRmsRaw": 0.07,
                "vibrationPeakHz": 1037.11,
                "acousticRmsRaw": 0.01,
                "acousticPeakHz": 216.4,
                "scenarioLabel": "normal",
                "isSynthetic": True,
            }
        )

        self.assertEqual(payload["scenarioLabel"], "normal")
        self.assertEqual(payload["sequence"], 1)

    def test_nullable_values_remain_json_null(self) -> None:
        payload = to_external_payload(
            {
                "timestamp": "2026-08-20T00:00:00Z",
                "sequence": "1",
                "site_id": "SYN-SITE-01",
                "asset_id": "SYN-ASSET-01",
                "device_id": "SYN-DEV-01",
                "rpm": "",
                "vibration_rms_raw": "0.07",
                "vibration_peak_hz": "1037.11",
                "acoustic_rms_raw": "",
                "acoustic_peak_hz": "",
                "scenario_label": "vibration_anomaly",
                "is_synthetic": "true",
            }
        )

        self.assertIsNone(payload["rpm"])
        self.assertIsNone(payload["acousticRmsRaw"])
        self.assertIsNone(payload["acousticPeakHz"])
        self.assertIsNone(payload["vibrationRmsMmS"])
        self.assertIsNone(payload["acousticDb"])

    def test_handoff_validation_allows_nullable_input_fields(self) -> None:
        row = {column: "" for column in REQUIRED_COLUMNS}
        row.update(
            {
                "timestamp": "2026-08-20T00:00:00Z",
                "device_id": "SYN-DEV-01",
                "asset_id": "SYN-ASSET-01",
                "vibration_rms_raw": "0.07",
                "vibration_peak_hz": "1037.11",
                "known_vibration_label": "NORMAL",
                "scenario_label": "vibration_anomaly",
                "source": "CWRU_only_synthetic",
                "is_synthetic": "true",
                "vibration_unit_note": "raw accelerometer output",
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(REQUIRED_COLUMNS))
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(len(read_and_validate_rows(path)), 1)


if __name__ == "__main__":
    unittest.main()
