"""Small WAV fixtures, not public-dataset training or field performance tests."""

import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from scipy.io import wavfile
from openpyxl import load_workbook
import torch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "week3/ai1/datasets"))
sys.path.insert(0, str(ROOT / "week4/ai1/dataset_versions"))
sys.path.insert(0, str(ROOT / "week4/ai1/freq_baseline"))
import register_acoustic_dataset as acoustic
from dataset_version import freeze_dataset_version, verify_frozen_integrity
from export_dataset import export_dataset
from model_version import BaselineVersionRegistry
from signal_baseline import (
    build_acoustic_baseline,
    build_rpm_baseline,
    register_signal_baseline,
)
from train_and_evaluate import (
    prepare_dense_splits,
    prepare_lstm_chunks,
    run_training_job,
)


def recordings(root, machine_count=3):
    for machine in range(machine_count):
        for index, condition in enumerate(("normal", "abnormal")):
            path = root / f"id_{machine:02d}" / condition / "00000000.wav"
            path.parent.mkdir(parents=True)
            signal = np.random.default_rng(machine * 10 + index).normal(
                0, 0.05 + index * 0.02, 2560
            )
            wavfile.write(
                path, 8000, np.column_stack((signal, signal * 0.5)).astype(np.float32)
            )


class AcousticPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        recordings(self.root)

    def build(self, **changes):
        args = dict(
            source_uri="fixture://mimii-pump",
            license_note="Generated test fixtures only",
            operating_conditions={"acquisition": "fixture", "snrDb": 0},
            window_size=256,
            hop_size=256,
            n_mfcc=0,
        )
        args.update(changes)
        return acoustic.build_acoustic_manifest(self.root, **args)

    def test_manifest_source_units_labels_and_machine_split(self):
        manifest = self.build()
        self.assertEqual(
            manifest["compatibility"]["units"], {"acoustic": "normalized_pcm"}
        )
        self.assertEqual(manifest["compatibility"]["samplingRateHz"], 8000)
        self.assertEqual(manifest["compatibility"]["channel"], 0)
        self.assertEqual(manifest["trainingEligibleCount"], 60)
        self.assertEqual(manifest["holdoutType"], "machine")
        self.assertFalse(
            any(name.startswith("mfcc_") for name in manifest["featureNames"])
        )
        self.assertIn("acoustic_peak_hz", manifest["featureNames"])
        for machine in {row["specimen_id"] for row in manifest["rows"]}:
            self.assertEqual(
                len(
                    {
                        row["split"]
                        for row in manifest["rows"]
                        if row["specimen_id"] == machine
                    }
                ),
                1,
            )
        self.assertIn("referenceChecksum", manifest["labelCriteria"])
        for row in manifest["rows"]:
            self.assertIn(row["source_file"], manifest["source"]["files"])
            self.assertEqual(row["window_end_sample"] - row["window_start_sample"], 256)
            self.assertIn("#samples=", row["source_ref"])
            self.assertIsNone(row["rpm"])

    def test_unsigned_pcm_silence_is_centered(self):
        path = self.root / "unsigned.wav"
        wavfile.write(path, 8000, np.array([0, 128, 255], dtype=np.uint8))
        rate, signal = acoustic._loader.load_wav_signal(str(path))
        self.assertEqual(rate, 8000)
        np.testing.assert_allclose(signal, [-1, 0, 127 / 128])

    def test_mfcc_schema_has_a_separate_dataset_identity(self):
        without = self.build()
        with_mfcc = self.build(n_mfcc=3)
        self.assertNotEqual(without["id"], with_mfcc["id"])
        self.assertIn("mfcc_1", with_mfcc["featureNames"])
        self.assertEqual(
            len(with_mfcc["featureNames"]), len(without["featureNames"]) + 3
        )

    def test_deterministic_identity_and_context_changes(self):
        first = self.build()
        self.assertEqual(first["id"], self.build()["id"])
        self.assertNotEqual(
            first["id"], self.build(operating_conditions={"acquisition": "other"})["id"]
        )
        self.assertNotEqual(
            first["id"], self.build(source_uri="fixture://another-source")["id"]
        )
        self.assertNotEqual(
            first["id"], self.build(license_note="different provenance")["id"]
        )

    def test_freeze_detects_feature_and_physical_context_tampering(self):
        manifest = self.build()
        frozen = freeze_dataset_version(manifest)
        verify_frozen_integrity(frozen)
        for mutate in (
            lambda item: item["compatibility"]["units"].update(acoustic="dB"),
            lambda item: item["rows"][0].update(rms_mean=999),
            lambda item: item["labelCriteria"].update(version="forged"),
            lambda item: item["featureNames"].append("unknown_feature"),
        ):
            changed = copy.deepcopy(manifest)
            mutate(changed)
            with self.assertRaises(ValueError):
                freeze_dataset_version(changed)
        frozen["rows"][0]["source_ref"] = "different.wav"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_mfcc_requires_dependency_or_explicit_schema_exclusion(self):
        with mock.patch.dict(
            acoustic.extract_all_features.__globals__, {"_HAS_LIBROSA": False}
        ):
            with self.assertRaises(ImportError):
                self.build(n_mfcc=13)
            self.assertFalse(
                any(name.startswith("mfcc_") for name in self.build()["featureNames"])
            )

    def test_mfcc_enabled_schema_contains_computed_coefficients(self):
        manifest = self.build(n_mfcc=3)
        self.assertEqual(
            len(
                [name for name in manifest["featureNames"] if name.startswith("mfcc_")]
            ),
            3,
        )
        self.assertTrue(any(row["mfcc_1"] != 0 for row in manifest["rows"]))

    def test_mixed_sample_rates_are_rejected(self):
        path = self.root / "id_00/normal/00000000.wav"
        _, signal = wavfile.read(path)
        wavfile.write(path, 16000, signal)
        with self.assertRaisesRegex(ValueError, "Mixed sample rates"):
            self.build()

    def test_duplicate_source_across_machines_cannot_claim_independence(self):
        source = self.root / "id_00/normal/00000000.wav"
        target = self.root / "id_01/normal/00000000.wav"
        target.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(ValueError, "Duplicate recording"):
            self.build()

    def test_insufficient_machines_do_not_fall_back_to_window_splitting(self):
        with mock.patch.object(
            acoustic._loader,
            "load_mimii_pump_dataset",
            return_value=acoustic._loader.load_mimii_pump_dataset(
                str(self.root), ["id_00"]
            ),
        ):
            with self.assertRaisesRegex(ValueError, "그룹"):
                self.build()

    def test_short_recording_and_invalid_window_options_fail(self):
        for changes in (
            {"window_size": 0},
            {"hop_size": True},
            {"hop_size": 512},
            {"n_mfcc": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(**changes)
        wavfile.write(
            self.root / "id_00/normal/00000000.wav",
            8000,
            np.zeros(10, dtype=np.float32),
        )
        with self.assertRaisesRegex(ValueError, "too-short"):
            self.build()

    def test_csv_xlsx_export_reuses_acoustic_rows_and_labels(self):
        manifest = self.build()
        export_dataset(manifest, str(self.root / "export"))
        folder = self.root / "export/versions" / manifest["id"]
        with (folder / "dataset_rows.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 60)
        self.assertEqual(
            rows[0]["known_acoustic_label"], manifest["rows"][0]["known_label"]
        )
        self.assertEqual(rows[0]["known_vibration_label"], "")
        self.assertEqual(rows[0]["label_status"], "verified")
        workbook = load_workbook(folder / "dataset_export.xlsx", read_only=True)
        try:
            metadata = dict(list(workbook["manifest"].values)[1:])
            self.assertEqual(metadata["compatibility.units.acoustic"], "normalized_pcm")
            self.assertEqual(
                metadata["compatibility.operatingConditions.acquisition"], "fixture"
            )
            self.assertEqual(
                json.loads(metadata["featureNames"]), manifest["featureNames"]
            )
            self.assertEqual(
                json.loads(metadata["labelCriteria"]), manifest["labelCriteria"]
            )
            self.assertNotIn("compatibility.units.vibration", metadata)
        finally:
            workbook.close()

    def test_dense_lstm_features_do_not_include_metadata_or_cross_recordings(self):
        frozen = freeze_dataset_version(self.build())
        names, dense = prepare_dense_splits(frozen)
        self.assertEqual(names, frozen["featureNames"])
        chunks = prepare_lstm_chunks(frozen, names)
        self.assertEqual(sum(map(len, dense.values())), 60)
        self.assertEqual(sum(map(len, chunks.values())), 12)
        for rows in chunks.values():
            for row in rows:
                ids = row["sample_id"].split("+")
                self.assertEqual(len({sample.rsplit("_", 1)[0] for sample in ids}), 1)
                self.assertEqual(row["vector"].shape, (5, len(names)))

    def test_small_fixture_can_train_existing_candidates_and_report_acoustic_context(
        self,
    ):
        torch.set_num_threads(1)
        frozen = freeze_dataset_version(self.build())
        report = run_training_job(
            frozen,
            dense_epochs=1,
            lstm_epochs=1,
            artifact_dir=str(self.root / "models"),
        )
        self.assertEqual(len(report["candidates"]), 2)
        self.assertEqual(report["domainGap"]["signalType"], "acoustic")
        self.assertEqual(report["metrics"]["selectionCriterion"], "validation_f1")
        self.assertEqual(
            sum("metrics" in candidate for candidate in report["candidates"]), 1
        )
        self.assertTrue(report["metrics"]["independentHoldout"])

    def test_cli_registration_export_training_and_baseline_handoff(self):
        frozen_path = self.root / "frozen.json"
        report_path = self.root / "report.json"
        baseline_path = self.root / "baseline.json"
        commands = [
            [
                ROOT / "week3/ai1/datasets/register_acoustic_dataset.py",
                "--pump-dir",
                self.root,
                "--source-uri",
                "fixture://pump",
                "--license-note",
                "generated fixture",
                "--operating-conditions",
                '{"acquisition":"fixture"}',
                "--window-size",
                "256",
                "--hop-size",
                "256",
                "--without-mfcc",
                "--freeze",
                "--output",
                frozen_path,
            ],
            [
                ROOT / "week3/ai1/datasets/export_dataset.py",
                "--manifest",
                frozen_path,
                "--output-dir",
                self.root / "handoff",
            ],
            [
                ROOT / "week4/ai1/freq_baseline/train_and_evaluate.py",
                "--manifest",
                frozen_path,
                "--dense-epochs",
                "1",
                "--lstm-epochs",
                "1",
                "--artifact-dir",
                self.root / "models",
                "--output",
                report_path,
            ],
            [
                ROOT / "week4/ai1/dataset_versions/signal_baseline.py",
                "--modality",
                "acoustic",
                "--input",
                frozen_path,
                "--site-id",
                "S1",
                "--asset-id",
                "A1",
                "--output",
                baseline_path,
            ],
        ]
        for command in commands:
            result = subprocess.run(
                [sys.executable, *map(str, command)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(frozen_path.read_text(encoding="utf-8"))["status"], "frozen"
        )
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8"))["domainGap"][
                "signalType"
            ],
            "acoustic",
        )
        self.assertEqual(
            json.loads(baseline_path.read_text(encoding="utf-8"))["status"], "draft"
        )

    def test_acoustic_baseline_uses_only_normal_train_and_registers_context(self):
        frozen = freeze_dataset_version(self.build())
        baseline = build_acoustic_baseline(frozen, site_id="S1", asset_id="A1")
        eligible = [
            row
            for row in frozen["rows"]
            if row["common_label"] == "NORMAL" and row["split"] == "train"
        ]
        self.assertEqual(baseline["signalContext"]["sampleCount"], len(eligible))
        self.assertAlmostEqual(
            baseline["features"]["rms_mean"]["mean"],
            np.mean([row["rms_mean"] for row in eligible]),
        )
        registry = BaselineVersionRegistry()
        draft = register_signal_baseline(registry, baseline)
        self.assertEqual(draft["status"], "draft")
        draft["signalContext"]["units"]["acoustic"] = "dB"
        self.assertEqual(
            registry.get(draft["id"])["signalContext"]["units"]["acoustic"],
            "normalized_pcm",
        )
        with self.assertRaises(ValueError):
            registry.activate(draft["id"])


class RpmBaselineTest(unittest.TestCase):
    def test_baseline_reuses_existing_feature_evaluator(self):
        evaluator = acoustic._import_module_from_path(
            "ai1_acoustic.feature_validation",
            str(ROOT / "week2/ai1/feature_extraction/validate_features.py"),
        )
        baseline = self.build()
        self.assertEqual(
            evaluator.check_missing_or_invalid({"rpm": 1450}, baseline), []
        )
        self.assertEqual(evaluator.check_outliers({"rpm": 1450}, baseline), [])
        self.assertEqual(
            evaluator.check_outliers({"rpm": 10000}, baseline)[0]["feature"], "rpm"
        )

    def rows(self):
        return [
            {
                "rpm": value,
                "rpm_unit": "rpm",
                "site_id": "S1",
                "asset_id": "A1",
                "operating_conditions": {"load": "rated"},
                "source_ref": f"device/sequence/{index}",
                "label_status": "verified",
                "training_eligible": True,
                "target_label": "NORMAL",
                "split": "train",
                "is_synthetic": False,
            }
            for index, value in enumerate((0, 1450, 1470, None))
        ]

    def build(self, rows=None, **changes):
        args = dict(
            dataset_id="DS1",
            site_id="S1",
            asset_id="A1",
            operating_conditions={"load": "rated"},
        )
        args.update(changes)
        return build_rpm_baseline(self.rows() if rows is None else rows, **args)

    def test_measured_zero_is_valid_and_missing_rpm_is_not_rated_rpm(self):
        rows = self.rows()
        rows[-1]["ratedRpm"] = 9999
        baseline = self.build(rows)
        self.assertEqual(baseline["signalContext"]["sampleCount"], 3)
        self.assertAlmostEqual(
            baseline["features"]["rpm"]["mean"], (0 + 1450 + 1470) / 3
        )
        self.assertEqual(baseline["features"]["rpm"]["min"], 0)

    def test_unreviewed_synthetic_and_nontrain_rows_are_excluded(self):
        original = self.build()
        for extra in (
            {"label_status": "weak"},
            {"training_eligible": False},
            {"is_synthetic": True},
            {"target_label": "ANOMALY"},
            {"split": "validation"},
            {"split": "test"},
        ):
            rows = self.rows() + [
                {**self.rows()[0], "source_ref": "ignored", "rpm": 99999, **extra}
            ]
            self.assertEqual(self.build(rows)["id"], original["id"])

    def test_mixed_asset_units_and_conditions_fail(self):
        for update in (
            {"asset_id": "A2"},
            {"site_id": "S2"},
            {"rpm_unit": "Hz"},
            {"operating_conditions": {"load": "idle"}},
        ):
            rows = self.rows()
            rows[0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.build(rows)

    def test_invalid_numbers_and_insufficient_measured_data_fail(self):
        for value in (True, "1450", -1, float("nan"), float("inf")):
            rows = self.rows()
            rows[0]["rpm"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(rows)
        with self.assertRaises(ValueError):
            self.build(self.rows()[-1:])

    def test_duplicate_references_and_invalid_sigma_fail(self):
        with self.assertRaises(ValueError):
            self.build([self.rows()[0], self.rows()[0]])
        for sigma in (0, -1, True, float("nan")):
            with self.subTest(sigma=sigma), self.assertRaises(ValueError):
                self.build(sigma=sigma)

    def test_registry_preserves_zero_variance_without_automatic_activation(self):
        rows = self.rows()[:2]
        rows[0]["rpm"] = rows[1]["rpm"] = 1450
        registry = BaselineVersionRegistry()
        draft = register_signal_baseline(registry, self.build(rows))
        self.assertEqual(draft["features"]["rpm"]["std"], 0)
        self.assertEqual(draft["status"], "draft")
        approved = registry.approve(
            draft["id"], approved_by="reviewer", reason="fixture approval"
        )
        self.assertEqual(approved["signalContext"]["units"], {"rpm": "rpm"})

    def test_registry_rejects_missing_or_invalid_modality_units(self):
        for units in ({"vibration": "g"}, {"rpm": None}, {"rpm": ""}):
            baseline = self.build()
            baseline["signalContext"]["units"] = units
            with self.subTest(units=units), self.assertRaises(ValueError):
                register_signal_baseline(BaselineVersionRegistry(), baseline)


if __name__ == "__main__":
    unittest.main()
