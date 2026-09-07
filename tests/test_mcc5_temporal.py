"""Synthetic tests of offline causal confirmation, not measured accuracy."""

import json
from pathlib import Path
import tempfile
import unittest

try:
    import numpy as np
    from ai.ai1.mcc5_training import temporal as t
except ImportError:
    np = t = None


@unittest.skipIf(t is None, "NumPy unavailable")
class TemporalTest(unittest.TestCase):
    def test_isolated_spike_suppressed_without_future_samples(self):
        hits = np.array([0, 0, 1, 0, 0, 1, 1, 1], dtype=bool)
        runs, starts = np.zeros(8, dtype=int), np.arange(8)
        full = t.decisions(hits, runs, starts, 3, 2)
        self.assertEqual(full.tolist(), [-1, -1, 0, 0, 0, 0, 1, 1])
        for length in range(1, 9):
            np.testing.assert_array_equal(
                t.decisions(hits[:length], runs[:length], starts[:length], 3, 2),
                full[:length],
            )

    def test_gap_run_boundary_and_invalid_measurement_reset_pending(self):
        hits = np.ones(8, dtype=bool)
        self.assertEqual(
            t.decisions(
                hits, np.zeros(8, dtype=int), np.array([0, 1, 2, 4, 5, 6, 7, 8]), 3, 3
            ).tolist(),
            [-1, -1, 1, -1, -1, 1, 1, 1],
        )
        self.assertEqual(
            t.decisions(
                hits,
                np.array([0, 0, 0, 1, 1, 1, 1, 1]),
                np.array([0, 1, 2, 0, 1, 2, 3, 4]),
                3,
                3,
            ).tolist(),
            [-1, -1, 1, -1, -1, 1, 1, 1],
        )
        valid = np.array([1, 1, 1, 0, 1, 1, 1, 1], dtype=bool)
        self.assertEqual(
            t.decisions(
                hits, np.zeros(8, dtype=int), np.arange(8), 3, 3, valid
            ).tolist(),
            [-1, -1, 1, -1, -1, -1, 1, 1],
        )

    def test_invalid_or_duplicate_indices_and_non_boolean_hits_rejected(self):
        for indices in ([0, 1, 1], [0, 2, 1], [0, -1, 2]):
            with self.assertRaises(ValueError):
                t.decisions(
                    np.ones(3, dtype=bool),
                    np.zeros(3, dtype=int),
                    np.array(indices),
                    3,
                    2,
                )
        with self.assertRaises(ValueError):
            t.decisions(np.array([1.0, float("nan")]), np.zeros(2), np.arange(2), 3, 2)

    def test_pending_is_not_counted_as_normal_or_hidden_from_recall(self):
        result = t.metrics(
            np.array([True, True, False, False]), np.array([-1, 1, -1, 0])
        )
        self.assertEqual(result["detectedRecall"], 0.5)
        self.assertEqual(result["coverage"], 0.5)
        self.assertEqual(result["pendingAnomalyWindows"], 1)
        self.assertEqual(result["pendingNormalWindows"], 1)

    def fixture(self, root):
        parent, source = root / "parent", root / "source"
        parent.mkdir()
        source.mkdir()
        rows, labels, runs, scores = [], [], [], []
        for condition in ("a", "b", "c"):
            for fault in (False, True):
                run = len(rows)
                rows.append(
                    {
                        "condition": condition,
                        "split": "train",
                        "label": "fault" if fault else "health",
                    }
                )
                labels.extend([fault] * 10)
                runs.extend([run] * 10)
                scores.extend([0.9 if fault else 0.1] * 10)
        labels, runs = np.array(labels), np.array(runs)
        np.savez(
            parent / "sequences.npz",
            y=labels,
            run=runs,
            start=np.tile(np.arange(10), 6),
            split=np.full(60, "train"),
        )
        t.dump(
            parent / "manifest.json",
            {"runs": rows, "cacheSha256": t.digest(parent / "sequences.npz")},
        )
        folds = [
            {"evaluation": e, "calibration": c, "fit": [f]}
            for e, c, f in (("a", "b", "c"), ("b", "c", "a"), ("c", "a", "b"))
        ]
        t.dump(
            source / "protocol.json",
            {
                "folds": folds,
                "parentManifestSha256": t.digest(parent / "manifest.json"),
                "testScored": False,
                "validationUsedForSelection": False,
            },
        )
        t.dump(
            source / "report.json",
            {
                "results": {
                    "rf66": {"folds": [{**fold, "threshold": 0.5} for fold in folds]}
                }
            },
        )
        np.savez(
            source / "rf66-oof.npz",
            runs=runs,
            labels=labels,
            scores=scores,
            thresholds=np.full(60, 0.5),
        )
        return parent, source, t.digest(source / "rf66-oof.npz")

    def test_complete_experiment_equal_comparison_population_no_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent, source, checksum = self.fixture(root)
            result = t.run(parent, source, root / "out", checksum)
            self.assertEqual(result["totalWindows"], 60)
            self.assertEqual(result["commonReadyWindows"], 36)
            self.assertFalse(result["activated"])
            self.assertFalse(result["fieldValidated"])
            self.assertTrue(result["protocol"]["testScored"] is False)
            self.assertEqual(
                result["results"]["five_consecutive"]["allWindows"]["pendingWindows"],
                24,
            )
            for value in result["results"].values():
                self.assertEqual(value["meanRecall"], 1.0)
            with self.assertRaises(FileExistsError):
                t.run(parent, source, root / "out", checksum)

    def test_checksum_leaked_folds_and_changed_thresholds_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent, source, checksum = self.fixture(root)
            with self.assertRaisesRegex(ValueError, "checksum"):
                t.load(parent, source, "0" * 64)
            path = source / "protocol.json"
            protocol = json.loads(path.read_text())
            protocol["folds"][0]["fit"] = ["a"]
            path.write_text(json.dumps(protocol))
            with self.assertRaisesRegex(ValueError, "Leaked"):
                t.load(parent, source, checksum)
            protocol["folds"][0]["fit"] = ["c"]
            path.write_text(json.dumps(protocol))
            path = source / "report.json"
            report = json.loads(path.read_text())
            report["results"]["rf66"]["folds"][0]["threshold"] = 0.6
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "thresholds"):
                t.load(parent, source, checksum)

    def test_gate_does_not_trade_unbounded_recall_for_lower_fpr(self):
        rows = {
            "single": {"meanRecall": 0.82, "meanFpr": 0.05, "worstFpr": 0.20},
            "five_consecutive": {"meanRecall": 0.70, "meanFpr": 0.01, "worstFpr": 0.03},
        }
        self.assertIsNone(t.choose(rows))
        rows["five_consecutive"]["meanRecall"] = 0.79
        self.assertEqual(t.choose(rows), "five_consecutive")
