"""Regression tests; synthetic scores are not public/field accuracy."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from ai.ai1.mcc5_training import compare as c
    import numpy as np
    import test_mcc5_training as fixtures
except ImportError:
    c = None


@unittest.skipIf(c is None, "Training extras unavailable")
class ComparisonTest(unittest.TestCase):
    def test_weight_classes_and_runs(self):
        y = np.array([0, 0, 0, 1, 1, 1, 1])
        runs = np.array([0, 0, 1, 2, 3, 3, 3])
        w = c.balanced_run_weights(y, runs)
        self.assertAlmostEqual(w[y == 0].sum(), w[y == 1].sum())
        self.assertAlmostEqual(w[runs == 0].sum(), w[runs == 1].sum())
        self.assertAlmostEqual(w[runs == 2].sum(), w[runs == 3].sum())
        with self.assertRaises(ValueError):
            c.balanced_run_weights([0, 1], [0, 0])

    def test_threshold_uses_only_normal_and_strict_ties(self):
        self.assertEqual(c.normal_threshold([0, 0, 1], [0.1, 0.2, 0.99]), 0.2)
        self.assertEqual(c.normal_threshold([0, 0, 1], [0.1, 0.2, 0.01]), 0.2)
        t = c.normal_threshold([0, 0, 1], [1, 1, 1])
        self.assertEqual(c.metrics([0, 0, 1], [1, 1, 1], t)["fp"], 0)
        with self.assertRaises(ValueError):
            c.normal_threshold([0, 1], [0.1, float("nan")])

    def test_selection_is_validation_only(self):
        metric = dict(balancedAccuracy=0.8, recall=0.7, falsePositiveRate=0.1)
        self.assertEqual(c.select_candidate({"b": metric, "a": metric}), "a")
        self.assertFalse(c.candidate_models()["hist_gradient_boosting"].early_stopping)

    def test_end_to_end_replay_and_frozen_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            prepared = Path(temp) / "prepared"
            prepared.mkdir()
            fixtures.Mcc5TrainingTest().fixture(prepared)
            models = c.candidate_models()
            models["hist_gradient_boosting"].set_params(max_iter=2, min_samples_leaf=2)
            models["random_forest"].set_params(n_estimators=5)
            output = Path(temp) / "result"
            with patch.object(c, "candidate_models", return_value=models):
                report = c.compare(prepared, output)
            selection = json.loads((output / "selection.json").read_text())
            self.assertEqual(selection["selected"], report["selected"])
            self.assertFalse(report["testUsedForSelection"])
            self.assertTrue(report["testPreviouslyInspected"])
            self.assertFalse(report["deployment"]["activated"])
            for name, result in report["candidates"].items():
                self.assertEqual(
                    result["sha256"], c.sha256(output / result["artifact"])
                )
                self.assertEqual(result["testDescriptive"]["overall"]["count"], 16)
                self.assertTrue((output / (name + "-golden.json")).is_file())
            with self.assertRaises(FileExistsError):
                c.compare(prepared, output)


if __name__ == "__main__":
    unittest.main()
