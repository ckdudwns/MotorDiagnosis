"""
모델·기준선 버전 등록/승인/롤백 (AI-1, 4주차 DATASET_MODEL_01)

API 명세서 v1.2 06_후속개발API의 FUT-005~007(모델 버전 등록/승인/롤백),
FUT-010~011(기준선 버전 조회/등록) 계약을 그대로 데이터 구조로 옮긴다.
근거는 dataset_version_format.md 참고.
"""

import copy
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ModelVersion (FUT-005~007)
# ---------------------------------------------------------------------------


def register_model_version(
    *, version: str, artifact_uri: str, dataset_id: str, baseline_version: str, metrics: dict
) -> dict:
    """새 모델 버전을 status="registered"로 등록한다 (FUT-005)."""
    return {
        "version": version,
        "artifactUri": artifact_uri,
        "datasetId": dataset_id,
        "baselineVersion": baseline_version,
        "metrics": copy.deepcopy(metrics),  # 호출자가 이후 원본 metrics를 바꿔도 안전
        "status": "registered",
        "createdAt": _now_iso(),
    }


def approve_model_version(model_version: dict, *, reason: str, metric_snapshot: dict = None) -> dict:
    """status가 registered인 모델 버전만 승인할 수 있다 (FUT-006).

    metric_snapshot을 생략하면 현재 metrics를 그대로 스냅샷으로 저장한다 — 나중에
    지표 재계산 로직이 바뀌어도 승인 당시 근거가 남도록.
    """
    if model_version.get("status") != "registered":
        raise ValueError(
            f"registered 상태만 승인할 수 있습니다 (현재 status={model_version.get('status')!r})."
        )
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    # 얕은 dict()는 중첩 metrics(혼동행렬 등)를 원본과 공유한다 — 승인 후 원본
    # metrics를 수정하면 승인 당시 근거(metricSnapshot)까지 바뀐다. 깊은 복사한다.
    approved = copy.deepcopy(model_version)
    approved["status"] = "approved"
    approved["approvalReason"] = reason
    snapshot_source = (
        metric_snapshot if metric_snapshot is not None else model_version["metrics"]
    )
    approved["metricSnapshot"] = copy.deepcopy(snapshot_source)
    approved["approvedAt"] = _now_iso()
    return approved


def rollback_model_version(
    current: dict, target: dict, *, reason: str, target_environment: str
) -> dict:
    """target으로 롤백하는 액션 레코드를 만든다 (FUT-007).

    "이전 승인 버전만 롤백 대상 허용" — target은 반드시 approved 상태여야 한다.
    배포와 롤백은 별도 작업으로 기록한다(반환값은 current/target을 바꾸지 않고
    새 액션 레코드만 만든다).
    """
    if target.get("status") != "approved":
        raise ValueError(
            f"승인된(approved) 버전으로만 롤백할 수 있습니다 "
            f"(target status={target.get('status')!r})."
        )
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    return {
        "action": "rollback",
        "fromVersion": current.get("version"),
        "toVersion": target["version"],
        "reason": reason,
        "targetEnvironment": target_environment,
        "at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# BaselineVersion (FUT-010~011)
# ---------------------------------------------------------------------------


def register_baseline_version(
    *,
    baseline_id: str,
    dataset_id: str,
    site_id: str,
    asset_id: str,
    features: dict,
    time_segment: str = None,
) -> dict:
    """week2 baseline.json과 같은 구조(mean/std/normal_range)를 설비 단위 버전으로
    등록한다. status="draft"로 시작 — 승인 전에는 배포할 수 없다 (FUT-011)."""
    return {
        "id": baseline_id,
        "datasetId": dataset_id,
        "siteId": site_id,
        "assetId": asset_id,
        "timeSegment": time_segment,
        "features": copy.deepcopy(features),  # draft 수정이 등록본에 새지 않도록
        "status": "draft",
        "createdAt": _now_iso(),
    }


def approve_baseline_version(baseline_version: dict, *, approved_by: str, reason: str) -> dict:
    if baseline_version.get("status") != "draft":
        raise ValueError(
            f"draft 상태만 승인할 수 있습니다 (현재 status={baseline_version.get('status')!r})."
        )
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    # features(mean/std/normal_range)를 참조 공유하면 draft 수정이 승인본·active
    # 기준선에까지 전파된다. 승인 결과를 깊은 복사로 독립시킨다.
    approved = copy.deepcopy(baseline_version)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["approvedAt"] = _now_iso()
    return approved


def activate_baseline_version(baseline_version: dict) -> dict:
    """승인된(approved) 기준선만 배포(active)할 수 있다 — "승인 전 배포 금지"를
    코드 레벨에서 강제한다."""
    if baseline_version.get("status") != "approved":
        raise ValueError(
            f"승인된(approved) 기준선만 배포할 수 있습니다 "
            f"(현재 status={baseline_version.get('status')!r})."
        )
    active = copy.deepcopy(baseline_version)
    active["status"] = "active"
    active["activatedAt"] = _now_iso()
    return active
