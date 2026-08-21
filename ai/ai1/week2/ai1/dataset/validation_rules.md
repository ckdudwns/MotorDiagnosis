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

아래 순서로 검사한다 (구현: `check_missing_or_invalid()`). 하나라도 걸리면 해당
레코드는 `is_valid = false`.

1. **빈 dict**: 특징값 dict가 완전히 비어 있으면(`{}`), 다른 검사는 하지 않고
   `reason=empty_features` 이슈 **하나만** 보고하고 즉시 종료한다. (키 개수만큼
   `missing_key`를 보고하지 않는다 — 빈 dict는 그 자체로 하나의 이슈로 취급)
2. **필수 키 누락** (baseline이 주어졌을 때만): baseline.json의 `features`에
   정의된 키가 입력 dict에 하나라도 없으면, 없는 키마다 `reason=missing_key`를
   보고한다. baseline을 주지 않으면 이 검사는 건너뛴다 (필수 키 개념 자체가
   baseline에서 나오기 때문).
3. 입력에 실제로 존재하는 각 값에 대해:
   - 값이 `null`/`None` → `reason=missing`
   - 값이 숫자(int/float)가 아님 → `reason=invalid_type`
   - 값이 숫자이고 `NaN` → `reason=nan`
   - 값이 숫자이고 `Inf`/`-Inf` → `reason=inf`

### bool 처리 주의 (invalid_type 판정 시 필수)

`invalid_type` 판정에서 "숫자인지" 검사할 때 **bool을 명시적으로 제외해야 한다.**
Python은 `bool`이 `int`의 서브클래스라 `isinstance(True, (int, float))`가
`True`로 나오는 함정이 있다 — 다른 언어에서도 참/거짓 값이 암묵적으로
0/1로 캐스팅되는 경우 동일한 함정이 있을 수 있으니 유의한다.

의사코드:

```
is_number(value):
    return (typeof value is int OR typeof value is float) AND (typeof value is NOT bool)
```

즉 `True`/`False`는 숫자가 아니라 `invalid_type`으로 거부해야 한다
(구현: `is_valid_number()` 헬퍼, `check_missing_or_invalid`/`check_outliers`
양쪽에서 공통으로 사용).

### reason 요약표

| reason | 트리거 조건 | baseline 필요 여부 |
|---|---|---|
| `empty_features` | 특징값 dict가 완전히 비어 있음 | 불필요 |
| `missing_key` | baseline에 정의된 키가 입력에 없음 | 필요 (없으면 이 검사 생략) |
| `missing` | 값이 `null`/`None` | 불필요 |
| `invalid_type` | 값이 숫자가 아님 (문자열, bool 포함) | 불필요 |
| `nan` | 값이 숫자이고 `NaN` | 불필요 |
| `inf` | 값이 숫자이고 `Inf`/`-Inf` | 불필요 |

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
- 숫자가 아닌 값(문자열, bool 포함)도 비교 대상에서 제외한다 — 규칙 1과 동일한
  `is_number()` 판정(위 bool 함정 포함)을 여기서도 그대로 적용해야 한다.

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
