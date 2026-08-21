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
from compute_baseline import (  # noqa: E402
    compute_feature_baseline,
    compute_feature_baseline_from_records,
)
from generate_synthetic_signal import (  # noqa: E402
    generate_normal,
    generate_bearing_fault,
)


def _build_fake_normal_records(n: int, sample_rate: int = 16000) -> list:
    """load_cwru_dataset()과 동일한 레코드 형식으로 합성 NORMAL 레코드를 만든다.

    실제 CWRU 파일을 읽지 않고도 compute_feature_baseline_from_records()에
    그대로 넣을 수 있도록, 그 함수가 실제로 참조하는 키(label/signal/sample_rate)를
    포함한 최소 형태로 구성한다.
    """
    return [
        {
            "sample_id": f"SYN_NORMAL_{i:03d}",
            "label": "NORMAL",
            "source_label": "synthetic_normal",
            "modality": "vibration",
            "sample_rate": sample_rate,
            "rpm": None,
            "signal": generate_normal(sample_rate=sample_rate),
        }
        for i in range(n)
    ]


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
    """합성(fixture) 신호로 실제 compute_feature_baseline_from_records()를 호출해
    핵심 로직(기준선 산출 → validate_features 이상치 탐지)이 항상 실행되도록 보장한다.

    PR #6 2차 리뷰: 이전 버전은 mean/std/3sigma 계산식을 테스트 안에서 직접
    재구현하고 있어, compute_baseline.py의 실제 함수가 고장 나도 테스트가
    통과하는 문제가 있었다. 지금은 합성 레코드를 실제 함수에 그대로 넣고,
    그 함수의 진짜 반환값을 검증한다 (자체 계산 vs 실제 계산 비교가 아님).

    CWRU 실데이터가 없는 환경(CI 등)에서도 EDGE_FEATURE_01의 핵심 흐름이
    최소 한 번은 검증된다.
    """

    @classmethod
    def setUpClass(cls):
        cls.config = FeatureConfig(sample_rate=16000)

        fake_records = _build_fake_normal_records(20, sample_rate=16000)
        # 실제 함수를 호출한다 — 계산 로직을 다시 구현하지 않고 진짜 반환값을 쓴다.
        cls.baseline = compute_feature_baseline_from_records(
            fake_records, sigma_multiplier=3.0
        )

    def test_real_function_returns_expected_baseline_shape(self):
        """compute_feature_baseline_from_records()의 실제 반환값 자체를 검증한다."""
        self.assertEqual(self.baseline["meta"]["n_windows"], 20)
        self.assertEqual(self.baseline["meta"]["sigma_multiplier"], 3.0)
        self.assertIn("rms_mean", self.baseline["features"])
        self.assertIn("kurtosis_mean", self.baseline["features"])

        rms_stats = self.baseline["features"]["rms_mean"]
        self.assertIn("mean", rms_stats)
        self.assertIn("std", rms_stats)
        self.assertGreater(rms_stats["std"], 0.0)

        low, high = rms_stats["normal_range"]
        # normal_range가 실제로 mean ± 3*std로 계산됐는지 확인 (함수가
        # 다른 배수를 쓰거나 부호를 뒤집는 등 고장 났다면 여기서 실패한다).
        self.assertAlmostEqual(low, rms_stats["mean"] - 3 * rms_stats["std"], places=9)
        self.assertAlmostEqual(high, rms_stats["mean"] + 3 * rms_stats["std"], places=9)

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
