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

_WEEK3_DATASETS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week3", "ai1", "datasets")
)

sys.path.insert(0, _FREQ_BASELINE_DIR)
sys.path.insert(0, _DATASET_VERSIONS_DIR)
sys.path.insert(0, _WEEK3_DATASETS_DIR)

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
    run_training_job,
    _evaluate_candidate,
    select_best,
    score_from_artifact,
    feature_names_from_manifest,
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


class TestFeaturesImportOrderIsolation(unittest.TestCase):
    """week1과 week2가 둘 다 `extract_features.py`라는 같은 이름의 모듈을 갖는다.
    week1이 먼저 "extract_features"라는 이름으로 캐시돼도 features.py는 파일
    경로로 직접 로드하므로 week2 전용 kurtosis 특징이 빠지면 안 된다."""

    def test_week2_features_present_even_if_week1_module_cached_first(self):
        import importlib.util

        week1_path = os.path.normpath(
            os.path.join(
                _THIS_DIR, "..", "..", "..", "week1", "ai1",
                "feature_extraction", "extract_features.py",
            )
        )
        spec = importlib.util.spec_from_file_location("extract_features", week1_path)
        week1_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(week1_module)

        prev_ef = sys.modules.get("extract_features")
        prev_features = sys.modules.get("features")
        sys.modules["extract_features"] = week1_module
        try:
            sys.modules.pop("features", None)
            import features as reloaded

            d = reloaded.build_feature_dict(np.zeros(4096), 12000)
            names = reloaded.feature_names(d)
            self.assertIn("kurtosis_mean", d)
            self.assertIn("kurtosis_std", d)
            self.assertIn("vibration_peak_hz", d)
            self.assertEqual(len(names), 27)
        finally:
            for key, mod in (("extract_features", prev_ef), ("features", prev_features)):
                if mod is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = mod


class TestFeatureScalerFromState(unittest.TestCase):
    def test_from_state_matches_fit(self):
        rng = np.random.default_rng(0)
        matrix = rng.normal(size=(50, 4))
        fitted = FeatureScaler().fit(matrix)
        restored = FeatureScaler.from_state(fitted.mean_.tolist(), fitted.std_.tolist())
        np.testing.assert_allclose(restored.transform(matrix), fitted.transform(matrix))


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


def _synthetic_split(n_features=4, seq=None):
    """정상은 0 주변, 이상은 크게 벗어난 합성 샘플 분할을 만든다."""
    rng = np.random.default_rng(0)

    def make(n, anomaly):
        shape = (n, seq, n_features) if seq else (n, n_features)
        base = rng.normal(scale=0.1, size=shape)
        if anomaly:
            base = base + 6.0
        return base

    def bucket(n_normal, n_anom):
        samples = []
        for v in make(n_normal, False):
            samples.append({"sample_id": "n", "known_label": "NORMAL",
                            "common_label": "NORMAL", "vector": v})
        for v in make(n_anom, True):
            samples.append({"sample_id": "a", "known_label": "FAULT",
                            "common_label": "ANOMALY", "vector": v})
        return samples

    return {
        "train": bucket(40, 0),
        "validation": bucket(15, 10),
        "test": bucket(15, 10),
    }


class TestArtifactReloadReproducesVerdict(unittest.TestCase):
    def _check(self, name, seq):
        import tempfile

        splits = _synthetic_split(seq=seq)
        names = [f"f{i}" for i in range(4)]
        test_labels = [s["common_label"] == "ANOMALY" for s in splits["test"]]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, f"{name}.pt")
            candidate, _ = _evaluate_candidate(
                name, "x", splits, feature_names=names, epochs=40, artifact_path=path
            )
            test_matrix = np.stack([s["vector"] for s in splits["test"]])
            reloaded = score_from_artifact(path, test_matrix)

        # 재로딩한 임계값·특징 순서가 학습 때와 같아야 한다.
        self.assertEqual(reloaded["feature_names"], names)
        self.assertAlmostEqual(reloaded["threshold"], candidate["threshold"], places=5)
        # 아티팩트만으로 계산한 판정이 학습 때 test 지표와 정확히 일치해야 한다
        # (같은 모델 가중치 + 스케일러 상태 + 임계값이 아티팩트에 저장됐으므로).
        reloaded_metrics = compute_metrics(test_labels, reloaded["verdict"])
        self.assertEqual(reloaded_metrics, candidate["metrics"])

    def test_dense_roundtrip(self):
        self._check("dense_autoencoder", seq=None)

    def test_lstm_roundtrip(self):
        self._check("lstm_autoencoder", seq=5)


class TestValidationBasedSelection(unittest.TestCase):
    def test_select_best_uses_validation_not_test(self):
        cand_a = {"name": "a", "validationMetrics": {"f1": 0.9}, "metrics": {"f1": 0.2}}
        cand_b = {"name": "b", "validationMetrics": {"f1": 0.4}, "metrics": {"f1": 0.99}}
        best = select_best([cand_a, cand_b])
        self.assertEqual(best["name"], "a")  # 검증 f1이 높은 쪽
        # 보고되는 최종 지표는 선택된 모델의 test 값
        self.assertEqual(best["metrics"]["f1"], 0.2)


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

    def test_selection_uses_validation_not_test(self):
        self.assertEqual(self.report["metrics"]["selectionCriterion"], "validation_f1")
        self.assertIn("validation", self.report["metrics"])

    def test_dense_uses_frozen_feature_columns_minus_operating_point(self):
        manifest_features = feature_names_from_manifest(self.frozen)
        dense = next(c for c in self.report["candidates"] if c["name"] == "dense_autoencoder")
        self.assertEqual(dense["featureCount"], len(manifest_features))
        self.assertEqual(dense["normalization"]["featureOrder"], manifest_features)
        self.assertNotIn("vibration_peak_hz", dense["normalization"]["featureOrder"])
        # 매니페스트 row에는 27개(peak 포함) 그대로 남아 있다.
        row_features = [
            k for k in self.frozen["rows"][0]
            if k not in {"sample_id", "source_file", "known_label", "common_label",
                         "split", "sample_rate_hz", "rpm"}
        ]
        self.assertIn("vibration_peak_hz", row_features)
        self.assertEqual(len(row_features), 27)

    def test_lstm_file_never_spans_two_splits(self):
        # group_split 불변식: 원본 파일 하나는 한 split에만. prepare_lstm_chunks가
        # 이를 검증하므로 예외 없이 완료됐다는 것으로 충분하지만, 명시적으로도 확인한다.
        splits_by_source = {}
        for row in self.frozen["rows"]:
            splits_by_source.setdefault(row["source_file"], set()).add(row["split"])
        self.assertTrue(all(len(s) == 1 for s in splits_by_source.values()))

    def test_artifacts_persist_scaler_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = run_training_job(
                _CWRU_DATA_DIR, self.frozen, dense_epochs=20, lstm_epochs=20,
                artifact_dir=tmp,
            )
            for name in ("dense_autoencoder", "lstm_autoencoder"):
                payload = torch.load(os.path.join(tmp, f"{name}.pt"), weights_only=False)
                self.assertIn("state_dict", payload)
                self.assertEqual(len(payload["scaler_mean"]), payload["input_dim"])
                self.assertEqual(len(payload["feature_names"]), payload["input_dim"])
                self.assertIn("threshold", payload)

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

    def test_report_is_json_serializable_without_nan(self):
        import json

        json.dumps(self.report, allow_nan=False)


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestMainPipelineIntegrationRealCwru(unittest.TestCase):
    """__main__과 같은 배선(build_manifest 기본 3-way -> freeze -> run_training_job)이
    최신 main에서 크래시 없이 완료되는지 확인한다 (P1)."""

    def test_end_to_end_default_split(self):
        import json
        import tempfile
        from register_dataset import build_manifest
        from dataset_version import freeze_dataset_version

        frozen = freeze_dataset_version(build_manifest(data_dir=_CWRU_DATA_DIR))
        with tempfile.TemporaryDirectory() as tmp:
            report = run_training_job(
                _CWRU_DATA_DIR, frozen, dense_epochs=20, lstm_epochs=20, artifact_dir=tmp,
            )
            self.assertEqual(report["datasetId"], frozen["id"])
            self.assertEqual(
                {c["name"] for c in report["candidates"]},
                {"dense_autoencoder", "lstm_autoencoder"},
            )
            self.assertTrue(os.path.exists(os.path.join(tmp, "dense_autoencoder.pt")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "lstm_autoencoder.pt")))
            json.dumps(report, allow_nan=False)

    def test_script_runs_as_subprocess(self):
        import subprocess
        import tempfile

        script = os.path.join(_FREQ_BASELINE_DIR, "train_and_evaluate.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            result = subprocess.run(
                [
                    sys.executable, script,
                    "--cwru-dir", _CWRU_DATA_DIR,
                    "--output", out,
                    "--artifact-dir", os.path.join(tmp, "models"),
                    "--dense-epochs", "15", "--lstm-epochs", "15",
                ],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
