"""
데이터셋 버전 동결/승인 상태 머신 (AI-1, 4주차 DATASET_MODEL_01)

3주차 DATA_EXPORT_01(../../week3/ai1/datasets/register_dataset.py)이 만든 "draft"
매니페스트를 동결(frozen)하고 승인(approved)하는 상태 전이만 다룬다 — 데이터셋을
정규화하는 로직 자체는 3주차 것을 그대로 재사용하고 중복 구현하지 않는다.

상태 머신: draft -> frozen -> approved (역방향 전이 없음, 재시도하려면 새 버전을 만든다)
근거는 dataset_version_format.md 참고.
"""

import copy
import json
import hashlib
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_dataset_checksum(manifest: dict) -> str:
    """rows + labelMapping + split만으로 체크섬을 계산한다 (createdAt 등 실행마다
    달라지는 필드는 제외 — 같은 seed로 재생성한 매니페스트가 같은 체크섬을 내야
    재현성 검증(수용 기준)이 의미가 있다)."""
    payload = json.dumps(
        {
            "rows": manifest["rows"],
            "labelMapping": manifest["labelMapping"],
            "split": manifest["split"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def freeze_dataset_version(manifest: dict) -> dict:
    """status가 draft인 매니페스트를 frozen으로 전이한다. 원본은 변경하지 않는다."""
    if manifest.get("status") != "draft":
        raise ValueError(
            f"draft 상태만 동결할 수 있습니다 (현재 status={manifest.get('status')!r})."
        )

    # 얕은 dict()는 중첩 rows 리스트를 원본과 공유한다 — 동결 후 원본 rows를
    # 변형하면 frozen 데이터도 함께 바뀌는데 datasetChecksum은 그대로라, 변조된
    # 데이터가 검증을 통과해 버린다. 깊은 복사로 동결 시점 내용을 독립 보존한다.
    frozen = copy.deepcopy(manifest)
    frozen["status"] = "frozen"
    frozen["datasetChecksum"] = compute_dataset_checksum(manifest)
    # API 명세서 v1.3: 라벨 정책·snapshot 스키마 버전과 동결 시점 snapshot checksum을
    # 함께 남긴다. snapshotChecksum은 3주차 compute_version_checksum() 결과
    # (source.checksum)를 그대로 재사용한다 — 원본 파일·전처리·특징 산출물·라벨
    # 정책까지 반영된 불변 체크섬이다. 구버전 draft(해당 키 없음)는 None으로 남겨
    # 기존 frozen 데이터셋을 재계산·변경하지 않는다.
    frozen["labelPolicyVersion"] = manifest.get("labelPolicyVersion")
    frozen["snapshotSchemaVersion"] = manifest.get("snapshotSchemaVersion")
    frozen["snapshotChecksum"] = manifest.get("source", {}).get("checksum")
    frozen["frozenAt"] = _now_iso()
    return frozen


def approve_dataset_version(frozen_manifest: dict, *, approved_by: str, reason: str) -> dict:
    """status가 frozen인 데이터셋 버전만 승인할 수 있다."""
    if frozen_manifest.get("status") != "frozen":
        raise ValueError(
            f"frozen 상태만 승인할 수 있습니다 (현재 status={frozen_manifest.get('status')!r})."
        )
    if not approved_by:
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    # 동결 이후 내용이 바뀌지 않았는지 승인 직전에 다시 검증한다 — 동결 시점
    # 체크섬과 현재 내용의 체크섬이 어긋나면(중첩 객체 변조 등) 승인을 거부한다.
    current_checksum = compute_dataset_checksum(frozen_manifest)
    if current_checksum != frozen_manifest.get("datasetChecksum"):
        raise ValueError(
            "동결 이후 매니페스트 내용이 변경되어 승인할 수 없습니다 "
            f"(frozen={frozen_manifest.get('datasetChecksum')!r}, "
            f"current={current_checksum!r})."
        )

    approved = copy.deepcopy(frozen_manifest)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["approvedAt"] = _now_iso()
    return approved


def verify_reproducibility(frozen_manifest: dict, recomputed_manifest: dict) -> bool:
    """동일 조건(같은 seed)으로 다시 만든 draft 매니페스트가 frozen 시점과 같은
    체크섬을 내는지 검증한다 (DATASET_MODEL_01 수용 기준: "동일 데이터셋 버전을
    재현할 수 있다")."""
    return frozen_manifest["datasetChecksum"] == compute_dataset_checksum(recomputed_manifest)


def dataset_version_summary(manifest: dict) -> dict:
    """GET /api/datasets/{id} 응답에 가까운, rows를 뺀 요약 뷰.

    얕은 dict comprehension은 labelMapping·split 같은 중첩 객체를 원본과 공유한다
    — 호출자가 요약 결과의 labelMapping만 바꿔도 frozen·approved 매니페스트의
    라벨 매핑이 함께 변하는데 status/datasetChecksum은 그대로라, 승인 내용과
    체크섬이 조용히 어긋난다. 깊은 복사로 요약 뷰를 원본과 완전히 분리한다
    (rows를 이미 뺐으므로 비용도 작다).
    """
    return copy.deepcopy({k: v for k, v in manifest.items() if k != "rows"})
