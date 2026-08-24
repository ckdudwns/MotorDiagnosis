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

## 모델 버전 (`model_version.py`, `ModelVersion`)

```json
{
  "version": "cwru-vibration-dense-ae-v1",
  "artifactUri": "file://.../model.pt",
  "datasetId": "DS-CWRU-VIBRATION-20260824",
  "baselineVersion": "2026-08-21T08:17:43.221079+00:00",
  "metrics": {"precision": 0.98, "recall": 1.0, "f1": 0.99},
  "status": "registered",
  "createdAt": "..."
}
```

상태 머신: `registered --approve_model_version()--> approved`. 승인에는 `reason`과
`metricSnapshot`(승인 시점 지표를 그대로 복사 — 나중에 지표 재계산 로직이 바뀌어도
승인 당시 근거가 남도록)이 필수다(FUT-006 계약과 동일).

`rollback_model_version(current, target, *, reason, target_environment)`은 **승인된
버전으로만** 롤백할 수 있다(`target.status == "approved"` 검증) — 검증되지 않은
버전으로 롤백하면 운영 배포가 더 나빠질 수 있으므로. 반환값은 별도의 rollback 액션
레코드(FUT-007 "배포와 롤백을 별도 작업으로 기록")다.

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

`activate_baseline_version()`은 `status == "approved"`가 아니면 예외를 던진다 —
"검증 데이터셋에 연결하고 승인 전 배포 금지"(FUT-011 수용 기준)를 코드 레벨에서 강제한다.

## 대상 확정 후 보완

- 지금은 데이터셋이 CWRU 하나뿐이라 `rollback`은 모델 버전에만 의미가 있다. 실제
  현장 데이터가 여러 데이터셋 버전으로 쌓이면 데이터셋 버전 자체의 rollback/활성화
  개념도 필요할 수 있다.
- `artifactUri`는 지금은 로컬 파일 경로 문자열이다 — 실제 운영에서는 오브젝트 스토리지
  URI로 교체.
