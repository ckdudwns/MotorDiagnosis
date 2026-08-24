from __future__ import annotations

import unittest

from ai.ai2.week2.anomaly_score import latest_asset_statuses, score_telemetry_point


BASELINE = {
    "meta": {"sigma_multiplier": 3.0},
    "features": {
        "rms_mean": {
            "mean": 10.0,
            "std": 2.0,
            "normal_range": [4.0, 16.0],
        }
    },
}


class AnomalyScoreTest(unittest.TestCase):
    def test_value_inside_normal_range_has_zero_score(self) -> None:
        result = score_telemetry_point({"vibrationRmsRaw": 16.0}, BASELINE)

        self.assertEqual(result["anomalyScore"], 0)
        self.assertEqual(result["anomalyStatus"], "normal")

    def test_six_sigma_value_is_critical(self) -> None:
        result = score_telemetry_point({"vibrationRmsRaw": 22.0}, BASELINE)

        self.assertEqual(result["anomalyScore"], 100)
        self.assertEqual(result["anomalyStatus"], "critical")
        self.assertEqual(result["anomalyEvidence"][0]["deviationSigma"], 6.0)

    def test_rounded_score_boundaries_match_status(self) -> None:
        warning = score_telemetry_point({"vibrationRmsRaw": 18.976}, BASELINE)
        critical = score_telemetry_point({"vibrationRmsRaw": 20.476}, BASELINE)

        self.assertEqual(warning["anomalyScore"], 50)
        self.assertEqual(warning["anomalyStatus"], "warning")
        self.assertEqual(critical["anomalyScore"], 75)
        self.assertEqual(critical["anomalyStatus"], "critical")

    def test_missing_raw_vibration_keeps_score_unavailable(self) -> None:
        result = score_telemetry_point({}, BASELINE)

        self.assertIsNone(result["anomalyScore"])
        self.assertEqual(result["anomalyStatus"], "unavailable")

    def test_non_finite_input_keeps_score_unavailable(self) -> None:
        result = score_telemetry_point({"vibrationRmsRaw": float("nan")}, BASELINE)

        self.assertIsNone(result["anomalyScore"])
        self.assertEqual(result["anomalyStatus"], "unavailable")

    def test_negative_rms_keeps_score_unavailable(self) -> None:
        result = score_telemetry_point({"vibrationRmsRaw": -0.1}, BASELINE)

        self.assertIsNone(result["anomalyScore"])
        self.assertEqual(result["anomalyStatus"], "unavailable")

    def test_same_timestamp_uses_higher_sequence_before_received_at(self) -> None:
        statuses = latest_asset_statuses(
            [
                {
                    "assetId": "SITE-01-GEN-01",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "receivedAt": "2026-08-24T00:00:02.000Z",
                    "sequence": 1,
                    "vibrationRmsRaw": 16.0,
                },
                {
                    "assetId": "SITE-01-GEN-01",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "receivedAt": "2026-08-24T00:00:01.000Z",
                    "sequence": 2,
                    "vibrationRmsRaw": 22.0,
                },
            ],
            BASELINE,
        )

        self.assertEqual(statuses["SITE-01-GEN-01"], "critical")
