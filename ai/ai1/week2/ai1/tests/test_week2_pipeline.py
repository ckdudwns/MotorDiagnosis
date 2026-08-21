"""
2주차 파이프라인 통합 테스트 (kurtosis, 기준선 산출, 특징값 품질 검증) — AI-1

unittest.TestCase 기반으로 작성해 `python -m unittest discover`가 실제로 수집한다
(PR #6 리뷰: 이전 버전은 일반 함수(test_*)라 discover가 0개로 인식하는 문제가 있었음).

- TestKurtosisFeature / TestSyntheticBaselineAndValidation: 합성(fixture) 신호를 써서
  CWRU 실데이터 없이도 핵심 로직(kurtosis 계산, 기준선 산출, 이상치 탐지)이 항상 실행된다.
- TestRealCwruData: 실제 CWRU 데이터(ai/ai1/week1/ai1/data/external/cwru/97.mat)가 있을
  때만 실행되며, 없으면 unittest.skipUnless로 "skipped"임이 실행 결과에 명시적으로
  드러난다 (조용히 통과로 보이지 않음).
"""

import os
import sys
import unittest

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(
        _THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru"
    )
)

sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "feature_extraction"))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "scripts"))

from load_cwru_vibration import load_cwru_dataset  # noqa: E402
from extract_features import extract_all_features, FeatureConfig  # noqa: E402
from validate_features import validate_features  # noqa: E402
from compute_baseline import compute_feature_baseline  # noqa: E402
from generate_synthetic_signal import (  # noqa: E402
    generate_normal,
    generate_bearing_fault,
)


def _cwru_data_available() -> bool:
    return os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat"))


CWRU_SKIP_REASON = f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}"


class TestKurtosisFeature(unittest.TestCase):
    """합성 신호로 항상 실행 — 실데이터 불필요."""

    def test_kurtosis_present_in_all_features(self):
        config = FeatureConfig()
        t = np.linspace(0, 1, config.sample_rate, endpoint=False)
        signal = 0.5 * np.sin(2 * np.pi * 120 * t)

        features = extract_all_features(signal, config)
        self.assertIn("kurtosis_mean", features)
        self.assertIn("kurtosis_std", features)
        self.assertIsInstance(features["kurtosis_mean"], float)


class TestSyntheticBaselineAndValidation(unittest.TestCase):
    """합성(fixture) 신호로 기준선 산출 + 검증 핵심 로직이 항상 실행되도록 보장한다.

    CWRU 실데이터가 없는 환경(CI 등)에서도 EDGE_FEATURE_01의 핵심 흐름
    (기준선 산출 → validate_features 이상치 탐지)이 최소 한 번은 검증된다.
    """

    @classmethod
    def setUpClass(cls):
        cls.config = FeatureConfig(sample_rate=16000)

        per_feature_values = {}
        for _ in range(20):
            signal = generate_normal(sample_rate=16000)
            features = extract_all_features(signal, cls.config)
            for name, value in features.items():
                per_feature_values.setdefault(name, []).append(value)

        baseline_features = {}
        for name, values in per_feature_values.items():
            arr = np.asarray(values, dtype=np.float64)
            mean, std = float(np.mean(arr)), float(np.std(arr))
            baseline_features[name] = {
                "mean": mean,
                "std": std,
                "normal_range": [mean - 3 * std, mean + 3 * std],
            }
        cls.baseline = {
            "meta": {"label": "NORMAL", "sigma_multiplier": 3.0, "source": "synthetic"},
            "features": baseline_features,
        }

    def test_normal_signal_passes_validation(self):
        signal = generate_normal(sample_rate=16000)
        features = extract_all_features(signal, self.config)
        report = validate_features(features, self.baseline)
        self.assertTrue(report["is_valid"])

    def test_bearing_fault_signal_is_flagged(self):
        signal = generate_bearing_fault(sample_rate=16000)
        features = extract_all_features(signal, self.config)
        report = validate_features(features, self.baseline)
        self.assertTrue(report["is_valid"])  # 값 자체는 유효 (무효값이 아님)
        self.assertGreater(
            len(report["outliers"]),
            0,
            "베어링 결함 합성 신호가 이상치로 전혀 플래그되지 않음",
        )


@unittest.skipUnless(_cwru_data_available(), CWRU_SKIP_REASON)
class TestRealCwruData(unittest.TestCase):
    """실제 CWRU 데이터가 있을 때만 실행.

    데이터가 없으면 unittest.skipUnless가 테스트를 "skipped"로 명시 처리한다
    (조용히 return하며 통과로 잡히던 이전 방식과 달리, 실행 결과 요약에
    'OK (skipped=N)' 형태로 드러난다).
    """

    @classmethod
    def setUpClass(cls):
        cls.baseline = compute_feature_baseline(_CWRU_DATA_DIR)
        cls.records = load_cwru_dataset(_CWRU_DATA_DIR)
        cls.config = FeatureConfig(sample_rate=cls.baseline["meta"]["sample_rate"])

    def test_baseline_from_real_normal_data(self):
        self.assertGreater(self.baseline["meta"]["n_windows"], 0)
        self.assertIn("kurtosis_mean", self.baseline["features"])

        rms_stats = self.baseline["features"]["rms_mean"]
        low, high = rms_stats["normal_range"]
        self.assertTrue(low < rms_stats["mean"] < high)

    def test_validate_features_flags_real_fault_windows(self):
        normal_records = [r for r in self.records if r["label"] == "NORMAL"]
        fault_records = [r for r in self.records if r["label"] != "NORMAL"]
        self.assertTrue(normal_records)
        self.assertTrue(fault_records)

        normal_outlier_windows = 0
        for rec in normal_records:
            features = extract_all_features(rec["signal"], self.config)
            report = validate_features(features, self.baseline)
            self.assertTrue(
                report["is_valid"], "NORMAL 윈도우에서 무효값(NaN/Inf) 발생"
            )
            if report["outliers"]:
                normal_outlier_windows += 1
        normal_outlier_ratio = normal_outlier_windows / len(normal_records)

        fault_flagged = 0
        for rec in fault_records:
            features = extract_all_features(rec["signal"], self.config)
            report = validate_features(features, self.baseline)
            if report["outliers"]:
                fault_flagged += 1
        fault_flag_ratio = fault_flagged / len(fault_records)

        self.assertGreater(
            fault_flag_ratio,
            normal_outlier_ratio,
            "결함 윈도우의 이상치 플래그 비율이 NORMAL보다 높아야 한다",
        )


if __name__ == "__main__":
    unittest.main()
