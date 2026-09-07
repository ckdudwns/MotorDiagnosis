"""Normal error cost, protected recall and matched control contracts."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
    from ai.ai1.mcc5_training import calibration as calibration
    from ai.ai1.mcc5_training import cost_sensitive as cost
except ImportError:
    cost = None


@unittest.skipIf(cost is None, "Training extras unavailable")
class CostTest(unittest.TestCase):
    def test_cost_only_increases_normal_total(self):
        labels = np.array([False, False, True, True, True])
        runs = np.array([0, 0, 1, 2, 2])
        baseline = cost.weights(labels, runs, 1)
        weighted = cost.weights(labels, runs, 4)
        np.testing.assert_allclose(weighted[labels], baseline[labels])
        np.testing.assert_allclose(weighted[~labels], 4 * baseline[~labels])
        self.assertAlmostEqual(weighted[~labels].sum() / weighted[labels].sum(), 4)

    def test_invalid_cost_or_labels(self):
        for labels, c in [([0, 1], 3), ([0, 2], 2), ([0, 0], 2)]:
            with self.assertRaises(ValueError):
                cost.weights(labels, [0, 1], c)

    def test_recall_floor_not_relaxed_for_lower_fpr(self):
        rows = {"a": {"meanFpr": 0, "worstFpr": 0, "meanRecall": 0.70}}
        self.assertIsNone(cost.choose(rows, 0.7755))
        rows["a"]["meanRecall"] = 0.80
        self.assertEqual(cost.choose(rows, 0.7755), "a")

    def test_end_to_end_control_reproduction_no_validation_scoring(self):
        from sklearn.base import clone

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "manifest.json").write_text("{}")
            rows, x, y, runs, splits = [], [], [], [], []
            for group in "abcdefv":
                for label in (False, True):
                    i = len(rows)
                    rows.append(
                        {
                            "condition": group,
                            "member": str(i),
                            "label": "bend" if label else "health",
                        }
                    )
                    for j in range(4):
                        x.append(
                            np.full(
                                66, np.nan if group == "v" else 1 + label + 0.01 * j
                            )
                        )
                        y.append(label)
                        runs.append(i)
                        splits.append("validation" if group == "v" else "train")
            data = (
                {"runs": rows},
                np.array(x),
                np.array(y),
                np.array(runs),
                np.array(splits),
            )
            tiny = cost.candidate_models()["random_forest"].set_params(n_estimators=3)
            factory = lambda: {"random_forest": clone(tiny)}
            with (
                patch.object(calibration, "load_spectral", return_value=data),
                patch.object(cost, "load_spectral", return_value=data),
                patch.object(calibration, "candidate_models", side_effect=factory),
                patch.object(cost, "candidate_models", side_effect=factory),
            ):
                reference = calibration.run(root, root, root / "baseline")
                result = cost.run(
                    root, root, root / "baseline/report.json", root / "result"
                )
                self.assertTrue(result["controlReproduced"])
                self.assertFalse(result["validationScored"])
                self.assertFalse(result["testScored"])
                self.assertFalse(result["activated"])
                self.assertEqual(
                    result["protocol"]["researchGate"]["minimumMeanRecall"],
                    reference["results"]["single_q99"]["meanRecall"] - 0.05,
                )
                with self.assertRaises(FileExistsError):
                    cost.run(root, root, root / "baseline/report.json", root / "result")
