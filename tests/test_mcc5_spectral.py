"""Spectral contracts and leakage guards, not physical board validation."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
    from ai.ai1.mcc5_training import spectral as s
except ImportError:
    s = None


@unittest.skipIf(s is None, "Training extras unavailable")
class SpectralTest(unittest.TestCase):
    def wave(self):
        time = np.arange(512) / 800
        a = np.sin(2 * np.pi * 25 * time)
        return np.column_stack([a, 2 * a, -a])

    def test_shape_order_and_base_exact(self):
        wave = self.wave()
        result = s.extract66(wave)
        self.assertEqual(result.shape, (66,))
        self.assertEqual(len(set(s.CONTRACT["featureNames"])), 66)
        np.testing.assert_array_equal(result[:21], s.extract(wave))
        np.testing.assert_allclose(result[-3:], [1, -1, -1], atol=1e-14)
        extra = result[21:63].reshape(3, 14)
        np.testing.assert_allclose(extra[:, :8].sum(axis=1), 1)
        np.testing.assert_allclose(extra[:, 9], 25, atol=1e-8)
        np.testing.assert_allclose(extra[:, 11], 25)
        self.assertTrue(np.all((extra[:, 8] >= 0) & (extra[:, 8] <= 1)))

    def test_scale_dc_invariance_and_no_mutation(self):
        wave = self.wave()
        before = wave.copy()
        a = s.extract66(wave)
        b = s.extract66(wave * 2 + np.array([0.3, -0.2, 0.4]))
        np.testing.assert_allclose(a[21:], b[21:], atol=1e-10)
        np.testing.assert_array_equal(wave, before)
        self.assertAlmostEqual(b[0], a[0] * 2)
        self.assertAlmostEqual(b[3], a[3] * 4)

    def test_invalid_windows_rejected(self):
        for wave in [
            np.zeros((512, 3)),
            self.wave() * np.nan,
            self.wave() * 16,
            np.ones((511, 3)),
        ]:
            with self.assertRaises(ValueError):
                s.extract66(wave)

    def test_selection_tie_prefers_fewer_features(self):
        rows = {
            name: {"meanBalancedAccuracy": 0.8, "worstFpr": 0.1}
            for name in s.CANDIDATES
        }
        self.assertEqual(s.CANDIDATES[s.select(rows)][1], 21)

    def test_compare_disallows_test_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            data = (
                {},
                np.ones((1, 66)),
                np.array([False]),
                np.array([0]),
                np.array(["test"]),
            )
            with patch.object(s, "load_spectral", return_value=data):
                with self.assertRaisesRegex(ValueError, "Test rows"):
                    s.run(folder, folder, Path(folder) / "result")

    def test_compare_roundtrip_and_no_overwrite(self):
        from sklearn.base import clone

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rows, x, labels, ids, splits = [], [], [], [], []
            for group, split in [
                ("a", "train"),
                ("b", "train"),
                ("c", "train"),
                ("d", "train"),
                ("v", "validation"),
            ]:
                for label in (False, True):
                    i = len(rows)
                    rows.append(
                        {
                            "condition": group,
                            "member": str(i),
                            "label": "bend" if label else "health",
                        }
                    )
                    for j in range(6):
                        x.append(s.extract66(self.wave() * (1 + label + j * 0.01)))
                        labels.append(label)
                        ids.append(i)
                        splits.append(split)
            data = (
                {"runs": rows},
                np.array(x),
                np.array(labels),
                np.array(ids),
                np.array(splits),
            )
            (root / "manifest.json").write_text("{}")
            (root / "golden-window.json").write_text(
                json.dumps(
                    {
                        "rawWindowG": self.wave().tolist(),
                        "expectedFeatures": s.extract(self.wave()).tolist(),
                    }
                )
            )
            tiny = s.candidate_models()["random_forest"].set_params(n_estimators=3)
            with (
                patch.object(s, "load_spectral", return_value=data),
                patch.object(
                    s,
                    "candidate_models",
                    side_effect=lambda: {"random_forest": clone(tiny)},
                ),
                patch.object(
                    s,
                    "CANDIDATES",
                    {"rf21": ("random_forest", 21), "rf66": ("random_forest", 66)},
                ),
            ):
                result = s.run(root, root, root / "result")
                self.assertFalse(result["testScored"])
                self.assertFalse(result["activated"])
                self.assertEqual(len(result["results"]["rf66"]["folds"]), 4)
                with self.assertRaises(FileExistsError):
                    s.run(root, root, root / "result")

    def test_cache_rejects_test_or_reordered_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            parent, prepared = root / "parent", root / "spectral"
            parent.mkdir()
            prepared.mkdir()
            (parent / "manifest.json").write_text("{}")
            x = np.tile(s.extract66(self.wave()), (3, 1))
            data = (
                {},
                x[:, :21],
                np.array([False, True, True]),
                np.array([0, 1, 2]),
                np.array(["train", "validation", "test"]),
            )
            for index in ([0, 2], [1, 0]):
                np.savez_compressed(prepared / "features.npz", x=x[:2], parentRow=index)
                (prepared / "manifest.json").write_text(
                    json.dumps(
                        {
                            "contract": s.CONTRACT,
                            "parentManifestSha256": s.sha256(parent / "manifest.json"),
                            "cacheSha256": s.sha256(prepared / "features.npz"),
                            "rows": 2,
                        }
                    )
                )
                with patch.object(s, "load_prepared", return_value=data):
                    with self.assertRaisesRegex(ValueError, "cache rows"):
                        s.load_spectral(parent, prepared)
