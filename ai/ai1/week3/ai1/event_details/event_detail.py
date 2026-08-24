"""
이벤트 상세 - 전후 신호/특징량 비교 데이터 빌더 (AI-1, 3주차 EVENT_DETAIL_01)

이벤트가 발생한 윈도우 인덱스를 기준으로 "이벤트 전 N초 vs 이후 N초" 구간의 특징값을
요약·비교한다. 필드 정의와 근거는 event_detail_format.md 참고.

ANOMALY_RULE_01(../anomaly_rules/)이 산출하는 이벤트 시작 인덱스와 독립적으로 동작한다 —
event_index만 주어지면 되므로, 이번 주에는 합성 이벤트(예: CWRU 정상→결함 구간 전환
지점)로 화면 데이터 구조를 먼저 검증한다.
"""

import math
import statistics


def summarize_window_features(feature_dicts: list) -> dict:
    """윈도우별 특징값 dict 리스트를 특징값 이름별 {mean,std,min,max,n}으로 요약한다."""
    if not feature_dicts:
        return {}

    per_feature: dict = {}
    for features in feature_dicts:
        for name, value in features.items():
            per_feature.setdefault(name, []).append(value)

    summary = {}
    for name, values in per_feature.items():
        summary[name] = {
            "mean": statistics.fmean(values),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
            "n": len(values),
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


def build_event_detail(
    *,
    event: dict,
    ordered_windows: list,
    event_index: int,
    window_seconds: float,
    window_duration_sec: float,
    applied_baseline_version: str = None,
    applied_threshold_version: str = None,
) -> dict:
    """이벤트 전후 구간을 비교하는 EVENT_DETAIL_01 응답 dict를 만든다."""
    if window_duration_sec <= 0:
        raise ValueError("window_duration_sec은 0보다 커야 합니다.")
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
