# AI-1 — 4주차 (통합 시연·AI 학습 준비 / 기존 데이터셋 베이스라인)

Bind Edge AI 프로젝트 4주차, AI-1 담당 영역. 4주차 기능정의서(W4.2, 2026-08-24)와
API 명세서(v1.3) 기준으로 작업했다. "선행일정" 시트 순서 그대로 진행했다:

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

API 명세서 v1.3에서 추가된 `labelPolicyVersion`(`LABEL-POLICY-V2`)·`snapshotSchemaVersion`(`2`)·
`snapshotChecksum`도 동결 산출물에 남긴다. `snapshotChecksum`은 3주차
`compute_version_checksum()` 결과를 재사용하고, `datasetChecksum` 계산식은 그대로 둬
이미 동결·재현성 검증된 데이터셋은 영향받지 않는다(라벨 정책이 바뀐 신규 데이터셋만
다른 `id`를 받는다).

`model_version.py`는 API 명세서 FUT-005~007(모델 버전 등록/승인/롤백), FUT-010~011
(기준선 버전 등록/승인)을 데이터 구조로 옮겼다. 롤백은 `approved` 상태의 대상으로만
허용하고, 기준선은 `approved`가 아니면 `activate`할 수 없게 해 "승인 전 배포 금지"를
코드 레벨에서 강제한다.

**무결성 (리뷰 P1 반영):**
- `compute_snapshot_digest`가 매니페스트 **전체 불변 필드**(source·compatibility·정책
  버전·splitStrategy·rows 등)를 해시한다. `approve_dataset_version`은 동결 이후 어떤
  불변 필드가 변조돼도 승인을 거부하고, `verify_frozen_integrity`는 학습 시작 전에
  같은 검증을 한다.
- 신규 동결은 v1.3 필수 필드(`labelPolicyVersion`/`snapshotSchemaVersion`/`source.checksum`/
  `featureOutputFingerprint`)를 요구한다. `featureOutputFingerprint`는 rows만으로 재계산
  가능한 값이라, freeze 시점에 draft가 신고한 값과 현재 rows를 대조해 build 이후 rows/라벨이
  바뀐 draft가 예전 `id`/`source.checksum`을 그대로 단 채 동결되는 것을 막는다(리뷰 P1, 2차).
- **legacy(v1) 취급은 매니페스트 필드로 추정하지 않는다(리뷰 P1, 2차)** — 매니페스트는
  호출자가 자유롭게 고칠 수 있는 데이터라서, `snapshotDigest` 부재로도 `snapshotSchemaVersion`
  부재로도 판별해봤지만 둘 다 "그 필드(들)만 지우면 우회된다"는 같은 구조로 뚫렸다.
  `approve_dataset_version`/`verify_frozen_integrity`/`verify_reproducibility`는 이제 호출자가
  매니페스트 바깥의 신뢰 가능한 저장소에서 확인한 사실을 `trusted_legacy=True`로 명시할
  때만 legacy 완화 검증을 적용한다(기본값 False = 항상 v1.3 엄격 검증).
- `verify_reproducibility`는 기본값에서 `datasetChecksum`(rows/labelMapping/split)뿐 아니라
  `labelPolicyVersion`/`snapshotSchemaVersion`/원본 `source.checksum`까지 함께 검증한다.
- **freeze는 `source.checksum`/id도 canonical 재계산으로 재검증한다(리뷰 P1, 3차).**
  `featureOutputFingerprint`만 rows와 대조하면, rows를 바꾸고 fingerprint를 함께 재계산해
  신고하거나(현재 rows와의 대조는 통과) fingerprint에는 안 들어가지만 checksum에는 들어가는
  필드(`labelMapping` 등)만 바꿔도 예전 `id`/`source.checksum`이 그대로 승계됐다. draft는
  이제 `checksumInputs`(window/hop/seed/파이프라인 버전/특징 설정/split 전략 키)를 노출하고,
  `compute_source_checksum()`이 이 필드 + draft 자신의 나머지 필드만으로 `source.checksum`을
  독립 재계산해 대조한다(week3 모듈은 import하지 않음). `verify_reproducibility`도 같은
  방식으로 recomputed_manifest의 `source.checksum` 필드값을 신뢰하지 않고 재계산해 대조한다.
- **`trusted_legacy`는 실제 `bool`만 허용한다(리뷰 P1, 3차).** 문자열 `"false"`는 파이썬에서
  truthy라서, 타입 검증 없이는 legacy 완화 경로가 잘못 켜질 수 있었다 — 이제 `bool`이 아니면
  `TypeError`, `is True`일 때만 완화 경로를 켠다.
- **`ModelVersionRegistry`/`BaselineVersionRegistry`(리뷰 P1, 5차)** — 이전에는 자유 함수
  `register_model_version`/`approve_model_version`/`rollback_model_version`이 승인·롤백을
  호출자가 만든 평범한 list(`registry`/`approved_history`)로 신뢰했다. `registrationDigest`/
  `approvalDigest`가 공개 함수의 결과라서, 호출자가 임의 레코드에 그 함수로 직접 계산한
  digest를 채워 넣고 `registry=[그 레코드]`로 전달하면 "자기 서명"이 되어 통과했다.
  이제 등록·승인된 레코드는 **레지스트리 인스턴스 자신**이 소유한다(`register()`만 기록,
  `approve()`/`rollback()`/`activate()`는 `version`/`baseline_id` 문자열로 그 레지스트리
  자체에서만 조회) — `register()`를 거치지 않은 버전·기준선은 어떤 문자열을 대도 조회되지
  않아 위조가 구조적으로 불가능하다. `register()`/`approve()`/`get()`은 항상 깊은 복사본만
  반환해, 반환값을 변조해도 저장소 내부 상태는 영향받지 않는다.
- `registry.register()`는 필수 문자열(version/artifactUri/datasetId/baselineVersion/
  artifactChecksum)의 nonblank와 `metrics`가 dict인지, `artifactChecksum`이 sha256
  hexdigest 형식인지(리뷰 P2) 검증하고, 불변 필드 전체의 `registrationDigest`를 함께
  저장한다. 같은 version을 두 번 등록할 수 없다. `registry.approve(version, ...)`은
  `approved_by`와 명시적인 `metric_snapshot`을 모두 필수로 받고(등록 시점 metrics를
  암묵적으로 재사용하지 않는다), version으로 이 레지스트리 안에서 조회한 등록 레코드만
  승인 대상으로 삼는다 — 누가, 어떤 지표를 보고 승인했는지 감사 가능해야 한다.
- `registry.rollback(current_version, target_version, ...)`은 target이 **실제로 current보다
  앞선 승인 버전**인지 검증한다. 계보는 이 레지스트리 자신이 보유한 전체 상태에서 나온다 —
  별도의 `approved_history` 인자는 없다(리뷰 P1, 5차). current/target 모두 이 레지스트리
  안에서 실제로 **승인(`status=="approved"`)된** 버전으로 존재해야 하며(등록조차 안 됐거나
  미승인이면 무조건 거부한다), **호출 순서가 아니라 각 항목의 실제 `approvedAt`**을 UTC로
  파싱해 시간순으로 비교한다 — 등록·승인 호출 순서가 뒤바뀌어도 forward rollback을 막는다.
  `target_environment`도 허용된 환경 값(`production`/`staging`/`development`)이어야 한다.
- `BaselineVersionRegistry().register()`는 필수 ID(baseline/dataset/site/asset)와 `features`
  구조·유한값(std>0)을 검증하고 `registrationDigest`를 저장하며, 같은 baseline_id를 두 번
  등록할 수 없다. `registry.approve(baseline_id, ...)`은 `approved_by`(이전에는 검증되지
  않았다)와 `reason`을 모두 필수로 받고, `registry.activate(baseline_id)`는 이 레지스트리
  안에서 실제로 승인된 baseline_id만 active로 전이한다 — 등록·승인 없이 status만 맞춘
  임의 객체로 active 기준선을 만드는 경로 자체가 없다(리뷰 P1, 3차·5차).

**재현성:** 같은 입력·`split_strategy`로 `build_manifest`를 두 번 생성해 체크섬이
동일함을 확인한다. `split_strategy`가 다르면(specimen_group vs operating_condition_holdout)
다른 데이터셋 id가 나온다.

## 2. AI_FREQ_MODEL_01 — Dense/LSTM Autoencoder 베이스라인

### 데이터 분할의 근본 한계 (리뷰 P1)

CWRU 부하별 `.mat` 4개(0/1/2/3 HP)는 **같은 물리 베어링(specimen)**을 부하만 바꿔
측정한 것이다. 데이터셋 계층의 기본 분할은 물리 specimen 단위(`specimen_group`)이고,
CWRU는 **건강한 베어링이 1개뿐**이라 3-way specimen 독립 분할이 불가능하다 —
`build_manifest` 기본값은 `InsufficientAssetGroupsError`로 정직하게 실패한다.
(결함 클래스 IR/Ball/OR은 0.007/0.014/0.021" 3 specimen을 확보했으나 NORMAL 제약이 남는다.)

데모 베이스라인은 **`operating_condition_holdout`**(test=0HP / validation=1HP /
train=2·3HP)으로 얻으며, 같은 물리 베어링이 모든 split에 들어간다 →
**`independentHoldout=false`**. 아래 지표는 "specimen 독립 일반화 성능"이 아니라
**운전조건 기준 in-distribution 평가**다.

### 모델

매니페스트 row에는 27개 특징(week2 `extract_all_features` 26개 + week1
`compute_peak_frequency`)이 담기고, **모델 입력은 `vibration_peak_hz`를 뺀 26개**다
— 이 특징은 RPM/60에 비례하고 `operating_condition_holdout`이 부하(=RPM) 기준 분할이라
train/validation 구간에서 이미 값 범위가 겹치지 않는다(train/validation 관찰 + 물리 근거로
판단, test 평가 전 동결). 두 후보 모두 정상(NORMAL) 데이터만으로 재구성 학습하고,
동결 매니페스트 rows의 inline 특징값만 입력으로 쓴다. 후보 선택은 **validation f1**으로만
하고 최종(비독립) test 지표는 분리 보고한다.

**실행 결과 (실제 CWRU 40파일, `operating_condition_holdout`, dense=lstm=8 epochs 스모크):**

| 후보 | 검증 f1 | (비독립) 테스트 f1 | 비고 |
|---|---|---|---|
| dense_autoencoder | ~0.90 | ~0.90 | `independentHoldout=false` — 운전조건 기준 in-distribution |
| lstm_autoencoder | ~1.00 | ~1.00 | 완전한 일반화 성능이 **아님**. specimen 독립 검증 미실시 |

epoch·시드에 따라 값이 흔들리며, 이 수치를 일반화 성능으로 해석하면 안 된다. NORMAL
독립 베어링 확보 후 `specimen_group`으로 재검증해야 한다.

`run_training_job`은 학습 전 `verify_frozen_integrity`로 동결본 변조를 검사하고, 매니페스트의
`independentHoldout` 선언도 rows의 specimen_id→split 관계로 재계산해 대조한다(선언=True인데
실제로 같은 specimen이 여러 split에 걸쳐 있으면 거부 — 선언값을 그대로 신뢰하지 않는다).
두 후보 모두 train/validation만으로 학습·검증하고 artifact를 저장하며(test holdout 미접근),
**validation f1으로 후보를 선택한 뒤에만** 선택된 후보 하나에 대해 test holdout을 한 번 열어
최종 지표·오류 사례를 계산한다(리뷰 P1 — 낙선 후보의 test 지표는 아예 계산하지 않는다).
artifact는 작업별 유일 `job_id` 아래 불변 경로에 저장하며(덮어쓰기 금지) 보고서에
`artifactChecksum`·`datasetSnapshotDigest`를 남긴다. 추론(`score_from_artifact`)은
`input_feature_names`와 `expected_checksum`을 모두 필수로 받아, 열 순서를 검증·재정렬하고
파일을 읽기 전에 SHA-256을 대조해 변조된 아티팩트(예: threshold 변경)를 거부한다. 모델별
입력 rank도 검증한다(리뷰 P1, 2차) — dense는 `ndim==2`, lstm은 `ndim==3` 및
`shape[-2]==seq_len`을 강제해, 1차원/3차원 입력이나 다른 길이의 시퀀스가 조용히 판정되는
것을 막는다.

```bash
# 기본(specimen_group)은 CWRU에서 InsufficientAssetGroupsError로 정직하게 실패
python ai/ai1/week4/ai1/freq_baseline/train_and_evaluate.py --split-strategy operating_condition_holdout
# 결과: ai/ai1/week4/ai1/data/training_job_report.json  (metrics.independentHoldout == false)
#       (+ data/models/<job_id>/*.pt)
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

- **specimen 독립 평가**: CWRU는 NORMAL 물리 베어링이 1개뿐이라 specimen 독립 holdout이
  불가능하다. 현재 데모 지표(`operating_condition_holdout`)는 `independentHoldout=false`이며
  일반화 성능이 아니다. NORMAL 독립 베어링(현장 정상 데이터 또는 추가 CWRU 베이스라인)
  확보 후 `split_strategy="specimen_group"`로 재검증
- `vibration_peak_hz`(RPM 프록시)는 `operating_condition_holdout`이 부하 기준 분할이라
  train/validation에서 값 범위가 겹치지 않아 모델 입력에서 제외했다 (매니페스트에는 유지)
- LSTM 후보는 시퀀스 청크 수가 Dense보다 적다 — 실측 데이터로 늘어나면 재검증 필요
- 음향(acoustic) 모달리티는 아직 없음 — MIMII 데이터 확보 후 같은 구조로 별도 후보 추가
- `artifactUri`는 로컬 파일을 가리키는 표준 `file://` URI(`Path(...).resolve().as_uri()`)
  — 운영 전 오브젝트 스토리지 URI로 교체
- 대상 모터·센서 확정 후 전이학습, 임계값 재보정, 드리프트 모니터링 진행
  (`freq_baseline/freq_baseline_format.md`의 "현장 보정 계획" 참고)
