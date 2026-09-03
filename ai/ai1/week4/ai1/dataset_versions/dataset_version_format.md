# 데이터셋·모델 버전 관리 스키마 (DATASET_MODEL_01)

기능ID: `DATASET_MODEL_01` (4주차 실행순서 1번, AI-1 주담당)
산출: `dataset_version.py`(데이터셋 버전 동결/승인) + `model_version.py`(모델·기준선 버전 등록/승인/롤백)

기능정의(W4.2): "기존 공개·보유 데이터셋과 내부 라벨 이벤트를 학습/검증 데이터셋 버전으로
동결한다. 출처, 라이선스, 신호 종류, 샘플링률, 단위, 운전 조건, 라벨 매핑, 분할, 체크섬과
승인 상태를 관리하고 모델·기준선 버전에 연결한다. 수용: 동일 데이터셋 버전을 재현할 수
있으며 대상 모터 현장 데이터 전에는 고장 유형·RUL 성능을 보장하지 않는다."

API 매핑: `POST/GET /api/datasets`(3주차 MVP-042/043, 이미 구현됨) + `POST /api/model-versions`,
`.../approve`, `.../rollback`(후속 FUT-005~007) + `GET/POST /api/baseline-versions`(FUT-010~011).

## 왜 3주차 코드를 다시 만들지 않는가

3주차 `DATA_EXPORT_01`(`../../week3/ai1/datasets/register_dataset.py`)이 이미 CWRU
데이터셋을 `source`/`compatibility`/`labelMapping`/`split`/체크섬을 갖춘 `status: "draft"`
매니페스트로 정규화했다. 4주차 `DATASET_MODEL_01`이 새로 하는 일은 **그 draft를
불변(immutable) 버전으로 동결하고, 승인 상태를 관리하고, 모델/기준선 버전과 연결하는
것**뿐이다 — 정규화 로직 자체는 3주차 것을 그대로 가져다 쓴다.

## 데이터셋 버전 상태 머신

```
draft --freeze_dataset_version()--> frozen --approve_dataset_version()--> approved
```

- `draft`: 3주차 `build_manifest()`가 만든 상태. 언제든 재생성 가능.
- `frozen`: 동결됨. `datasetChecksum`(행 데이터 + 핵심 메타데이터의 sha256)이 함께
  저장되어, 나중에 같은 조건(같은 `seed`)으로 다시 만든 매니페스트와 체크섬을 비교해
  "동일 데이터셋 버전을 재현할 수 있다"는 수용 기준을 검증할 수 있다.
- `approved`: 운영자가 승인. 승인자(`approvedBy`)·시각(`approvedAt`)·사유(`approvalReason`)를
  함께 기록한다. **오직 `frozen` 상태에서만 승인할 수 있다** — `draft`를 바로 승인하는
  경로는 없다(동결 없이는 재현성이 보장되지 않으므로).

각 전이는 역방향으로 되돌릴 수 없다(재동결/재승인 불가 — 재시도하려면 새 버전을 만든다).

**중첩 객체 격리**: `freeze`/`approve`는 `copy.deepcopy`로 매니페스트를 독립 복사한다
(얕은 `dict()`는 `rows` 리스트를 공유해, 동결 후 원본 `rows`를 변조해도 `datasetChecksum`이
안 바뀌어 검증을 통과하는 문제가 있었다). `approve`는 복사 전에 현재 내용이 동결 시점과
일치하는지 재검증한다 — `datasetChecksum`(v1 호환) **과** `snapshotDigest`(전체 불변 필드,
아래) 둘 다 대조하고 하나라도 어긋나면 승인을 거부한다.

## `datasetChecksum` 계산 방식

```python
payload = json.dumps(
    {"rows": manifest["rows"], "labelMapping": manifest["labelMapping"], "split": manifest["split"]},
    sort_keys=True, ensure_ascii=False,
)
checksum = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`rows`(윈도우별 정규화 데이터)와 라벨 매핑·분할 비율이 바뀌면 체크섬이 달라진다.
`createdAt`처럼 실행마다 달라지는 필드는 포함하지 않는다 — 같은 `seed`로 다시 실행한
매니페스트가 같은 체크섬을 내야 재현성 검증이 의미가 있기 때문이다.

**이 계산식은 바꾸지 않는다** — 아래 v1.3 신규 필드를 추가해도 `datasetChecksum` payload는
`rows`/`labelMapping`/`split` 그대로라, 이미 동결·재현성 검증된 데이터셋이 영향을 받지 않는다.

## v1.3 신규 필드 (`labelPolicyVersion` / `snapshotSchemaVersion` / `snapshotChecksum`)

API 명세서 v1.3 `05_데이터모델`이 데이터셋 버전에 3개 필드를 추가했다. `freeze_dataset_version()`이
draft 매니페스트에서 그대로 물려받아 frozen 산출물에 남긴다:

| 필드 | 값 | 출처 |
|---|---|---|
| `labelPolicyVersion` | `LABEL-POLICY-V2` | 3주차 `register_dataset.build_manifest()`가 채운 값. 없으면(구버전 draft) `None` |
| `snapshotSchemaVersion` | `2` | 위와 동일 |
| `snapshotChecksum` | `manifest["source"]["checksum"]` | 3주차 `compute_version_checksum()` 결과를 **그대로 재사용** — 원본 파일 sha256 + 전처리/분할/특징 설정 + 실제 특징 산출물 fingerprint + **라벨 정책 버전**까지 반영된 불변 체크섬. 별도 계산 로직을 새로 두지 않는다 |

- **정책 버전은 `id`(fingerprint)에 포함**: `compute_version_checksum()` payload에
  `label_policy_version`/`snapshot_schema_version`이 들어가므로, 라벨 정책이 바뀐 신규
  데이터셋은 기존 frozen 버전과 **다른 `id`**를 받는다.
- `approve_dataset_version()`은 frozen을 `copy.deepcopy`하므로 자동 승계되고,
  `dataset_version_summary()`도 깊은 복사한 뒤 `rows`만 빼므로 자동 노출된다.

### 신규 동결 필수 필드 · v1 호환 (리뷰 P1)

`freeze_dataset_version()`은 **신규 draft에 `labelPolicyVersion`·`snapshotSchemaVersion`·
`source.checksum`·`featureOutputFingerprint`가 없으면 거부**한다 — "이미 동결된 v1을
유지"하는 것과 "구형 draft를 지금 새로 동결"하는 것은 다른 요구사항이다.

**legacy(v1) 판별은 매니페스트 필드로 하지 않는다 (리뷰 P1, 2차).** 처음엔 `snapshotDigest`
부재로, 그다음엔 `snapshotSchemaVersion` 부재로 legacy를 판별했는데, 두 시도 모두
"매니페스트에서 그 필드(들)만 지우면 legacy 관용 경로로 강등된다"는 같은 구조의 우회를
허용했다(매니페스트는 호출자가 자유롭게 수정 가능한 데이터라서, 판별 기준으로 쓰는 필드가
무엇이든 함께 지우면 우회된다). 그래서 `approve_dataset_version`/`verify_frozen_integrity`/
`verify_reproducibility`는 이제 매니페스트를 보고 추정하지 않고, 호출자가 매니페스트
**바깥의** 신뢰 가능한 저장소(최초 동결 시점에 별도로 기록해 둔 스키마 버전 메타데이터 등)
에서 확인한 사실을 `trusted_legacy=True`로 명시적으로 전달할 때만 legacy 완화 검증
(`datasetChecksum` 폴백)을 적용한다. 기본값(`trusted_legacy=False`)에서는 항상 v1.3 엄격
검증을 적용하므로, schema/digest가 없는 레코드는 legacy로 봐주지 않고 무조건 거부한다.
`is_legacy_v1_frozen(frozen)`은 이제 순수 정보성 추정 함수로만 남는다(무결성 검증에는
쓰이지 않음).

### `featureOutputFingerprint` — draft의 id/checksum이 현재 rows를 정직하게 반영하는지 (리뷰 P1, 2차)

`build_manifest()`가 계산하는 `source.checksum`(및 거기서 파생된 `id`)은 원본 파일·전처리
설정·**실제 산출된 rows**로 결정된다. 그런데 `freeze_dataset_version()`은 이 값을
재검증 없이 그대로 복사만 했다 — draft를 만든 뒤 `id`/`source.checksum`은 그대로 둔 채
`rows`(라벨 등)만 바꿔도 동결이 통과해서, 서로 다른 rows(그래서 다른 `datasetChecksum`)를
가진 두 데이터셋이 같은 `id`·`snapshotChecksum`으로 모두 승인될 수 있었다.
`featureOutputFingerprint`는 rows만으로 결정되는 값(원본 파일 접근 불필요)이라, freeze
시점에 draft가 신고한 값과 현재 rows에서 다시 계산한 값을 대조해 build 이후 rows가 바뀐
draft의 동결을 거부한다.

### `checksumInputs` / `compute_source_checksum()` — freeze 시점 canonical 재계산 (리뷰 P1, 3차)

`featureOutputFingerprint` 대조만으로는 rows를 바꾸고 fingerprint를 새 rows에 맞게
**함께** 재계산해 신고하거나(현재 rows와의 대조는 통과), fingerprint에는 들어가지
않지만 `source.checksum` 계산에는 들어가는 필드(`labelMapping` 등)만 바꾸는 공격을
잡지 못한다 — `id`/`source.checksum`이 예전 rows/설정 기준 값 그대로 방치돼도
동결·승인이 통과했다.

`build_manifest()`는 이제 `compute_version_checksum()` payload 중 rows/labelMapping/
split만으로는 재현 불가능한 나머지 입력(`window_size`, `hop_size`, `seed`,
`feature_pipeline_version`, `feature_config`, `split_strategy` 키)을 draft의
`checksumInputs` 필드로 그대로 노출한다. `dataset_version.compute_source_checksum(manifest)`는
week3 모듈을 import하지 않고(`compute_rows_fingerprint`와 같은 이유) 이 필드들과
`source.files`/`split`/`labelTaxonomyVersion`/`labelMapping`/`featureOutputFingerprint`/
`labelPolicyVersion`/`snapshotSchemaVersion`만으로 `compute_version_checksum()`과
동일한 payload를 독립 재구성해 sha256을 재계산한다.

- `freeze_dataset_version()`은 신규 필수 필드에 `checksumInputs`를 추가하고,
  fingerprint 대조 이후 이 재계산 값을 `manifest["source"]["checksum"]`과 대조하며,
  `id`가 재계산 checksum의 hex suffix(12자)로 끝나는지도 검증한다 — 어느 한쪽만
  뒤처져도 동결을 거부한다.
- `verify_reproducibility(frozen, recomputed, *, trusted_legacy=False)`도
  `checksumInputs`가 양쪽에 있으면 `source.checksum` **필드값**을 서로 비교하지
  않고, 각자 내용으로 `compute_source_checksum()`을 독립 재계산해 대조한다 —
  `recomputed_manifest["source"]["checksum"]` 필드 자체가(재계산 없이) 다른 값으로
  위조돼도 잡아낸다.

## `snapshotDigest` — 전체 불변 필드 변조 탐지 (리뷰 P1)

`datasetChecksum`은 `rows`/`labelMapping`/`split`만 보므로, freeze 이후 `source.license`,
`source.checksum`, `compatibility.samplingRateHz`, `labelPolicyVersion`,
`snapshotSchemaVersion`, `splitStrategy`, `independentHoldout` 등을 바꿔도 승인이
통과했다. `compute_snapshot_digest()`는 휘발성 키(`status`, `createdAt`, `frozenAt`,
`approvedAt`, `approvedBy`, `approvalReason`, `datasetChecksum`, `snapshotDigest`)를 뺀
**매니페스트 전체**의 canonical sha256이다.

- `freeze`가 `frozen["snapshotDigest"]`를 저장한다(v1.3 필드 세팅 후).
- `approve`는 `datasetChecksum`(v1 호환) **+** `snapshotDigest`를 모두 재검증하고,
  둘 중 하나라도 어긋나면 승인을 거부한다.
- `verify_frozen_integrity(frozen, *, trusted_legacy=False)`는 같은 검증을 학습·배포
  시작 전에 하도록 노출한 함수다 — 하류(`train_and_evaluate.run_training_job`)가
  `status=="frozen"`만 보지 않고 이걸 호출한다. `trusted_legacy=True`일 때만
  `datasetChecksum`으로 폴백하고, 기본값에서는 `snapshotDigest`가 없으면 그 자체로 거부한다.
  `approve_dataset_version`/`verify_frozen_integrity`/`verify_reproducibility` 모두
  `trusted_legacy`가 실제 `bool`이 아니면(예: 문자열 `"false"` — 파이썬에서 truthy)
  `TypeError`로 즉시 거부한다(리뷰 P1, 3차) — `is True`로만 완화 경로를 켠다.
- `verify_reproducibility(frozen, recomputed, *, trusted_legacy=False)`도 기본값에서는
  `datasetChecksum`뿐 아니라 `labelPolicyVersion`/`snapshotSchemaVersion`/원본
  `source.checksum`까지 함께 대조한다 — rows/labelMapping/split만 같고 원본·정책 버전이
  다른 데이터셋을 "재현 성공"으로 오판하지 않도록.

## `checksum` vs `digest` 역할 구분

| 값 | 목적 | 입력 |
|---|---|---|
| `source.checksum` = `snapshotChecksum` | **id 재현성** — 같은 정규화 입력이면 같은 id | 원본 파일 sha256 + 전처리/분할 전략/특징 설정 + 특징 산출물 fingerprint + 정책 버전 |
| `snapshotDigest` | **변조 탐지** — 동결 이후 내용이 바뀌지 않았는가 | 매니페스트 전체(라이선스 텍스트·rowCount·splitCounts 등 포함) |

## 모델 버전 (`model_version.py`, `ModelVersion`)

```json
{
  "version": "cwru-vibration-dense-ae-v1",
  "artifactUri": "file:///C:/.../model.pt",
  "datasetId": "DS-CWRU-VIBRATION-20260824",
  "baselineVersion": "2026-08-21T08:17:43.221079+00:00",
  "metrics": {"precision": 0.98, "recall": 1.0, "f1": 0.99},
  "status": "registered",
  "createdAt": "..."
}
```

`register_model_version()`은 필수 문자열(`version`/`artifact_uri`/`dataset_id`/
`baseline_version`/`artifact_checksum`)이 비어 있으면 거부하고 `metrics`가 dict가
아니어도 거부한다(리뷰 P1) — 불완전한 레코드가 승인 상태까지 조용히 전이되는 것을
막는다. `artifact_checksum`(학습 시 저장한 아티팩트의 sha256)도 필수로 받아
`artifactChecksum`으로 등록 레코드에 보존한다(리뷰 P1, 3차). 등록 시 불변 필드
전체(`version`/`artifactUri`/`artifactChecksum`/`datasetId`/`baselineVersion`/
`metrics`, `status`/시각류 제외)에 대한 canonical sha256을 `registrationDigest`로
함께 저장한다.

상태 머신: `registered --approve_model_version()--> approved`. 승인에는 `approved_by`,
`reason`, **명시적** `metricSnapshot`(승인 시점에 실제로 검토한 지표 — 승인 후 깊은
복사로 저장해, 나중에 원본 `metrics`나 재계산 로직이 바뀌어도 승인 당시 근거가 남도록)이
모두 필수다(FUT-006 계약과 동일, 리뷰 P1). 등록 시점 `metrics`를 암묵적으로 재사용하는
기본값은 없다 — 누가·어떤 지표로 승인했는지 감사할 수 있어야 한다. 기준선 `features`도
`register`/`approve`/`activate` 각 단계에서 깊은 복사해, draft 수정이 승인본·active
기준선으로 새지 않는다.

`approve_model_version(model_version, *, approved_by, reason, metric_snapshot, registry=None)`은
승인을 canonical 등록 레코드에 결속한다(리뷰 P1, 3차) — 예전에는 `status`만
`"registered"`이면 `version`/`artifactUri`/`datasetId`/`baselineVersion`이 없는 임의
객체도, 정상 등록 결과를 등록 이후에 변조한 객체도 그대로 승인됐다.

- `registry`(호출자가 유지하는 canonical 등록 이력, `rollback_model_version`의
  `approved_history`와 같은 패턴)가 주어지면 `version`으로 그 안에서 정확히 한 건의
  `registered` 레코드를 조회해 그 레코드를 승인 대상으로 삼는다 — 전달된
  `model_version`이 그 레코드와 내용이 다르면(예: `artifactUri`를 바꿔 전달) 거부한다.
- `registry`가 없으면 `model_version` 자신의 `registrationDigest`가 현재 내용과
  일치하는지 재계산·대조한다 — 이 필드가 없는(`register_model_version()`을 거치지
  않은) 레코드는 무조건 거부한다.

`rollback_model_version(current, target, *, reason, target_environment, approved_history)`은
target이 **실제로 이전 승인 버전**인지 검증한다 (리뷰 P1):

- `target.status == "approved"` 이고 `current.version != target.version` (자기 롤백 금지)
- `approved_history`는 **필수** 인자다(리뷰 P1, 3차) — 예전에는 이 인자를 생략하면
  `current`/`target` 객체 자체의 `approvedAt` 필드를 그대로 신뢰하는 폴백 경로가 있어서,
  `registered` 상태인 `current`에 임의의 `approvedAt`만 넣어도(실제 승인 이력 없이)
  과거 `target`으로의 롤백 액션이 만들어졌다. 이제 호출자가 신뢰 가능한 승인 이력(또는
  canonical registry)을 항상 제공해야 하고, `current`/`target`은 반드시 그 계보 안에서
  조회한 값만 쓴다.
- target/current가 계보 안에서 **정확히 한 건의 승인(`status == "approved"`) canonical
  레코드**로 존재해야 한다(리뷰 P1, 2차) — 계보에 없으면 호출자가 넘긴 current/target
  객체 자체의 값으로 대체하지 않고 무조건 거부한다. 그다음 각 항목의 `approvedAt`을
  실제로 파싱해(UTC 정규화) **배열의 index 순서가 아니라 시간순으로 비교**한다 — history가
  시간 역순으로 전달돼도 forward rollback을 막는다(이전에는 배열 index로 계보 순서를
  판단해 역순 배열을 넘기면 우회됐다).
- `target_environment`도 비어있지 않은 허용 환경 값(`production`/`staging`/
  `development`)이어야 한다(리뷰 P1, 3차) — 임의 문자열을 그대로 기록하지 않는다.
- 반환 레코드에 `targetApprovedAt`를 함께 남긴다.

반환값은 별도의 rollback 액션 레코드(FUT-007 "배포와 롤백을 별도 작업으로 기록")다.

## 기준선 버전 (`BaselineVersion`, FUT-010/011)

week2 `baseline.json`과 같은 구조(`mean`/`std`/`normal_range`)를 `features`에 담되,
설비·시간대 단위로 버전 관리한다:

```json
{
  "id": "BL-SITE-01-MOT-02-20260824",
  "datasetId": "DS-CWRU-VIBRATION-20260824",
  "siteId": "SITE-01",
  "assetId": "SITE-01-MOT-02",
  "timeSegment": null,
  "features": { "...": {"mean": 0, "std": 0, "normal_range": [0, 0]} },
  "status": "draft"
}
```

`register_baseline_version()`은 `baseline_id`/`dataset_id`/`site_id`/`asset_id`가
비어 있거나 `features`가 `{name: {"mean", "std", "normal_range"}}` 구조·유한값(`std`는
양수)을 만족하지 않으면 거부한다(리뷰 P1) — 등록 시점에 검증된 내용만
`registrationDigest`(불변 필드 canonical sha256)로 봉인해 이후 approve/activate가
변조·누락을 탐지한다.

`approve_baseline_version(baseline_version, *, approved_by, reason)`은 `reason`뿐 아니라
`approved_by`도 비어있지 않은지 검증한다(리뷰 P1 — 예전에는 `approved_by` 검증이
누락돼 공백 승인자가 승인 레코드에 그대로 남을 수 있었다). 승인 대상 객체 자체의
식별 필드·features 구조도 재검증하고, `registrationDigest`가 등록 시점 내용과
일치하는지 대조해 `register_baseline_version()`을 거치지 않은(또는 등록 이후 변조된)
객체의 승인을 거부한다.

`activate_baseline_version()`은 `status == "approved"`가 아니면 예외를 던진다 —
"검증 데이터셋에 연결하고 승인 전 배포 금지"(FUT-011 수용 기준)를 코드 레벨에서
강제한다. `status`만 맞춘 임의 객체(ID·features 없음)가 그대로 active로 전이되지
않도록(리뷰 P1) 식별 필드·features를 다시 검증하고, `registrationDigest`가 승인~
활성화 사이 변조되지 않았는지도 대조한다.

## 대상 확정 후 보완

- 지금은 데이터셋이 CWRU 하나뿐이라 `rollback`은 모델 버전에만 의미가 있다. 실제
  현장 데이터가 여러 데이터셋 버전으로 쌓이면 데이터셋 버전 자체의 rollback/활성화
  개념도 필요할 수 있다.
- `artifactUri`는 지금은 로컬 파일을 가리키는 표준 `file://` URI다(train_and_evaluate가
  `Path(...).resolve().as_uri()`로 생성 — Windows 경로도 `file:///C:/...` 형태의 유효한
  URI가 된다). 실제 운영에서는 오브젝트 스토리지 URI로 교체.
