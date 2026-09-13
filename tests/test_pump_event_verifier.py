"""Regression tests for immediate-event confirmation against prior server history."""

from datetime import datetime, timedelta, timezone
import unittest

import numpy as np

from ai.ai2.pump_event_verifier import (
    HISTORY_ROWS,
    PumpEventVerifier,
    train_event_verifier,
)


class PumpEventVerifierTest(unittest.TestCase):
    def rows(self, count=600):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [
            {
                "_document_id": str(index),
                "createdAt": (start + timedelta(seconds=25 * index)).isoformat(),
                "rms_a_1": str(5 + np.sin(index / 7)),
            }
            for index in range(count)
        ]

    def verifier(self):
        model, _ = train_event_verifier({"sensor": self.rows()}, ["rms_a_1"])
        return PumpEventVerifier(model)

    def history(self, rows, start=100):
        return [
            {
                "timestamp": row["createdAt"],
                "sequence": index,
                "values": [float(row["rms_a_1"])],
            }
            for index, row in enumerate(rows[start : start + HISTORY_ROWS], start)
        ]

    def test_current_event_is_compared_with_exactly_24_prior_records(self):
        rows = self.rows()
        result = self.verifier().verify(
            "sensor",
            rows[124]["createdAt"],
            [100.0],
            self.history(rows),
            quality_ok=True,
        )
        self.assertEqual(result["decision"], "possible_anomaly")
        self.assertEqual(result["historicalRecordsUsed"], HISTORY_ROWS)
        self.assertFalse(result["groundTruthAvailable"])

    def test_missing_or_discontinuous_history_is_not_silently_scored(self):
        rows = self.rows()
        verifier = self.verifier()
        missing = verifier.verify(
            "sensor",
            rows[124]["createdAt"],
            [5.0],
            self.history(rows)[:-1],
            quality_ok=True,
        )
        self.assertEqual(missing["decision"], "insufficient_history")
        broken_history = self.history(rows)
        broken_history[5]["sequence"] = 99
        broken = verifier.verify(
            "sensor", rows[124]["createdAt"], [5.0], broken_history, quality_ok=True
        )
        self.assertEqual(
            broken["reason"], "history_timestamp_or_sequence_discontinuity"
        )

    def test_invalid_event_quality_is_classified_as_sensor_issue(self):
        rows = self.rows()
        result = self.verifier().verify(
            "sensor",
            rows[124]["createdAt"],
            [5.0],
            self.history(rows),
            quality_ok=False,
        )
        self.assertEqual(result["decision"], "likely_sensor_issue")

    def test_impossible_crest_factor_is_not_scored_as_a_real_anomaly(self):
        model, _ = train_event_verifier({"sensor": self.rows()}, ["rms_a_1"])
        model["features"] = ["cf_a_1"]
        rows = self.rows()
        result = PumpEventVerifier(model).verify(
            "sensor",
            rows[124]["createdAt"],
            [-0.323],
            self.history(rows),
            quality_ok=True,
        )
        self.assertEqual(result["decision"], "likely_sensor_issue")
        self.assertEqual(result["reason"], "input_physical_constraint_violation")


if __name__ == "__main__":
    unittest.main()
