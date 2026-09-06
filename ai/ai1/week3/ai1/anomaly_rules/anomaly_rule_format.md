# 통계 임계값 · 히스테리시스 규칙 (ANOMALY_RULE_01)

기능ID: `ANOMALY_RULE_01` (3주차 실행순서 4번, AI-1 주담당, AI-2·백엔드 협업)
산출: `anomaly_rule.py` — 설비별 기준선 레지스트리 + 히스테리시스 상태 머신

기능정의: "자산별 정상 기준선과 진동·음향·RPM 임계값을 버전 관리한다. 단발성 스파이크는
지속시간과 히스테리시스로 억제한다. 수용: 임계값 변경 전후 이벤트에 적용 버전이 남는다."
API 매핑: `PUT /api/anomaly/rules/{assetId}`(MVP-012) — "자산별 임계값, 지속시간, 재확인
조건 변경".

## 왜 새로 계산하지 않고 week2를 재사용하는가

week2 `validate_features.py`가 이미 "baseline의 mean/std로 정상범위(mean ± sigma*std)를
벗어난 값을 찾는" `check_outliers()`를 구현하고 검증했다(CWRU 실데이터로 결함 윈도우
177/177 100% 플래그 확인). 이 모듈은 그 로직을 **그대로 가져다 쓰고**, 3주차에 새로
필요한 두 가지만 추가한다:

1. **설비별 기준선 레지스트리** — 지금은 모터 1종류뿐이지만, `asset_id`/`asset_type`별로
   다른 `baseline.json`을 등록할 수 있는 구조.
2. **히스테리시스 상태 머신** — `check_outliers()`는 "이 윈도우 하나가 정상범위를
   벗어났는가"만 판정한다. 그것만으로 이벤트를 만들면 경계값 근처에서 단발성 스파이크가
   이벤트를 깜빡이며 반복 생성한다. 그래서 "진입 임계값"과 "복귀 임계값"을 다르게 두고,
   각각 몇 윈도우 연속돼야 상태가 바뀌는지(지속시간 조건)를 추가한다.

## 히스테리시스 설계

```
sigma_enter = 3.0   # 이상 "진입" 임계값 (baseline.json의 기본 sigma_multiplier와 동일)
sigma_exit  = 2.0   # 정상 "복귀" 임계값 (진입보다 완화)
min_consecutive_enter = 2   # 연속 2윈도우 이상 이탈해야 이상 진입 확정
min_consecutive_exit  = 2   # 연속 2윈도우 이상 정상범위 안이어야 정상 복귀 확정
```

- `sigma_exit < sigma_enter`인 이유: 경계값(3σ) 바로 위/아래를 오가는 값이 있을 때,
  진입과 복귀 기준이 같으면(둘 다 3σ) 매 윈도우 이상↔정상이 뒤집히며 이벤트가
  깜빡인다. 복귀 기준을 낮추면(2σ) 한 번 이상 상태에 들어간 뒤에는 값이 확실히
  안정될 때까지 이상 상태를 유지해 이벤트가 쪼개지지 않는다.
- `min_consecutive_*`(지속시간 조건)는 "단발성 스파이크"를 걸러낸다 — 노이즈로 인한
  1윈도우짜리 이탈은 이상으로 확정하지 않는다.
- 각 윈도우의 최대 편차(`max_deviation_sigma`)는 week2 `feature_deviations()`로
  정상범위 안의 특징값까지 조회해서 구한다. 편차 조회를 위해 기준선의 sigma를
  0으로 덮어쓰지 않는다. 이상 특징 목록은 별도의 `check_outliers()`로 구한다.

### 신호별 저장 범위와의 호환

`signalContext`가 있는 RPM·음향 기준선은 전체 생성본/등록본을 전달한다.
이상 진입은 저장된 `normal_range` 밖에서만 발생하고 경계값은 정상으로 취급한다.
설정을 생략하면 저장된 `sigmaMultiplier`를 진입값으로, 그 2/3을 복귀값으로 사용한다
(기존 기본 3:2 비율 유지). 명시적 설정은 저장 sigma와 진입값이 같아야 하며,
복귀값과 연속 윈도우 수는 조정할 수 있다. 다른 진입값은 새 기준선을 만들어야 한다.

0분산 특징은 저장된 절대 허용오차 밖이면 진입 후보, 범위 안이면 복귀 후보가 된다.
다른 유효 특징이 함께 있으면 그 특징들도 복귀 sigma 미만이어야 복귀한다.
0분산이 포함된 윈도우/이벤트의 최대 sigma는 정의할 수 없어 JSON `null`로 표시하며,
임의 표준편차·Infinity로 대체하거나 이 값을 이벤트 진입 판정에 사용하지 않는다.
무효 윈도우는 기존과 같이 연속 횟수를 끊고 진행 중 이벤트를 종료하지 않는다.
문맥 없는 기존 기준선의 sigma 및 경계 판정은 그대로 유지한다.

## 설비별 기준선 레지스트리 (`AssetBaselineRegistry`)

```python
registry = AssetBaselineRegistry()
registry.register(motor_baseline, asset_type="MOTOR", is_default=True)
registry.register(pump_baseline, asset_id="SITE-01-PUMP-04")  # 특정 설비 예외 등록

entry = registry.resolve(asset_id="SITE-01-MOT-02", asset_type="MOTOR")
# asset_id 우선 조회 -> 없으면 asset_type -> 없으면 default -> 다 없으면 KeyError
```

지금은 CWRU 기반 진동 기준선(`week2/ai1/dataset/baseline.json`) 하나만 `asset_type="MOTOR"`
기본값으로 등록해 사용한다. 향후 설비가 여러 종류(모터/펌프/발전기)로 늘어나면 각
설비 유형별로, 나아가 특정 설비 단위로 `register()`를 추가 호출하기만 하면 된다 —
`evaluate_feature_stream()`/`evaluate_asset_stream()`의 로직은 바뀌지 않는다.

## 버전 추적 (수용 기준)

이상 이벤트 구간(`span`)마다 아래 정보를 함께 기록해, "임계값 변경 전후 이벤트에
적용 버전이 남는다"를 만족한다:

```json
{
  "start_index": 119,
  "end_index": 296,
  "// 참고": "인덱스는 예시값 — 정상(0HP 97.mat 119윈도우) 다음 결함 구간에서 이벤트가 열린다",
  "max_deviation_sigma": 12.4,
  "asset_id": "SITE-01-MOT-02",
  "baseline_version": "2026-08-21T08:17:43.221079+00:00",
  "config_version": "v1"
}
```

- `baseline_version`은 `baseline.json`의 `meta.generated_at` (기준선이 재산출되면 바뀜)
- `config_version`은 `AnomalyRuleConfig.version` (임계값/지속시간 설정이 바뀌면 값을
  올려야 함 — `PUT /api/anomaly/rules/{assetId}` 호출 시 백엔드가 이 값을 증가시켜
  저장하는 것을 전제로 한다)

## 대상 확정 후 보완

- `sigma_enter`/`sigma_exit`/`min_consecutive_*`는 CWRU 프로토타입 기준 출발값이다.
  실측 정상·이상 분포로 오탐/미탐 트레이드오프를 다시 튜닝해야 한다.
- 지금은 진동(vibration) 기준선만 있다 — 음향(acoustic)·RPM 임계값은 해당 모달리티
  데이터 확보 후 같은 구조로 추가.
