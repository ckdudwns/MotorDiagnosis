"""
이벤트 상세 - 전후 신호/특징량 비교 데이터 빌더 (AI-1, 3주차 EVENT_DETAIL_01)

이벤트가 발생한 윈도우 인덱스를 기준으로 "이벤트 전 N초 vs 이후 N초" 구간의 특징값을
요약·비교한다. 필드 정의와 근거는 event_detail_format.md 참고.

ANOMALY_RULE_01(../anomaly_rules/)이 산출하는 이벤트 시작 인덱스와 독립적으로 동작한다 —
event_index만 주어지면 되므로, 이번 주에는 합성 이벤트(예: CWRU 정상→결함 구간 전환
지점)로 화면 데이터 구조를 먼저 검증한다.
"""

import os
import sys
import math
import statistics

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from validate_features import is_valid_number  # noqa: E402


def summarize_window_features(feature_dicts: list) -> dict:
    """윈도우별 특징값 dict 리스트를 특징값 이름별 {mean,std,min,max,n}으로 요약한다.

    None/bool/문자열 같은 비숫자 값과 NaN/Inf는 통계에 넣지 않고 제외한다 —
    그러지 않으면 mean/std가 NaN/Inf로 오염되어 이후 feature_delta의
    delta/pct_change까지 전파되고, data_missing=false인 채로 strict JSON
    직렬화(allow_nan=False)가 실패한다. 제외된 개수는 `n_invalid`로 남겨
    데이터 누락을 드러낸다. 한 특징값의 모든 윈도우가 무효면 그 특징값
    자체를 응답에서 제외한다(통계를 낼 수 있는 값이 없으므로).
    """
    if not feature_dicts:
        return {}

    per_feature: dict = {}
    for features in feature_dicts:
        for name, value in features.items():
            per_feature.setdefault(name, []).append(value)

    summary = {}
    for name, values in per_feature.items():
        valid_values = [v for v in values if is_valid_number(v) and math.isfinite(v)]
        if not valid_values:
            continue
        summary[name] = {
            "mean": statistics.fmean(valid_values),
            "std": statistics.pstdev(valid_values) if len(valid_values) > 1 else 0.0,
            "min": min(valid_values),
            "max": max(valid_values),
            "n": len(valid_values),
            "n_invalid": len(values) - len(valid_values),
        }
    return summary


def _feature_delta(before_summary: dict, after_summary: dict) -> dict:
    delta = {}
    for name in before_summary.keys() | after_summary.keys():
        before_stats = before_summary.get(name)
        after_stats = after_summary.get(name)
        if before_stats is None or after_stats is None:
            continue
        before_mean = before_stats["mean"]
        after_mean = after_stats["mean"]
        pct_change = (
            (after_mean - before_mean) / abs(before_mean) * 100
            if abs(before_mean) > 1e-12
            else None
        )
        delta[name] = {
            "before_mean": before_mean,
            "after_mean": after_mean,
            "delta": after_mean - before_mean,
            "pct_change": pct_change,
        }
    return delta


def _validate_positive_finite_number(name: str, value) -> None:
    """bool을 제외한 유한한 양수인지 검증한다 (0/음수/NaN/inf/bool/문자열 거부)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name}는 0보다 큰 유한한 숫자여야 합니다: {value!r}")


def build_event_detail(
    *,
    event: dict,
    ordered_windows: list,
    event_index: int,
    window_seconds: float,
    window_duration_sec: float,
    applied_baseline_version: str = None,
    applied_threshold_version: str = None,
    applied_model_version: str = None,
) -> dict:
    """이벤트 전후 구간을 비교하는 EVENT_DETAIL_01 응답 dict를 만든다."""
    # window_duration_sec이 무한대/NaN/bool/문자열이면 이후 n_windows 계산이나
    # time_range 값이 조용히 잘못된 값(예: inf)으로 새 나간다 — 먼저 거부한다.
    _validate_positive_finite_number("window_duration_sec", window_duration_sec)
    # window_seconds가 0/음수/NaN이면 max(1, ceil(...))이 조용히 1개 윈도우
    # 요청으로 바꿔버려 응답의 요청값과 실제 계산이 어긋난다 — 먼저 거부한다.
    _validate_positive_finite_number("window_seconds", window_seconds)
    if isinstance(event_index, bool) or not isinstance(event_index, int):
        # bool은 int의 서브클래스라 True/False가 조용히 1/0 인덱스로 쓰이고,
        # float(0.5)은 범위 비교는 통과했다가 이후 슬라이싱에서야 TypeError로
        # 터진다 — 여기서 먼저 정수 타입인지 확실히 검증한다.
        raise ValueError(f"event_index는 정수여야 합니다: {event_index!r}")
    if not ordered_windows or not (0 <= event_index < len(ordered_windows)):
        raise ValueError(
            f"event_index({event_index})가 ordered_windows 범위(0~{len(ordered_windows) - 1})를 "
            "벗어났습니다 (실제 이벤트 윈도우가 있어야 합니다)."
        )

    # round()는 요청한 구간(window_seconds)보다 짧은 윈도우 수를 반환할 수 있으므로
    # (예: 4.1 -> 4) 요청 구간을 항상 포함하도록 ceil을 쓴다.
    n_windows = max(1, math.ceil(window_seconds / window_duration_sec))

    before_slice = ordered_windows[max(0, event_index - n_windows) : event_index]
    after_slice = ordered_windows[event_index : event_index + n_windows]

    before_available = len(before_slice) == n_windows
    after_available = len(after_slice) == n_windows

    before_summary = summarize_window_features([w["features"] for w in before_slice])
    after_summary = summarize_window_features([w["features"] for w in after_slice])

    before_span_sec = len(before_slice) * window_duration_sec
    after_span_sec = len(after_slice) * window_duration_sec

    return {
        "event_id": event["id"],
        "asset_id": event.get("assetId"),
        "applied_baseline_version": applied_baseline_version,
        "applied_threshold_version": applied_threshold_version,
        # 판정 당시 사용된 모델 버전 — 호출자가 명시적으로 넘기면 그 값을
        # 우선하고, 없으면 event 자체에 실려 있는 modelVersion(판정 시점에
        # 이벤트에 기록돼 있었을 값)을 그대로 보존한다. 둘 다 없으면 None.
        "applied_model_version": (
            applied_model_version
            if applied_model_version is not None
            else event.get("modelVersion")
        ),
        "window_seconds_requested": window_seconds,
        "window_duration_sec": window_duration_sec,
        "before": {
            "window_count_requested": n_windows,
            "window_count_available": len(before_slice),
            "sample_ids": [w.get("sample_id") for w in before_slice],
            "time_range": [-before_span_sec, 0.0],
            "features": before_summary,
            "data_missing": not before_available,
        },
        "after": {
            "window_count_requested": n_windows,
            "window_count_available": len(after_slice),
            "sample_ids": [w.get("sample_id") for w in after_slice],
            "time_range": [0.0, after_span_sec],
            "features": after_summary,
            "data_missing": not after_available,
        },
        "feature_delta": _feature_delta(before_summary, after_summary),
        "data_completeness": {
            "before_available": before_available,
            "after_available": after_available,
            "gap_detected": not (before_available and after_available),
        },
    }
