"""Matched calibration tests; not field false-positive certification."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
    from ai.ai1.mcc5_training import calibration as c
except ImportError:
    c = None


@unittest.skipIf(c is None, "Training extras unavailable")
class CalibrationTest(unittest.TestCase):
    def test_disjoint_complete_partitions(self):
        groups = np.repeat(list("abcdefgh"), 2)
        y = np.tile([False, True], 8)
        seen = np.zeros(len(y))
        for held, conditions, fit, cal, evaluate in c.folds(groups, y):
            self.assertFalse(np.any(fit & cal | fit & evaluate | cal & evaluate))
            self.assertTrue(np.all(fit | cal | evaluate))
            self.assertEqual(len(set(groups[fit])), 5)
            self.assertEqual(set(groups[cal]), set(conditions))
            self.assertEqual(set(groups[evaluate]), {held})
            seen += evaluate
        np.testing.assert_array_equal(seen, np.ones(len(y)))

    def test_invalid_partitions(self):
        for groups, labels in [
            (np.array(list("abcde")), np.zeros(5)),
            (np.array(list("abcdef")), np.zeros(6)),
            (np.array(list("abcdef")), np.zeros(5)),
        ]:
            with self.assertRaises(ValueError):
                list(c.folds(groups, labels))

    def test_threshold_formulas_and_monotonic_max(self):
        values = np.r_[np.linspace(0.01, 0.3, 140), np.linspace(0.4, 0.8, 140)]
        groups = np.repeat(["a", "b"], 140)
        before = values.copy()
        result = c.thresholds(values, groups, ("a", "b"))
        self.assertEqual(
            result["single_q99"], np.quantile(values[:140], 0.99, method="higher")
        )
        self.assertEqual(
            result["pooled_q99"], np.quantile(values, 0.99, method="higher")
        )
        self.assertEqual(
            result["max_condition_q99"],
            np.quantile(values[140:], 0.99, method="higher"),
        )
        self.assertGreaterEqual(result["max_condition_q99"], result["single_q99"])
        np.testing.assert_array_equal(values, before)

    def test_missing_invalid_normal_groups_rejected(self):
        for values, groups, order in [
            ([0.1], ["a"], ("a", "b")),
            ([0.1, np.nan], ["a", "b"], ("a", "b")),
            ([0.1, 1.1], ["a", "b"], ("a", "b")),
            ([0.1, 0.2], ["a", "b"], ("a", "a")),
            ([0.1], ["a", "b"], ("a", "b")),
        ]:
            with self.assertRaises(ValueError):
                c.thresholds(values, groups, order)

    def test_gate_rejects_recall_collapse_and_bad_worst_condition(self):
        rows = {
            p: {"meanRecall": 0.8, "meanFpr": 0.02, "worstFpr": 0.08}
            for p in c.POLICIES
        }
        rows["pooled_q99"]["meanRecall"] = 0
        rows["max_condition_q99"]["worstFpr"] = 0.2
        self.assertIsNone(c.choose(rows))
        rows["max_condition_q99"]["worstFpr"] = 0.08
        rows["max_condition_q99"]["meanRecall"] = 0.77
        self.assertEqual(c.choose(rows), "max_condition_q99")

    def test_no_validation_scoring_and_no_overwrite(self):
        from sklearn.base import clone

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "manifest.json").write_text("{}")
            rows, x, y, runs, split = [], [], [], [], []
            for g in list("abcdefv"):
                for label in (False, True):
                    identifier = len(rows)
                    rows.append(
                        {
                            "member": str(identifier),
                            "condition": g,
                            "label": "bend" if label else "health",
                        }
                    )
                    for i in range(4):
                        x.append(
                            np.full(66, np.nan if g == "v" else 1 + label + i * 0.01)
                        )
                        y.append(label)
                        runs.append(identifier)
                        split.append("validation" if g == "v" else "train")
            data = (
                {"runs": rows},
                np.array(x),
                np.array(y),
                np.array(runs),
                np.array(split),
            )
            model = c.candidate_models()["random_forest"].set_params(n_estimators=3)
            with (
                patch.object(c, "load_spectral", return_value=data),
                patch.object(
                    c,
                    "candidate_models",
                    side_effect=lambda: {"random_forest": clone(model)},
                ),
            ):
                result = c.run(root, root, root / "result")
                self.assertFalse(result["validationScored"])
                self.assertFalse(result["testScored"])
                self.assertFalse(result["activated"])
                self.assertEqual(len(result["results"]["single_q99"]["folds"]), 6)
                with self.assertRaises(FileExistsError):
                    c.run(root, root, root / "result")

    def test_test_rows_forbidden(self):
        with tempfile.TemporaryDirectory() as folder:
            data = (
                {},
                np.zeros((1, 66)),
                np.array([True]),
                np.array([0]),
                np.array(["test"]),
            )
            with patch.object(c, "load_spectral", return_value=data):
                with self.assertRaisesRegex(ValueError, "Test rows"):
                    c.run(folder, folder, Path(folder) / "result")
