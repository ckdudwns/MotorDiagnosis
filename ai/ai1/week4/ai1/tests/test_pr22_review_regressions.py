"""PR22 failure-boundary regressions; only locally generated WAV/JSON fixtures."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scipy.io import wavfile

import test_acoustic_pipeline as fixtures
import train_and_evaluate as training


class ReviewAcousticTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        fixtures.recordings(self.root)

    def build(self):
        return fixtures.acoustic.build_acoustic_manifest(
            self.root,
            source_uri="fixture://pump",
            license_note="generated fixture",
            operating_conditions={"acquisition": "fixture"},
            window_size=256,
            hop_size=256,
            n_mfcc=0,
        )

    def snapshot(self, folder):
        return {
            p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob("*")
            if p.is_file()
        }

    def test_tampered_frozen_export_leaves_existing_and_new_outputs_untouched(self):
        frozen = fixtures.freeze_dataset_version(self.build())
        existing = self.root / "existing"
        fixtures.export_dataset(frozen, str(existing))
        before = self.snapshot(existing)
        for field in ("row", "unit", "label", "status"):
            broken = copy.deepcopy(frozen)
            if field == "row":
                broken["rows"][0]["rms_mean"] += 123
            elif field == "unit":
                broken["compatibility"]["units"]["acoustic"] = "dB"
            elif field == "label":
                broken["labelMapping"]["PUMP_ANOMALY"] = "NORMAL"
            else:
                broken["status"] = "draft"
            for folder in (existing, self.root / "never-created"):
                with (
                    self.subTest(field=field, folder=folder),
                    self.assertRaises(ValueError),
                ):
                    fixtures.export_dataset(broken, str(folder))
            self.assertEqual(self.snapshot(existing), before)
            self.assertFalse((self.root / "never-created").exists())

    def test_cli_rejects_changed_snapshot_before_touching_current(self):
        frozen = fixtures.freeze_dataset_version(self.build())
        folder = self.root / "export"
        fixtures.export_dataset(frozen, str(folder))
        before = self.snapshot(folder)
        frozen["rows"][0]["rms_mean"] += 123
        source = self.root / "changed.json"
        source.write_text(json.dumps(frozen), encoding="utf-8")
        for target in (folder, self.root / "never-created"):
            result = subprocess.run(
                [
                    sys.executable,
                    str(fixtures.ROOT / "week3/ai1/datasets/export_dataset.py"),
                    "--manifest",
                    str(source),
                    "--output-dir",
                    str(target),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(folder), before)
        self.assertFalse((self.root / "never-created").exists())

    def test_draft_and_valid_frozen_export_remain_supported(self):
        draft = self.build()
        for index, manifest in enumerate(
            (draft, fixtures.freeze_dataset_version(draft))
        ):
            result = fixtures.export_dataset(
                manifest, str(self.root / f"export-{index}")
            )
            self.assertEqual(result["row_count"], 60)

    def test_short_anomaly_recording_fails_before_any_candidate_is_trained(self):
        for path in self.root.glob("id_*/abnormal/*.wav"):
            rate, signal = wavfile.read(path)
            wavfile.write(path, rate, signal[:1024])
        frozen = fixtures.freeze_dataset_version(self.build())
        names, dense = training.prepare_dense_splits(frozen)
        for rows in dense.values():
            self.assertEqual(sum(r["common_label"] == "ANOMALY" for r in rows), 4)
        with self.assertRaisesRegex(ValueError, "5.*window"):
            training.prepare_lstm_chunks(frozen, names)
        with mock.patch.object(training, "_train_and_validate_candidate") as train:
            with self.assertRaisesRegex(ValueError, "5.*window"):
                training.run_training_job(
                    frozen,
                    dense_epochs=1,
                    lstm_epochs=1,
                    artifact_dir=str(self.root / "models"),
                )
            train.assert_not_called()
        self.assertFalse((self.root / "models").exists())

    def test_same_machine_cannot_relabel_identical_wav(self):
        normal = self.root / "id_00/normal/00000000.wav"
        abnormal = self.root / "id_00/abnormal/00000000.wav"
        abnormal.write_bytes(normal.read_bytes())
        with self.assertRaisesRegex(ValueError, "[Cc]onflicting.*label"):
            self.build()

    def test_duplicate_recordings_across_machines_still_fail(self):
        source = self.root / "id_00/normal/00000000.wav"
        (self.root / "id_01/normal/00000000.wav").write_bytes(source.read_bytes())
        with self.assertRaisesRegex(ValueError, "independent holdout"):
            self.build()


class ReviewRpmTest(unittest.TestCase):
    def setUp(self):
        self.evaluator = fixtures.acoustic._import_module_from_path(
            "ai1_pr22.feature_validation",
            str(fixtures.ROOT / "week2/ai1/feature_extraction/validate_features.py"),
        )

    def rows(self, values, conditions=None):
        condition = {"load": "rated"} if conditions is None else conditions
        return [
            {
                "rpm": value,
                "rpm_unit": "rpm",
                "site_id": "S1",
                "asset_id": "A1",
                "operating_conditions": copy.deepcopy(condition),
                "source_ref": f"seq/{index}",
                "label_status": "verified",
                "training_eligible": True,
                "target_label": "NORMAL",
                "split": "train",
                "is_synthetic": False,
            }
            for index, value in enumerate(values)
        ]

    def build(self, values=(0, 10, 20), sigma=3.0, conditions=None, rows=None):
        condition = {"load": "rated"} if conditions is None else conditions
        return fixtures.build_rpm_baseline(
            self.rows(values, condition) if rows is None else rows,
            dataset_id="DS1",
            site_id="S1",
            asset_id="A1",
            operating_conditions=condition,
            sigma=sigma,
        )

    def test_zero_variance_uses_explicit_tolerance_without_fabricating_std(self):
        baseline = self.build((1450, 1450))
        self.assertEqual(baseline["features"]["rpm"]["std"], 0)
        self.assertEqual(
            baseline["signalContext"]["evaluationPolicy"]["absoluteTolerance"], 0
        )
        self.assertEqual(self.evaluator.check_outliers({"rpm": 1450}, baseline), [])
        outliers = self.evaluator.check_outliers({"rpm": 10000}, baseline)
        self.assertEqual(outliers[0]["normal_range"], [1450, 1450])
        self.assertIsNone(outliers[0]["deviation_sigma"])
        json.dumps(outliers, allow_nan=False)

    def test_stored_sigma_is_used_by_default_and_registered_draft(self):
        baseline = self.build(sigma=1)
        draft = fixtures.register_signal_baseline(
            fixtures.BaselineVersionRegistry(), baseline
        )
        for record in (baseline, draft):
            outliers = self.evaluator.check_outliers({"rpm": 25}, record)
            self.assertEqual(len(outliers), 1)
            self.assertEqual(
                outliers[0]["normal_range"], record["features"]["rpm"]["normal_range"]
            )
            self.assertEqual(
                len(self.evaluator.validate_features({"rpm": 25}, record)["outliers"]),
                1,
            )
            for boundary in record["features"]["rpm"]["normal_range"]:
                self.assertEqual(
                    self.evaluator.check_outliers({"rpm": boundary}, record), []
                )

    def test_contextual_range_cannot_be_silently_overridden(self):
        baseline = self.build(sigma=1)
        with self.assertRaises(ValueError):
            self.evaluator.check_outliers({"rpm": 25}, baseline, sigma_multiplier=3)

    def test_legacy_baseline_retains_its_existing_sigma_contract(self):
        legacy = {"features": {"rpm": {"mean": 10, "std": 10, "normal_range": [9, 11]}}}
        self.assertEqual(self.evaluator.check_outliers({"rpm": 25}, legacy), [])
        self.assertEqual(len(self.evaluator.check_outliers({"rpm": 25}, legacy, 1)), 1)

    def test_json_condition_types_cannot_be_mixed(self):
        for left, right in (
            (True, 1),
            (False, 0),
            (1, 1.0),
            ([True], [1]),
            ({"speed": [True, {"mode": 1}]}, {"speed": [1, {"mode": True}]}),
        ):
            conditions = {"load": left}
            rows = self.rows((1000, 2000), conditions)
            rows[1]["operating_conditions"] = {"load": right}
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                self.build(conditions=conditions, rows=rows)

    def test_json_condition_key_order_does_not_change_identity(self):
        a = {"load": {"enabled": True, "speed": [1, 2.0]}, "mode": "rated"}
        b = {"mode": "rated", "load": {"speed": [1, 2.0], "enabled": True}}
        rows = self.rows((1000, 2000), a)
        rows[1]["operating_conditions"] = b
        self.assertEqual(
            self.build(conditions=a, rows=rows)["id"],
            self.build(conditions=b, rows=rows)["id"],
        )


if __name__ == "__main__":
    unittest.main()
