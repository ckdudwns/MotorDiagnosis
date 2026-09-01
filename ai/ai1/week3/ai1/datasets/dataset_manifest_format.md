# 데이터셋 등록 매니페스트 스키마 (DATA_EXPORT_01)

기능ID: `DATA_EXPORT_01` (3주차 실행순서 1번, AI-1 주담당)
산출 스크립트: `register_dataset.py`(매니페스트 생성) → `export_dataset.py`(CSV/XLSX 내보내기)

이번 주 목표는 **기존 공개·보유 데이터셋(CWRU Bearing Dataset)을 선정하고, 출처·라이선스·
샘플링률·단위·라벨을 정규화해 4주차 `AI_FREQ_MODEL_01` 선행학습 입력을 준비하는 것**이다.
API 명세서 v1.3의 `09_보완API상세` 시트 `POST /api/datasets`(MVP-042) / `GET /api/datasets/{id}`
(MVP-043) 계약을 그대로 따른다 — v1.3에서 추가된 `labelPolicyVersion`/`snapshotSchemaVersion`,
export 행의 라벨 상태(`label_status`/`training_eligible`), manifest 라벨 집계까지 반영한다.

## 왜 CWRU만 쓰는가

1주차 인계 문서(`ai/ai1/week1/ai1/README.md`)에서 이미 확인했듯 이 저장소에는 MIMII 음향
데이터의 실제 파일(`.wav`)이 배치되어 있지 않다(`ai/ai1/week1/ai1/scripts/load_mimii_acoustic.py`
로더 코드만 존재). 따라서 이번 3주차 데이터셋 등록·내보내기는 **CWRU Bearing Dataset(진동)만
대상으로 한다.** MIMII가 추가되면 같은 스키마로 `modality: "acoustic"` 데이터셋 버전을 별도
등록하면 된다(아래 "TODO" 참고).

## 매니페스트 필드 (`POST /api/datasets` 요청과 1:1 대응)

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | string | 데이터셋 버전 ID (`DS-CWRU-VIBRATION-<날짜>-<버전체크섬 12자리>` 형식) — 같은 날짜라도 입력 파일/라벨/윈도우/분할 설정/라벨 taxonomy·mapping/특징 추출 설정이 다르면 다른 ID가 나온다 |
| `name` | string | 데이터셋 이름 (`cwru-bearing-vibration-v1`) |
| `source.type` | string | `external` (외부 공개 데이터셋) |
| `source.uri` | string | CWRU Bearing Data Center 공식 URL |
| `source.license` | string | 라이선스/이용 조건 메모 (학술 공개, 재배포 시 출처 표기) |
| `source.files` | object | 실제 배치된 원본 파일별 `{sha256, label}` — **체크섬으로 원본 추적** |
| `source.checksum` | string | 원본 파일 `{sha256, label}` 전체 + window/hop 크기 + 분할 비율 + seed + labelTaxonomyVersion + labelMapping + 특징 추출 설정(FeatureConfig: sample_rate/frame_length/hop_length/n_mfcc/band_edges) + `FEATURE_PIPELINE_VERSION` + **실제 산출된 특징값의 fingerprint**(`compute_feature_output_fingerprint()`)로 만든 불변 버전 체크섬(`compute_version_checksum()`) — `id`의 접미사와 동일 값. 실제 정규화 산출물(known_label/특징값)에 영향을 주는 입력을 빠짐없이 포함해야, 같은 파일 sha256에서 source label만 바뀌거나 특징 추출 설정/로직만 바뀐 경우에도 같은 id가 재사용되는 것을 막을 수 있다. fingerprint는 메타데이터가 아니라 최종 특징값 자체를 해시하므로, librosa 유무처럼 소스 코드/설정에는 드러나지 않는 실행 환경 차이(MFCC 0벡터 폴백 등)도 잡아낸다 |
| `compatibility.signalType` | string[] | `["vibration"]` |
| `compatibility.samplingRateHz` | number | `12000` (CWRU Drive-End 12kHz) |
| `compatibility.units` | object | `{"vibration": "g (raw accelerometer output, uncalibrated)"}` |
| `compatibility.operatingConditions` | object | `{"rpmRange": [min, max], "load": ...}` — 파일 메타데이터의 RPM 실측값 기반 |
| `labelTaxonomyVersion` | string | `CWRU-FAULT-V1` |
| `labelMapping` | object | CWRU 원본 라벨 → 공통 학습 라벨(`NORMAL`/`ANOMALY`) |
| `labelPolicyVersion` | string | `LABEL-POLICY-V2` (API 명세서 v1.3). `compute_version_checksum()` payload + manifest에 포함 — 정책이 바뀌면 다른 `id`가 나온다. 기존 frozen 데이터셋은 재계산하지 않는다 |
| `snapshotSchemaVersion` | string | `2` (API 명세서 v1.3). 동일하게 fingerprint + manifest에 포함 |
| `labelCounts` | object | `{verified, weak, unlabeled, unmapped}` — 행의 라벨 상태별 건수 (합 = `rowCount`) |
| `trainingEligibleCount` | number | `training_eligible=true` 전체 건수 |
| `trainingEligibleSplitCounts` | object | `{train, validation, test}` — split별 학습 가능 건수 |
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

## 산출물 배치 (원자적 게시)

`export_dataset.py`는 세 산출물(csv/xlsx/manifest.json)을 `output_dir/versions/<dataset
id>/`에 모두 만들고 검증한 뒤 그 디렉터리 자체를 단일 rename으로 배치하고, 마지막으로
`output_dir/CURRENT` 포인터 파일을 새 버전 id로 원자적으로 교체한다. 파일별로 따로
교체하면 중간 실패나 동시 export 시 서로 다른 버전의 csv/xlsx/manifest가 섞여 보일 수
있어, 세 파일의 "집합"을 바꾸는 동작 자체를 단일 원자적 연산으로 묶었다. `version_id`는
`compute_version_checksum()`이 만드는 불변 체크섬을 담고 있으므로, 같은 입력으로 다시
내보내면 같은 version_dir을 재사용한다(idempotent).

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
  (이 동작은 합성 케이스로 `TestGroupSplitSynthetic`가 계속 검증한다.)
- CWRU는 이제 **라벨당 자산(파일) 4개** — 부하 조건 0/1/2/3 HP — 를 확보해 기본 3-way
  비율이 리크 없이 성립한다. 파일 크기가 전부 달라 분할은 `seed`에 의존하지 않고
  결정적이다: **test = 0HP(97/105/118/130), validation = 1HP(98/106/119/131),
  train = 2HP+3HP**. 각 원본 파일은 정확히 하나의 split에만 들어간다.
- 유의: CWRU에서는 "자산 = 부하 조건"이라 split 간 운전 조건이 겹치지 않는다. 정상
  재구성 임계값이 train에 없던 부하 조건에서는 보정되지 않으므로, 하류 모델
  (`AI_FREQ_MODEL_01`)은 RPM에 강하게 묶인 `vibration_peak_hz`를 모델 입력에서 제외하고
  운전 조건별 임계값 재보정을 도메인갭으로 기록한다.

## 행(rows) 스키마 — CSV/XLSX로 내보내는 실제 컬럼

| 컬럼 | 설명 |
|---|---|
| `sample_id` | `load_cwru_vibration.load_cwru_dataset()`가 부여한 윈도우 ID |
| `source_file` | 원본 `.mat` 파일명 (체크섬 추적용 키) |
| `known_label` | CWRU 원본 라벨 |
| `common_label` | 정규화된 공통 라벨 (`NORMAL`/`ANOMALY`) |
| `split` | `train`/`validation`/`test` |
| `sample_rate_hz`, `rpm` | 원본 메타데이터 (RPM 키 없는 `98/99.mat`은 부하 조건으로 추정) |
| `rms_mean`, `kurtosis_mean`, ... `vibration_peak_hz` | `extract_all_features()`(week2, 26개) + `compute_peak_frequency()`(week1, `vibration_peak_hz`) = 27개. 계산 로직은 중복 구현하지 않고 재사용 |

**매니페스트 `rows`는 위 특징 컬럼만** 담는다 — `compute_feature_output_fingerprint()`가
행 전체를 해시하므로 라벨 상태 같은 파생값을 섞으면 fingerprint 의미가 흐려진다.

### export 산출물(CSV/XLSX)에만 붙는 v1.3 라벨 컬럼

`export_dataset.py`가 `register_dataset.dataset_export_label_fields()`로 행마다 파생해 CSV·XLSX
`rows` 시트 뒤에 덧붙인다(매니페스트 `rows`에는 넣지 않음). API 명세서 v1.3 `DatasetExportRow`.

| 컬럼 | CWRU에서의 값 |
|---|---|
| `label_status` | `verified`(known_label이 labelMapping에 있음) / `unmapped`(매핑 실패) / `unlabeled`(라벨 없음). `weak`(미검수 후보만 존재)는 CWRU 경로엔 없음 |
| `training_eligible` | `label_status == "verified"`인 행만 `true` — 지도학습 대상 |
| `ground_truth_label` | `known_label` (CWRU 원본 라벨) |
| `ground_truth_source` | `dataset_registration` — 공개 데이터셋 파일→라벨 맵(검증 가능한 신뢰 출처) |
| `target_label` / `target_label_taxonomy_version` | 매핑 성공 시 `common_label` / `CWRU-FAULT-V1`, 실패 시 `null` |
| `scenario_label`, `known_vibration_label`, `known_acoustic_label`, `event_reviewed` | CWRU는 텔레메트리 시나리오·이벤트가 없어 항상 `null`/`false` (CSV 빈 셀) |

**신뢰된 라벨 출처만 supervised target으로 승격**한다(v1.3): CWRU는 공개 데이터셋의
파일→라벨 맵이라는 검증 가능한 import 출처가 있어 `verified`다. 미검수 이벤트·모델 판정·
규칙 기반 이상 후보는 `ground_truth`/`target_label`로 올리지 않는다 — CWRU 경로엔 그런
후보가 없고, 이벤트 라벨을 시드하는 `EVENT_LABEL_01`의 `seed_label_from_dataset()` 결과도
"검수 전 출발점"일 뿐이다(`../event_labels/event_label_schema.md` 참고).

## 수용 기준 검증 방법

- 변환 전후 건수 일치: `len(records) == manifest["rowCount"] == len(rows)` (테스트로 검증)
- 분할 건수 일치: `sum(splitCounts.values()) == rowCount`
- 원본 추적: `rows[i].source_file`로 `manifest["source"]["files"][source_file]["sha256"]`를
  찾아 원본 체크섬 재계산 값과 비교 가능

## TODO (실제 센서/추가 데이터셋 확보 후)

- [ ] MIMII 음향 데이터가 배치되면 `modality: "acoustic"` 데이터셋 버전을 동일 스키마로 추가 등록
- [x] 라벨당 자산 4개(0~3HP)를 확보해 기본 3-way group split로 전환 완료. 남은 한계:
      CWRU는 자산 = 부하 조건이라 split 간 운전 조건이 겹치지 않는다 — 현장에서 다양한
      운전 조건의 자산이 쌓이면 해소된다
- [ ] 실제 센서 채널 확보 후 이 CWRU 버전과 별도의 신규 데이터셋 버전으로 등록 (섞지 않음 —
      MVP 기획서 "역할배정" 시트의 "대상 확정 후 보완" 항목)
- [ ] 텔레메트리 스냅샷 기반 내부 데이터셋(`POST /api/telemetry/ingest` → export)의 라벨 권한·
      provenance 검증은 백엔드(`motor_diagnosis/`) 담당 — AI-1은 CWRU 공개 데이터셋 경로만 맡는다
