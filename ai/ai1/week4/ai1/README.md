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
체크섬이 동일함을 확인했다. CWRU 16파일은 크기가 전부 달라 `group_split`이 seed에
의존하지 않으므로, 재현성 반례는 **분할 비율을 바꿔** 체크섬이 달라짐을 확인하는
방식으로 검증한다.

## 2. AI_FREQ_MODEL_01 — Dense/LSTM Autoencoder 베이스라인

3주차 매니페스트 row에는 week2 `extract_all_features()`(RMS/스펙트럴/대역에너지/
kurtosis/MFCC, 26개)에 week1 `compute_peak_frequency()`(`vibration_peak_hz`)를 더한
**27개** 특징이 담긴다(계산 로직 재구현 없음). 다만 리크 없는 `group_split`이 사실상
부하 조건별 분리라 RPM 프록시인 `vibration_peak_hz`는 정규화가 깨진다 — **모델 입력은
이를 뺀 26개**를 쓰고, 매니페스트에는 27개를 그대로 남긴다.

`models.py`의 두 후보 모두 **정상(NORMAL) 데이터만으로 재구성을 학습**하는 비지도
오토인코더 방식이며, 3주차 `group_split`(원본 파일=부하조건 단위) 배정을 그대로 따른다:

- **Dense Autoencoder**: 윈도우 단위. **동결 매니페스트 rows의 inline 특징값을 직접**
  입력으로 쓴다(재윈도우/재계산 없음 — 학습 입력이 `datasetId`가 가리키는 데이터와 일치).
- **LSTM Autoencoder**: 길이 5 연속 윈도우 시퀀스. 원신호를 매니페스트와 같은 윈도우로
  재분할하되, 한 원본 파일의 모든 시퀀스는 그 파일이 배정된 한 split에만 들어간다
  (파일 내부 재분할 없음 — 근거는 `freq_baseline/freq_baseline_format.md`).

후보 선택은 **validation f1**으로만 하고(`selectionCriterion`), 최종 지표는 선택된
후보의 **test** 평가로 분리 보고한다. 임계값은 `mean(validation NORMAL 재구성오차)
+ 3*std` (week2/3주차와 동일한 sigma 관례).

**실행 결과 (실제 CWRU 16파일, 기본 3-way group split, dense=lstm=150 epochs):**

| 후보 | train(NORMAL) | validation | test | 검증 f1 | 테스트 precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| dense_autoencoder | 473 | 413 | 296 | 1.000 | 1.000 | 1.000 | 1.000 |
| lstm_autoencoder | 94 | 80 | 56 | 1.000 | 1.000 | 1.000 | 1.000 |

두 후보 모두 오탐/미탐 0건으로 완전 분리됐다. 단, 이 결과는 `vibration_peak_hz`를
모델 입력에서 제외했을 때다 — 포함하면 부하 조건 간 정규화가 깨져 f1이 0이 된다.
정상 재구성 임계값이 train에 없던 부하 조건에서 보정되지 않는 것은 `domainGap`에
"운전 조건별 임계값 재보정 필요"로 기록했다.

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

- CWRU에서는 자산 = 부하 조건이라 group split의 각 split이 서로 다른 운전 조건이다.
  RPM 결합 특징(`vibration_peak_hz`)을 모델 입력에서 제외하고 운전 조건별 임계값
  재보정을 도메인갭으로 기록했다 — 현장에서 다양한 조건의 자산이 쌓이면 해소된다
- LSTM 후보는 CWRU 규모에서 시퀀스 청크 수가 적어(train NORMAL 94개) Dense보다 표본이
  적다 — 실측 데이터로 늘어나면 재검증 필요
- 음향(acoustic) 모달리티는 아직 없음 — MIMII 데이터 확보 후 같은 구조로 별도 후보 추가
- `artifactUri`는 로컬 파일을 가리키는 표준 `file://` URI(`Path(...).resolve().as_uri()`)
  — 운영 전 오브젝트 스토리지 URI로 교체
- 대상 모터·센서 확정 후 전이학습, 임계값 재보정, 드리프트 모니터링 진행
  (`freq_baseline/freq_baseline_format.md`의 "현장 보정 계획" 참고)
