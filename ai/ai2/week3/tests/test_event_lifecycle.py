from __future__ import annotations

import json
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
        self.assertEqual(closed[0]["event"]["endAt"], "2026-08-31T00:00:15Z")
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

    def test_sensor_fault_breaks_the_merge_boundary(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(
                min_consecutive_enter=1,
                min_consecutive_exit=1,
                merge_gap_sec=30,
            )
        )
        original = lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))[0][
            "event"
        ]
        lifecycle.process_point(point("2026-08-31T00:00:05Z", 40))
        lifecycle.process_point(point("2026-08-31T00:00:10Z", 100), sensor_fault=True)

        restarted = lifecycle.process_point(point("2026-08-31T00:00:20Z", 90))

        self.assertEqual(restarted[0]["kind"], "asset_event_started")
        self.assertNotEqual(restarted[0]["event"]["id"], original["id"])

    def test_sensor_fault_closes_open_event_and_prevents_reopen_merge(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(min_consecutive_enter=1, merge_gap_sec=30)
        )
        original = lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))[0][
            "event"
        ]
        lifecycle.process_point(point("2026-08-31T00:00:05Z", 100), sensor_fault=True)

        restarted = lifecycle.process_point(point("2026-08-31T00:00:20Z", 90))

        self.assertEqual(restarted[0]["kind"], "asset_event_started")
        self.assertNotEqual(restarted[0]["event"]["id"], original["id"])

    def test_event_ids_do_not_collide_between_lifecycle_instances(self) -> None:
        config = EventLifecycleConfig(min_consecutive_enter=1)
        first = AnomalyEventLifecycle(config).process_point(
            point("2026-08-31T00:00:00Z", 80)
        )[0]["event"]
        second = AnomalyEventLifecycle(config).process_point(
            point("2026-08-31T00:00:00Z", 80)
        )[0]["event"]

        self.assertNotEqual(first["id"], second["id"])

    def test_event_start_and_max_score_model_versions_are_preserved(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        started = lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 95, anomalyModel="model-v1")
        )[0]["event"]

        updated = lifecycle.process_point(
            point("2026-08-31T00:00:05Z", 10, anomalyModel="model-v2")
        )[0]["event"]

        self.assertEqual(started["modelVersion"], "model-v1")
        self.assertEqual(updated["modelVersion"], "model-v1")
        self.assertEqual(updated["maxScoreModelVersion"], "model-v1")

    def test_candidate_models_are_preserved_when_starting_an_event(self) -> None:
        self.lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 95, anomalyModel="model-v1")
        )

        started = self.lifecycle.process_point(
            point("2026-08-31T00:00:05Z", 80, anomalyModel="model-v2")
        )[0]["event"]

        self.assertEqual(started["modelVersion"], "model-v1")
        self.assertEqual(started["maxScore"], 95)
        self.assertEqual(started["maxScoreModelVersion"], "model-v1")

    def test_candidate_models_are_preserved_when_merging_an_event(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(
                min_consecutive_enter=2,
                min_consecutive_exit=1,
                merge_gap_sec=30,
            )
        )
        lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 80, anomalyModel="model-v0")
        )
        lifecycle.process_point(
            point("2026-08-31T00:00:01Z", 80, anomalyModel="model-v0")
        )
        lifecycle.process_point(point("2026-08-31T00:00:05Z", 40))
        lifecycle.process_point(
            point("2026-08-31T00:00:10Z", 95, anomalyModel="model-v1")
        )
        merged = lifecycle.process_point(
            point("2026-08-31T00:00:15Z", 80, anomalyModel="model-v2")
        )

        self.assertEqual(merged[0]["kind"], "asset_event_merged")
        self.assertEqual(merged[0]["event"]["modelVersion"], "model-v0")
        self.assertEqual(merged[0]["event"]["maxScore"], 95)
        self.assertEqual(merged[0]["event"]["maxScoreModelVersion"], "model-v1")

    def test_ignores_duplicate_point_and_rejects_out_of_order_point(self) -> None:
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:10Z", 80)), []
        )
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:10Z", 80)), []
        )
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(point("2026-08-31T00:00:05Z", 80))

        started = self.lifecycle.process_point(point("2026-08-31T00:00:15Z", 80))
        self.assertEqual(started[0]["kind"], "asset_event_started")

    def test_overflow_score_resets_candidate_before_duplicate_is_ignored(self) -> None:
        self.lifecycle.process_point(point("2026-08-31T00:00:00Z", 80))
        overflow_score = 10**400
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:05Z", overflow_score)),
            [],
        )
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:05Z", overflow_score)),
            [],
        )
        self.assertEqual(
            self.lifecycle.process_point(point("2026-08-31T00:00:10Z", 80)), []
        )
        started = self.lifecycle.process_point(point("2026-08-31T00:00:15Z", 80))
        self.assertEqual(started[0]["kind"], "asset_event_started")

    def test_normalizes_numeric_duplicate_telemetry_and_rejects_conflict(self) -> None:
        first = point(
            "2026-08-31T00:00:00Z",
            80,
            deviceId="DEVICE-01",
            sequence=1,
            vibrationRmsRaw=3,
        )
        retransmission = point(
            "2026-08-31T00:00:00Z",
            80.0,
            deviceId="DEVICE-01",
            sequence=1,
            vibrationRmsRaw=3.0,
        )
        self.assertEqual(self.lifecycle.process_point(first), [])
        self.assertEqual(self.lifecycle.process_point(retransmission), [])
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(
                point(
                    "2026-08-31T00:00:00Z",
                    81,
                    deviceId="DEVICE-01",
                    sequence=1,
                    vibrationRmsRaw=4.0,
                )
            )

        started = self.lifecycle.process_point(
            point("2026-08-31T00:00:05Z", 80, deviceId="DEVICE-01", sequence=2)
        )
        self.assertEqual(started[0]["kind"], "asset_event_started")

    def test_matches_api_payload_normalization_for_idempotency(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        first_point = point("2026-08-31T00:00:00Z", 80)
        first_payload = {
            "timestamp": "2026-08-31T00:00:00Z",
            "sequence": 1,
            "siteId": "site-01",
            "assetId": "site-01-mot-02",
            "deviceId": "device-01",
            "vibrationRmsRaw": 3,
            "vibrationRmsMmS": None,
            "acousticDb": None,
            "source": "  prototype  ",
        }
        retried_payload = {
            **first_payload,
            "timestamp": "2026-08-31T00:00:00+00:00",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "deviceId": "DEVICE-01",
            "vibrationRmsRaw": 3.0,
            "source": "prototype",
        }

        lifecycle.process_point(first_point, telemetry_payload=first_payload)
        self.assertEqual(
            lifecycle.process_point(first_point, telemetry_payload=retried_payload), []
        )

    def test_persistence_failure_does_not_commit_idempotency_checkpoint(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        record = point(
            "2026-08-31T00:00:00Z",
            80,
            deviceId="DEVICE-01",
            sequence=1,
            vibrationRmsRaw=3.0,
        )

        def persist_failure(_: dict[str, object], __: list[dict[str, object]]) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            lifecycle.process_point(record, persist_transaction=persist_failure)

        restarted = lifecycle.process_point(record)
        self.assertEqual(restarted[0]["kind"], "asset_event_started")

    def test_snapshot_restores_open_event_and_idempotency_history(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        first = point(
            "2026-08-31T00:00:00Z",
            80,
            deviceId="DEVICE-01",
            sequence=1,
            vibrationRmsRaw=3.0,
        )
        started = lifecycle.process_point(first)[0]["event"]

        snapshot = lifecycle.snapshot()
        json.dumps(snapshot, allow_nan=False)
        restored = AnomalyEventLifecycle.from_snapshot(snapshot)
        self.assertEqual(restored.process_point(first), [])
        updated = restored.process_point(
            point(
                "2026-08-31T00:00:05Z",
                90,
                deviceId="DEVICE-01",
                sequence=2,
                vibrationRmsRaw=4.0,
            )
        )[0]["event"]

        self.assertEqual(updated["id"], started["id"])

    def test_idempotency_cache_is_bounded(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(min_consecutive_enter=1, idempotency_cache_size=2)
        )
        for sequence in range(1, 4):
            lifecycle.process_point(
                point(
                    f"2026-08-31T00:00:0{sequence}Z",
                    80,
                    deviceId="DEVICE-01",
                    sequence=sequence,
                    vibrationRmsRaw=float(sequence),
                )
            )

        stored = lifecycle.snapshot()["assets"]["SITE-01-MOT-02"]["processedTelemetry"]
        self.assertEqual(len(stored), 2)
        self.assertEqual([row["sequence"] for row in stored], [2, 3])

    def test_rejects_reverse_sequence_at_the_same_timestamp(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        lifecycle.process_point(
            point(
                "2026-08-31T00:00:00Z",
                90,
                deviceId="DEVICE-01",
                sequence=2,
                vibrationRmsRaw=3.0,
            )
        )

        with self.assertRaisesRegex(ValueError, "sequence must not decrease"):
            lifecycle.process_point(
                point(
                    "2026-08-31T00:00:00Z",
                    10,
                    deviceId="DEVICE-01",
                    sequence=1,
                    vibrationRmsRaw=2.0,
                )
            )

    def test_zero_score_keeps_its_max_score_model_version(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(
                score_enter=0,
                score_exit=0,
                min_consecutive_enter=1,
            )
        )
        started = lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 0, anomalyModel="model-v0")
        )[0]["event"]

        self.assertEqual(started["maxScore"], 0)
        self.assertEqual(started["maxScoreModelVersion"], "model-v0")

    def test_tracks_max_score_model_before_rounding_the_external_value(self) -> None:
        lifecycle = AnomalyEventLifecycle(EventLifecycleConfig(min_consecutive_enter=1))
        lifecycle.process_point(
            point("2026-08-31T00:00:00Z", 95.1, anomalyModel="model-v1")
        )

        updated = lifecycle.process_point(
            point("2026-08-31T00:00:05Z", 95.4, anomalyModel="model-v2")
        )[0]["event"]

        self.assertEqual(updated["maxScore"], 95)
        self.assertEqual(updated["maxScoreModelVersion"], "model-v2")
        self.assertNotIn("_maxScoreRaw", updated)

    def test_recovery_end_at_and_statistics_use_the_confirmation_point(self) -> None:
        lifecycle = AnomalyEventLifecycle(
            EventLifecycleConfig(min_consecutive_enter=1, min_consecutive_exit=2)
        )
        lifecycle.process_point(point("2026-08-31T00:00:00Z", 90))
        lifecycle.process_point(point("2026-08-31T00:00:10Z", 40))

        closed = lifecycle.process_point(point("2026-08-31T00:00:20Z", 30))[0]["event"]

        self.assertEqual(closed["endAt"], "2026-08-31T00:00:20Z")
        self.assertEqual(closed["lastScore"], 30)
        self.assertEqual(closed["sampleCount"], 3)

    def test_allows_zero_hysteresis_and_zero_merge_gap(self) -> None:
        config = EventLifecycleConfig(
            score_enter=75,
            score_exit=75,
            min_consecutive_enter=1,
            min_consecutive_exit=1,
            merge_gap_sec=0,
        )
        lifecycle = AnomalyEventLifecycle(config)
        lifecycle.process_point(point("2026-08-31T00:00:00Z", 75))
        lifecycle.process_point(point("2026-08-31T00:00:05Z", 74))

        merged = lifecycle.process_point(point("2026-08-31T00:00:05Z", 75))

        self.assertEqual(merged[0]["kind"], "asset_event_merged")

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
            EventLifecycleConfig(score_enter=50, score_exit=51)
        with self.assertRaises(ValueError):
            EventLifecycleConfig(merge_gap_sec=-1)
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(point("2026-08-31 00:00:00", 80))
        with self.assertRaises(ValueError):
            self.lifecycle.process_point(point("2026-08-31 00:00:00+00:00", 80))
