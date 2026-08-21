# AI-1 — 2주차 (EDGE_FEATURE_01: 특징량 계산)

Bind Edge AI 프로젝트 2주차, AI-1 담당 영역.
담당 기능: `EDGE_FEATURE_01`(특징량 계산). 이번 주 목표: **정상 기준선과 특징값 품질 점검**.

1주차 인계 파이프라인(`../../week1/ai1/`)은 그대로 유지하고, 2주차 작업은 이 폴더에 추가한다.
CWRU/MIMII 원본 데이터와 로더(`load_cwru_vibration.py`, `load_mimii_acoustic.py`)는
1주차 경로(`ai/ai1/week1/ai1/`)의 것을 그대로 재사용하며 복제하지 않는다.

## 폴더 구조

```
ai/ai1/week2/ai1/
├── feature_extraction/
│   ├── extract_features.py     # 1주차 버전 + kurtosis(첨도) 추가
│   └── validate_features.py    # 특징값 품질 검증 (결측/무효값, 기준선 대비 이상치)
├── scripts/
│   └── compute_baseline.py     # NORMAL(97.mat) 기준선 산출 스크립트
├── dataset/
│   ├── baseline.json           # 산출된 정상 기준선 (mean/std/normal_range, 특징값별)
│   └── validation_rules.md     # 검증 규칙 문서 (TELEMETRY_VALIDATE_01 참고자료)
└── tests/
    └── test_week2_pipeline.py  # 실제 CWRU 데이터로 전체 흐름 검증
```

## 1. kurtosis(첨도) 추가

`feature_extraction/extract_features.py`에 `compute_kurtosis()`를 추가했다.
기존 `compute_rms`/`compute_zero_crossing_rate`와 같은 프레임 단위 스타일로 작성했고,
Fisher 정의(초과 첨도, excess kurtosis — 정규분포는 0에 가까움)를 numpy만으로 직접
구현해 라이브러리 의존성을 늘리지 않았다. 베어링 결함처럼 충격성(impulsive) 파형이
섞이면 값이 커지는 특성이 있어 이상탐지에 유용하다.

`extract_all_features()` 반환 dict에 `kurtosis_mean`/`kurtosis_std`가 추가된다.

## 2. 정상 기준선 산출 (`compute_baseline.py`)

CWRU `97.mat`(NORMAL) 데이터를 `load_cwru_vibration.py`로 로드해 윈도우(2048샘플)
단위로 분할하고, 각 윈도우에서 `extract_all_features()`로 특징값을 뽑은 뒤
특징값별 평균/표준편차와 정상범위(`mean ± sigma*std`, 기본 `sigma=3`)를 계산한다.

```bash
python ai/ai1/week2/ai1/scripts/compute_baseline.py
# 옵션: --data-dir, --window-size, --hop-size, --sigma, --output
```

결과: `ai/ai1/week2/ai1/dataset/baseline.json`.

**실행 결과 (실제 CWRU 97.mat, librosa 설치 환경, 2026-08-21 기준):** NORMAL
윈도우 119개로 26개 특징값(rms_mean/std, zcr_mean, kurtosis_mean/std,
spectral_centroid/bandwidth/rolloff, band_energy 5구간, mfcc 13개)의 기준선을
산출했다.

- `rms_std`/`kurtosis_std`는 윈도우 크기와 프레임 크기가 같아서(2048=2048)
  프레임 내 분산이 0으로 나온다 — 계산 오류가 아니라 "윈도우당 프레임 1개"
  구조에서 나오는 자연스러운 결과다 (프레임 여러 개가 필요한 통계라
  프레임이 1개면 std가 항상 0).
- `mfcc_1~13`은 librosa로 정상 계산된 실제 값이다 (예: `mfcc_1` mean=-151.41,
  std=3.87 / `mfcc_2` mean=124.63, std=4.00 — 전체 값은 `dataset/baseline.json`
  참고). **librosa가 설치되지 않은 환경에서 `compute_baseline.py`를 실행하면
  MFCC가 0벡터로 대체되므로**(`extract_features.py`의 `compute_mfcc()` fallback),
  `feature_extraction/requirements.txt`의 librosa가 실제로 설치돼 있는지 먼저
  확인한 뒤 재실행해야 한다.

## 3. 특징값 품질 검증 (`validate_features.py`)

두 단계로 분리했다 (자세한 근거는 [`dataset/validation_rules.md`](./dataset/validation_rules.md) 참고):

1. **결측/무효값 검사** — `None`/`NaN`/`Inf` → 거부(reject) 대상 (데이터 품질 문제)
2. **기준선 대비 이상치 검사** — `baseline.json` 기준 정상범위(`mean ± sigma*std`)를
   벗어난 값 → 플래그(flag)만, 저장은 유지 (실제 이상탐지 후보이므로 버리면 안 됨)

```python
from validate_features import validate_features, load_baseline

baseline = load_baseline("ai1/dataset/baseline.json")
report = validate_features(features, baseline)
# report = {"is_valid": bool, "missing_or_invalid": [...], "outliers": [...]}
```

`TELEMETRY_VALIDATE_01`(백엔드)이 언어 상관없이 규칙만 재구현할 수 있도록
[`dataset/validation_rules.md`](./dataset/validation_rules.md)에 규칙과 `baseline.json`
스키마를 정리해 두었다.

## 테스트 (실제 CWRU 데이터 기준)

`tests/test_week2_pipeline.py`를 `ai/ai1/week1/ai1/data/external/cwru/`의 실제
97/105/118/130.mat 데이터로 실행해 확인했다:

```bash
python ai/ai1/week2/ai1/tests/test_week2_pipeline.py
```

- kurtosis 필드가 `extract_all_features()` 결과에 정상 포함됨
- 97.mat(NORMAL) 119윈도우로 기준선 산출 → 정상범위가 평균을 포함하는 유효한 구간으로 계산됨
- 기준선(NORMAL) 대비 검증 시:
  - NORMAL 윈도우 자기 자신의 이상치 플래그 비율: **4/119 (3.4%)**
  - 결함 윈도우(105/118/130.mat, 총 177개)의 이상치 플래그 비율: **177/177 (100%)**
  - 그중 `kurtosis_mean` 단독으로 잡힌 비율: **120/177 (67.8%)**
- NaN/Inf를 인위적으로 주입한 값이 `missing_or_invalid`로 정확히 검출됨

정상/결함 데이터의 이상치 플래그 비율이 뚜렷하게 갈리는 것으로, 기준선과 검증
로직이 실제로 이상탐지 신호로 기능함을 확인했다.

## 다음 단계

- 실제 센서 데이터 도착 후 `compute_baseline.py`를 재실행해 `baseline.json` 갱신
- 음향(acoustic) 모달리티용 기준선 별도 산출 (현재는 진동만 포함)
- AI-2 `DASH_SIGNAL_01`(실시간 신호 차트)에 `baseline.json`의 `normal_range`를
  참조선으로 연동
- 백엔드 `TELEMETRY_VALIDATE_01`이 `dataset/validation_rules.md` 규칙을 검증
  파이프라인에 반영했는지 확인
