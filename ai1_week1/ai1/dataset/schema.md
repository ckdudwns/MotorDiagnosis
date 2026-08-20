# 학습·검증 데이터셋 구조 (초안)

기능ID 연관: `ACOUSTIC_LABEL_01`(AI-1 주담당), `INSTALL_POINT_01`(IoT 주담당), `ASSET_MGMT_01`(백엔드 주담당)

## 설계 원칙

- 실시간 관제 파이프라인과 분리된 **분석/학습 전용 데이터**로 관리 (`DATA_PIPELINE_01`과 정합)
- 원시 파형, 특징량, 라벨을 각각 별도 테이블/파일로 관리해 재처리 용이하게 구성
- 기준정보(사이트/설비/설치위치)는 백엔드·IoT 팀의 스키마를 참조(FK)만 하고 중복 저장하지 않음

## 테이블 구조 (초안)

### 1. `raw_samples` — 원시 오디오/진동 샘플

| 필드 | 타입 | 설명 |
|---|---|---|
| `sample_id` | string (PK) | 샘플 고유 ID |
| `asset_id` | string (FK → 설비 관리) | 대상 설비 |
| `install_point_id` | string (FK → 센서 설치 위치 관리) | 센서 설치 위치 |
| `device_id` | string (FK → 장치·센서 매핑) | 수집 장치 |
| `recorded_at` | datetime | 녹음 시각 |
| `sample_rate` | int | 샘플링 레이트 (Hz) |
| `duration_sec` | float | 길이(초) |
| `file_path` | string | 원시 파일 저장 경로/URI |
| `nearby_noise_source` | string (nullable) | 주변 소음원 메모 (혼합 신호 관리용) |

### 2. `features` — 추출된 특징량

| 필드 | 타입 | 설명 |
|---|---|---|
| `feature_id` | string (PK) | 특징량 레코드 ID |
| `sample_id` | string (FK → raw_samples) | 원본 샘플 |
| `feature_version` | string | 추출 로직 버전 (재현성 관리) |
| `rms_mean`, `rms_std`, `zcr_mean` | float | 기본 통계 특징량 |
| `spectral_centroid`, `spectral_bandwidth`, `spectral_rolloff` | float | 스펙트럴 특징량 |
| `band_*Hz` | float | 대역별 에너지 비율 |
| `mfcc_1` ~ `mfcc_n` | float | MFCC 계수 |

### 3. `labels` — 라벨링 결과

| 필드 | 타입 | 설명 |
|---|---|---|
| `label_id` | string (PK) | 라벨 레코드 ID |
| `sample_id` | string (FK → raw_samples) | 대상 샘플 |
| `label` | string | 라벨 코드 (`NORMAL`, `BEARING_FAULT`, `FRICTION`, `IMBALANCE`, `UNKNOWN`) |
| `labeler_id` | string | 1차 라벨링 담당자 |
| `verified_by` | string (nullable) | 교차 검증/전문가 확인자 |
| `confidence` | string | `expert` / `cross_reviewed` / `pending` |
| `label_criteria_version` | string | 참조한 라벨 기준표 버전 (`labels/acoustic_label_criteria.md`) |

### 4. `dataset_splits` — 학습/검증/테스트 분리

| 필드 | 타입 | 설명 |
|---|---|---|
| `sample_id` | string (FK → raw_samples) | 대상 샘플 |
| `split` | string | `train` / `val` / `test` |
| `split_version` | string | 분할 기준 버전 |

> 분할 시 동일 설비/설치위치의 샘플이 train/test에 섞이지 않도록 설비 단위(group split) 고려.

## 파일 저장 규칙 (초안, 센서 도착 전 로컬 개발용)

```
data/
├── raw/{asset_id}/{sample_id}.wav
├── features/{feature_version}/{sample_id}.json
└── labels/labels.csv
```

## TODO

- [ ] 백엔드 팀과 `asset_id`, `install_point_id`, `device_id` FK 필드명/타입 정합 확인
- [ ] 실제 DB(RDB/NoSQL) 선정 후 스키마를 DDL로 변환
- [ ] `dataset_splits` 분할 전략(설비 단위 vs 시간 단위) 결정
