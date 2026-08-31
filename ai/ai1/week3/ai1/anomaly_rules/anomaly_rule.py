"""
설비별 통계 임계값 + 히스테리시스 이상 판정 (AI-1, 3주차 ANOMALY_RULE_01)

week2 `validate_features.check_outliers()`(baseline mean/std 기반 정상범위 판정)를
그대로 재사용한다 — 새로 계산 로직을 만들지 않는다. 이 모듈이 추가하는 것은:

1. 설비(asset_id/asset_type)별로 서로 다른 baseline/설정을 등록할 수 있는 레지스트리
   (`AssetBaselineRegistry`) — 지금은 모터 1종류뿐이지만 확장 가능한 구조.
2. 단발성 스파이크를 걸러내는 히스테리시스 상태 머신 (`evaluate_feature_stream`) —
   진입 임계값(sigma_enter) ≠ 복귀 임계값(sigma_exit) + 지속시간 조건(min_consecutive_*).

설계 근거는 anomaly_rule_format.md 참고.
"""

import os
import sys
import copy
import math
from dataclasses import dataclass

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from validate_features import check_outliers, check_missing_or_invalid  # noqa: E402


@dataclass
class AnomalyRuleConfig:
    sigma_enter: float = 3.0
    sigma_exit: float = 2.0
    min_consecutive_enter: int = 2
    min_consecutive_exit: int = 2
    version: str = "v1"

    def __post_init__(self):
        for name in ("sigma_enter", "sigma_exit"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                # NaN/음수 sigma는 이상 진입이 아예 안 되거나(예: NaN 비교는 항상
                # False) 모든 윈도우가 이상으로 판정되는 결과로 이어진다.
                raise ValueError(f"{name}는 0보다 큰 유한한 숫자여야 합니다: {value!r}")
        for name in ("min_consecutive_enter", "min_consecutive_exit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}는 1 이상의 정수여야 합니다: {value!r}")
        if self.sigma_exit >= self.sigma_enter:
            raise ValueError(
                "sigma_exit는 sigma_enter보다 작아야 합니다 (히스테리시스 조건, "
                f"sigma_enter={self.sigma_enter}, sigma_exit={self.sigma_exit})"
            )


def _validate_baseline(baseline: dict) -> None:
    """baseline의 구조와 통계값을 등록 시점에 검증한다.

    check_outliers()는 mean/std가 NaN이면 정상범위(low/high)도 NaN이 되고,
    NaN과의 모든 비교는 False이므로 `value < low or value > high`가 항상
    False가 되어 어떤 값도 이상치로 잡히지 않는다 — 즉 손상된 baseline이
    등록되면 판정 로직이 예외 없이 조용히 fail-open(전부 NORMAL)된다.
    등록 시점에 막아야 판정 단계에서 이 실패가 소리 없이 퍼지지 않는다.
    """
    if not isinstance(baseline, dict):
        raise ValueError(f"baseline은 dict여야 합니다: {baseline!r}")

    features = baseline.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError(
            f"baseline['features']는 비어 있지 않은 dict여야 합니다: {features!r}"
        )

    for name, stats in features.items():
        if not isinstance(stats, dict):
            raise ValueError(f"baseline['features'][{name!r}]는 dict여야 합니다: {stats!r}")
        for key in ("mean", "std"):
            if key not in stats:
                raise ValueError(f"baseline['features'][{name!r}]에 {key!r}가 없습니다.")
            value = stats[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"baseline['features'][{name!r}][{key!r}]는 숫자여야 합니다: {value!r}"
                )
            if not math.isfinite(value):
                raise ValueError(
                    f"baseline['features'][{name!r}][{key!r}]는 유한해야 합니다: {value!r}"
                )
        if stats["std"] < 0:
            raise ValueError(
                f"baseline['features'][{name!r}]['std']는 0 이상이어야 합니다: {stats['std']!r}"
            )


class AssetBaselineRegistry:
    """설비(asset_id) 또는 설비유형(asset_type)별 baseline/설정 레지스트리.

    조회 우선순위: asset_id 정확히 일치 -> asset_type 일치 -> default. 셋 다 없으면
    KeyError. 지금은 모터 1종류만 등록돼 있지만, 설비가 늘어나면 register()를
    추가 호출하는 것만으로 대응할 수 있다 (evaluate_asset_stream 로직은 불변).
    """

    def __init__(self):
        self._by_asset_id: dict = {}
        self._by_asset_type: dict = {}
        self._default = None

    def register(
        self,
        baseline: dict,
        config: AnomalyRuleConfig = None,
        *,
        asset_id: str = None,
        asset_type: str = None,
        is_default: bool = False,
    ) -> None:
        _validate_baseline(baseline)
        # 검증을 통과한 객체를 그대로 저장하면, 등록 후 호출자가 원본
        # baseline/config를 변경(예: std를 NaN으로)했을 때 그 변경이 이미
        # 등록된 항목에도 반영돼 검증을 우회한다. deepcopy로 등록 시점의
        # 값을 스냅샷으로 고정한다.
        entry = {
            "baseline": copy.deepcopy(baseline),
            "config": copy.deepcopy(config) if config is not None else AnomalyRuleConfig(),
        }
        if asset_id:
            self._by_asset_id[asset_id] = entry
        if asset_type:
            self._by_asset_type[asset_type] = entry
        if is_default or (asset_id is None and asset_type is None):
            self._default = entry

    def resolve(self, *, asset_id: str = None, asset_type: str = None) -> dict:
        if asset_id and asset_id in self._by_asset_id:
            entry = self._by_asset_id[asset_id]
        elif asset_type and asset_type in self._by_asset_type:
            entry = self._by_asset_type[asset_type]
        elif self._default is not None:
            entry = self._default
        else:
            raise KeyError(
                f"등록된 기준선이 없습니다: asset_id={asset_id!r}, asset_type={asset_type!r}"
            )
        # register()가 deepcopy로 저장해도, 여기서 내부 entry를 그대로
        # 돌려주면 호출자가 조회 결과(예: config.sigma_enter)를 그 자리에서
        # 수정할 때 등록된 상태 자체가 오염된다 — 특히 asset_type/default
        # 항목은 여러 설비가 같은 entry 객체를 공유하므로, 한 설비 조회
        # 결과를 고치면 그 항목을 공유하는 다른 모든 설비에도 전파된다.
        # 그래서 조회 시점에도 다시 deepcopy해 반환한다.
        return {"baseline": copy.deepcopy(entry["baseline"]), "config": copy.deepcopy(entry["config"])}


def _max_deviation_sigma(features: dict, baseline: dict) -> float:
    """모든 특징값 중 baseline 대비 최대 편차(단위: sigma)를 구한다.

    check_outliers(sigma_multiplier=0.0)를 호출하면 표준편차가 0이 아닌 모든
    특징값이 "이상치"로 반환되므로(정상범위가 [mean, mean]이 되어 사실상 전부
    벗어남), 그 deviation_sigma들의 최댓값이 "이 윈도우가 기준선에서 얼마나
    벗어났는가"의 단일 지표가 된다. 판정 로직을 새로 만들지 않고 기존 함수를
    재사용하는 방식이다.
    """
    outliers = check_outliers(features, baseline, sigma_multiplier=0.0)
    return max((o["deviation_sigma"] for o in outliers), default=0.0)


def evaluate_feature_stream(
    feature_windows: list, baseline: dict, config: AnomalyRuleConfig = None
) -> dict:
    """시간순 특징값 dict 리스트를 히스테리시스 상태 머신으로 평가한다.

    반환:
    {
        "window_states": [{"index", "state", "max_deviation_sigma", "outlier_features"}, ...],
        "events": [{"start_index", "end_index", "max_deviation_sigma",
                     "baseline_version", "config_version"}, ...],
    }

    end_index가 None인 이벤트는 스트림이 끝날 때까지 이상 상태가 지속됐음을 뜻한다.
    """
    config = config or AnomalyRuleConfig()
    baseline_version = baseline.get("meta", {}).get("generated_at")

    window_states = []
    events = []
    state = "NORMAL"
    consecutive_over = 0
    consecutive_under = 0
    current_event = None
    pending_max_dev = 0.0  # 진입 대기(consecutive_over) 구간에서 관측된 최대 편차 누적

    for i, features in enumerate(feature_windows):
        invalid_issues = check_missing_or_invalid(features, baseline)
        if invalid_issues:
            # NaN/Inf/결측 특징값은 check_outliers()가 조용히 건너뛰어
            # _max_deviation_sigma()가 0.0(=NORMAL)을 반환한다. 이를 그대로
            # 두면 진행 중인 이상 이벤트가 무효 윈도우 때문에 조기 종료될 수
            # 있으므로 현재 이벤트는 건드리지 않는다. 다만 "연속된 유효
            # 윈도우"라는 min_consecutive_* 조건을 지키려면 진행 중이던
            # 연속 카운터는 끊어야 한다 — 그러지 않으면 INVALID로 갈라진
            # 두 유효 윈도우가 연속 2회로 잘못 합산되어 start_index/end_index가
            # INVALID 윈도우를 가리키는 이벤트가 생긴다.
            if state == "NORMAL":
                consecutive_over = 0
                pending_max_dev = 0.0
            else:  # state == "ANOMALY"
                consecutive_under = 0
            window_states.append(
                {
                    "index": i,
                    "state": "INVALID",
                    "max_deviation_sigma": None,
                    "outlier_features": [],
                    "invalid_reasons": sorted({issue["reason"] for issue in invalid_issues}),
                }
            )
            continue

        max_dev = _max_deviation_sigma(features, baseline)
        over_enter = max_dev >= config.sigma_enter
        under_exit = max_dev < config.sigma_exit

        outlier_features = [
            o["feature"]
            for o in check_outliers(features, baseline, sigma_multiplier=config.sigma_enter)
        ]

        if state == "NORMAL":
            if over_enter:
                consecutive_over += 1
                pending_max_dev = max(pending_max_dev, max_dev)
            else:
                consecutive_over = 0
                pending_max_dev = 0.0
            if consecutive_over >= config.min_consecutive_enter:
                state = "ANOMALY"
                current_event = {
                    "start_index": i - config.min_consecutive_enter + 1,
                    "end_index": None,
                    "max_deviation_sigma": pending_max_dev,
                    "baseline_version": baseline_version,
                    "config_version": config.version,
                }
                consecutive_under = 0
                pending_max_dev = 0.0
        else:  # state == "ANOMALY"
            current_event["max_deviation_sigma"] = max(
                current_event["max_deviation_sigma"], max_dev
            )
            consecutive_under = consecutive_under + 1 if under_exit else 0
            if consecutive_under >= config.min_consecutive_exit:
                current_event["end_index"] = i - config.min_consecutive_exit + 1
                events.append(current_event)
                current_event = None
                state = "NORMAL"
                consecutive_over = 0
                consecutive_under = 0

        window_states.append(
            {
                "index": i,
                "state": state,
                "max_deviation_sigma": max_dev,
                "outlier_features": outlier_features,
            }
        )

    if current_event is not None:
        events.append(current_event)  # end_index=None -> 스트림 끝까지 지속 중

    return {"window_states": window_states, "events": events}


def evaluate_asset_stream(
    asset_id: str,
    feature_windows: list,
    registry: AssetBaselineRegistry,
    *,
    asset_type: str = None,
) -> dict:
    """asset_id/asset_type으로 레지스트리에서 기준선·설정을 찾아 평가하고,
    산출된 이벤트마다 asset_id를 채워 반환한다 (버전 추적용 baseline_version/
    config_version은 evaluate_feature_stream이 이미 채운다).
    """
    entry = registry.resolve(asset_id=asset_id, asset_type=asset_type)
    result = evaluate_feature_stream(feature_windows, entry["baseline"], entry["config"])
    for event in result["events"]:
        event["asset_id"] = asset_id
    return result
