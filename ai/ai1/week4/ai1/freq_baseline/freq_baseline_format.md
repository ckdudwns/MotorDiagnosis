# 기존 데이터셋 기반 주파수 AI 선행학습 (AI_FREQ_MODEL_01)

기능ID: `AI_FREQ_MODEL_01` (4주차 실행순서 2번, AI-1 주담당, AI-2·IoT 협업)
산출: `features.py`(특징 벡터) + `models.py`(Autoencoder/LSTM 후보) + `train_and_evaluate.py`
(학습·평가·보고서 생성)

기능정의(W4.2): "기존 공개·보유 음향·진동 데이터셋을 정제하고 샘플링률, 단위, 라벨 체계를
통일한 뒤 FFT·스펙트럼 특징, RMS, 피크, 대역 에너지로 이상 탐지 베이스라인 모델을
선행학습·검증한다. Autoencoder·LSTM 후보를 비교하고... 수용: 데이터셋 출처·라이선스·
운전 조건, 베이스라인 지표, 오류 사례, 도메인 차이와 현장 추가 수집·보정 항목을 보고한다."

API 매핑: `POST/GET /api/training-jobs`(FUT-008/009) — 이 모듈의 출력은 `GET
/api/training-jobs/{jobId}` 응답 형태(`status, candidates, metrics, errorCases, domainGap,
fieldCalibrationPlan`)를 그대로 따른다.

## 특징 벡터 — 새로 정의하지 않고 이미 있는 것만 합침

"FFT·스펙트럼 특징, RMS, 피크, 대역 에너지"는:

- RMS, 스펙트럴 centroid/bandwidth/rolloff, 대역 에너지 5구간, kurtosis, MFCC 13개 →
  week2 `extract_all_features()`가 이미 계산 (재사용, 재구현 안 함)
- 피크 주파수(peak_hz) → week1 `build_ai1_handoff_dataset.py`의 `compute_peak_frequency()`를
  재사용 (`extract_all_features()`에는 없는 필드라 이 모듈에서 결합만 함)

`features.py`는 이 둘을 합쳐 **고정된 순서의 숫자 벡터**로 변환하는 `vectorize()`만
추가한다 (모델 입력은 순서가 고정된 벡터/텐서여야 하므로).

## 데이터 분할 — specimen 독립이 원칙, CWRU 데모는 비독립 홀드아웃

**중요 한계 (리뷰 P1 반영).** CWRU 부하별 `.mat` 4개(0/1/2/3 HP)는 서로 다른 자산이
아니라 **같은 물리 베어링(specimen)**을 부하만 바꿔 측정한 파일 묶음이다. 데이터셋
계층(`DATASET_MODEL_01`)의 기본 분할은 물리 specimen 단위(`specimen_group`)이고,
CWRU는 **건강한 베어링이 1개뿐**이라(NORMAL specimen 1개) 3-way specimen 독립 분할이
불가능하다 — `build_manifest` 기본값은 `InsufficientAssetGroupsError`로 정직하게 실패한다.

이 문서의 베이스라인 실행 결과는 **`operating_condition_holdout`**(부하조건 기준:
test=0HP, validation=1HP, train=2·3HP)으로 얻은 것이며, 같은 물리 베어링이
train/validation/test에 함께 들어간다. 따라서 **`independentHoldout=false`**이고,
지표는 "specimen 독립 일반화 성능"이 아니라 **운전조건 기준 in-distribution 평가**다.
결함 클래스(IR/Ball/OR)는 0.007/0.014/0.021" 3개 specimen을 확보해 결함 간에는 specimen
독립 분할이 가능하지만, NORMAL 제약으로 전체 3-way는 여전히 불가하다.

두 후보 모두 동결 매니페스트 rows의 inline 특징값만 입력으로 쓴다(원본 재로드·재계산 없음):

| 후보 모델 | 입력 단위 | 특징 소스 |
|---|---|---|
| Dense Autoencoder | 윈도우(개별 특징 벡터) | 동결 매니페스트 rows의 inline 특징값 직접 사용 |
| LSTM Autoencoder | 연속 윈도우 시퀀스(길이 5) | 동결 rows를 `sample_id` 윈도우 순번으로 정렬해 시퀀스로 묶음 (한 파일의 모든 시퀀스는 한 split에만) |

`run_training_job`은 학습 시작 전에 `verify_frozen_integrity()`로 동결본이 변조되지
않았는지(snapshotDigest 재계산·비교) 확인하고, artifact는 작업별 유일 `job_id` 아래
불변 경로에 저장하며(`<artifact_dir>/<job_id>/<name>.pt`, 덮어쓰기 금지) 보고서에
`artifactChecksum`을 함께 남긴다.

### 모델 입력 특징 — 27개 중 26개

매니페스트 row에는 27개 특징이 있지만 **모델 입력은 `vibration_peak_hz`를 뺀 26개**다.
`vibration_peak_hz ≈ 회전 주파수 = RPM/60`이고 CWRU는 부하조건별 RPM이 고정
(0HP≈1797 / 3HP≈1730)이다. `operating_condition_holdout`은 바로 그 부하조건 기준 분할이라
이 특징의 값 범위가 **train과 validation에서 이미 겹치지 않는다**(z-score 발산 → 재구성
오차·임계값 보정 붕괴). 이 제외 결정은 **train/validation 관찰 + 물리 근거**로 했고
test 평가 전에 동결했다 — test split은 이 결정에 쓰지 않았다. 데이터셋 산출물은 완전해야
하므로 매니페스트에는 27개를 남기고 모델 입력만 좁혔다.
`candidate.normalization.featureOrder`에 실제 사용한 26개 순서가 기록된다.

## 학습 방식 — 비지도 이상탐지 (Autoencoder)

두 후보 모두 **train split의 NORMAL 샘플만으로** 재구성(reconstruction) 학습한다 —
정상 신호의 패턴만 배워서, 결함 신호가 들어오면 재구성 오차가 커지는 원리(전형적인
오토인코더 기반 비지도 이상탐지). `common_label`(ANOMALY) 라벨은 학습에 쓰지 않고
평가에만 쓴다 — 실제 현장에서도 결함 라벨은 희소하므로 이 구조가 현실적이다.

- **Dense Autoencoder**: 입력 특징 벡터(26차원) → 인코더(16→8) → 디코더(16→26).
  개별 윈도우 단위 재구성 오차(MSE)로 판정.
- **LSTM Autoencoder**: 길이 5 시퀀스(5×26) → LSTM 인코더 → 마지막 은닉상태를
  시퀀스 길이만큼 반복 → LSTM 디코더 → 선형층으로 26차원 복원. 시퀀스 전체 평균
  재구성 오차로 판정.

## 임계값 — week2/ANOMALY_RULE_01과 같은 sigma 관례

```
threshold = mean(validation set의 NORMAL 재구성오차) + 3 * std(같은 것)
```

`ANOMALY_RULE_01`(3주차)이 `baseline.json`의 mean±3σ를 정상범위로 쓴 것과 같은
관례를 재구성 오차에도 그대로 적용했다 — 프로젝트 전체에서 "정상 분포에서 3σ
벗어나면 이상"이라는 하나의 일관된 기준을 쓴다.

## 후보 선택 vs 최종 평가 (분리)

- **선택**: 두 후보를 **validation split 지표(f1)**로만 비교해 최적 후보를 고른다
  (`select_best`, `metrics.selectionCriterion == "validation_f1"`). 각 후보의
  `validationMetrics`에 검증 지표가 담긴다.
- **최종 평가**: 선택된 후보에 대해서만 **test split**으로 혼동행렬과
  precision/recall/f1/accuracy를 계산해 `metrics`에 보고한다.
- 예전에는 test f1으로 후보를 고르고 같은 값을 최종 성능으로 보고해 test 데이터가
  모델 선정에도 쓰였다(낙관 편향). 이제 선택과 최종 평가의 데이터가 분리된다.

test split에서 `predicted = error > threshold` vs `true = (common_label == "ANOMALY")`로
계산한다. 오류 사례(`errorCases`)는 FP(정상인데 이상으로 오판)와 FN(결함인데 정상으로
오판) 각각 `sample_id`/`known_label`/재구성오차/threshold를 최대 10건까지 기록한다.

## 모델 아티팩트 (`.pt`) — 정규화 상태 포함, 작업별 불변 경로

각 후보의 `.pt`는 가중치만이 아니라 학습 당시 입력 변환을 복원할 수 있는 dict를
저장한다: `model_type`, `state_dict`, `input_dim`, `seq_len`, `feature_names`(26개
순서), `scaler_mean`/`scaler_std`(z-score 정규화 상태), `threshold`, `sigma`.

**작업별 불변 경로 (리뷰 P1).** artifact는 `<artifact_dir>/<job_id>/<name>.pt`에
저장되고 `job_id`는 작업마다 유일하다(`TJ-<UTC타임스탬프>-<uuid8>`). 같은 경로가
이미 존재하면 저장을 거부한다 — 다음 학습이 이전 모델을 덮어써 보고서의
`artifactUri`가 나중 모델을 가리키는 문제를 막는다. 보고서의 각 candidate에는 파일
sha256(`artifactChecksum`)이, 최상위에는 검증에 쓴 `datasetSnapshotDigest`가 기록된다.

**추론 스키마 검증 (리뷰 P1).** `score_from_artifact(path, matrix, *, input_feature_names)`는
입력 열 이름을 **필수**로 받아 artifact에 저장된 학습 시점 순서와 대조한다: 특징
집합이 다르면 거부, 순서만 다르면 이름 기준 재정렬, 열 개수 불일치·NaN/Inf도 거부.
이름 없이 shape만 맞는 입력을 넘겨 열이 뒤바뀌어도 조용히 다른 판정이 나오던 문제를
막는다. 재로딩 전후 판정이 학습 때 (비독립) test 지표와 일치함을 테스트로 검증한다.

## 도메인 차이·현장 보정 계획 (`domainGap` / `fieldCalibrationPlan`)

MVP 기획서(v1.2) "8. AI 및 데이터 기획"의 "AI 보장 범위" 문구를 그대로 반영한다:
이 베이스라인은 **CWRU 공개 데이터 기반**이며, 대상 모터의 고장 유형 분류·RUL 성능은
보장하지 않는다. 정적으로 기록하는 항목:

- 샘플링률: CWRU 12kHz vs 대상 설비 미확정
- 설치 위치: CWRU는 Drive-End 베어링 하우징 고정식 가속도계 vs 대상 설비 미확정
- 운전 조건: CWRU는 파일별 고정 부하(0~3HP)/고정 RPM 근사 vs 대상 설비는 가변 부하·RPM 예상
  - **이 베이스라인 지표는 `operating_condition_holdout`(부하조건 기준) 평가이며 specimen
    독립 검증이 아니다** — 같은 물리 베어링이 train/validation/test에 함께 들어간다.
    CWRU는 건강한 베어링이 1개뿐이라 specimen 독립 holdout 자체가 불가능하다. 정상
    재구성 임계값이 train에 없던 부하 조건에서는 보정되지 않고, RPM 결합 특징
    (`vibration_peak_hz`)은 모델 입력에서 제외했다. 현장 데이터 또는 추가 독립 베어링
    확보 후 specimen 독립 평가로 재검증하고 운전 조건별 임계값을 재보정해야 한다
    (`domainGap.operatingConditions.note`, `fieldCalibrationPlan`에 기록).
- 라벨: CWRU 베어링 결함 3종 vs 대상 설비 고장 유형 미확정 (도서발전소 환경 특유의
  염분·진동원 혼입 가능성)

`fieldCalibrationPlan`은 MVP 기획서 "11. 후속 로드맵" 항목을 그대로 가져와 체크리스트
형태로 정리한다 (전이학습, 임계값 재보정, 오탐 추적, 드리프트 점검).

## 출력 형식 (`GET /api/training-jobs/{jobId}` 응답)

```json
{
  "id": "TJ-CWRU-VIBRATION-...",
  "datasetId": "DS-CWRU-VIBRATION-...",
  "status": "completed",
  "candidates": [
    {"name": "dense_autoencoder", "splitStrategy": "...", "metrics": {...}},
    {"name": "lstm_autoencoder", "splitStrategy": "...", "metrics": {...}}
  ],
  "datasetSnapshotDigest": "sha256:...",
  "metrics": {
    "bestCandidate": "dense_autoencoder",
    "selectionCriterion": "validation_f1",
    "independentHoldout": false,
    "holdoutType": "operating_condition",
    "evaluation": "operating_condition_holdout — NOT specimen-independent",
    "validation": {"...": "..."}, "f1": 0.0, "...": "..."
  },
  "errorCases": {"dense_autoencoder": [...], "lstm_autoencoder": [...]},
  "domainGap": {...},
  "fieldCalibrationPlan": [...],
  "createdAt": "..."
}
```

## 대상 확정 후 보완

- 지금은 진동(vibration) 단일 모달리티 — 음향 데이터 확보 후 같은 구조로 별도 후보 추가
- 하이퍼파라미터(은닉 차원, 시퀀스 길이, epoch)는 CWRU 규모(40파일 / 10 물리 specimen,
  약 3000윈도우)에 맞춘 값이며, 실측 데이터 규모에 맞춰 재튜닝 필요
- **specimen 독립 평가**: NORMAL 독립 베어링(현장 정상 데이터 또는 추가 CWRU 베이스라인)
  확보 후 `split_strategy="specimen_group"`로 재검증 — 그때까지 이 지표는 일반화 성능이 아님
- `artifactUri`(학습된 모델 가중치 저장 경로)는 로컬 파일 경로를 표준 file URI로
  변환한 값(`Path(...).resolve().as_uri()` → `file:///C:/...` 또는 `file:///...`)
  이다. Windows 경로에 `file://`를 그대로 붙인 비표준 URI는 백엔드 모델 등록에서
  `400 INVALID_ARTIFACT_URI`로 거부된다. 운영 전 오브젝트 스토리지 URI로 교체
