"""
주파수/시간영역 특징 벡터 빌더 (AI-1, 4주차 AI_FREQ_MODEL_01)

week2 extract_all_features()(RMS/스펙트럴/대역에너지/kurtosis/MFCC)와 week1
build_ai1_handoff_dataset.compute_peak_frequency()(피크 주파수)를 합쳐 "FFT·
스펙트럼 특징, RMS, 피크, 대역 에너지"를 모두 포함한 고정 순서 벡터로 만든다.
계산 로직 자체는 재구현하지 않고 기존 함수를 그대로 재사용한다. 근거는
freq_baseline_format.md 참고.
"""

import os
import sys

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from extract_features import extract_all_features, FeatureConfig  # noqa: E402
from build_ai1_handoff_dataset import compute_peak_frequency  # noqa: E402

PEAK_FEATURE_NAME = "vibration_peak_hz"


def build_feature_dict(signal, sample_rate: int, config: FeatureConfig = None) -> dict:
    """단일 신호(윈도우)에서 전체 특징값 dict를 만든다 (기존 계산 재사용 + peak 결합)."""
    config = config or FeatureConfig(sample_rate=sample_rate)
    features = extract_all_features(signal, config)
    features[PEAK_FEATURE_NAME] = compute_peak_frequency(signal, sample_rate)
    return features


def feature_names(sample_features: dict) -> list:
    """모델 입력 벡터의 고정 순서를 정의한다 (알파벳순 — 재현 가능하도록 결정적)."""
    return sorted(sample_features.keys())


def vectorize(features: dict, names: list) -> np.ndarray:
    """고정된 names 순서로 features dict를 numpy 벡터로 변환한다."""
    return np.array([features[name] for name in names], dtype=np.float64)
