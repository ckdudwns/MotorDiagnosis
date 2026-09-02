"""
모델·기준선 버전 등록/승인/롤백 (AI-1, 4주차 DATASET_MODEL_01)

API 명세서 v1.3 06_후속개발API의 FUT-005~007(모델 버전 등록/승인/롤백),
FUT-010~011(기준선 버전 조회/등록) 계약을 그대로 데이터 구조로 옮긴다.
근거는 dataset_version_format.md 참고.
"""

import copy
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(timestamp: str, *, context: str) -> datetime:
    """approvedAt 문자열을 UTC datetime으로 파싱한다.

    [리뷰 P1] 문자열 비교(`>=`)나 배열 순서는 신뢰할 수 있는 정렬 기준이 아니다 —
    형식이 다르거나(예: 'Z' 접미사 vs '+00:00') 배열이 뒤집혀 전달되면 실제로는
    더 최신인 버전이 "이전" 버전으로 오판된다. 실제 시각으로 파싱·정규화해 비교한다.
    """
    if not timestamp or not isinstance(timestamp, str):
        raise ValueError(f"{context}: approvedAt이 비어 있습니다: {timestamp!r}")
    normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{context}: approvedAt을 파싱할 수 없습니다: {timestamp!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# ModelVersion (FUT-005~007)
# ---------------------------------------------------------------------------


_REQUIRED_MODEL_VERSION_FIELDS = ("version", "artifact_uri", "dataset_id", "baseline_version")


def register_model_version(
    *, version: str, artifact_uri: str, dataset_id: str, baseline_version: str, metrics: dict
) -> dict:
    """새 모델 버전을 status="registered"로 등록한다 (FUT-005).

    [리뷰 P1] 필수 문자열이 비어 있거나 metrics가 유효한 dict가 아니면 거부한다 —
    불완전한 레코드가 등록·승인 상태까지 조용히 전이되는 것을 막는다.
    """
    values = {
        "version": version,
        "artifact_uri": artifact_uri,
        "dataset_id": dataset_id,
        "baseline_version": baseline_version,
    }
    blank = [
        name
        for name in _REQUIRED_MODEL_VERSION_FIELDS
        if not isinstance(values[name], str) or not values[name].strip()
    ]
    if blank:
        raise ValueError(f"다음 필드는 비어있지 않은 문자열이어야 합니다: {blank}")
    if not isinstance(metrics, dict):
        raise ValueError(f"metrics는 dict여야 합니다: {metrics!r}")
    return {
        "version": version,
        "artifactUri": artifact_uri,
        "datasetId": dataset_id,
        "baselineVersion": baseline_version,
        "metrics": copy.deepcopy(metrics),  # 호출자가 이후 원본 metrics를 바꿔도 안전
        "status": "registered",
        "createdAt": _now_iso(),
    }


def approve_model_version(
    model_version: dict, *, approved_by: str, reason: str, metric_snapshot: dict
) -> dict:
    """status가 registered인 모델 버전만 승인할 수 있다 (FUT-006).

    [리뷰 P1] approved_by와 metric_snapshot을 모두 명시적으로 필수 요구한다 — 이전엔
    reason만으로 승인이 가능해 승인자가 기록되지 않았고, metric_snapshot을 생략하면
    등록 시점 metrics를 암묵적으로 재사용해 "승인 당시 실제로 검토한 지표"를
    감사할 수 없었다. 호출자가 승인 시점에 검토한 지표를 매번 명시적으로 제출해야
    한다.
    """
    if model_version.get("status") != "registered":
        raise ValueError(
            f"registered 상태만 승인할 수 있습니다 (현재 status={model_version.get('status')!r})."
        )
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")
    if not isinstance(metric_snapshot, dict) or not metric_snapshot:
        raise ValueError(
            f"metric_snapshot은 비어있지 않은 dict로 명시적으로 제출해야 합니다: {metric_snapshot!r}"
        )

    # 얕은 dict()는 중첩 metrics(혼동행렬 등)를 원본과 공유한다 — 승인 후 원본
    # metric_snapshot을 수정하면 승인 당시 근거까지 바뀐다. 깊은 복사한다.
    approved = copy.deepcopy(model_version)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["metricSnapshot"] = copy.deepcopy(metric_snapshot)
    approved["approvedAt"] = _now_iso()
    return approved


def rollback_model_version(
    current: dict,
    target: dict,
    *,
    reason: str,
    target_environment: str,
    approved_history: list[dict] | None = None,
) -> dict:
    """target으로 롤백하는 액션 레코드를 만든다 (FUT-007).

    "이전 승인 버전만 롤백 대상 허용" — target은 다음을 모두 만족해야 한다:
    - `status == "approved"`
    - `current`와 다른 버전 (자기 자신으로의 롤백 금지)
    - **`current`보다 앞선 승인 버전** — `target.approvedAt < current.approvedAt`(UTC
      기준 실제 파싱·비교)를 요구한다 (더 최신/동일 버전으로의 "롤백"을 차단).

    [리뷰 P1] `approved_history`가 주어져도 그 **배열 순서를 신뢰하지 않는다** —
    이전에는 배열 index로 계보 순서를 판단해, history를 역순으로 넘기면 실제로는
    더 최신인 버전으로의 forward rollback이 통과했다. 대신 history 각 항목의
    `approvedAt`을 실제로 파싱해 시간순으로 비교한다. current/target이 모두 유일한
    버전으로 계보에 있는지도 함께 검증한다.

    배포와 롤백은 별도 작업으로 기록한다(반환값은 current/target을 바꾸지 않고
    새 액션 레코드만 만든다).
    """
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")
    if target.get("status") != "approved":
        raise ValueError(
            f"승인된(approved) 버전으로만 롤백할 수 있습니다 "
            f"(target status={target.get('status')!r})."
        )
    if current.get("version") is None or target.get("version") is None:
        raise ValueError("current/target 모두 version이 필요합니다.")
    if current["version"] == target["version"]:
        raise ValueError(
            f"자기 자신({target['version']})으로는 롤백할 수 없습니다."
        )

    if approved_history is not None:
        versions = [item.get("version") for item in approved_history]
        if len(set(versions)) != len(versions):
            raise ValueError(f"approved_history에 중복 버전이 있습니다: {versions}")

        def _canonical_entry(label: str, subject: dict) -> dict:
            # [리뷰 P1, 2차] history가 주어지면 current/target 모두 그 안에서
            # 정확히 한 건의 **승인된** canonical 레코드로 존재해야 한다. 이전에는
            # current가 계보에 없으면 호출자가 넘긴 current 객체 자체의
            # approvedAt으로 대체했는데, 그러면 실제로는 registered 상태인
            # current에 approvedAt만 위조해 심어 놓고 history에는 target만 넣는
            # 방식으로 계보 검증을 완전히 우회할 수 있었다. 계보가 주어졌다면
            # 계보 밖 값으로 대체하지 않고 무조건 거부한다.
            entries = [
                item for item in approved_history if item.get("version") == subject.get("version")
            ]
            if not entries:
                raise ValueError(
                    f"{label} {subject.get('version')!r}이 승인 계보에 없습니다: {versions}. "
                    "계보가 주어지면 호출자 객체 자체의 값으로 대체하지 않습니다."
                )
            entry = entries[0]
            if entry.get("status") != "approved":
                raise ValueError(
                    f"{label} {subject.get('version')!r}의 계보 레코드가 승인(approved) "
                    f"상태가 아닙니다 (status={entry.get('status')!r})."
                )
            return entry

        target_entry = _canonical_entry("target", target)
        current_entry = _canonical_entry("current", current)

        target_approved_at_raw = target_entry.get("approvedAt")
        target_approved_at_dt = _parse_utc(
            target_approved_at_raw, context=f"target({target['version']!r})"
        )
        current_approved_at_dt = _parse_utc(
            current_entry.get("approvedAt"), context=f"current({current['version']!r})"
        )

        if target_approved_at_dt >= current_approved_at_dt:
            raise ValueError(
                f"target(approvedAt={target_approved_at_dt.isoformat()})은 current"
                f"(approvedAt={current_approved_at_dt.isoformat()})보다 먼저 승인된 "
                "버전이어야 합니다 (실제 승인 시각 기준)."
            )
        target_approved_at = target_approved_at_raw
    else:
        target_approved_at_raw = target.get("approvedAt")
        current_approved_at_raw = current.get("approvedAt")
        if not target_approved_at_raw or not current_approved_at_raw:
            raise ValueError(
                "approved_history가 없으면 current/target 모두 approvedAt이 필요합니다 "
                "(계보 검증용)."
            )
        target_approved_at_dt = _parse_utc(
            target_approved_at_raw, context=f"target({target['version']!r})"
        )
        current_approved_at_dt = _parse_utc(
            current_approved_at_raw, context=f"current({current['version']!r})"
        )
        if target_approved_at_dt >= current_approved_at_dt:
            raise ValueError(
                f"target(approvedAt={target_approved_at_raw})은 current"
                f"(approvedAt={current_approved_at_raw})보다 먼저 승인된 버전이어야 합니다."
            )
        target_approved_at = target_approved_at_raw

    return {
        "action": "rollback",
        "fromVersion": current["version"],
        "toVersion": target["version"],
        "targetApprovedAt": target_approved_at,
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
