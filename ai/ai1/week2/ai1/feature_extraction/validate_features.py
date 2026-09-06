"""
특징값 품질 검증 모듈 (AI-1, 2주차 EDGE_FEATURE_01)

extract_all_features()가 반환한 특징값 dict의 품질을 점검한다.
검증 규칙은 두 단계로 분리한다 (백엔드 TELEMETRY_VALIDATE_01이 그대로 참고 가능):

1. 결측/무효값 검사 (check_missing_or_invalid)
   - None, NaN, Inf/-Inf → 데이터 품질 문제. 계산 오류 또는 센서 결함 가능성이 높으므로
     원칙적으로 "거부(reject)" 대상. 이상치가 아니라 애초에 신뢰할 수 없는 값이다.
   - 특징값 dict가 비어있으면 거부한다 (reason="empty_features").
   - baseline이 주어지면, baseline에 정의된 특징값 키가 하나라도 빠져 있으면 거부한다
     (reason="missing_key").
   - 값이 실제 숫자(int/float)가 아니면 거부한다 (reason="invalid_type").
     Python에서 bool은 int의 서브클래스라 `isinstance(True, (int, float))`가 True로
     나오는 함정이 있어, bool은 명시적으로 숫자가 아닌 것으로 취급한다.

2. 기준선 대비 이상치 검사 (check_outliers)
   - baseline.json(`compute_baseline.py` 산출물)의 mean/std를 이용해
     정상범위(mean ± sigma_multiplier*std)를 벗어난 값을 "플래그(flag)" 처리.
   - 이 경우 값 자체는 유효하지만 정상 범위를 벗어난 것이므로 거부하지 않고
     이상탐지 후보로 표시만 한다 (실제 이상일 수도 있으므로 버리면 안 됨).

이 두 단계를 구분하는 이유: "무효값"과 "이상치"는 처리 방식이 다르다.
무효값은 저장 전에 걸러야 하는 데이터 품질 문제이고, 이상치는 그대로 저장하되
표시만 해서 다운스트림(대시보드, 알림)이 활용할 수 있어야 한다.
"""

import math
import json

DEFAULT_SIGMA_MULTIPLIER = 3.0


def is_valid_number(value) -> bool:
    """value가 실제 숫자(int/float)인지 판정한다. bool은 숫자로 취급하지 않는다.

    Python에서 bool은 int의 서브클래스라 isinstance(True, (int, float))가 True로
    나오는 함정이 있어, 여기서 명시적으로 bool을 제외한다.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_missing_or_invalid(features: dict, baseline: dict = None) -> list:
    """None/NaN/Inf/빈 dict/필수 키 누락/비숫자 값을 검출해 이슈 목록을 반환한다.

    baseline이 주어지면 baseline["features"]에 정의된 키가 features에 하나라도
    빠져 있는지도 함께 검사한다 (reason="missing_key").
    """
    if not features:
        return [{"feature": None, "value": None, "reason": "empty_features"}]

    invalid = []

    if baseline is not None:
        baseline_features = baseline.get("features", baseline)
        for required_name in baseline_features:
            if required_name not in features:
                invalid.append(
                    {"feature": required_name, "value": None, "reason": "missing_key"}
                )

    for name, value in features.items():
        if value is None:
            invalid.append({"feature": name, "value": None, "reason": "missing"})
            continue
        if not is_valid_number(value):
            invalid.append({"feature": name, "value": value, "reason": "invalid_type"})
            continue
        if isinstance(value, float) and math.isnan(value):
            invalid.append({"feature": name, "value": value, "reason": "nan"})
        elif isinstance(value, float) and math.isinf(value):
            invalid.append({"feature": name, "value": value, "reason": "inf"})

    return invalid


def _stored_range_tolerance(baseline: dict, sigma_multiplier):
    """Contextual drafts use their recorded policy; legacy baselines keep 3-sigma."""
    context = baseline.get("signalContext")
    if context is None:
        return None
    if not isinstance(context, dict):
        raise ValueError("signalContext must be an object")
    policy = context.get("evaluationPolicy")
    if not isinstance(policy, dict) or (
        policy.get("rangeSource") != "normal_range"
        or policy.get("zeroVariance") != "absolute_tolerance"
    ):
        raise ValueError(
            "Contextual baseline requires an explicit stored-range evaluation policy"
        )
    tolerance = policy.get("absoluteTolerance")
    stored_sigma = context.get("sigmaMultiplier")
    if not is_valid_number(tolerance) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("absoluteTolerance must be a finite nonnegative number")
    if (
        not is_valid_number(stored_sigma)
        or not math.isfinite(stored_sigma)
        or stored_sigma <= 0
    ):
        raise ValueError("Stored sigmaMultiplier must be a finite positive number")
    if sigma_multiplier is not None and (
        not is_valid_number(sigma_multiplier)
        or not math.isfinite(sigma_multiplier)
        or sigma_multiplier != stored_sigma
    ):
        raise ValueError(
            "Cannot override the stored baseline range; build a new baseline instead"
        )
    return tolerance


def check_outliers(
    features: dict,
    baseline: dict,
    sigma_multiplier: float = None,
) -> list:
    """baseline의 mean/std 기준으로 정상범위를 벗어난 특징값을 플래그 처리.

    baseline은 compute_baseline.py가 생성한 dict(또는 그 "features" 서브dict)를 받는다.
    baseline에 없는 특징값(신규 특징량 등)은 비교 대상에서 제외한다.
    NaN/Inf 값은 여기서 다루지 않는다 — check_missing_or_invalid()로 먼저 걸러야 한다.

    signalContext가 있는 신호별 기준선은 저장된 normal_range를 그대로 쓴다.
    0분산은 명시한 절대 허용오차 정책을 사용하고 deviation_sigma는 None이다.
    해당 문맥이 없는 기존 기준선만 종전의 sigma(기본 3) 재계산/0분산 제외를 유지한다.
    """
    baseline_features = baseline.get("features", baseline)
    tolerance = _stored_range_tolerance(baseline, sigma_multiplier)
    legacy_sigma = (
        DEFAULT_SIGMA_MULTIPLIER if sigma_multiplier is None else sigma_multiplier
    )
    outliers = []

    for name, value in features.items():
        if not is_valid_number(value):
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue  # 무효값은 check_missing_or_invalid의 책임

        stats = baseline_features.get(name)
        if stats is None:
            continue  # 기준선에 없는 특징값은 판단 불가 → 건너뜀

        mean, std = stats["mean"], stats["std"]
        if tolerance is not None:
            stored_range = stats.get("normal_range")
            if (
                not all(is_valid_number(v) and math.isfinite(v) for v in (mean, std))
                or std < 0
                or not isinstance(stored_range, (list, tuple))
                or len(stored_range) != 2
                or not all(
                    is_valid_number(v) and math.isfinite(v) for v in stored_range
                )
                or stored_range[0] > stored_range[1]
            ):
                raise ValueError(f"Invalid stored normal_range/statistics: {name}")
            low, high = stored_range
            if std == 0 and (low != mean - tolerance or high != mean + tolerance):
                raise ValueError(
                    f"Stored zero-variance range disagrees with tolerance: {name}"
                )
        else:
            if std <= 1e-12:
                # Preserve the existing contract for legacy, context-free baselines.
                continue
            low, high = mean - legacy_sigma * std, mean + legacy_sigma * std
        if value < low or value > high:
            deviation_sigma = abs(value - mean) / std if std else None
            outliers.append(
                {
                    "feature": name,
                    "value": value,
                    "mean": mean,
                    "std": std,
                    "normal_range": [low, high],
                    "deviation_sigma": deviation_sigma,
                }
            )

    return outliers


def validate_features(
    features: dict,
    baseline: dict = None,
    sigma_multiplier: float = None,
) -> dict:
    """특징값 dict 하나를 종합 검증해 결과 리포트를 반환한다.

    반환 예:
    {
        "is_valid": bool,          # 결측/무효값/빈 dict/필수 키 누락이 없으면 True
        "missing_or_invalid": [...],
        "outliers": [...],         # baseline이 주어졌을 때만 채워짐
    }
    """
    missing_or_invalid = check_missing_or_invalid(features, baseline)
    outliers = check_outliers(features, baseline, sigma_multiplier) if baseline else []

    return {
        "is_valid": len(missing_or_invalid) == 0,
        "missing_or_invalid": missing_or_invalid,
        "outliers": outliers,
    }


def load_baseline(baseline_path: str) -> dict:
    """compute_baseline.py가 만든 baseline.json을 로드."""
    with open(baseline_path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    # 간단 동작 확인용 예시
    sample_features = {
        "rms_mean": 0.074,
        "kurtosis_mean": 5.2,  # 기준선 범위를 크게 벗어나는 값(가상)
        "spectral_centroid": float("nan"),  # 무효값(가상)
    }
    fake_baseline = {
        "features": {
            "rms_mean": {"mean": 0.07374, "std": 0.00196},
            "kurtosis_mean": {"mean": -0.24415, "std": 0.13095},
            "spectral_centroid": {"mean": 825.42, "std": 28.57},
        }
    }

    report = validate_features(sample_features, fake_baseline)
    print(json.dumps(report, ensure_ascii=False, indent=2))
