"""Input agreement and resolution stress tests; fixtures are synthetic."""

import unittest
from unittest.mock import patch
import json
from pathlib import Path
import tempfile

try:
    import numpy as np
    from ai.ai1.mcc5_training import raw_input as r
    from ai.ai1.mcc5_training import sensor_robustness as s
    from ai.ai1.mcc5_training.spectral import extract66
except ImportError:
    r = s = None


@unittest.skipIf(r is None, "ML environment unavailable")
class RawInputTest(unittest.TestCase):
    def window(self):
        time = np.arange(512) / 800
        return np.column_stack(
            [
                0.12 * np.sin(2 * np.pi * f * time) + offset
                for f, offset in ((25, 0), (75, 0.2), (125, 1.0))
            ]
        )

    def call(self, raw, **changes):
        arguments = dict(
            profile_id=r.SPECTRAL_PROFILE,
            sample_rate_hz=800,
            sample_count=512,
            axes=["X", "Y", "Z"],
            unit="g",
            quality="valid",
        )
        arguments.update(changes)
        return r.model_input(raw, **arguments)

    def test_exact_numeric_policy_and_single_window_shape(self):
        raw = self.window()
        before = raw.copy()
        result = self.call(raw)
        expected = extract66(raw)
        expected[:21] = expected[:21].astype(np.float32).astype(np.float64)
        np.testing.assert_array_equal(result["values"], expected)
        np.testing.assert_array_equal(raw, before)
        self.assertEqual(result["inputShape"], [1, 66])
        self.assertEqual(result["sequenceLength"], 1)
        self.assertFalse(result["affectsAlerts"])
        base = self.call(raw, profile_id=r.BASE_PROFILE)
        np.testing.assert_array_equal(base["values"], expected[:21])
        self.assertEqual(len(result["featureNames"]), 66)

    def test_wrong_contract_never_resamples_or_zero_pads(self):
        for change in (
            {"sample_rate_hz": 12800},
            {"unit": "mm/s"},
            {"axes": ["Z", "Y", "X"]},
            {"profile_id": "unknown"},
            {"sample_count": 511},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call(self.window(), **change)
        for raw in (
            self.window()[:511],
            self.window().T,
            [1] * 21,
            np.zeros((5, 512, 3)),
        ):
            with self.assertRaises(ValueError):
                self.call(raw)

    def test_invalid_quality_constant_clipped_and_nonfinite_are_not_normal(self):
        for quality in (
            "fifo_overrun",
            "sample_gap",
            "sensor_unavailable",
            "processing_overflow",
        ):
            with self.assertRaises(r.InputUnavailable):
                self.call(None, quality=quality)
        for raw in (np.ones((512, 3)), np.full((512, 3), 16.0)):
            with self.assertRaises(r.InputUnavailable):
                self.call(raw)
        raw = self.window()
        raw[0, 0] = np.nan
        with self.assertRaises(ValueError):
            self.call(raw)
        for value in (True, "0.1"):
            raw = self.window().tolist()
            raw[0][0] = value
            with self.assertRaises(ValueError):
                self.call(raw)

    def test_count_rails_match_cpp_and_are_checked_before_mean_removal(self):
        counts = np.rint(self.window() / 0.0039).astype(int)
        for rail in (-4096, 4095):
            changed = counts.copy()
            changed[0, 0] = rail
            with self.assertRaisesRegex(r.InputUnavailable, "clipped"):
                r.counts_to_g(changed)
        for safe in (-4095, 4094):
            changed = counts.copy()
            changed[0, 0] = safe
            self.assertAlmostEqual(r.counts_to_g(changed)[0, 0], safe * 0.0039)
        with self.assertRaises(ValueError):
            r.counts_to_g(counts.astype(float))

    def test_quantization_is_bounded_resolution_only_and_never_clamps(self):
        raw = self.window()
        quantized = r.quantize_public_window(raw)
        self.assertLessEqual(np.max(np.abs(quantized - raw)), 0.0039 / 2 + 1e-15)
        np.testing.assert_allclose(
            quantized / 0.0039, np.rint(quantized / 0.0039), atol=1e-12
        )
        with self.assertRaises(r.InputUnavailable):
            r.quantize_public_window(np.full((512, 3), 16.0))
        with self.assertRaises(r.InputUnavailable):
            r.spectral_input(r.quantize_public_window(np.full((512, 3), 0.0001)))

    def test_augmentation_retains_sample_count_and_does_not_mutate_inputs(self):
        clean, quant = np.zeros((6, 66)), np.ones((6, 66))
        augmented = s.augment_fit(clean, quant, np.arange(6))
        self.assertEqual(augmented.shape, clean.shape)
        np.testing.assert_array_equal(augmented[::2], quant[::2])
        np.testing.assert_array_equal(augmented[1::2], clean[1::2])
        self.assertTrue(np.all(clean == 0))

    def test_model_promotion_requires_both_domains_and_strict_stress_improvement(self):
        def row(recall, fpr, worst):
            return {
                "three_consecutive": {
                    "meanRecall": recall,
                    "meanFpr": fpr,
                    "worstFpr": worst,
                }
            }

        results = {
            "clean_fit": {
                "clean": row(0.8, 0.01, 0.06),
                "quantized": row(0.79, 0.02, 0.09),
            },
            "half_quantized_fit": {
                "clean": row(0.79, 0.01, 0.06),
                "quantized": row(0.80, 0.01, 0.06),
            },
        }
        self.assertTrue(s.eligible(results))
        results["half_quantized_fit"]["clean"] = row(0.6, 0.01, 0.06)
        self.assertFalse(s.eligible(results))

    def test_complete_fit_experiment_and_corrupt_cache_rejection(self):
        from sklearn.ensemble import RandomForestClassifier

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent, cache = root / "parent", root / "cache"
            parent.mkdir()
            cache.mkdir()
            labels = np.tile(np.repeat([False, True], 8), 4)
            runs = np.repeat(np.arange(8), 8)
            starts = np.tile(np.arange(8), 8)
            clean = np.repeat((labels.astype(float) + 0.1)[:, None], 66, axis=1)
            np.savez(
                parent / "sequences.npz",
                y=labels,
                run=runs,
                start=starts,
                split=np.full(64, "train"),
            )
            rows = [
                {
                    "condition": str(i // 2),
                    "split": "train",
                    "label": "health" if i % 2 == 0 else "fault",
                }
                for i in range(8)
            ]
            s.dump(
                parent / "manifest.json",
                {"runs": rows, "cacheSha256": s.sha256(parent / "sequences.npz")},
            )
            np.savez(
                cache / "features.npz",
                clean=clean,
                quantized=clean + 0.001,
                labels=labels,
                runs=runs,
                starts=starts,
                reasons=np.full(64, ""),
            )
            s.dump(
                cache / "manifest.json",
                {
                    "parentManifestSha256": s.sha256(parent / "manifest.json"),
                    "cacheSha256": s.sha256(cache / "features.npz"),
                    "numericPolicy": r.NUMERIC_POLICY,
                },
            )
            with patch.object(
                s,
                "candidate_models",
                side_effect=lambda: {
                    "random_forest": RandomForestClassifier(
                        n_estimators=4, random_state=42, n_jobs=1
                    )
                },
            ):
                report = s.run(parent, cache, root / "result")
            self.assertIsNone(report["selected"])
            self.assertFalse(report["activated"])
            self.assertFalse(report["artifactExported"])
            self.assertFalse(report["protocol"]["testScored"])
            self.assertEqual(len(report["protocol"]["folds"]), 4)
            self.assertEqual(report["protocol"]["inputShape"], [1, 66])
            with self.assertRaises(FileExistsError):
                s.run(parent, cache, root / "result")
            meta = json.loads((cache / "manifest.json").read_text())
            meta["cacheSha256"] = "wrong"
            (cache / "manifest.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "provenance"):
                s.run(parent, cache, root / "wrong")

    def test_export_never_overwrites_an_existing_candidate(self):
        from ai.ai1.mcc5_training.export_resolution_candidate import export

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(FileExistsError):
                export(root, root, root, root, root)

    def test_export_refuses_failed_selection_before_training(self):
        from ai.ai1.mcc5_training import export_resolution_candidate as e

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            s.dump(root / "report.json", {"selected": None})
            s.dump(root / "manifest.json", {})
            with patch.object(e, "load_spectral") as load:
                with self.assertRaisesRegex(ValueError, "gate or provenance"):
                    e.export(root, root, root, root, root / "candidate")
                load.assert_not_called()
            self.assertFalse((root / "candidate").exists())
