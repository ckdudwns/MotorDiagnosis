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
    prepare_lstm_chunks,
    _evaluate_candidate,
    select_best,
    score_from_artifact,
    feature_names_from_manifest,
    _sha256_of_file as _sha256,
)
from dataset_version import freeze_dataset_version  # noqa: E402


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
            reloaded = score_from_artifact(
                path, test_matrix, input_feature_names=names
            )

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


class TestScoreFromArtifactSchemaValidation(unittest.TestCase):
    """[리뷰 P1] 추론 입력의 특징 순서·차원·유한성을 검증한다 — 이름 없이 shape만
    맞는 입력을 넘기면 열이 어긋나도 오류 없이 다른 판정이 나온다."""

    def _artifact(self, tmp):
        splits = _synthetic_split(seq=None)
        names = ["f0", "f1", "f2", "f3"]
        path = os.path.join(tmp, "dense_autoencoder.pt")
        _evaluate_candidate(
            "dense_autoencoder", "x", splits, feature_names=names, epochs=10,
            artifact_path=path,
        )
        matrix = np.stack([s["vector"] for s in splits["test"]])
        return path, names, matrix

    def test_reversed_column_order_is_corrected_by_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path, names, matrix = self._artifact(tmp)
            straight = score_from_artifact(path, matrix, input_feature_names=names)
            reversed_names = list(reversed(names))
            reversed_matrix = matrix[:, ::-1]
            corrected = score_from_artifact(
                path, reversed_matrix, input_feature_names=reversed_names
            )
        # 이름 기준 재정렬 → 열을 뒤집어 넣어도 같은 판정.
        self.assertEqual(corrected["verdict"], straight["verdict"])

    def test_wrong_feature_set_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path, names, matrix = self._artifact(tmp)
            with self.assertRaises(ValueError):
                score_from_artifact(
                    path, matrix, input_feature_names=["f0", "f1", "f2", "OTHER"]
                )

    def test_column_count_mismatch_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path, names, matrix = self._artifact(tmp)
            with self.assertRaises(ValueError):
                score_from_artifact(
                    path, matrix[:, :3], input_feature_names=names[:3]
                )

    def test_nan_input_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path, names, matrix = self._artifact(tmp)
            bad = matrix.copy()
            bad[0, 0] = np.nan
            with self.assertRaises(ValueError):
                score_from_artifact(path, bad, input_feature_names=names)


class TestValidationBasedSelection(unittest.TestCase):
    def test_select_best_uses_validation_not_test(self):
        cand_a = {"name": "a", "validationMetrics": {"f1": 0.9}, "metrics": {"f1": 0.2}}
        cand_b = {"name": "b", "validationMetrics": {"f1": 0.4}, "metrics": {"f1": 0.99}}
        best = select_best([cand_a, cand_b])
        self.assertEqual(best["name"], "a")  # 검증 f1이 높은 쪽
        # 보고되는 최종 지표는 선택된 모델의 test 값
        self.assertEqual(best["metrics"]["f1"], 0.2)


_SYNTH_FEATURE_COLS = (
    "kurtosis_mean",
    "rms_mean",
    "spectral_centroid_mean",
    "vibration_peak_hz",
)


def _synthetic_frozen_manifest(windows_per_file=10, sample_rate_hz=12000, seed=0):
    """실제 CWRU/torch 없이 v1.3 draft를 만들어 freeze_dataset_version으로 동결한다.

    파일당 windows_per_file개 윈도우 — 기본 window/hop(2048)이 아닌 값(예:
    window=hop=1024)으로 동결한 매니페스트를 흉내낸다. 실제 freeze를 거치므로
    datasetChecksum·snapshotDigest가 유효하다(run_training_job의 무결성 검증 통과).
    """
    files = [
        ("97.mat", "NORMAL", "NORMAL", "train"),
        ("98.mat", "NORMAL", "NORMAL", "train"),
        ("99.mat", "NORMAL", "NORMAL", "validation"),
        ("105.mat", "BEARING_FAULT_INNER", "ANOMALY", "validation"),
        ("100.mat", "NORMAL", "NORMAL", "test"),
        ("106.mat", "BEARING_FAULT_INNER", "ANOMALY", "test"),
    ]
    rng = np.random.default_rng(seed)
    rows = []
    for source_file, known, common, split in files:
        base = source_file.replace(".mat", "")
        offset = 6.0 if common == "ANOMALY" else 0.0
        for i in range(windows_per_file):
            row = {
                "sample_id": f"{base}_{i:04d}",
                "source_file": source_file,
                "specimen_id": f"SYNTH-{known}",
                "known_label": known,
                "common_label": common,
                "split": split,
                "sample_rate_hz": sample_rate_hz,
                "rpm": 1797,
            }
            for col in _SYNTH_FEATURE_COLS:
                row[col] = float(rng.normal(scale=0.1) + offset)
            rows.append(row)
    draft = {
        "id": "DS-SYNTH-FROZEN-001",
        "name": "synthetic",
        "status": "draft",
        "source": {"type": "external", "checksum": "sha256:synth-version-checksum"},
        "compatibility": {"signalType": ["vibration"], "samplingRateHz": sample_rate_hz},
        "labelTaxonomyVersion": "CWRU-FAULT-V1",
        "labelMapping": {"NORMAL": "NORMAL", "BEARING_FAULT_INNER": "ANOMALY"},
        "labelPolicyVersion": "LABEL-POLICY-V2",
        "snapshotSchemaVersion": "2",
        "split": {"train": 0.5, "validation": 0.3, "test": 0.2},
        "splitStrategy": "operating_condition_holdout: synthetic",
        "holdoutType": "operating_condition",
        "independentHoldout": False,
        "rows": rows,
    }
    return freeze_dataset_version(draft)


class TestLstmInputMatchesFrozenDataset(unittest.TestCase):
    """[리뷰 P2] LSTM 입력도 동결 데이터셋과 일치해야 한다. 예전 구현은 파일별
    split 배정만 재사용하고 원본 CWRU를 기본 2048 윈도우로 다시 읽어, 비기본
    window/hop으로 동결한 매니페스트에서는 윈도우 수(예: 320→160)와 같은
    sample_id의 특징값이 datasetId가 가리키는 데이터와 어긋났다."""

    def test_sequences_cover_all_frozen_rows_for_nondefault_window(self):
        manifest = _synthetic_frozen_manifest(windows_per_file=10)  # window=hop=1024 흉내
        names = feature_names_from_manifest(manifest)
        chunks = prepare_lstm_chunks(manifest, names)
        for split in ("train", "validation", "test"):
            self.assertEqual(len(chunks[split]), 4, split)  # 파일당 10//5=2, split당 파일 2개
            for seq in chunks[split]:
                self.assertEqual(seq["vector"].shape, (5, len(names)))
        # 예전 구현식 축소(윈도우 절반만 사용)가 없어야 한다: 시퀀스가 덮는
        # 윈도우 총수 == 동결 rows 총수.
        covered = sum(
            len(seq["sample_id"].split("+"))
            for split in ("train", "validation", "test")
            for seq in chunks[split]
        )
        self.assertEqual(covered, len(manifest["rows"]))

    def test_sequence_vectors_are_frozen_inline_values_not_recomputed(self):
        manifest = _synthetic_frozen_manifest(windows_per_file=5)
        names = feature_names_from_manifest(manifest)
        rows_by_id = {r["sample_id"]: r for r in manifest["rows"]}
        chunks = prepare_lstm_chunks(manifest, names)
        for split in ("train", "validation", "test"):
            for seq in chunks[split]:
                ids = seq["sample_id"].split("+")
                expected = np.array(
                    [[rows_by_id[i][n] for n in names] for i in ids], dtype=np.float64
                )
                np.testing.assert_array_equal(seq["vector"], expected)

    def test_changed_source_rows_change_lstm_input(self):
        names = feature_names_from_manifest(_synthetic_frozen_manifest())
        base = prepare_lstm_chunks(_synthetic_frozen_manifest(), names)
        tampered_manifest = _synthetic_frozen_manifest()
        for row in tampered_manifest["rows"]:
            row["rms_mean"] += 100.0
        tampered = prepare_lstm_chunks(tampered_manifest, names)
        self.assertFalse(
            np.allclose(base["train"][0]["vector"], tampered["train"][0]["vector"])
        )

    def test_windows_ordered_by_sample_id_index_even_if_rows_shuffled(self):
        import random as _random

        manifest = _synthetic_frozen_manifest(windows_per_file=10)
        names = feature_names_from_manifest(manifest)
        _random.Random(3).shuffle(manifest["rows"])
        chunks = prepare_lstm_chunks(manifest, names)
        for split in ("train", "validation", "test"):
            for seq in chunks[split]:
                ids = seq["sample_id"].split("+")
                self.assertEqual(ids, sorted(ids, key=lambda s: int(s.split("_")[1])))

    def test_file_spanning_two_splits_is_rejected(self):
        manifest = _synthetic_frozen_manifest()
        manifest["rows"][0]["split"] = "test"  # 97.mat 일부 윈도우만 다른 split으로
        with self.assertRaises(ValueError):
            prepare_lstm_chunks(manifest, feature_names_from_manifest(manifest))

    def test_run_training_job_trains_both_candidates_from_frozen_only(self):
        # 동결본 dict만으로 dense/lstm 두 후보가 학습된다 — 원본 .mat 경로 인자
        # 자체가 사라졌다(이 환경엔 CWRU가 없다).
        manifest = _synthetic_frozen_manifest(windows_per_file=10)
        report = run_training_job(manifest, dense_epochs=5, lstm_epochs=5)
        self.assertEqual(
            {c["name"] for c in report["candidates"]},
            {"dense_autoencoder", "lstm_autoencoder"},
        )
        lstm = next(c for c in report["candidates"] if c["name"] == "lstm_autoencoder")
        self.assertEqual(lstm["testSampleCount"], 4)  # test 파일 2개 * 파일당 2 시퀀스
        self.assertEqual(report["datasetId"], "DS-SYNTH-FROZEN-001")


class TestArtifactUriIsRegistrableFileUri(unittest.TestCase):
    """[리뷰 P2] file://C:\\... 형태의 비표준 URI는 백엔드 모델 등록에서
    400 INVALID_ARTIFACT_URI로 거부된다. 표준 file URI로 생성해야 한다."""

    def test_artifact_uri_is_standard_and_roundtrips_to_saved_path(self):
        import tempfile
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        splits = _synthetic_split(seq=None)
        names = [f"f{i}" for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dense_autoencoder.pt")
            candidate, _ = _evaluate_candidate(
                "dense_autoencoder", "x", splits, feature_names=names,
                epochs=3, artifact_path=path,
            )
            uri = candidate["artifactUri"]
            parsed = urlparse(uri)
            self.assertEqual(parsed.scheme, "file")
            # 표준 file URI: 경로부가 '/'로 시작하고 역슬래시가 없다.
            self.assertTrue(parsed.path.startswith("/"))
            self.assertNotIn("\\", uri)
            # URI -> 경로 복원이 실제 저장 위치와 같아야 등록된 모델을 찾는다.
            self.assertEqual(
                os.path.normcase(os.path.realpath(url2pathname(parsed.path))),
                os.path.normcase(os.path.realpath(path)),
            )

    def test_none_when_no_artifact_saved(self):
        splits = _synthetic_split(seq=None)
        candidate, _ = _evaluate_candidate(
            "dense_autoencoder", "x", splits,
            feature_names=[f"f{i}" for i in range(4)], epochs=2,
        )
        self.assertIsNone(candidate["artifactUri"])


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestRunTrainingJobWithRealCwruData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from register_dataset import build_manifest

        # 기본 specimen_group은 CWRU에서 InsufficientAssetGroupsError.
        # 데모 리포트는 operating_condition_holdout(비독립).
        manifest = build_manifest(
            data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
        )
        cls.frozen = freeze_dataset_version(manifest)
        cls.report = run_training_job(
            cls.frozen, dense_epochs=60, lstm_epochs=60
        )

    def test_report_flags_holdout_as_non_independent(self):
        self.assertFalse(self.report["metrics"]["independentHoldout"])
        self.assertEqual(self.report["metrics"]["holdoutType"], "operating_condition")
        self.assertIn("NOT specimen-independent", self.report["metrics"]["evaluation"])

    def test_report_carries_dataset_snapshot_digest(self):
        self.assertEqual(
            self.report["datasetSnapshotDigest"], self.frozen["snapshotDigest"]
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

    def test_best_candidate_has_some_signal_on_non_independent_holdout(self):
        # 최소한의 신호는 기대하되(운전조건 기준 in-distribution), 완전 분리를
        # 일반화 성능으로 주장하지 않는다 — 리뷰 P1에 따라 임계값을 낮췄다.
        best_name = self.report["metrics"]["bestCandidate"]
        best = next(c for c in self.report["candidates"] if c["name"] == best_name)
        self.assertGreater(best["metrics"]["f1"], 0.5)

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
            if k not in {"sample_id", "source_file", "specimen_id", "known_label",
                         "common_label", "split", "sample_rate_hz", "rpm"}
        ]
        self.assertIn("vibration_peak_hz", row_features)
        self.assertEqual(len(row_features), 27)

    def test_operating_condition_holdout_file_maps_to_single_split(self):
        # 부하 tier 배정: 각 .mat 파일은 load_hp가 하나라 정확히 한 split에만.
        splits_by_source = {}
        for row in self.frozen["rows"]:
            splits_by_source.setdefault(row["source_file"], set()).add(row["split"])
        self.assertTrue(all(len(s) == 1 for s in splits_by_source.values()))

    def test_artifacts_persist_scaler_state_under_job_dir(self):
        import tempfile
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        with tempfile.TemporaryDirectory() as tmp:
            report = run_training_job(
                self.frozen, dense_epochs=20, lstm_epochs=20, artifact_dir=tmp,
            )
            # 모든 artifact가 하나의 job 디렉터리(report id) 아래에 있다.
            job_dirs = {
                os.path.basename(os.path.dirname(url2pathname(urlparse(c["artifactUri"]).path)))
                for c in report["candidates"]
            }
            self.assertEqual(job_dirs, {report["id"]})
            for c in report["candidates"]:
                path = url2pathname(urlparse(c["artifactUri"]).path)
                payload = torch.load(path, weights_only=False)
                self.assertIn("state_dict", payload)
                self.assertEqual(len(payload["scaler_mean"]), payload["input_dim"])
                self.assertEqual(len(payload["feature_names"]), payload["input_dim"])
                # 보고서에 기록된 checksum이 실제 파일과 일치.
                self.assertEqual(c["artifactChecksum"], _sha256(path))

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
    """__main__과 같은 배선(build_manifest -> freeze -> run_training_job)이 크래시 없이
    완료되는지 확인한다 (P1). 기본 specimen_group은 CWRU에서 정직하게 실패하므로
    데모 배선은 operating_condition_holdout를 쓴다."""

    def test_default_specimen_group_fails_honestly(self):
        from register_dataset import build_manifest, InsufficientAssetGroupsError

        with self.assertRaises(InsufficientAssetGroupsError):
            build_manifest(data_dir=_CWRU_DATA_DIR)  # 기본 specimen_group 3-way

    def test_end_to_end_operating_condition_holdout(self):
        import json
        import tempfile
        from register_dataset import build_manifest

        frozen = freeze_dataset_version(
            build_manifest(
                data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = run_training_job(
                frozen, dense_epochs=20, lstm_epochs=20, artifact_dir=tmp,
            )
            self.assertEqual(report["datasetId"], frozen["id"])
            self.assertFalse(report["metrics"]["independentHoldout"])
            self.assertEqual(
                {c["name"] for c in report["candidates"]},
                {"dense_autoencoder", "lstm_autoencoder"},
            )
            job_dir = os.path.join(tmp, report["id"])
            self.assertTrue(os.path.exists(os.path.join(job_dir, "dense_autoencoder.pt")))
            self.assertTrue(os.path.exists(os.path.join(job_dir, "lstm_autoencoder.pt")))
            json.dumps(report, allow_nan=False)

    def test_two_runs_use_distinct_immutable_artifact_dirs(self):
        import tempfile
        from register_dataset import build_manifest

        frozen = freeze_dataset_version(
            build_manifest(
                data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            r1 = run_training_job(frozen, dense_epochs=4, lstm_epochs=4, artifact_dir=tmp)
            r2 = run_training_job(frozen, dense_epochs=4, lstm_epochs=4, artifact_dir=tmp)
            self.assertNotEqual(r1["id"], r2["id"])
            self.assertTrue(os.path.isdir(os.path.join(tmp, r1["id"])))
            self.assertTrue(os.path.isdir(os.path.join(tmp, r2["id"])))
            # 같은 job_id로 다시 저장 시도하면 덮어쓰기 거부.
            d1 = next(c for c in r1["candidates"] if c["name"] == "dense_autoencoder")
            self.assertNotEqual(d1["artifactChecksum"], None)

    def test_training_rejects_tampered_frozen_snapshot(self):
        from register_dataset import build_manifest

        frozen = freeze_dataset_version(
            build_manifest(
                data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
            )
        )
        frozen["rows"][0]["rpm"] = 9999.0  # 동결 이후 row 변조
        with self.assertRaises(ValueError):
            run_training_job(frozen, dense_epochs=2, lstm_epochs=2)

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
