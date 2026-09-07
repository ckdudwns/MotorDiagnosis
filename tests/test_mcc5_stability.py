"""Grouped ablation contracts; synthetic tests are not field validation."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
    from ai.ai1.mcc5_training import stability as s
except ImportError:
    s = None


@unittest.skipIf(s is None, "Training extras unavailable")
class StabilityTest(unittest.TestCase):
    def base(self):
        return np.tile([2.0, 6.0, 3.0, 1.0, 2.0, 3.0, 4.0], (3, 1)).reshape(1, 21)

    def test_ratio_order_and_scale_invariance(self):
        x = self.base()
        derived = s.transform(x, "ratios36")
        self.assertEqual(derived.shape, (1, 36))
        np.testing.assert_allclose(derived[0, 21:26], [0.1, 0.2, 0.3, 0.4, 3.0])
        scaled = x.reshape(-1, 3, 7).copy()
        scaled[:, :, :2] *= 2
        scaled[:, :, 3:] *= 4
        np.testing.assert_allclose(
            s.transform(scaled.reshape(1, 21), "ratios36")[:, 21:], derived[:, 21:]
        )
        self.assertEqual(len(s.contract("log_ratios36")["featureNames"]), 36)

    def test_log_is_fixed_and_does_not_mutate(self):
        x = self.base()
        before = x.copy()
        result = s.transform(x, "log_ratios36")
        self.assertAlmostEqual(result[0, 0], np.log1p(200))
        self.assertEqual(result[0, 2], 3.0)
        np.testing.assert_array_equal(x, before)

    def test_invalid_features_rejected(self):
        for matrix in (
            np.zeros((1, 21)),
            self.base() * np.nan,
            -self.base(),
            np.ones((1, 20)),
        ):
            with self.assertRaises(ValueError):
                s.transform(matrix, "ratios36")

    def test_grouped_folds_disjoint_and_complete(self):
        groups = np.repeat(["a", "b", "c", "d"], 2)
        labels = np.tile([False, True], 4)
        seen = np.zeros(8)
        for held, cal, fit, calibrate, evaluate in s.grouped_folds(groups, labels):
            self.assertFalse(
                np.any(fit & calibrate | fit & evaluate | calibrate & evaluate)
            )
            self.assertEqual(set(groups[evaluate]), {held})
            self.assertEqual(set(groups[calibrate]), {cal})
            seen += evaluate
        np.testing.assert_array_equal(seen, np.ones(8))
        with self.assertRaises(ValueError):
            s.grouped_folds(groups[:6], labels[:6])

    def test_tie_prefers_base(self):
        values = {v: {"meanBalancedAccuracy": 0.8, "worstFpr": 0.1} for v in s.VARIANTS}
        self.assertEqual(s.choose(values), "base21")

    def test_run_never_scores_test(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Test features are NaN sentinels. Mocked load bypasses normal cache
            # integrity validation: any accidental test transform/scoring fails.
            x, labels, runs, splits, rows = [], [], [], [], []
            for group, split in [
                ("a", "train"),
                ("b", "train"),
                ("c", "train"),
                ("d", "train"),
                ("v", "validation"),
                ("t", "test"),
            ]:
                for label in (False, True):
                    i = len(rows)
                    rows.append(
                        {
                            "condition": group,
                            "member": f"{i}.csv",
                            "label": "bend" if label else "health",
                        }
                    )
                    for j in range(6):
                        x.append(
                            np.full(21, np.nan)
                            if split == "test"
                            else self.base()[0] * (1 + label + j * 0.01)
                        )
                        labels.append(label)
                        runs.append(i)
                        splits.append(split)
            clock = np.arange(512) / 800
            wave = np.column_stack([np.sin(2 * np.pi * 25 * clock)] * 3)
            (root / "golden-window.json").write_text(
                json.dumps(
                    {
                        "rawWindowG": wave.tolist(),
                        "expectedFeatures": s.extract(wave).tolist(),
                    }
                )
            )
            (root / "manifest.json").write_text("{}")
            data = (
                {"runs": rows},
                np.array(x),
                np.array(labels),
                np.array(runs),
                np.array(splits),
            )
            model = s.candidate_models()["random_forest"].set_params(n_estimators=3)
            from sklearn.base import clone

            with (
                patch.object(s, "load_prepared", return_value=data),
                patch.object(
                    s,
                    "candidate_models",
                    side_effect=lambda: {"random_forest": clone(model)},
                ),
            ):
                report = s.run(root, root / "result")
            self.assertFalse(report["testScored"])
            self.assertFalse(report["activated"])
            self.assertEqual(len(report["results"]["base21"]["folds"]), 4)
            with self.assertRaises(FileExistsError):
                s.run(root, root / "result")
