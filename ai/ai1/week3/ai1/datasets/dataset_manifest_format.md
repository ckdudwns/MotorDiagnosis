# 데이터셋 등록 매니페스트 스키마 (DATA_EXPORT_01)

기능ID: `DATA_EXPORT_01` (3주차 실행순서 1번, AI-1 주담당)
산출 스크립트: `register_dataset.py`(매니페스트 생성) → `export_dataset.py`(CSV/XLSX 내보내기)

이번 주 목표는 **기존 공개·보유 데이터셋(CWRU Bearing Dataset)을 선정하고, 출처·라이선스·
샘플링률·단위·라벨을 정규화해 4주차 `AI_FREQ_MODEL_01` 선행학습 입력을 준비하는 것**이다.
API 명세서 v1.2의 `09_보완API상세` 시트 `POST /api/datasets`(MVP-042) / `GET /api/datasets/{id}`
(MVP-043) 계약을 그대로 따른다.

## 왜 CWRU만 쓰는가

1주차 인계 문서(`ai/ai1/week1/ai1/README.md`)에서 이미 확인했듯 이 저장소에는 MIMII 음향
데이터의 실제 파일(`.wav`)이 배치되어 있지 않다(`ai/ai1/week1/ai1/scripts/load_mimii_acoustic.py`
로더 코드만 존재). 따라서 이번 3주차 데이터셋 등록·내보내기는 **CWRU Bearing Dataset(진동)만
대상으로 한다.** MIMII가 추가되면 같은 스키마로 `modality: "acoustic"` 데이터셋 버전을 별도
등록하면 된다(아래 "TODO" 참고).

## 매니페스트 필드 (`POST /api/datasets` 요청과 1:1 대응)

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | string | 데이터셋 버전 ID (`DS-CWRU-VIBRATION-<날짜>-<버전체크섬 12자리>` 형식) — 같은 날짜라도 입력 파일/윈도우/분할 설정이 다르면 다른 ID가 나온다 |
| `name` | string | 데이터셋 이름 (`cwru-bearing-vibration-v1`) |
| `source.type` | string | `external` (외부 공개 데이터셋) |
| `source.uri` | string | CWRU Bearing Data Center 공식 URL |
| `source.license` | string | 라이선스/이용 조건 메모 (학술 공개, 재배포 시 출처 표기) |
| `source.files` | object | 실제 배치된 원본 파일별 `{sha256, label}` — **체크섬으로 원본 추적** |
| `source.checksum` | string | 원본 파일 체크섬들 + window/hop 크기 + 분할 비율 + seed로 만든 불변 버전 체크섬(`compute_version_checksum()`) — `id`의 접미사와 동일 값 |
| `compatibility.signalType` | string[] | `["vibration"]` |
| `compatibility.samplingRateHz` | number | `12000` (CWRU Drive-End 12kHz) |
| `compatibility.units` | object | `{"vibration": "g (raw accelerometer output, uncalibrated)"}` |
| `compatibility.operatingConditions` | object | `{"rpmRange": [min, max], "load": ...}` — 파일 메타데이터의 RPM 실측값 기반 |
| `labelTaxonomyVersion` | string | `CWRU-FAULT-V1` |
| `labelMapping` | object | CWRU 원본 라벨 → 공통 학습 라벨(`NORMAL`/`ANOMALY`) |
| `split` | object | `{"train":0.7,"validation":0.2,"test":0.1}` |
| `splitStrategy` | string | 분할 전략 설명 (아래 "분할 전략" 참고) |
| `status` | string | `draft` (승인 전) |
| `reason` | string | 등록 사유 |
| `createdAt` | ISO8601 | 생성 시각 |
| `rowCount` / `splitCounts` | number / object | 변환 전후 건수 검증용 |
| `rows` | array | 윈도우 단위 정규화 레코드 (아래 "행 스키마") — CSV/XLSX로 내보낼 실제 데이터 |

`GET /api/datasets/{id}` 응답은 여기서 `rows`를 뺀 나머지 필드 + `artifactRefs`(내보낸
CSV/XLSX 경로)로 구성한다 — `export_dataset.py`가 이 형태로 별도 `dataset_manifest.json`을
만든다.

## 라벨 매핑 (`labelMapping`)

| CWRU 원본 라벨 (`known_label`) | 공통 학습 라벨 (`common_label`) |
|---|---|
| `NORMAL` | `NORMAL` |
| `BEARING_FAULT_INNER` | `ANOMALY` |
| `BEARING_FAULT_BALL` | `ANOMALY` |
| `BEARING_FAULT_OUTER` | `ANOMALY` |

이 매핑은 `EVENT_LABEL_01`의 `seed_label_from_dataset()`이 재사용해, 이 데이터셋을 리플레이한
합성 이벤트의 초기 운영자 라벨(`confirmed_anomaly`/`normal_false_positive`)을 자동으로
채우는 데 쓰인다 (자세한 내용은 `../event_labels/event_label_schema.md` 참고).

## 분할 전략 (`splitStrategy`)

`dataset/schema.md`(1주차)는 "동일 설비의 샘플이 train/test에 섞이지 않도록 설비 단위(group
split)"를 권장한다. `register_dataset.py`의 `group_split()`은 이를 그대로 따른다:

- **`source_label`(원본 `.mat` 파일) 단위로 그룹을 통째로 하나의 split에만 배정**한다.
  동일 그룹의 윈도우가 여러 split에 나뉘어 들어가는 것을 원천적으로 막아 데이터 누수를
  방지한다 (`seed` 고정, 라벨별로 독립적으로 그룹을 배정).
- 라벨 하나의 독립 그룹 수가 요청한 분할 개수(기본 3: train/validation/test)보다 적으면
  **그룹을 쪼개서 윈도우 단위로 섞는 대신 `InsufficientAssetGroupsError`를 발생시킨다.**
  CWRU는 **라벨 하나당 실제 자산(파일)이 1개뿐**이라(`97.mat` = NORMAL 자산 1대,
  `105/118/130.mat` = 결함 자산 각 1대) 기본 3-way 비율로는 이 예외가 항상 발생하는 것이
  정상 동작이다.
- 그래서 지금은 `--train-ratio 1 --validation-ratio 0 --test-ratio 0`처럼 **train 전용
  비율을 명시적으로 지정**해 파이프라인(체크섬 추적/라벨 매핑/내보내기)을 검증한다.
  실제 현장 데이터처럼 **동일 라벨 안에 자산이 여러 대** 확보되면 기본 3-way 비율로
  전환하면 된다.

## 행(rows) 스키마 — CSV/XLSX로 내보내는 실제 컬럼

| 컬럼 | 설명 |
|---|---|
| `sample_id` | `load_cwru_vibration.load_cwru_dataset()`가 부여한 윈도우 ID |
| `source_file` | 원본 `.mat` 파일명 (체크섬 추적용 키) |
| `known_label` | CWRU 원본 라벨 |
| `common_label` | 정규화된 공통 라벨 (`NORMAL`/`ANOMALY`) |
| `split` | `train`/`validation`/`test` |
| `sample_rate_hz`, `rpm` | 원본 메타데이터 |
| `rms_mean`, `kurtosis_mean`, ... | `extract_all_features()`(week2 특징량 로직 재사용) 결과 — feature_extraction 로직을 중복 구현하지 않는다 |

## 수용 기준 검증 방법

- 변환 전후 건수 일치: `len(records) == manifest["rowCount"] == len(rows)` (테스트로 검증)
- 분할 건수 일치: `sum(splitCounts.values()) == rowCount`
- 원본 추적: `rows[i].source_file`로 `manifest["source"]["files"][source_file]["sha256"]`를
  찾아 원본 체크섬 재계산 값과 비교 가능

## TODO (실제 센서/추가 데이터셋 확보 후)

- [ ] MIMII 음향 데이터가 배치되면 `modality: "acoustic"` 데이터셋 버전을 동일 스키마로 추가 등록
- [ ] 라벨당 자산이 여러 개가 되면 기본 3-way 비율(`--train-ratio 0.7 --validation-ratio 0.2
      --test-ratio 0.1`)로 전환 (분할 로직 자체는 이미 설비 단위 group split — 자산 부족 문제만 남음)
- [ ] 실제 센서 채널 확보 후 이 CWRU 버전과 별도의 신규 데이터셋 버전으로 등록 (섞지 않음 —
      MVP 기획서 v1.2 "역할배정" 시트의 "대상 확정 후 보완" 항목)
