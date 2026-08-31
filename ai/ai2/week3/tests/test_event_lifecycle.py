from __future__ import annotations

import unittest

from ai.ai2.week3.event_lifecycle import AnomalyEventLifecycle, EventLifecycleConfig


def point(timestamp: str, score: float, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "assetId": "SITE-01-MOT-02",
        "timestamp": timestamp,
        "anomalyScore": score,
        "anomalyModel": "ai1-week2-baseline-v1",
    }
    value.update(changes)
    return value


class AnomalyEventLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(
                score_enter=75,
                score_exit=50,
                min_consecutive_enter=2,
                min_consecutive_exit=2,
                merge_gap_sec=30,
                rule_version="RULE-SITE-01-MOT-02-v1",
            )
        )

    def test_starts_after_persistent_high_score_and_closes_after_recovery(self) -> None:
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:00Z", 95)), []
        )

        started = self.lifecycle.process_point(point("2026-08-31T00:00:05Z", 90))
        event = started[0]["event"]

        self.assertEqual(started[0]["kind"], "asset_event_started")
        self.assertEqual(event["startAt"], "2026-08-31T00:00:00Z")
        self.assertEqual(event["maxScore"], 95)
        self.assertEqual(event["sampleCount"], 2)
        self.assertEqual(event["thresholdVersion"], "RULE-SITE-01-MOT-02-v1")

        self.lifecycle.process_point(point("2026-08-31T00:00:10Z", 45))
        closed = self.lifecycle.process_point(point("2026-08-31T00:00:15Z", 40))

        self.assertEqual(closed[0]["kind"], "asset_event_closed")
        self.assertEqual(closed[0]["event"]["endAt"], "2026-08-31T00:00:10Z")
        self.assertEqual(closed[0]["event"]["endReason"], "score_recovered")

    def test_merges_a_reopened_event_inside_the_merge_gap(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(
                min_consecutive_enter=1,
                min_consecutive_exit=1,
                merge_gap_sec=30,
            )
        )
        started = lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))
        event_id = started[0]["event"]["id"]
        lifecycle.process_point(point("2026-08-31T00:00:05Z", 40))

        merged = lifecycle.process_point(point("2026-08-31T00:00:20Z", 90))

        self.assertEqual(merged[0]["kind"], "asset_event_merged")
        self.assertEqual(merged[0]["event"]["id"], event_id)
        self.assertEqual(merged[0]["event"]["mergeCount"], 1)
        self.assertEqual(merged[0]["event"]["maxScore"], 90)

    def test_sensor_fault_never_creates_an_asset_event(self) -> None:
        updates = self.lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 100),
            sensor_fault=True,
            sensor_fault_reason="stuck_value",
        )
        self.assertEqual(updates[0]["kind"], "sensor_fault_suppressed")
        self.assertTrue(updates[0]["assetEventExcluded"])
        self.assertEqual(updates[0]["classification"], "sensor_fault")
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:05Z", 100)), []
        )

    def test_sensor_fault_closes_open_event_without_creating_a_sensor_asset_event(
        self,
    ) -> None:
        self.lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))
        self.lifecycle.process_point(point("2026-08-31T00:00:05Z", 80))

        updates = self.lifecycle.process_point(
            point("2026-08-31T00:00:10Z", 90), sensor_fault=True
        )

        self.assertEqual(updates[0]["kind"], "sensor_fault_suppressed")
        self.assertEqual(updates[1]["kind"], "asset_event_closed")
        self.assertEqual(updates[1]["event"]["endReason"], "sensor_fault_detected")

    def test_invalid_score_breaks_entry_streak(self) -> None:
        self.lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:05Z", float("nan"))),
            [],
        )
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:10Z", 80)), []
        )

    def test_rejects_invalid_config_and_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            EventLifecycleConfig(score_enter=50, score_exit=50)
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(point("2026-08-31 00:00:00", 80))
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(point("2026-08-31 00:00:00+00:00", 80))
