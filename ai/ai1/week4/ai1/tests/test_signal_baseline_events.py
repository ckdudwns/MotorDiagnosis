"""PR22: generated/registered signal baselines reach the existing event evaluator."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "week4/ai1/dataset_versions"))
sys.path.insert(0, str(ROOT / "week3/ai1/anomaly_rules"))

import anomaly_rule as rules
import signal_baseline as signals
import validate_features as validation


def rpm_baseline(values=(1450, 1460, 1470), sigma=3):
    rows = [
        {
            "rpm": value,
            "rpm_unit": "rpm",
            "site_id": "S1",
            "asset_id": "A1",
            "operating_conditions": {"load": "rated"},
            "source_ref": f"sample/{index}",
            "label_status": "verified",
            "training_eligible": True,
            "target_label": "NORMAL",
            "split": "train",
            "is_synthetic": False,
        }
        for index, value in enumerate(values)
    ]
    return signals.build_rpm_baseline(
        rows,
        dataset_id="DS1",
        site_id="S1",
        asset_id="A1",
        operating_conditions={"load": "rated"},
        sigma=sigma,
    )


def lifecycle(baseline):
    registry = signals.BaselineVersionRegistry()
    draft = signals.register_signal_baseline(registry, baseline)
    approved = registry.approve(baseline["id"], approved_by="fixture", reason="test")
    active = registry.activate(baseline["id"])
    return baseline, draft, approved, active


class SignalBaselineEventsTest(unittest.TestCase):
    def evaluate(self, baseline, values, config=None):
        before = copy.deepcopy(baseline)
        result = rules.evaluate_feature_stream(
            [{"rpm": value} for value in values], baseline, config
        )
        self.assertEqual(baseline, before)
        json.dumps(result, allow_nan=False)
        return result

    def assert_cycle(self, result):
        self.assertEqual(
            [w["state"] for w in result["window_states"]],
            ["NORMAL", "NORMAL", "ANOMALY", "ANOMALY", "NORMAL"],
        )
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["start_index"], 1)
        self.assertEqual(result["events"][0]["end_index"], 3)
        self.assertEqual(result["window_states"][1]["outlier_features"], ["rpm"])

    def test_review_reproduction_through_all_registry_states(self):
        for baseline in lifecycle(rpm_baseline()):
            with self.subTest(status=baseline.get("status", "generated")):
                self.assert_cycle(
                    self.evaluate(baseline, [1460, 2000, 2000, 1460, 1460])
                )

    def test_custom_sigma_keeps_stored_entry_and_scaled_recovery(self):
        for sigma in (0.5, 1, 5):
            for baseline in lifecycle(rpm_baseline(sigma=sigma)):
                stats = baseline["features"]["rpm"]
                mean, std = stats["mean"], stats["std"]
                outside = mean + 1.2 * sigma * std
                middle = mean + 0.8 * sigma * std
                inside = mean + 0.5 * sigma * std
                with self.subTest(sigma=sigma, status=baseline.get("status")):
                    result = self.evaluate(
                        baseline,
                        [mean, outside, outside, middle, middle, inside, inside],
                    )
                    self.assertEqual(
                        [w["state"] for w in result["window_states"]],
                        [
                            "NORMAL",
                            "NORMAL",
                            "ANOMALY",
                            "ANOMALY",
                            "ANOMALY",
                            "ANOMALY",
                            "NORMAL",
                        ],
                    )
                    self.assertEqual(result["events"][0]["end_index"], 5)
                    self.assertAlmostEqual(
                        result["events"][0]["max_deviation_sigma"], 1.2 * sigma
                    )

    def test_stored_range_boundaries_do_not_trigger_entry(self):
        baseline = rpm_baseline(sigma=1)
        for bound in baseline["features"]["rpm"]["normal_range"]:
            result = self.evaluate(baseline, [bound, bound, bound])
            self.assertEqual(result["events"], [])
            self.assertTrue(
                all(not w["outlier_features"] for w in result["window_states"])
            )

    def test_zero_variance_triggers_and_recovers_without_nonfinite_scores(self):
        for baseline in lifecycle(rpm_baseline((1450, 1450))):
            result = self.evaluate(baseline, [1450, 10000, 10000, 1450, 1450])
            self.assert_cycle(result)
            self.assertIsNone(result["events"][0]["max_deviation_sigma"])
            self.assertTrue(
                all(w["max_deviation_sigma"] is None for w in result["window_states"])
            )

    def test_zero_variance_tolerance_controls_entry_and_recovery(self):
        baseline = rpm_baseline((1450, 1450))
        # Explicit policy fixture; no attempt to calibrate or deploy a real sensor.
        baseline["signalContext"]["evaluationPolicy"]["absoluteTolerance"] = 1
        baseline["features"]["rpm"]["normal_range"] = [1449, 1451]
        self.assert_cycle(self.evaluate(baseline, [1451, 1452, 1452, 1449, 1451]))

    def test_explicit_matching_rule_config_is_supported(self):
        baseline = rpm_baseline(sigma=1)
        config = rules.AnomalyRuleConfig(sigma_enter=1, sigma_exit=0.25)
        self.assert_cycle(
            self.evaluate(baseline, [1460, 2000, 2000, 1460, 1460], config)
        )

    def test_explicit_rule_config_cannot_override_stored_range(self):
        baseline = rpm_baseline(sigma=1)
        with self.assertRaisesRegex(ValueError, "override"):
            self.evaluate(baseline, [1460], rules.AnomalyRuleConfig())
        for sigma in (0, 3):
            with self.assertRaisesRegex(ValueError, "override"):
                validation.check_outliers({"rpm": 2000}, baseline, sigma)

    def test_asset_registry_preserves_custom_default_config(self):
        baseline = lifecycle(rpm_baseline(sigma=1))[-1]
        registry = rules.AssetBaselineRegistry()
        registry.register(baseline, asset_id="A1")
        stats = baseline["features"]["rpm"]
        high = stats["mean"] + 1.2 * stats["std"]
        result = rules.evaluate_asset_stream(
            "A1", [{"rpm": v} for v in [1460, high, high, 1460, 1460]], registry
        )
        self.assert_cycle(result)
        self.assertEqual(result["events"][0]["asset_id"], "A1")

    def test_invalid_windows_break_contextual_entry_and_recovery_counts(self):
        for baseline in (rpm_baseline(), rpm_baseline((1460, 1460))):
            result = self.evaluate(baseline, [2000, None, 2000, 1460, 1460])
            self.assertEqual(result["events"], [])
            result = self.evaluate(baseline, [2000, 2000, 1460, None, 1460])
            self.assertEqual(len(result["events"]), 1)
            self.assertIsNone(result["events"][0]["end_index"])

    def test_deviation_lookup_does_not_change_thresholds(self):
        baseline = rpm_baseline(sigma=1)
        stats = baseline["features"]["rpm"]
        inside = stats["mean"] + 0.5 * stats["std"]
        deviations = validation.feature_deviations({"rpm": inside}, baseline)
        self.assertAlmostEqual(deviations[0]["deviation_sigma"], 0.5)
        self.assertEqual(validation.check_outliers({"rpm": inside}, baseline), [])
        zero = validation.feature_deviations({"rpm": 10000}, rpm_baseline((1450, 1450)))
        self.assertIsNone(zero[0]["deviation_sigma"])
        json.dumps(zero, allow_nan=False)

    def test_zero_variance_feature_does_not_hide_other_features_recovery_band(self):
        baseline = rpm_baseline((1460, 1460))
        baseline["features"]["variable"] = {
            "mean": 0,
            "std": 1,
            "normal_range": [-3, 3],
        }
        result = rules.evaluate_feature_stream(
            [
                {"rpm": rpm, "variable": value}
                for rpm, value in [
                    (2000, 4),
                    (2000, 4),
                    (1460, 2.5),
                    (1460, 2.5),
                    (1460, 0),
                    (1460, 0),
                ]
            ],
            baseline,
        )
        self.assertEqual(result["events"][0]["end_index"], 4)
        self.assertIsNone(result["events"][0]["max_deviation_sigma"])
        json.dumps(result, allow_nan=False)

    def test_generated_acoustic_baseline_through_registry_and_event_evaluator(self):
        import test_acoustic_pipeline as fixtures

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures.recordings(root)
            manifest = fixtures.acoustic.build_acoustic_manifest(
                root,
                source_uri="fixture://pump",
                license_note="generated fixture",
                operating_conditions={"load": "rated"},
                window_size=256,
                hop_size=256,
                n_mfcc=0,
            )
            baseline = signals.build_acoustic_baseline(
                fixtures.freeze_dataset_version(manifest),
                site_id="S1",
                asset_id="A1",
                sigma=1,
            )
        normal = {key: stats["mean"] for key, stats in baseline["features"].items()}
        abnormal = dict(normal)
        name = next(iter(abnormal))
        stats = baseline["features"][name]
        abnormal[name] = stats["mean"] + max(1, 2 * stats["std"])
        for record in lifecycle(baseline):
            before = copy.deepcopy(record)
            result = rules.evaluate_feature_stream(
                [normal, abnormal, abnormal, normal, normal], record
            )
            self.assertEqual(
                [w["state"] for w in result["window_states"]],
                ["NORMAL", "NORMAL", "ANOMALY", "ANOMALY", "NORMAL"],
            )
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["end_index"], 3)
            self.assertEqual(record, before)
            json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
