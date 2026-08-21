# 특징값 품질 검증 규칙 (EDGE_FEATURE_01 → TELEMETRY_VALIDATE_01 참고자료)

AI-1이 `feature_extraction/validate_features.py`에 구현한 검증 규칙을, 백엔드
`TELEMETRY_VALIDATE_01`(수집·검증·중복 제거)이 언어에 상관없이 그대로 재구현할 수 있도록
규칙만 분리해 정리한다. 구현은 Python이지만 규칙 자체는 특정 언어에 종속적이지 않다.

## 왜 두 단계로 나누는가

| 구분 | 대상 | 처리 방식 | 이유 |
|---|---|---|---|
| 1. 결측/무효값 | `None`, `NaN`, `Inf`/`-Inf` | **거부(reject)** — 저장 전 격리 | 계산 오류·센서 결함일 가능성이 높은, 애초에 신뢰할 수 없는 값 |
| 2. 기준선 대비 이상치 | 기준선 범위(mean ± N·std)를 벗어난 값 | **플래그(flag)** — 저장은 하되 표시만 | 값 자체는 유효하며, 실제 설비 이상일 수 있으므로 버리면 안 됨 |

무효값과 이상치를 같은 방식으로 처리하면 안 된다. 무효값을 이상치처럼 "표시만" 하고
저장하면 대시보드에 깨진 값이 그대로 노출되고, 반대로 이상치를 무효값처럼 "거부"하면
실제 설비 이상 신호를 유실하게 된다.

## 규칙 1: 결측/무효값 검사

특징값 dict의 각 값에 대해:

- 값이 `null`/`None` → invalid, reason=`missing`
- 값이 숫자이고 `NaN` → invalid, reason=`nan`
- 값이 숫자이고 `Inf`/`-Inf` → invalid, reason=`inf`

하나라도 걸리면 해당 레코드는 `is_valid = false`.

## 규칙 2: 기준선 대비 이상치 검사

`compute_baseline.py`가 산출한 `baseline.json`의 `features.<name>.mean` /
`features.<name>.std`를 사용해, 특징값별로:

```
normal_range = [mean - sigma_multiplier * std, mean + sigma_multiplier * std]
```

- `sigma_multiplier` 기본값 3.0 (약 99.7% 정상 범위, 정규분포 가정 시).
- 값이 `normal_range` 밖이면 이상치로 플래그. `deviation_sigma = |value - mean| / std`도
  함께 기록해 얼마나 벗어났는지 알 수 있게 한다.
- `std`가 0에 가까우면(예: 윈도우당 프레임이 1개뿐이라 프레임 내 분산이 0인 경우)
  범위 비교가 무의미하므로 건너뛴다.
- 기준선에 없는 특징값(신규 특징량 등)은 비교 대상에서 제외한다 — 알 수 없으니 통과시킨다.
- NaN/Inf 값은 여기서 다루지 않는다. 규칙 1에서 먼저 걸러야 한다.

## `baseline.json` 스키마

`ai1/dataset/baseline.json` (2주차, CWRU 97.mat NORMAL 데이터 기준):

```json
{
  "meta": {
    "label": "NORMAL",
    "source": "CWRU 97.mat (Drive-End, NORMAL)",
    "modality": "vibration",
    "n_windows": 119,
    "window_size": 2048,
    "hop_size": 2048,
    "sample_rate": 12000,
    "sigma_multiplier": 3.0,
    "generated_at": "2026-08-21T07:01:03.284785+00:00"
  },
  "features": {
    "<feature_name>": {
      "mean": 0.07374,
      "std": 0.00196,
      "min": 0.0691,
      "max": 0.0801,
      "normal_range": [0.06787, 0.07961]
    }
  }
}
```

## 실 데이터 검증 결과 (참고용)

CWRU 97.mat(NORMAL, 119윈도우)로 기준선을 만들고, 같은 97.mat 윈도우와
105/118/130.mat(베어링 결함, 총 177윈도우)에 검증 규칙을 적용한 결과:

- NORMAL 윈도우 중 이상치 플래그 비율: 4/119 (3.4%) — 기준선 자체 데이터이므로 낮게 나옴
- 결함 윈도우 중 이상치 플래그 비율: 177/177 (100%)
- 그중 `kurtosis_mean` 단독으로 잡힌 비율: 120/177 (67.8%) — 베어링 결함의 충격성
  성분을 첨도가 잘 반영함을 시사 (자세한 재현은 `../tests/test_week2_pipeline.py` 참고)

이 수치는 CWRU 프로토타입 데이터 기준이며, 실제 센서 데이터 도착 후에는 기준선을
반드시 재산출해야 한다.

## 백엔드 참고 시 주의사항

- 진동(vibration)과 음향(acoustic)은 물리량이 다르므로 기준선도 모달리티별로 분리
  관리해야 한다. 이번 `baseline.json`은 진동(CWRU)만 포함한다.
- `sigma_multiplier`는 오탐/미탐 트레이드오프에 따라 조정 가능한 값이다 (3.0은 출발점).
- 이 기준선은 CWRU 공개 데이터 기반 프로토타입이다. 실제 설비 데이터로 교체 시
  `compute_baseline.py`를 재실행해 `baseline.json`을 갱신해야 한다.
