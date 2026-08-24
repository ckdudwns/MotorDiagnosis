# 이벤트 상세 데이터 구조 (EVENT_DETAIL_01)

기능ID: `EVENT_DETAIL_01` (3주차 실행순서 3번, AI-1 주담당)
산출: `event_detail.py` — 이벤트 전후 신호·특징량 비교 데이터 빌더

기능정의: "이벤트 전후 신호, 특징량, 적용 임계값/모델 버전, 장치 상태 스냅샷을 조회한다.
원본 데이터 누락 여부를 함께 표시한다. 수용: 목록에서 선택한 이벤트와 상세 시각 범위가
일치한다." (API 매핑: `GET /api/anomaly/events/{eventId}` — MVP-011)

이번 주에는 "합성 이벤트로 전후 신호·특징 비교 화면 정의"가 지금 할 일이므로, `ANOMALY_RULE_01`
(다음 순서, `../anomaly_rules/`)의 판정 결과와는 독립적으로 동작한다 — 이벤트가 발생한
**윈도우 인덱스**만 주어지면 그 전후 구간을 비교할 수 있고, 나중에 `ANOMALY_RULE_01`이
산출하는 이벤트 시작 인덱스를 그대로 넣어 연결할 수 있다.

## 입력

| 인자 | 설명 |
|---|---|
| `event` | `{id, assetId, ...}` — 최소한 `id` 필요 |
| `ordered_windows` | 시간순 정렬된 윈도우 리스트, 각 항목 `{sample_id, features: {...}}` (`extract_all_features()` 결과) |
| `event_index` | `ordered_windows`에서 이상이 시작된 인덱스 (이 인덱스부터 "이후" 구간) |
| `window_seconds` | 전/후 비교 구간 길이 (초) — "이벤트 전 N초 vs 이후 N초" |
| `window_duration_sec` | 개별 윈도우 하나의 길이 (초) = `window_size / sample_rate` |
| `applied_baseline_version` / `applied_threshold_version` | 적용된 기준선/임계값 버전(선택) — 있으면 그대로 기록만 함 |

`window_seconds`를 `window_duration_sec`으로 나눠 필요한 윈도우 개수(`n_windows`)를
구하고, `event_index` 기준으로 `[event_index - n_windows, event_index)`를 "전(before)",
`[event_index, event_index + n_windows)`를 "후(after)"로 슬라이싱한다 — after는 이상이
시작된 순간을 포함한다.

## 출력 구조

```json
{
  "event_id": "EV-241",
  "asset_id": "SITE-01-MOT-02",
  "applied_baseline_version": "2026-08-21T08:17:43.221079+00:00",
  "applied_threshold_version": null,
  "window_seconds_requested": 1.7,
  "window_duration_sec": 0.1707,
  "before": {
    "window_count_requested": 10,
    "window_count_available": 10,
    "sample_ids": ["97_0109", "...", "97_0118"],
    "time_range": [-1.7, 0.0],
    "features": { "kurtosis_mean": {"mean": -0.24, "std": 0.05, "min": -0.3, "max": -0.1, "n": 10}, "...": {} },
    "data_missing": false
  },
  "after": {
    "window_count_requested": 10,
    "window_count_available": 10,
    "sample_ids": ["105_0000", "...", "105_0009"],
    "time_range": [0.0, 1.7],
    "features": { "kurtosis_mean": {"mean": 4.8, "std": 1.2, "min": 2.1, "max": 6.9, "n": 10}, "...": {} },
    "data_missing": false
  },
  "feature_delta": {
    "kurtosis_mean": {"before_mean": -0.24, "after_mean": 4.8, "delta": 5.04, "pct_change": -2100.0}
  },
  "data_completeness": {
    "before_available": true,
    "after_available": true,
    "gap_detected": false
  }
}
```

- **원본 데이터 누락 여부**: 요청한 윈도우 수(`window_count_requested`)만큼 실제로
  확보되지 않으면(`window_count_available < requested`, 예: 이벤트가 스트림 맨 앞/끝
  근처라 전/후 구간이 짧게 잘림) `data_missing=true`로 표시하고, `data_completeness.gap_detected`도
  `true`가 된다. 화면에서는 이 값으로 "일부 구간 데이터 없음" 경고를 띄운다.
- **시각 범위 일치 검증**: `time_range`는 이벤트 발생 시각(0.0 기준 상대초)으로
  정렬되므로, `after.time_range[0] == 0.0`이 항상 목록에서 선택한 이벤트의 발생 시각과
  일치한다 (수용 기준 대응).
- `feature_delta.pct_change`는 `before_mean`이 0에 매우 가까우면(`abs(before_mean) <= 1e-12`)
  `null`로 둔다 (0 나누기 방지, week2 `validate_features.py`의 `std <= 1e-12` 가드와 동일한
  패턴).

## 대상 확정 후 보완

- `applied_baseline_version`/`applied_threshold_version`은 `ANOMALY_RULE_01`이 실제로
  판정을 수행하면 그 결과(`baseline_version`, `config_version`)를 그대로 채워 넣는다.
- 장치 상태 스냅샷(`DEVICE_HEALTH_01`)은 이번 주 범위 밖 — 백엔드가 이벤트 발생 시각의
  장치 상태를 별도로 조인해 채워야 한다.
