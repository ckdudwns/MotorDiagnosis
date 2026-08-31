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
import importlib.util

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)


def _import_from_path(module_name: str, file_path: str):
    """모듈 이름이 아니라 파일 경로로 정확히 특정해 import한다.

    week1(`week1/ai1/feature_extraction/`)과 week2(`week2/ai1/feature_extraction/`)
    모두 `extract_features.py`라는 같은 이름의 모듈을 갖는다. 같은 프로세스에서
    week1 쪽이 먼저 평범한 `from extract_features import ...`로 import되면(예: week1
    테스트가 먼저 수집·실행됨) 그 이름이 sys.modules에 캐시되고, 이후 sys.path
    앞쪽에 week2 디렉터리를 넣고 같은 이름으로 import해도 캐시된 week1 모듈이
    조용히 재사용된다 — week2 전용 kurtosis_mean/std가 빠져 특징이 27→25개가
    되는데도 오류가 나지 않는다. 고유 이름 + 파일 경로로 직접 로드해 차단한다
    (3주차 register_dataset.py와 동일한 패턴).
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_week2_extract_features = _import_from_path(
    "ai1_week4_freq_baseline.week2_extract_features",
    os.path.join(_WEEK2_FEATURE_DIR, "extract_features.py"),
)
extract_all_features = _week2_extract_features.extract_all_features
FeatureConfig = _week2_extract_features.FeatureConfig

_week1_build_handoff = _import_from_path(
    "ai1_week4_freq_baseline.week1_build_handoff",
    os.path.join(_WEEK1_SCRIPTS_DIR, "build_ai1_handoff_dataset.py"),
)
compute_peak_frequency = _week1_build_handoff.compute_peak_frequency

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


def canonical_feature_names(sample_rate: int) -> list:
    """이 모듈이 만드는 특징 벡터의 고정 순서(27개). LSTM 경로처럼 매니페스트
    없이 원신호에서 직접 시퀀스를 만들 때 열 순서를 고정하는 데 쓴다."""
    return feature_names(build_feature_dict(np.zeros(4096), sample_rate))
