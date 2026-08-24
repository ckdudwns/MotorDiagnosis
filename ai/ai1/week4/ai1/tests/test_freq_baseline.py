"""
AI_FREQ_MODEL_01 테스트 — 특징 벡터, 후보 모델, 학습·평가·보고서 검증 (AI-1, 4주차)

지표/오류 사례 집계 로직과 모델 forward/학습 동작은 합성 데이터로 항상 검증하고,
전체 학습 파이프라인(run_training_job)은 실제 CWRU 데이터가 있을 때만 실행한다
(느린 실제 학습은 skipUnless로 분리 — week2/week3와 동일한 패턴).
"""

import os
import sys
import unittest

import numpy as np
import torch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_FREQ_BASELINE_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "freq_baseline"))
_DATASET_VERSIONS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _FREQ_BASELINE_DIR)
sys.path.insert(0, _DATASET_VERSIONS_DIR)

from models import (  # noqa: E402
    DenseAutoencoder,
    LstmAutoencoder,
    FeatureScaler,
    train_autoencoder,
    reconstruction_error,
)
from train_and_evaluate import (  # noqa: E402
    compute_metrics,
    collect_error_cases,
    _contiguous_split_counts,
    run_training_job,
)


class TestComputeMetrics(unittest.TestCase):
    def test_perfect_separation(self):
        y_true = [False, False, True, True]
        y_pred = [False, False, True, True]
        m = compute_metrics(y_true, y_pred)
        self.assertEqual(m, {"tp": 2, "fp": 0, "fn": 0, "tn": 2, "precision": 1.0, "recall": 1.0, "f1": 1.0, "accuracy": 1.0})

    def test_all_false_positive(self):
        y_true = [False, False]
        y_pred = [True, True]
        m = compute_metrics(y_true, y_pred)
        self.assertEqual(m["precision"], 0.0)
        self.assertEqual(m["recall"], 0.0)  # tp+fn == 0 -> recall 정의상 0.0
        self.assertEqual(m["fp"], 2)

    def test_empty_input(self):
        m = compute_metrics([], [])
        self.assertEqual(m["accuracy"], 0.0)


class TestCollectErrorCases(unittest.TestCase):
    def test_fp_and_fn_captured_separately(self):
        samples = [{"sample_id": f"S{i}", "known_label": "X"} for i in range(4)]
        y_true = [False, True, False, True]
        y_pred = [True, False, False, True]  # S0: FP, S1: FN, S2: TN, S3: TP
        errors = [5.0, 6.0, 0.1, 5.5]
        result = collect_error_cases(samples, y_true, y_pred, errors, threshold=2.0)
        self.assertEqual(len(result["false_positives"]), 1)
        self.assertEqual(result["false_positives"][0]["sample_id"], "S0")
        self.assertEqual(len(result["false_negatives"]), 1)
        self.assertEqual(result["false_negatives"][0]["sample_id"], "S1")

    def test_limit_caps_case_count(self):
        n = 15
        samples = [{"sample_id": f"S{i}", "known_label": "X"} for i in range(n)]
        y_true = [False] * n
        y_pred = [True] * n
        errors = [1.0] * n
        result = collect_error_cases(samples, y_true, y_pred, errors, threshold=0.5, limit=10)
        self.assertEqual(len(result["false_positives"]), 10)


class TestContiguousSplitCounts(unittest.TestCase):
    def test_counts_sum_to_n(self):
        for n in (1, 3, 11, 23, 59, 119):
            n_train, n_val, n_test = _contiguous_split_counts(n)
            self.assertEqual(n_train + n_val + n_test, n)
            self.assertGreaterEqual(n_train, 0)
            self.assertGreaterEqual(n_val, 0)
            self.assertGreaterEqual(n_test, 0)


class TestFeatureScaler(unittest.TestCase):
    def test_transform_gives_zero_mean_unit_std_on_2d(self):
        rng = np.random.default_rng(0)
        matrix = rng.normal(loc=[10.0, -5.0, 100.0], scale=[1.0, 2.0, 50.0], size=(200, 3))
        scaler = FeatureScaler().fit(matrix)
        transformed = scaler.transform(matrix)
        np.testing.assert_allclose(transformed.mean(axis=0), [0, 0, 0], atol=1e-6)
        np.testing.assert_allclose(transformed.std(axis=0), [1, 1, 1], atol=1e-6)

    def test_transform_handles_3d_sequence_input(self):
        rng = np.random.default_rng(0)
        matrix = rng.normal(size=(10, 5, 4))  # N x seq x features
        scaler = FeatureScaler().fit(matrix)
        transformed = scaler.transform(matrix)
        self.assertEqual(transformed.shape, matrix.shape)

    def test_constant_feature_does_not_divide_by_zero(self):
        matrix = np.ones((10, 2))
        scaler = FeatureScaler().fit(matrix)
        transformed = scaler.transform(matrix)
        self.assertTrue(np.all(np.isfinite(transformed)))


class TestAutoencoderModels(unittest.TestCase):
    def test_dense_autoencoder_forward_shape(self):
        model = DenseAutoencoder(input_dim=6)
        x = torch.randn(4, 6)
        out = model(x)
        self.assertEqual(out.shape, x.shape)

    def test_lstm_autoencoder_forward_shape(self):
        model = LstmAutoencoder(input_dim=6)
        x = torch.randn(4, 5, 6)  # batch, seq_len, features
        out = model(x)
        self.assertEqual(out.shape, x.shape)

    def test_training_reduces_loss(self):
        torch.manual_seed(0)
        model = DenseAutoencoder(input_dim=4, hidden_dim=2)
        x = torch.randn(20, 4)
        losses = train_autoencoder(model, x, epochs=50)
        self.assertLess(losses[-1], losses[0])

    def test_reconstruction_error_shapes(self):
        model = DenseAutoencoder(input_dim=4)
        x = torch.randn(7, 4)
        errors = reconstruction_error(model, x)
        self.assertEqual(errors.shape, (7,))

        lstm_model = LstmAutoencoder(input_dim=4)
        x_seq = torch.randn(7, 5, 4)
        errors_seq = reconstruction_error(lstm_model, x_seq)
        self.assertEqual(errors_seq.shape, (7,))


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestRunTrainingJobWithRealCwruData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from register_dataset import build_manifest
        from dataset_version import freeze_dataset_version

        manifest = build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)
        cls.frozen = freeze_dataset_version(manifest)
        cls.report = run_training_job(
            _CWRU_DATA_DIR, cls.frozen, dense_epochs=60, lstm_epochs=60
        )

    def test_report_has_both_candidates(self):
        names = {c["name"] for c in self.report["candidates"]}
        self.assertEqual(names, {"dense_autoencoder", "lstm_autoencoder"})

    def test_candidates_use_different_split_strategies(self):
        strategies = {c["splitStrategy"] for c in self.report["candidates"]}
        self.assertEqual(len(strategies), 2)

    def test_metrics_are_valid_probabilities(self):
        for candidate in self.report["candidates"]:
            for key in ("precision", "recall", "f1", "accuracy"):
                value = candidate["metrics"][key]
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_best_candidate_metrics_reasonably_separates_fault_from_normal(self):
        # CWRU 결함은 week2/week3에서도 뚜렷하게 분리됐으므로 최소한의 성능을 기대한다
        best_name = self.report["metrics"]["bestCandidate"]
        best = next(c for c in self.report["candidates"] if c["name"] == best_name)
        self.assertGreater(best["metrics"]["f1"], 0.7)

    def test_error_cases_present_for_each_candidate(self):
        self.assertIn("dense_autoencoder", self.report["errorCases"])
        self.assertIn("lstm_autoencoder", self.report["errorCases"])
        for cases in self.report["errorCases"].values():
            self.assertIn("false_positives", cases)
            self.assertIn("false_negatives", cases)

    def test_domain_gap_and_field_calibration_plan_present(self):
        self.assertIn("guaranteeScope", self.report["domainGap"])
        self.assertGreater(len(self.report["fieldCalibrationPlan"]), 0)

    def test_dataset_id_linked_to_frozen_manifest(self):
        self.assertEqual(self.report["datasetId"], self.frozen["id"])


if __name__ == "__main__":
    unittest.main()
