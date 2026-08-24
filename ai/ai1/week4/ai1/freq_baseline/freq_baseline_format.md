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

## 왜 두 데이터 분할이 서로 다른가

| 후보 모델 | 분할 단위 | 분할 전략 | 이유 |
|---|---|---|---|
| Dense Autoencoder | 윈도우(개별 특징 벡터) | 3주차 `DATA_EXPORT_01`의 라벨별 층화 분할 그대로 재사용 | 순서 무관 — 윈도우 하나하나가 독립 샘플 |
| LSTM Autoencoder | 연속 윈도우 시퀀스(길이 5) | **원본 파일 내 시간순 비중첩 청크**를 앞 70%/다음 20%/뒤 10%로 분할 (셔플 없음) | 시퀀스는 시간적 인접성이 있어야 의미가 있다. 3주차처럼 윈도우 단위로 셔플해 분할하면 같은 시퀀스 안에 서로 다른 split의 윈도우가 섞여 데이터 누수가 생긴다. 시계열 데이터는 시간순 분할이 표준적인 관행이기도 하다. |

즉 Dense 후보는 3주차 매니페스트의 `split` 컬럼을 그대로 쓰고, LSTM 후보는 이 모듈이
CWRU 원본 파일 순서에서 직접 만든 별도 분할을 쓴다. 이 차이는 의도된 것이며 두 후보의
`splitStrategy` 필드에 각각 명시한다.

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

## 평가 지표·오류 사례

test split에서 `predicted = error > threshold` vs `true = (common_label == "ANOMALY")`로
혼동행렬(TP/FP/TN/FN)과 precision/recall/f1/accuracy를 계산한다. 오류 사례
(`errorCases`)는 FP(정상인데 이상으로 오판)와 FN(결함인데 정상으로 오판) 각각
`sample_id`/`known_label`/재구성오차/threshold를 최대 10건까지 기록한다.

## 도메인 차이·현장 보정 계획 (`domainGap` / `fieldCalibrationPlan`)

MVP 기획서(v1.2) "8. AI 및 데이터 기획"의 "AI 보장 범위" 문구를 그대로 반영한다:
이 베이스라인은 **CWRU 공개 데이터 기반**이며, 대상 모터의 고장 유형 분류·RUL 성능은
보장하지 않는다. 정적으로 기록하는 항목:

- 샘플링률: CWRU 12kHz vs 대상 설비 미확정
- 설치 위치: CWRU는 Drive-End 베어링 하우징 고정식 가속도계 vs 대상 설비 미확정
- 운전 조건: CWRU는 파일별 고정 부하(0~3HP)/고정 RPM 근사 vs 대상 설비는 가변 부하·RPM 예상
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
  "metrics": { "bestCandidate": "dense_autoencoder", "...": "..." },
  "errorCases": {"dense_autoencoder": [...], "lstm_autoencoder": [...]},
  "domainGap": {...},
  "fieldCalibrationPlan": [...],
  "createdAt": "..."
}
```

## 대상 확정 후 보완

- 지금은 진동(vibration) 단일 모달리티 — 음향 데이터 확보 후 같은 구조로 별도 후보 추가
- 하이퍼파라미터(은닉 차원, 시퀀스 길이, epoch)는 CWRU 프로토타입 규모(296윈도우)에
  맞춘 값이며, 실측 데이터 규모에 맞춰 재튜닝 필요
- `artifactUri`(학습된 모델 가중치 저장 경로)는 로컬 파일 경로 — 운영 전 오브젝트
  스토리지로 교체
