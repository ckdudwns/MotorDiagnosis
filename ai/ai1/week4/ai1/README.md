# AI-1 — 4주차 (통합 시연·AI 학습 준비 / 기존 데이터셋 베이스라인)

Bind Edge AI 프로젝트 4주차, AI-1 담당 영역. 4주차 기능정의서(W4.2, 2026-08-24)와
API 명세서(v1.2) 기준으로 작업했다. "선행일정" 시트 순서 그대로 진행했다:

| 순서 | 기능ID | 폴더 | 상태 |
|---|---|---|---|
| 1 | `DATASET_MODEL_01` | `dataset_versions/` | 완료 |
| 2 | `AI_FREQ_MODEL_01` | `freq_baseline/` | 완료 |

1~3주차 인계 파이프라인(`../../week1/ai1/`, `../../week2/ai1/`, `../../week3/ai1/`)은
그대로 유지하고 복제하지 않는다. 4주차는 3주차 `DATA_EXPORT_01`(`register_dataset.py`)이
정규화한 CWRU 매니페스트를 동결·승인하고(`DATASET_MODEL_01`), 그 위에서 실제 베이스라인
모델을 학습·평가한다(`AI_FREQ_MODEL_01`).

## 폴더 구조

```
ai/ai1/week4/ai1/
├── dataset_versions/                # DATASET_MODEL_01
│   ├── dataset_version_format.md
│   ├── dataset_version.py           # draft -> frozen -> approved 상태 머신 + 체크섬 재현성
│   └── model_version.py             # ModelVersion/BaselineVersion 등록·승인·롤백
├── freq_baseline/                   # AI_FREQ_MODEL_01
│   ├── freq_baseline_format.md
│   ├── features.py                  # FFT/스펙트럼/RMS/피크/대역에너지 특징 벡터
│   ├── models.py                    # DenseAutoencoder / LstmAutoencoder / FeatureScaler
│   ├── train_and_evaluate.py        # 학습·평가·training-job 보고서 생성
│   └── requirements.txt
└── tests/
    ├── test_dataset_version.py
    └── test_freq_baseline.py
```

## 1. DATASET_MODEL_01 — 데이터셋·모델·기준선 버전 관리

3주차 `DATA_EXPORT_01`이 만든 `draft` 매니페스트를 새로 계산하지 않고 그대로 가져와
`draft → frozen → approved` 상태 머신으로 동결·승인한다. 동결 시점에 `rows`/`labelMapping`/
`split`만으로 `datasetChecksum`(sha256)을 계산해 저장하고, 나중에 같은 `seed`로 다시
만든 매니페스트와 비교해 "동일 데이터셋 버전을 재현할 수 있다"는 수용 기준을 검증한다.

`model_version.py`는 API 명세서 FUT-005~007(모델 버전 등록/승인/롤백), FUT-010~011
(기준선 버전 등록/승인)을 데이터 구조로 옮겼다. 롤백은 `approved` 상태의 대상으로만
허용하고, 기준선은 `approved`가 아니면 `activate`할 수 없게 해 "승인 전 배포 금지"를
코드 레벨에서 강제한다.

**실행 결과 (실제 CWRU 데이터, seed=42):** 같은 seed로 매니페스트를 두 번 생성해
체크섬이 동일함을 확인했고, 다른 seed(99)로 생성하면 체크섬이 달라짐을 함께 확인해
체크섬이 실제로 내용 변화에 민감하게 반응하는 것을 검증했다.

## 2. AI_FREQ_MODEL_01 — Dense/LSTM Autoencoder 베이스라인

`features.py`가 week2 `extract_all_features()`(RMS/스펙트럴/대역에너지/kurtosis/MFCC)와
week1 `compute_peak_frequency()`(피크 주파수)를 합쳐 **27차원** 고정 순서 특징 벡터를
만든다(계산 로직 재구현 없음). `models.py`의 두 후보 모두 **정상(NORMAL) 데이터만으로
재구성을 학습**하는 비지도 오토인코더 방식이다:

- **Dense Autoencoder**: 윈도우 단위 특징 벡터(27차원) 재구성. 3주차 `DATA_EXPORT_01`의
  라벨별 층화 분할(window-level)을 그대로 재사용.
- **LSTM Autoencoder**: 길이 5 연속 윈도우 시퀀스 재구성. 시퀀스는 시간적 인접성이
  필요해 3주차 분할을 못 쓰므로, **파일별 시간순 비중첩 청크**를 셔플 없이 앞 70%/
  다음 20%/뒤 10%로 나누는 별도 분할을 썼다 (데이터 누수 방지 — 근거는
  `freq_baseline/freq_baseline_format.md` 참고).

임계값은 `mean(validation NORMAL 재구성오차) + 3*std`로, week2 `baseline.json`/3주차
`ANOMALY_RULE_01`과 동일한 sigma 관례를 재구성 오차에도 그대로 적용해 프로젝트 전체가
일관된 "정상분포에서 3σ 벗어나면 이상" 기준을 쓰도록 했다.

**실행 결과 (실제 CWRU 데이터, 296윈도우, dense_epochs=lstm_epochs=150):**

| 후보 | train(NORMAL만) | validation | test | threshold | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| dense_autoencoder | 83 | 60 | 30 | 1.113 | 1.000 | 1.000 | 1.000 |
| lstm_autoencoder | 16 | 11 | 5 | 1.027 | 1.000 | 1.000 | 1.000 |

두 후보 모두 test split에서 오탐/미탐 0건으로 완전 분리됐다 — week2(결함 윈도우
100% 이상치 플래그)·3주차(정상→결함 경계에서 이벤트 1개 안정적 유지)에서 이미
확인된 CWRU 베어링 결함의 강한 분리도와 일치하는 결과다. LSTM 후보는 시퀀스 청크
특성상 표본 수가 훨씬 적어(train 16개) 통계적 신뢰도는 Dense 후보보다 낮다 — 아래
"대상 확정 후 보완" 참고.

`domainGap`/`fieldCalibrationPlan`은 MVP 기획서(v1.2) "AI 보장 범위"·"11. 후속
로드맵" 문구를 그대로 반영해, 이 베이스라인이 CWRU 공개 데이터 기반이며 대상 모터의
고장 유형·RUL 성능을 보장하지 않는다는 점과 현장 전이학습·임계값 재보정 계획을
`training-job` 보고서에 함께 담는다.

```bash
python ai/ai1/week4/ai1/freq_baseline/train_and_evaluate.py
# 결과: ai/ai1/week4/ai1/data/training_job_report.json (+ data/models/*.pt)
```

## 테스트 실행

```bash
python ai/ai1/week4/ai1/tests/test_dataset_version.py
python ai/ai1/week4/ai1/tests/test_freq_baseline.py
```

CWRU 실데이터가 없는 환경에서도 합성(fixture) 데이터 기반 테스트(상태 머신 규칙,
지표 계산, 모델 forward/학습 동작)는 항상 실행되고, 실데이터 전용 테스트는
`unittest.skipUnless`로 명시적으로 skip 처리된다(1~3주차와 동일한 패턴).

## 대상 확정 후 보완

- LSTM 후보는 CWRU 규모(파일당 윈도우 59~119개)에서 시퀀스 청크 수가 적어(train
  NORMAL 16개) 지표 신뢰도가 낮다 — 실측 데이터로 표본이 늘어나면 재검증 필요
- 음향(acoustic) 모달리티는 아직 없음 — MIMII 데이터 확보 후 같은 구조로 별도 후보 추가
- `artifactUri`는 로컬 파일 경로 — 운영 전 오브젝트 스토리지로 교체
- 대상 모터·센서 확정 후 전이학습, 임계값 재보정, 드리프트 모니터링 진행
  (`freq_baseline/freq_baseline_format.md`의 "현장 보정 계획" 참고)
