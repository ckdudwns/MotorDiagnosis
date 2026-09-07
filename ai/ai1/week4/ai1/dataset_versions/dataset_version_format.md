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
draft --freeze_dataset_version()--> frozen --DatasetVersionRegistry.approve(id)--> approved
```

**승인은 `DatasetVersionRegistry`를 통해서만 가능하다 (리뷰 P1, 6차).** `Model`/
`BaselineVersionRegistry`(model_version.py)와 같은 신뢰 경계다 — `DatasetVersionRegistry().freeze(draft)`가
`freeze_dataset_version()`(아래 v1.3 필수 필드·fingerprint·checksum 교차검증)을 실제로
거친 결과만 레지스트리 내부 저장소에 `id`로 기록하고, `registry.approve(dataset_id, ...)`는
그 `id`로 레지스트리 자신에게서 조회한 레코드만 승인 대상으로 삼는다 — 호출자가 만든
`frozen_manifest` dict를 직접 받지 않는다. 예전에는 `approve_dataset_version(frozen_manifest, ...)`이
공개 함수라서, `freeze_dataset_version()`을 거친 적 없는 임의 dict도 `compute_dataset_checksum()`/
`compute_snapshot_digest()`(둘 다 공개 함수)로 자기 자신과만 일관된 checksum/digest를
채워 넣으면 승인을 통과시킬 수 있었다. 이미 다른 프로세스/시점에 동결된 v1(legacy) 레코드는
`registry.adopt_legacy_frozen(frozen_manifest)`으로 가져온다(`trusted_legacy=True`와 같은
계약 — 호출자가 매니페스트 바깥에서 이미 확인한 사실만 전달).

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
- `DatasetVersionRegistry.approve()`는 frozen을 `copy.deepcopy`하므로 자동 승계되고,
  `dataset_version_summary()`도 깊은 복사한 뒤 `rows`만 빼므로 자동 노출된다.

### 신규 동결 필수 필드 · v1 호환 (리뷰 P1)

`freeze_dataset_version()`은 **신규 draft에 `labelPolicyVersion`·`snapshotSchemaVersion`·
`source.checksum`·`featureOutputFingerprint`가 없으면 거부**한다 — "이미 동결된 v1을
유지"하는 것과 "구형 draft를 지금 새로 동결"하는 것은 다른 요구사항이다.

**legacy(v1) 판별은 매니페스트 필드로 하지 않는다 (리뷰 P1, 2차).** 처음엔 `snapshotDigest`
부재로, 그다음엔 `snapshotSchemaVersion` 부재로 legacy를 판별했는데, 두 시도 모두
"매니페스트에서 그 필드(들)만 지우면 legacy 관용 경로로 강등된다"는 같은 구조의 우회를
허용했다(매니페스트는 호출자가 자유롭게 수정 가능한 데이터라서, 판별 기준으로 쓰는 필드가
무엇이든 함께 지우면 우회된다). 그래서 `DatasetVersionRegistry.approve`/`verify_frozen_integrity`/
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
  `DatasetVersionRegistry.approve`/`verify_frozen_integrity`/`verify_reproducibility` 모두
  `trusted_legacy`가 실제 `bool`이 아니면(예: 문자열 `"false"` — 파이썬에서 truthy)
  `TypeError`로 즉시 거부한다(리뷰 P1, 3차) — `is True`로만 완화 경로를 켠다.
- `verify_reproducibility(frozen, recomputed, *, trusted_legacy=False)`도 기본값에서는
  `datasetChecksum`뿐 아니라 `labelPolicyVersion`/`snapshotSchemaVersion`/원본
  `source.checksum`까지 함께 대조한다 — rows/labelMapping/split만 같고 원본·정책 버전이
  다른 데이터셋을 "재현 성공"으로 오판하지 않도록. 매니페스트 전체(생명주기·시간·파생
  체크섬·id 제외) 비교는 canonical JSON 문자열로 하고(리뷰 P1, 6차 — 파이썬 dict `==`는
  `False==0`/`12000==12000.0`을 참으로 봐서 JSON 타입만 바뀐 변조를 놓쳤다), `id`는
  날짜 등 가변 부분을 빼고 마지막 12자리 16진수 checksum suffix만 각자 재계산한
  `source.checksum` suffix와 일치하는지 검증한다(리뷰 P1, 6차 — 전체를 빼면 완전히
  무관한 id로 바꿔도 재현 성공으로 오판됐다).

## `checksum` vs `digest` 역할 구분

| 값 | 목적 | 입력 |
|---|---|---|
| `source.checksum` = `snapshotChecksum` | **id 재현성** — 같은 정규화 입력이면 같은 id | 원본 파일 sha256 + 전처리/분할 전략/특징 설정 + 특징 산출물 fingerprint + 정책 버전 |
| `snapshotDigest` | **변조 탐지** — 동결 이후 내용이 바뀌지 않았는가 | 매니페스트 전체(라이선스 텍스트·rowCount·splitCounts 등 포함) |

## 모델 버전 (`model_version.py`, `ModelVersionRegistry`, `ModelVersion`)

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

### 신뢰 경계 — 왜 자유 함수 + `registry` 인자가 아니라 `ModelVersionRegistry` 클래스인가 (리뷰 P1, 5차)

이전 버전은 자유 함수 `register_model_version()`/`approve_model_version()`/
`rollback_model_version()`이었고, 승인·롤백은 호출자가 만들어 넘기는 **평범한
list**(`registry`/`approved_history`)를 등록 이력·승인 계보로 신뢰했다.
`registrationDigest`/`approvalDigest`는 이 모듈의 공개 함수(`compute_registration_digest`
등)로 누구나 계산할 수 있어서, 호출자가 임의의 레코드에 그 함수로 직접 계산한
digest를 채워 넣고 `registry=[그 레코드]`(또는 `approved_history=[...]`)로
전달하면 "자기 서명"이 되어 승인·롤백이 그대로 통과했다 — "canonical 등록
이력"이라는 이름의 값이 실제로는 호출자가 무엇이든 담을 수 있는 값이었기
때문이다(3차·4차 리뷰가 digest 정합성까지는 막았지만, "이 digest 쌍을 가진
list를 통째로 지어내는" 공격 자체는 막지 못했다).

`ModelVersionRegistry`는 등록·승인된 레코드를 **인스턴스 자신이 소유**한다
(`self._entries`, 클래스 바깥에 노출하지 않음). `register()`만 여기에 새 항목을
기록하고, `approve()`/`rollback()`은 호출자가 넘긴 dict/list를 검증 입력으로
받지 않는다 — `version` 문자열로 **그 레지스트리 자체**에서 조회한 값만
신뢰한다. 새 `ModelVersionRegistry()`는 빈 저장소이므로, 그 인스턴스의
`register()`를 실제로 거치지 않은 버전은 어떤 문자열을 대도 조회되지 않는다 —
"registry=[forged]" 같은 위조가 애초에 구조적으로 불가능하다.
`register()`/`approve()`/`get()`은 항상 내부 레코드의 **깊은 복사본**만
반환해, 반환값을 호출자가 수정해도 저장소 내부 상태는 전혀 영향받지 않는다.

### API

`ModelVersionRegistry().register(*, version, artifact_uri, dataset_id, baseline_version,
metrics, artifact_checksum)`은 필수 문자열이 비어 있으면 거부하고 `metrics`가 dict가
아니어도 거부한다(리뷰 P1) — 불완전한 레코드가 승인 상태까지 조용히 전이되는 것을
막는다. `artifact_checksum`(학습 시 저장한 아티팩트의 sha256)도 필수로 받아
`artifactChecksum`으로 등록 레코드에 보존하며, `sha256:` 뒤에 64자리 16진수가
오는 형식인지도 검증한다(리뷰 P2). 같은 `version`을 이 레지스트리에 두 번
등록할 수 없다(재등록 시도는 거부 — 감사 이력이 조용히 덮어써지지 않도록). 등록
시 불변 필드 전체(`version`/`artifactUri`/`artifactChecksum`/`datasetId`/
`baselineVersion`/`metrics`, `status`/시각류 제외)에 대한 canonical sha256을
`registrationDigest`로 함께 저장한다.

상태 머신: `registered --registry.approve(version, ...)--> approved`. 승인에는
`approved_by`, `reason`, **명시적** `metric_snapshot`(승인 시점에 실제로 검토한
지표 — 승인 후 깊은 복사로 저장해, 나중에 원본 `metrics`나 재계산 로직이
바뀌어도 승인 당시 근거가 남도록)이 모두 필수다(FUT-006 계약과 동일, 리뷰 P1).
`approve()`는 `version` 문자열로 이 레지스트리 안에서 등록 레코드를 조회한다 —
전달할 수 있는 다른 내용이 없으므로 "등록 레코드와 다른 내용을 승인 대상으로
전달"하는 공격 자체가 성립하지 않는다.

`ModelVersionRegistry().rollback(current_version, target_version, *, reason,
target_environment)`은 target이 **실제로 이전 승인 버전**인지 검증한다 (리뷰 P1):

- `current_version`/`target_version` 모두 **이 레지스트리에 실제로 approve()된**
  버전이어야 하고(등록만 되고 미승인이거나, 이 레지스트리에 등록조차 된 적
  없으면 거부), `current_version != target_version`(자기 롤백 금지).
- 계보는 이 레지스트리 자신이 보유한 전체 상태에서 나온다 — 별도의
  `approved_history` 인자를 넘길 필요도, 넘길 방법도 없다(리뷰 P1, 5차. 예전에는
  이 인자를 생략하면 `current`/`target` 객체 자체의 `approvedAt` 필드를 신뢰하는
  폴백 경로가 있었다).
- 각 항목의 `approvedAt`을 실제로 파싱해(UTC 정규화) 비교한다 — **호출 순서가
  아니라 실제 승인 시각 기준**으로 target이 current보다 먼저 승인됐어야 한다
  (리뷰 P1 — 예전에는 배열 index로 계보 순서를 판단해 역순 배열을 넘기면
  우회됐다).
- `target_environment`도 비어있지 않은 허용 환경 값(`production`/`staging`/
  `development`)이어야 한다(리뷰 P1, 3차) — 임의 문자열을 그대로 기록하지 않는다.
- 반환 레코드에 `targetApprovedAt`를 함께 남긴다.

반환값은 별도의 rollback 액션 레코드(FUT-007 "배포와 롤백을 별도 작업으로 기록")다.
`rollback()` 자신은 레지스트리 상태를 바꾸지 않는다.

## 기준선 버전 (`BaselineVersionRegistry`, `BaselineVersion`, FUT-010/011)

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

`BaselineVersionRegistry`는 `ModelVersionRegistry`와 같은 신뢰 경계를 쓴다(리뷰
P1, 5차) — `register()`/`approve()`/`activate()`는 호출자가 만든 dict를 검증
입력으로 받지 않고, `baseline_id` 문자열로 이 레지스트리 자체에서 조회한 값만
신뢰한다. 이전에는 `approve_baseline_version()`/`activate_baseline_version()`이
전달받은 객체 자체의 `registrationDigest`/`approvalDigest`를 재계산·대조했는데,
그 두 함수 모두 공개 함수라서 호출자가 register/approve를 한 번도 거치지 않은
객체에도 두 digest를 직접 계산해 채워 넣으면(자기 서명) 그대로 통과했다.

`registry.register(*, baseline_id, dataset_id, site_id, asset_id, features,
time_segment=None)`은 `baseline_id`/`dataset_id`/`site_id`/`asset_id`가 비어
있거나 `features`가 `{name: {"mean", "std", "normal_range"}}` 구조·유한값(`std`는
양수)을 만족하지 않으면 거부한다(리뷰 P1). 같은 `baseline_id`를 두 번 등록할 수
없다.

`registry.approve(baseline_id, *, approved_by, reason)`은 `reason`뿐 아니라
`approved_by`도 비어있지 않은지 검증한다(리뷰 P1). `baseline_id`로 이 레지스트리
안에서 draft 레코드를 조회해 승인한다 — 등록된 적 없는 id는 거부된다.

`registry.activate(baseline_id)`는 이 레지스트리 안에서 `status == "approved"`인
레코드만 배포(active)로 전이한다 — "승인 전 배포 금지"(FUT-011 수용 기준)를
코드 레벨에서 강제한다. 등록된 적 없거나 아직 승인되지 않은 `baseline_id`는
거부된다.

## 대상 확정 후 보완

- 지금은 데이터셋이 CWRU 하나뿐이라 `rollback`은 모델 버전에만 의미가 있다. 실제
  현장 데이터가 여러 데이터셋 버전으로 쌓이면 데이터셋 버전 자체의 rollback/활성화
  개념도 필요할 수 있다.
- `artifactUri`는 지금은 로컬 파일을 가리키는 표준 `file://` URI다(train_and_evaluate가
  `Path(...).resolve().as_uri()`로 생성 — Windows 경로도 `file:///C:/...` 형태의 유효한
  URI가 된다). 실제 운영에서는 오브젝트 스토리지 URI로 교체.
