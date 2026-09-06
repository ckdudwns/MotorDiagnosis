"""Selected planning gaps: real ingest, frozen evidence and explicit activation."""

import copy
import unittest
from datetime import timedelta

from motor_diagnosis import data
from tests.test_measured_rpm import RpmSetup


class SignalOperationsTest(RpmSetup):
    def activate(self):
        data.BASELINE_VERSIONS.append(
            {
                "version": "BASELINE-FIXTURE",
                "datasetId": "DATASET-FIXTURE",
                "siteId": "SITE-01",
                "assetId": "SITE-01-GEN-01",
                "scopeSiteIds": ["SITE-01"],
                "features": {
                    "vibration": {"normal_range": [0, 0.2]},
                    "acoustic": {"normal_range": [0, 1]},
                    "rpm": {"normal_range": [1400, 1500]},
                },
            }
        )
        payload = self.payload()
        selections = {
            field: {
                "baselineVersion": "BASELINE-FIXTURE",
                "feature": feature,
                "unitNote": unit,
                "scale": scale,
            }
            for field, feature, unit, scale in [
                ("vibrationRmsRaw", "vibration", payload["vibrationUnitNote"], 0.2),
                ("acousticRmsRaw", "acoustic", payload["acousticUnitNote"], 1),
                ("rpm", "rpm", "rpm", 100),
            ]
        }
        return data.update_anomaly_rule(
            self.admin,
            "SITE-01-GEN-01",
            {
                "signalBaselines": selections,
                "durationSec": 1,
                "reason": "Explicit statistical activation fixture",
            },
        )

    def test_acoustic_only_and_rpm_only_drive_actual_ingest(self):
        self.activate()
        for index, changes in enumerate([{"acousticRmsRaw": 2}, {"rpm": 1600}]):
            point = self.payload(index + 1, vibrationRmsRaw=0.1, **changes)
            result, status = data.ingest_telemetry(self.principal, point)
            self.assertEqual(status, 201)
            self.assertEqual(result["anomalyScore"], 100)
        event = next(
            e
            for e in data.EVENTS
            if e.get("triggerTelemetry", {}).get("deviceId") == "DEV-01-GEN-01"
        )
        self.assertTrue(event["triggerEvidence"])
        self.assertEqual(
            event["triggerEvidence"][0]["baselineVersion"], "BASELINE-FIXTURE"
        )

    def test_missing_rpm_and_wrong_units_are_not_scored_as_zero(self):
        self.activate()
        data.ingest_telemetry(
            self.principal,
            self.payload(
                rpm=None,
                rpmStatus="unavailable",
                rpmMeasuredAt=None,
                rpmSource=None,
                vibrationUnitNote="wrong unit",
                acousticRmsRaw=None,
            ),
        )
        stored = data.TELEMETRY_RECORDS[-1]
        self.assertIsNone(stored["anomalyScore"])
        self.assertEqual(
            {r["status"] for r in stored["anomalyEvidence"]},
            {"missing_or_invalid", "unit_mismatch"},
        )

    def test_bound_statistics_are_frozen_and_scope_is_enforced(self):
        rule = self.activate()
        original = copy.deepcopy(rule["signalBaselines"])
        data.BASELINE_VERSIONS[-1]["features"]["rpm"]["normal_range"] = [0, 99999]
        self.assertEqual(
            data.anomaly_rule_for("SITE-01-GEN-01")["signalBaselines"], original
        )
        selected = {
            k: {
                key: v[key]
                for key in ("baselineVersion", "feature", "unitNote", "scale")
            }
            for k, v in original.items()
        }
        with self.assertRaises(data.ApiError):
            data.update_anomaly_rule(
                self.admin,
                "SITE-01-MOT-01",
                {"signalBaselines": selected, "reason": "wrong target"},
            )

    def test_event_installation_stays_frozen_and_legacy_is_unknown(self):
        point = data.INSTALL_POINTS[0]
        event = {
            "id": "EV-INSTALL-TEST",
            "siteId": point["siteId"],
            "assetId": point["assetId"],
            "occurredAt": data.now_iso(),
        }
        point["createdAt"] = data.format_rfc3339(self.started - timedelta(days=1))
        snapshot = data._freeze_event_evidence(event, [], creating=True)[
            "installationSnapshot"
        ]
        self.assertEqual(snapshot["status"], "recorded")
        old_position = snapshot["points"][0]["position"]
        point["position"] = "changed after occurrence"
        self.assertEqual(
            data._freeze_event_evidence(event, [])["installationSnapshot"]["points"][0][
                "position"
            ],
            old_position,
        )
        legacy = {**event, "id": "EV-OLD-INSTALL"}
        self.assertEqual(
            data._freeze_event_evidence(legacy, [])["installationSnapshot"]["status"],
            "unavailable",
        )

    def test_historical_scores_are_not_recomputed_with_current_baseline(self):
        from ai.ai2.week2.anomaly_score import score_telemetry_point

        self.activate()
        original = copy.deepcopy(self.ingest(self.payload(acousticRmsRaw=2)))
        data.BASELINE_VERSIONS.clear()
        rescored = score_telemetry_point(original)
        self.assertEqual(rescored["anomalyScore"], 100)
        self.assertEqual(rescored["anomalyEvidence"], original["anomalyEvidence"])

    def test_live_duration_uses_elapsed_seconds_with_selected_profile(self):
        self.activate()
        data.update_anomaly_rule(
            self.admin,
            "SITE-01-GEN-01",
            {"durationSec": 10, "reason": "Elapsed time fixture"},
        )

        def anomaly_events():
            return [
                e
                for e in data.EVENTS
                if e.get("triggerTelemetry", {}).get("deviceId") == "DEV-01-GEN-01"
            ]

        for index in range(10):
            timestamp = data.format_rfc3339(
                self.started + timedelta(seconds=index / 10)
            )
            self.ingest(
                self.payload(
                    index + 1,
                    timestamp=timestamp,
                    rpmMeasuredAt=timestamp,
                    acousticRmsRaw=2,
                )
            )
        self.assertFalse(anomaly_events())
        timestamp = data.format_rfc3339(self.started + timedelta(seconds=10))
        self.ingest(
            self.payload(
                11, timestamp=timestamp, rpmMeasuredAt=timestamp, acousticRmsRaw=2
            )
        )
        self.assertTrue(anomaly_events())

    def test_installation_reconstructs_before_later_edit_for_replayed_event(self):
        point = data.INSTALL_POINTS[0]
        at = self.started
        before = data.install_point_snapshot(point)
        before["position"] = "original mounting"
        point["position"] = "replacement mounting"
        point["createdAt"] = data.format_rfc3339(at - timedelta(days=1))
        point["changeHistory"] = [
            {
                "changedAt": data.format_rfc3339(at + timedelta(seconds=1)),
                "before": before,
            }
        ]
        event = {
            "id": "EV-REPLAY-INSTALL",
            "siteId": point["siteId"],
            "assetId": point["assetId"],
            "occurredAt": data.format_rfc3339(at),
        }
        snapshot = data._freeze_event_evidence(event, [], creating=True)[
            "installationSnapshot"
        ]
        self.assertEqual(snapshot["points"][0]["position"], "original mounting")

    def test_binding_rejects_nonfinite_boolean_and_unbounded_scale_atomically(self):
        original = self.activate()
        for scale in (0, True, float("nan"), 10**400):
            with self.subTest(scale=scale):
                with self.assertRaises(data.ApiError):
                    data.update_anomaly_rule(
                        self.admin,
                        "SITE-01-GEN-01",
                        {
                            "reason": "Invalid range",
                            "signalBaselines": {
                                "rpm": {
                                    "baselineVersion": "BASELINE-FIXTURE",
                                    "feature": "rpm",
                                    "unitNote": "rpm",
                                    "scale": scale,
                                },
                            },
                        },
                    )
                self.assertEqual(data.anomaly_rule_for("SITE-01-GEN-01"), original)


if __name__ == "__main__":
    unittest.main()
