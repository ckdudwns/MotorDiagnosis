# AI-1 — 3주차 (이상 이벤트·운영 검토 / 기존 데이터셋 정규화)

Bind Edge AI 프로젝트 3주차, AI-1 담당 영역.

추가 AI1-01/02: `datasets/register_acoustic_dataset.py`에서 MIMII 음향 manifest를
생성하고 기존 CSV/XLSX 내보내기·4주차 동결/후보 학습에 연결한다.
실행 방법과 신호별 기준선 입력 계약은 [음향/RPM 안내](../../../../docs/ai1-acoustic-baselines.md)를 참고한다.

3주차 기능정의서(W3.2, 2026-08-24)와 API 명세서(v1.3)를 기준으로 작업했다. 원래
초안(W3.1)과 달리 **DATA_EXPORT_01의 범위가 "기존 공개·보유 데이터셋 선정·정규화"로
확장**되었고, AI-1의 착수 순서도 재조정되었다. 이 폴더의 4개 기능은 "선행일정" 시트에
명시된 순서 그대로 작업했다:

| 순서 | 기능ID | 폴더 | 상태 |
|---|---|---|---|
| 1 | `DATA_EXPORT_01` | `datasets/` | 완료 |
| 2 | `EVENT_LABEL_01` | `event_labels/` | 완료 |
| 3 | `EVENT_DETAIL_01` | `event_details/` | 완료 |
| 4 | `ANOMALY_RULE_01` | `anomaly_rules/` | 완료 |

1·2주차 인계 파이프라인(`../../week1/ai1/`, `../../week2/ai1/`)은 그대로 유지하고
복제하지 않는다. CWRU 원본 데이터와 로더는 1주차 경로를, 특징량 추출(`extract_all_features`)과
정상 기준선(`baseline.json`)·이상치 판정(`check_outliers`)은 2주차 경로를 그대로
재사용한다.

## 폴더 구조

```
ai/ai1/week3/ai1/
├── datasets/                        # DATA_EXPORT_01
│   ├── dataset_manifest_format.md
│   ├── register_dataset.py          # 매니페스트(source/compat/labelMapping/split) 생성
│   └── export_dataset.py            # CSV/XLSX 내보내기
├── event_labels/                    # EVENT_LABEL_01
│   ├── event_label_schema.md
│   └── event_label.py               # 라벨 변경 이력 + 기존 데이터셋 라벨 매핑
├── event_details/                   # EVENT_DETAIL_01
│   ├── event_detail_format.md
│   └── event_detail.py              # 이벤트 전후 특징량 비교 데이터 빌더
├── anomaly_rules/                   # ANOMALY_RULE_01
│   ├── anomaly_rule_format.md
│   └── anomaly_rule.py              # 설비별 기준선 레지스트리 + 히스테리시스
└── tests/
    ├── test_dataset_export.py
    ├── test_event_label.py
    ├── test_event_detail.py
    └── test_anomaly_rule.py
```

## 1. DATA_EXPORT_01 — 기존 데이터셋 정규화·내보내기

MIMII 음향 데이터는 이 저장소에 실제 파일이 배치돼 있지 않아(로더 코드만 존재),
이번 3주차 등록·내보내기는 **CWRU Bearing Dataset(진동)만** 대상으로 했다.

`register_dataset.py`가 CWRU 40개 `.mat` 파일(10 물리 specimen, 각 부하조건 4개)을 API 명세서 v1.3
`POST /api/datasets` 계약(`source`/`compatibility`/`labelMapping`/`split` +
`labelPolicyVersion`/`snapshotSchemaVersion`/`labelCounts`) 형태의 매니페스트로 정규화하고,
`export_dataset.py`가 `dataset_manifest.json`(GET 응답 형태) + `dataset_rows.csv` +
`dataset_export.xlsx`(manifest/rows 2개 시트)로 내보낸다. CSV/XLSX 행에는 v1.3
`DatasetExportRow` 라벨 컬럼(`label_status`/`training_eligible`/`ground_truth_*`/`target_label*`)이
붙는다 — CWRU 원본 라벨은 신뢰된 외부 라벨이라 전 행 `verified`/`training_eligible`.

**XLSX 내보내기는 루트 `requirements.txt`에 없는 `openpyxl`이 필요하다** — 실행·테스트
전에 이 폴더의 의존성을 추가로 설치해야 한다:

```bash
python -m pip install -r ai/ai1/week3/ai1/requirements.txt
```

- 원본 파일별 SHA-256 체크섬을 매니페스트에 기록해 원본 추적 가능 — 원본 체크섬들 +
  window/hop 크기 + 분할 비율 + seed로 만든 불변 버전 체크섬(`source.checksum`)을 데이터셋
  `id`에도 반영해, 같은 날짜에 입력·설정이 다른 버전이 같은 ID로 충돌하지 않도록 했다
- **물리 베어링(specimen) 단위 group split** — CWRU 부하별 4파일은 같은 베어링이므로
  `specimen_id`(결함타입+직경) 단위로 배정한다. `build_manifest` 기본 `specimen_group`은
  NORMAL specimen이 1개뿐이라 3-way에서 `InsufficientAssetGroupsError`로 **정직하게
  실패**한다. 데모용 `operating_condition_holdout`(부하조건 기준)은 `independentHoldout=false`
  플래그를 붙인다 (근거: `datasets/dataset_manifest_format.md`)
- 특징값은 week2 `extract_all_features()`(26개)에 week1 `compute_peak_frequency()`
  (`vibration_peak_hz`)를 더한 27개 — 계산 로직은 재구현하지 않고 그대로 재사용
- **산출물 3종(csv/xlsx/manifest.json)을 원자적으로 배치** — `output_dir/versions/<dataset
  id>/`에 세 파일을 모두 만들고 검증한 뒤 그 디렉터리 자체를 단일 rename으로 배치하고,
  `output_dir/CURRENT` 포인터를 새 버전으로 원자적으로 전환한다. 중간 실패나 동시 export가
  서로 다른 버전의 파일을 섞어 놓지 않는다(근거·구현: `datasets/export_dataset.py`)

**실행 결과 (실제 CWRU 40개 파일 / 10 물리 specimen):**
- 기본 `specimen_group` 3-way → `InsufficientAssetGroupsError` (NORMAL specimen 1개). 정상.
- `operating_condition_holdout` → 총 2953행을 부하 tier 기준으로 등록
  (test=0HP, validation=1HP, train=2·3HP). `independentHoldout=false` — 같은 물리 베어링이
  여러 split에 등장하므로 이 분할은 specimen 독립 검증이 아니다.
- 결함 3클래스는 specimen 3개(0.007/0.014/0.021")라 결함 데이터만으로는 specimen 독립
  3-way가 성립함을 테스트로 확인했다.

```bash
python ai/ai1/week3/ai1/datasets/export_dataset.py
# 결과: ai/ai1/week3/ai1/data/handoff/versions/<dataset id>/{dataset_manifest.json,
#       dataset_rows.csv, dataset_export.xlsx} + CURRENT 포인터(최신 버전 id)
```

## 2. EVENT_LABEL_01 — 라벨 지정 및 변경 이력

새 라벨 체계를 만들지 않고 **백엔드(`motor_diagnosis/data.py review_event()`)가 이미
쓰고 있는 라벨 5종**(`needs_review`/`normal_false_positive`/`confirmed_anomaly`/
`repair_completed`/`sensor_issue`)을 그대로 재사용했다 — 새 체계를 만들면 백엔드·AI-2와
어긋난다.

`event_label.py`의 `apply_label_change()`는 이벤트의 불변 필드(`id`, `severity`, `score`
등)는 건드리지 않고 `label`/`note`/`reviewedAt`만 갱신하며, 변경 전후 값·변경자·사유를
history 엔트리로 반환한다(원본 이벤트 값 불변 수용 기준 충족). `seed_label_from_dataset()`은
`DATA_EXPORT_01`이 정규화한 CWRU 공통 라벨(`NORMAL`/`ANOMALY`)을 이벤트 초기 라벨
(`normal_false_positive`/`confirmed_anomaly`)로 매핑해, 데이터셋을 리플레이한 합성
이벤트의 학습 라벨을 자동으로 채운다.

실제 CWRU 매니페스트의 전 행을 시딩해본 결과 두 라벨(`normal_false_positive`,
`confirmed_anomaly`)이 모두 정상적으로 나오는 것을 확인했다.

## 3. EVENT_DETAIL_01 — 이벤트 전후 특징량 비교

`event_detail.py`의 `build_event_detail()`은 이벤트가 발생한 윈도우 인덱스를 기준으로
"이벤트 전 N초 vs 이후 N초" 구간의 특징값을 요약·비교한다. `ANOMALY_RULE_01`의 판정
결과와는 독립적으로 동작해(이벤트 인덱스만 있으면 됨), 요청한 윈도우 수를 채우지
못하면(스트림 경계 근처) `data_missing`/`gap_detected`로 원본 데이터 누락을 표시하고,
`after.time_range[0]`이 항상 `0.0`(이벤트 발생 시각)이 되도록 해 "목록-상세 시각 범위
일치" 수용 기준을 만족시킨다.

**실행 결과 (CWRU 97.mat→105.mat 전환 지점, 전후 각 10윈도우 ≈ 1.7초):**
`kurtosis_mean`이 전(-0.243) → 후(2.438)로 뚜렷하게 상승(Δ=2.68, +1103%)해, 실제
결함 전환을 전후 비교 구조가 잡아내는 것을 확인했다.

## 4. ANOMALY_RULE_01 — 통계 임계값·히스테리시스 (나머지의 기반)

`anomaly_rule.py`는 week2 `validate_features.check_outliers()`(baseline mean/std 기반
정상범위 판정)를 그대로 재사용하고, 3주차에 새로 필요한 두 가지만 추가했다:

- **`AssetBaselineRegistry`**: `asset_id` > `asset_type` > `default` 순으로 기준선을
  조회. 지금은 모터 1종류만 등록돼 있지만, 설비가 늘어나면 `register()` 호출만
  추가하면 되는 구조 (평가 로직은 불변).
- **히스테리시스 상태 머신**(`evaluate_feature_stream`): 진입 임계값(`sigma_enter=3.0`)과
  복귀 임계값(`sigma_exit=2.0`)을 분리하고, 각각 연속 2윈도우 조건(`min_consecutive_enter/exit`)을
  둬서 단발성 스파이크로 이벤트가 깜빡이는 것을 막는다. 이벤트마다 `baseline_version`/
  `config_version`을 기록해 임계값 변경 전후 적용 버전을 추적한다.

**실행 결과 (실제 CWRU 데이터, baseline 출처와 같은 0HP 정상 97.mat 119윈도우 →
결함 파일 전환, 기본 설정):** 정상 구간에서는 이벤트가 **전혀** 생기지 않았고
(week2에서 확인된 정상 구간 자체 이상치 플래그는 연속되지 않는 단발성이라
히스테리시스에 걸러짐), 정상→결함 경계(인덱스 119)에서 이벤트가 열려 결함 구간
끝까지 유지됐다. 결함 윈도우 100%가 개별적으로는 이상치로 플래그되는 상황에서도
이벤트 수가 낮게 안정화됨을 확인 — 히스테리시스가 실제로 깜빡임을 억제하는 것을
검증했다. (기준선은 0HP 정상 단독으로 산출됐으므로, 다른 부하 조건의 정상 데이터는
이 기준선 대비 크게 벗어난다 — 운전 조건별 기준선 재보정이 필요하다.)

```bash
python -m unittest discover ai/ai1/week3/ai1/tests
```

## 테스트 실행

```bash
python ai/ai1/week3/ai1/tests/test_dataset_export.py
python ai/ai1/week3/ai1/tests/test_event_label.py
python ai/ai1/week3/ai1/tests/test_event_detail.py
python ai/ai1/week3/ai1/tests/test_anomaly_rule.py
```

CWRU 실데이터(`ai/ai1/week1/ai1/data/external/cwru/*.mat`)가 없는 환경에서도 합성
(fixture) 신호로 핵심 로직은 항상 실행되고, 실데이터 전용 테스트는
`unittest.skipUnless`로 명시적으로 skip 처리된다(week2와 동일한 패턴).

## 대상 확정 후 보완 (공통)

- MIMII 음향 데이터가 배치되면 `DATA_EXPORT_01`에 `modality: "acoustic"` 데이터셋
  버전을 같은 스키마로 추가 등록
- (완료) CWRU 자산을 라벨당 4개로 확보해 `DATA_EXPORT_01`을 기본 3-way group split로
  전환했다. 다만 CWRU에서 자산 = 부하 조건(0/1/2/3 HP)이라 split 간 운전 조건이
  겹치지 않는다 — 현장에서 라벨당 여러 자산이 다양한 운전 조건으로 쌓이면 이
  한계가 자연히 해소된다
- `sigma_enter`/`sigma_exit`/`min_consecutive_*`는 실측 정상·이상 분포로 재보정 필요
- `EVENT_DETAIL_01`의 장치 상태 스냅샷 연동, `ANOMALY_RULE_01`의 음향·RPM 임계값 추가는
  해당 데이터 확보 후 진행
