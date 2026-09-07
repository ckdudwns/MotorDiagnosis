"""Offline contracts only; synthetic tests do not establish field accuracy."""

import io
import json
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    from ai.ai1.mcc5_training import features, prepare
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy/SciPy training extras unavailable")
class Mcc5ContractTest(unittest.TestCase):
    def window(self, frequency=25):
        signal = np.sin(2 * np.pi * frequency * np.arange(512) / 800)
        return np.column_stack([signal, signal * 2, signal * 3])

    def test_feature_count_and_dc_invariance(self):
        values = features.extract(self.window())
        self.assertEqual(len(values), 21)
        np.testing.assert_allclose(
            values, features.extract(self.window() + [1, 2, 3]), atol=1e-12
        )
        self.assertAlmostEqual(values[0], 1 / np.sqrt(2))
        self.assertAlmostEqual(values[3], 0.5)
        self.assertAlmostEqual(values[2], 1.5)

    def test_band_and_axis_order(self):
        values = features.extract(self.window(150))
        self.assertAlmostEqual(values[5], 0.5)
        self.assertAlmostEqual(values[12], 2)
        self.assertAlmostEqual(values[19], 4.5)

    def test_invalid_windows_fail_closed(self):
        for value in (
            np.zeros((512, 3)),
            np.ones((511, 3)),
            self.window() * 16,
            self.window() * np.nan,
        ):
            with self.subTest(shape=value.shape), self.assertRaises(ValueError):
                features.extract(value)

    def test_downsampling_filters_high_frequencies(self):
        clock = np.arange(12800 * 2) / 12800
        low = np.sin(2 * np.pi * 25 * clock)
        high = np.sin(2 * np.pi * 1000 * clock)
        result = features.to_800hz(np.column_stack([low, low, low]) * 0.1)
        self.assertEqual(result.shape, (1600 - 64, 3))
        self.assertAlmostEqual(
            np.sqrt(np.mean(result[:, 0] ** 2)), 1 / np.sqrt(2), places=3
        )
        removed = features.to_800hz(np.column_stack([high, high, high]) * 0.1)
        self.assertLess(np.max(np.abs(removed)), 1e-4)

    def test_sequences_do_not_bridge_gaps(self):
        entries = [(i, np.ones(21) * i) for i in [0, 1, 2, 3, 5, 6, 7, 8, 9, 10]]
        sequences, starts = features.make_sequences(entries)
        self.assertEqual(starts, [0, 1, 2, 3, 5, 6, 7, 8, 9, 10])
        self.assertEqual(sequences[0].shape, (1, 21))
        self.assertFalse(features.PROFILE["requiresConsecutiveWindows"])

    def test_metadata_and_apple_sidecars(self):
        name = "root/health_speed_circulation_20Nm_1000rpm_250702161631.csv"
        self.assertEqual(prepare.metadata(name)["label"], "health")
        self.assertEqual(prepare.metadata(name.replace(".csv", "d.csv"))["suffix"], "d")
        self.assertEqual(
            prepare.metadata(name.replace("_250702161631", ""))["label"], "health"
        )
        self.assertIsNone(prepare.metadata(name.replace("/health", "/._health")))
        with self.assertRaises(ValueError):
            prepare.metadata("../" + name)
        with self.assertRaises(ValueError):
            prepare.metadata(name.replace("health", "unreviewed"))

    def test_split_is_condition_grouped_deterministic(self):
        rows = [
            {"condition": f"{mode}_{i}", "label": label}
            for mode in ("speed", "torque")
            for i in range(6)
            for label in ("health", "bend")
        ]
        splits = prepare.split_conditions(rows)
        self.assertEqual(splits, prepare.split_conditions(list(reversed(rows))))
        self.assertEqual(list(splits.values()).count("train"), 8)
        self.assertEqual(list(splits.values()).count("validation"), 2)
        self.assertEqual(list(splits.values()).count("test"), 2)

    def test_parse_9_columns_not_hf_8_column_description(self):
        values = np.zeros((8192, 9))
        values[:, 0] = np.arange(8192) / 12800
        values[:, 3:6] = [1, 2, 3]
        buf = io.BytesIO()
        np.savetxt(buf, values, delimiter=",")
        result = prepare.parse_csv(buf.getvalue())
        np.testing.assert_allclose(result[0], [1, 2, 3])
        bad = io.BytesIO()
        np.savetxt(bad, values[:, 1:], delimiter=",")
        with self.assertRaises(ValueError):
            prepare.parse_csv(bad.getvalue())

    def test_timestamp_mismatch_rejected(self):
        values = np.zeros((8192, 9))
        values[:, 0] = np.arange(8192) / 12000
        buf = io.BytesIO()
        np.savetxt(buf, values, delimiter=",")
        with self.assertRaises(ValueError):
            prepare.parse_csv(buf.getvalue())

    def test_condition_without_healthy_reference_rejected(self):
        with self.assertRaises(ValueError):
            prepare.split_conditions([{"condition": "speed_1", "label": "bend"}])


try:
    if np is None:
        raise ImportError()
    from ai.ai1.mcc5_training import train
    from ai.ai1.mcc5_training.score_window import score_window
    from ai.ai1.mcc5_training.download import sha256
    from ai.ai2.model_runtime import Checkpoint
except ImportError:
    train = None


@unittest.skipIf(train is None, "PyTorch training extras unavailable")
class Mcc5TrainingTest(unittest.TestCase):
    def test_metrics_include_false_positives_and_misses(self):
        result = train.metrics([False, False, True, True], [0, 2, 1, 3], 1.5)
        self.assertEqual([result[k] for k in ("tp", "tn", "fp", "fn")], [1, 1, 1, 1])
        self.assertEqual(result["balancedAccuracy"], 0.5)
        self.assertEqual(result["rocAuc"], 0.75)

    def test_metrics_one_class_does_not_claim_auc(self):
        result = train.metrics([False, False], [0, 1], 2)
        self.assertIsNone(result["rocAuc"])
        self.assertEqual(result["falsePositiveRate"], 0)

    def fixture(self, root):
        x, y, run, split, rows = [], [], [], [], []
        clock = np.arange(512) / 800
        wave = np.column_stack(
            [
                np.sin(2 * np.pi * 25 * clock),
                np.sin(2 * np.pi * 50 * clock),
                np.sin(2 * np.pi * 100 * clock),
            ]
        )
        values = features.extract(wave)
        for group in ("train", "validation", "test"):
            for label in ("health", "bend"):
                i = len(rows)
                for j in range(8):
                    x.append(
                        [values * (1 + 0.01 * j) * (1 if label == "health" else 2)]
                    )
                    y.append(label != "health")
                    run.append(i)
                    split.append(group)
                rows.append(
                    {
                        "label": label,
                        "condition": group,
                        "split": group,
                        "sequences": 8,
                        "member": f"synthetic-{i}.csv",
                        "excludedWindows": {},
                    }
                )
        np.savez_compressed(
            root / "sequences.npz",
            x=x,
            y=np.array(y, dtype=np.int8),
            run=run,
            split=split,
        )
        manifest = {
            "profile": features.PROFILE,
            "cacheSha256": sha256(root / "sequences.npz"),
            "runs": rows,
            "splitByCondition": {s: s for s in ("train", "validation", "test")},
            "sourceDOI": "synthetic-unit-test-only",
            "sourceRevision": "synthetic",
            "holdout": "synthetic-test-only",
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "golden-window.json").write_text(
            json.dumps(
                {"rawWindowG": wave.tolist(), "expectedFeatures": values.tolist()}
            ),
            encoding="utf-8",
        )
        return manifest

    def test_training_reload_and_non_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepared = root / "prepared"
            prepared.mkdir()
            self.fixture(prepared)
            result = root / "model"
            train.train(prepared, result, epochs=2)
            checkpoint = Checkpoint.load(
                result / "dense_autoencoder.pt",
                candidate="dense_autoencoder",
                expected_checksum="sha256:" + sha256(result / "dense_autoencoder.pt"),
            )
            example = json.loads((result / "golden-example.json").read_text())
            actual = checkpoint.predict([example["expectedFeatures"]], features.NAMES)
            self.assertAlmostEqual(
                actual["errors"][0], example["expectedError"], places=5
            )
            report = json.loads((result / "training_report.json").read_text())
            self.assertFalse(report["deployment"]["activated"])
            self.assertFalse(report["deployment"]["fieldValidated"])
            checked = score_window(
                result / "dense_autoencoder.pt", checkpoint.checksum, example
            )
            self.assertAlmostEqual(
                checked["errors"][0], example["expectedError"], places=5
            )
            example["quality"] = "unknown"
            with self.assertRaises(ValueError):
                score_window(
                    result / "dense_autoencoder.pt", checkpoint.checksum, example
                )
            example["quality"] = "valid"
            example["rawWindowG"][0][0] = True
            with self.assertRaises(ValueError):
                score_window(
                    result / "dense_autoencoder.pt", checkpoint.checksum, example
                )
            with self.assertRaises(FileExistsError):
                train.train(prepared, result, epochs=2)

    def test_cache_corruption_blocks_training(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            with (root / "sequences.npz").open("ab") as handle:
                handle.write(b"altered")
            with self.assertRaises(ValueError):
                train.load_prepared(root)

    def test_cross_profile_split_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            manifest["splitByCondition"]["test"] = "train"
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                train.load_prepared(root)


if __name__ == "__main__":
    unittest.main()
