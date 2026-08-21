"""
2주차 파이프라인(kurtosis 추가, 기준선 산출, 특징값 품질 검증) 동작 확인 (AI-1)

실제 CWRU 데이터(`ai/ai1/week1/ai1/data/external/cwru/`)를 사용한다.
데이터가 없으면 관련 테스트를 건너뛴다.
pytest 없이도 `python test_week2_pipeline.py`로 바로 실행 가능하도록 작성.
"""

import os
import sys

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
from validate_features import validate_features, load_baseline  # noqa: E402
from compute_baseline import compute_feature_baseline  # noqa: E402


def _cwru_data_available() -> bool:
    return os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat"))


def test_kurtosis_present_in_all_features():
    """extract_all_features()가 kurtosis_mean/kurtosis_std를 반환하는지 확인."""
    import numpy as np

    config = FeatureConfig()
    t = np.linspace(0, 1, config.sample_rate, endpoint=False)
    signal = 0.5 * np.sin(2 * np.pi * 120 * t)

    features = extract_all_features(signal, config)
    assert "kurtosis_mean" in features
    assert "kurtosis_std" in features
    assert isinstance(features["kurtosis_mean"], float)
    print(
        "kurtosis 필드 확인 완료:", features["kurtosis_mean"], features["kurtosis_std"]
    )


def test_baseline_from_real_normal_data():
    """실제 CWRU 97.mat(NORMAL)로 기준선을 산출하고 정상 범위가 유효한지 확인."""
    if not _cwru_data_available():
        print("[건너뜀] CWRU 데이터 없음:", _CWRU_DATA_DIR)
        return

    baseline = compute_feature_baseline(_CWRU_DATA_DIR)
    assert baseline["meta"]["n_windows"] > 0
    assert "kurtosis_mean" in baseline["features"]

    rms_stats = baseline["features"]["rms_mean"]
    assert rms_stats["std"] >= 0
    low, high = rms_stats["normal_range"]
    assert low < rms_stats["mean"] < high
    print(f"기준선 검증 완료 — NORMAL 윈도우 {baseline['meta']['n_windows']}개")


def test_validate_features_flags_real_fault_windows():
    """97.mat(NORMAL) 기준선 대비, 105/118/130.mat(결함) 윈도우가 이상치로 잡히는지 확인."""
    if not _cwru_data_available():
        print("[건너뜀] CWRU 데이터 없음:", _CWRU_DATA_DIR)
        return

    baseline = compute_feature_baseline(_CWRU_DATA_DIR)
    records = load_cwru_dataset(_CWRU_DATA_DIR)
    config = FeatureConfig(sample_rate=baseline["meta"]["sample_rate"])

    normal_records = [r for r in records if r["label"] == "NORMAL"]
    fault_records = [r for r in records if r["label"] != "NORMAL"]
    assert normal_records and fault_records

    # NORMAL 윈도우 자기 자신에 대해서는 이상치가 거의 없어야 한다 (기준선 자체 데이터이므로).
    normal_outlier_windows = 0
    for rec in normal_records:
        features = extract_all_features(rec["signal"], config)
        report = validate_features(features, baseline)
        assert report["is_valid"], "NORMAL 윈도우에서 무효값(NaN/Inf) 발생"
        if report["outliers"]:
            normal_outlier_windows += 1

    normal_outlier_ratio = normal_outlier_windows / len(normal_records)
    print(
        f"NORMAL 윈도우 중 이상치 플래그 비율: "
        f"{normal_outlier_windows}/{len(normal_records)} ({normal_outlier_ratio:.1%})"
    )
    # 기준선 자체 데이터이므로 대부분은 정상범위 안에 있어야 한다.
    assert normal_outlier_ratio < 0.5

    # 결함(FAULT) 윈도우는 최소 하나 이상 이상치로 플래그되는 것이 많아야 한다
    # (특히 kurtosis_mean이 베어링 결함의 충격성 신호를 잘 잡아내는지 확인).
    fault_flagged = 0
    kurtosis_flagged = 0
    for rec in fault_records:
        features = extract_all_features(rec["signal"], config)
        report = validate_features(features, baseline)
        if report["outliers"]:
            fault_flagged += 1
        if any(o["feature"] == "kurtosis_mean" for o in report["outliers"]):
            kurtosis_flagged += 1

    fault_flag_ratio = fault_flagged / len(fault_records)
    print(
        f"결함 윈도우 중 이상치 플래그 비율: "
        f"{fault_flagged}/{len(fault_records)} ({fault_flag_ratio:.1%}), "
        f"kurtosis_mean으로 잡힌 비율: {kurtosis_flagged}/{len(fault_records)}"
    )
    assert (
        fault_flag_ratio > normal_outlier_ratio
    ), "결함 윈도우의 이상치 플래그 비율이 NORMAL보다 높아야 한다"


def test_validate_features_catches_nan_inf():
    """인위적으로 주입한 NaN/Inf 값이 missing_or_invalid로 잡히는지 확인."""
    features = {
        "rms_mean": 0.07,
        "kurtosis_mean": float("nan"),
        "spectral_centroid": float("inf"),
    }
    report = validate_features(features, baseline=None)
    assert not report["is_valid"]
    flagged = {item["feature"] for item in report["missing_or_invalid"]}
    assert flagged == {"kurtosis_mean", "spectral_centroid"}
    print("NaN/Inf 검출 확인 완료:", report["missing_or_invalid"])


if __name__ == "__main__":
    test_kurtosis_present_in_all_features()
    test_baseline_from_real_normal_data()
    test_validate_features_flags_real_fault_windows()
    test_validate_features_catches_nan_inf()
    print("\n✅ test_week2_pipeline 전체 통과")
