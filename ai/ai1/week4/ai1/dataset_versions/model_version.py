"""
모델·기준선 버전 등록/승인/롤백 (AI-1, 4주차 DATASET_MODEL_01)

API 명세서 v1.3 06_후속개발API의 FUT-005~007(모델 버전 등록/승인/롤백),
FUT-010~011(기준선 버전 조회/등록) 계약을 그대로 데이터 구조로 옮긴다.
근거는 dataset_version_format.md 참고.
"""

import copy
import hashlib
import json
import math
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


_REQUIRED_MODEL_VERSION_FIELDS = (
    "version",
    "artifact_uri",
    "dataset_id",
    "baseline_version",
    "artifact_checksum",
)

# registrationDigest 계산에서 뺀다 — 등록 이후 상태 전이마다 달라지는 키.
_VOLATILE_MODEL_VERSION_KEYS = frozenset(
    {
        "status",
        "createdAt",
        "approvedAt",
        "approvedBy",
        "approvalReason",
        "metricSnapshot",
        "registrationDigest",
    }
)


def compute_registration_digest(model_version: dict) -> str:
    """등록 레코드의 **모든 불변 필드**(version/artifactUri/artifactChecksum/
    datasetId/baselineVersion/metrics)에 대한 canonical sha256.

    [리뷰 P1] `approve_model_version()`이 `model_version.get("status")`만 확인하고
    나머지 내용을 신뢰하면, 호출자가 `{"status": "registered"}`처럼 필수 정보가
    없는 객체를 직접 만들어 승인하거나, 정상 등록 결과의 `version`/`artifactUri`를
    등록 이후에 바꿔서 승인할 수 있었다. 등록 시점에 이 digest를 계산해 레코드에
    실어 두고, 승인 시 재계산·대조해 등록 이후 변조나 필드 누락을 잡아낸다.
    """
    stable = {k: v for k, v in model_version.items() if k not in _VOLATILE_MODEL_VERSION_KEYS}
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register_model_version(
    *,
    version: str,
    artifact_uri: str,
    dataset_id: str,
    baseline_version: str,
    metrics: dict,
    artifact_checksum: str,
) -> dict:
    """새 모델 버전을 status="registered"로 등록한다 (FUT-005).

    [리뷰 P1] 필수 문자열이 비어 있거나 metrics가 유효한 dict가 아니면 거부한다 —
    불완전한 레코드가 등록·승인 상태까지 조용히 전이되는 것을 막는다.
    `artifact_checksum`(학습 시 저장한 아티팩트의 sha256, `train_and_evaluate.
    _sha256_of_file()` 결과)도 필수로 받아 등록 레코드에 보존한다 — 그래야
    승인·롤백된 모델 버전이 어떤 아티팩트 바이트를 가리키는지 감사할 수 있고,
    `registrationDigest`(아래)가 아티팩트 교체까지 탐지한다.
    """
    values = {
        "version": version,
        "artifact_uri": artifact_uri,
        "dataset_id": dataset_id,
        "baseline_version": baseline_version,
        "artifact_checksum": artifact_checksum,
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

    record = {
        "version": version,
        "artifactUri": artifact_uri,
        "artifactChecksum": artifact_checksum,
        "datasetId": dataset_id,
        "baselineVersion": baseline_version,
        "metrics": copy.deepcopy(metrics),  # 호출자가 이후 원본 metrics를 바꿔도 안전
        "status": "registered",
        "createdAt": _now_iso(),
    }
    record["registrationDigest"] = compute_registration_digest(record)
    return record


def approve_model_version(
    model_version: dict,
    *,
    approved_by: str,
    reason: str,
    metric_snapshot: dict,
    registry: list[dict] | None = None,
) -> dict:
    """status가 registered인 모델 버전만 승인할 수 있다 (FUT-006).

    [리뷰 P1] approved_by와 metric_snapshot을 모두 명시적으로 필수 요구한다 — 이전엔
    reason만으로 승인이 가능해 승인자가 기록되지 않았고, metric_snapshot을 생략하면
    등록 시점 metrics를 암묵적으로 재사용해 "승인 당시 실제로 검토한 지표"를
    감사할 수 없었다. 호출자가 승인 시점에 검토한 지표를 매번 명시적으로 제출해야
    한다.

    [리뷰 P1, 3차] 승인을 canonical 등록 레코드에 결속한다. `registry`(호출자가
    유지하는 canonical 등록 이력, `rollback_model_version`의 `approved_history`와
    같은 패턴)가 주어지면 `version`으로 그 안에서 정확히 한 건의 `registered`
    레코드를 조회해 그 레코드를 승인 대상으로 삼는다 — 전달된 `model_version`이
    등록 레코드와 다른 내용(빈 필드, 변경된 artifactUri 등)이면 거부한다.
    `registry`가 없으면 최소한 `model_version` 자신의 `registrationDigest`가
    현재 내용과 일치하는지(등록 이후 변조되지 않았는지) 검증한다 — 이 digest가
    없는(register_model_version()을 거치지 않은) 레코드는 무조건 거부한다.
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

    if registry is not None:
        version_key = model_version.get("version")
        matches = [item for item in registry if item.get("version") == version_key]
        if len(matches) != 1:
            raise ValueError(
                f"버전 {version_key!r}이 canonical 등록 레코드(registry)에 정확히 한 건으로 "
                f"존재하지 않습니다 ({len(matches)}건 발견). 계보가 주어지면 호출자가 전달한 "
                "model_version 객체 자체의 값으로 대체하지 않습니다."
            )
        registry_entry = matches[0]
        if registry_entry.get("status") != "registered":
            raise ValueError(
                f"등록 레코드 {version_key!r}가 registered 상태가 아닙니다 "
                f"(status={registry_entry.get('status')!r})."
            )
        registry_digest = registry_entry.get("registrationDigest")
        if registry_digest is None or compute_registration_digest(registry_entry) != registry_digest:
            raise ValueError(
                f"등록 레코드 {version_key!r}의 registrationDigest가 내용과 일치하지 않습니다 "
                "(등록 이후 registry 자체가 변조된 것으로 보입니다)."
            )
        if compute_registration_digest(model_version) != registry_digest:
            raise ValueError(
                f"전달된 model_version이 canonical 등록 레코드({version_key!r})와 내용이 "
                "다릅니다 — registry에 등록된 값을 승인 대상으로 씁니다."
            )
        base = registry_entry
    else:
        stored_digest = model_version.get("registrationDigest")
        if stored_digest is None:
            raise ValueError(
                "registrationDigest가 없어 승인할 수 없습니다 — register_model_version()이 "
                "만든 레코드를 그대로 전달했는지 확인하세요(무결성 검증을 우회할 수 없습니다)."
            )
        if compute_registration_digest(model_version) != stored_digest:
            raise ValueError(
                "등록 이후 model_version 내용이 변경되어 승인할 수 없습니다 "
                f"(registrationDigest registered={stored_digest!r}, "
                f"current={compute_registration_digest(model_version)!r})."
            )
        base = model_version

    # 얕은 dict()는 중첩 metrics(혼동행렬 등)를 원본과 공유한다 — 승인 후 원본
    # metric_snapshot을 수정하면 승인 당시 근거까지 바뀐다. 깊은 복사한다.
    approved = copy.deepcopy(base)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["metricSnapshot"] = copy.deepcopy(metric_snapshot)
    approved["approvedAt"] = _now_iso()
    return approved


_ALLOWED_TARGET_ENVIRONMENTS = frozenset({"production", "staging", "development"})


def rollback_model_version(
    current: dict,
    target: dict,
    *,
    reason: str,
    target_environment: str,
    approved_history: list[dict],
) -> dict:
    """target으로 롤백하는 액션 레코드를 만든다 (FUT-007).

    "이전 승인 버전만 롤백 대상 허용" — target은 다음을 모두 만족해야 한다:
    - `status == "approved"`
    - `current`와 다른 버전 (자기 자신으로의 롤백 금지)
    - **`current`보다 앞선 승인 버전** — `target.approvedAt < current.approvedAt`(UTC
      기준 실제 파싱·비교)를 요구한다 (더 최신/동일 버전으로의 "롤백"을 차단).

    [리뷰 P1, 3차] `approved_history`를 **필수** 인자로 만든다 — 예전에는 이
    인자를 생략하면 `current`/`target` 객체 자체의 `approvedAt` 필드를 그대로
    신뢰하는 폴백 경로가 있어서, `registered` 상태인 `current`에 임의의
    `approvedAt`만 넣어도(실제 승인 이력 없이) 과거 `target`으로의 롤백 액션이
    만들어졌다. 이제 호출자가 신뢰 가능한 승인 이력(또는 canonical registry)을
    항상 제공해야 하고, `current`/`target`은 반드시 그 계보 안에서 조회한 값만
    쓴다 — 호출자가 넘긴 객체 자체의 값을 신뢰하는 경로 자체가 없다.

    [리뷰 P1] `approved_history`가 주어져도 그 **배열 순서를 신뢰하지 않는다** —
    이전에는 배열 index로 계보 순서를 판단해, history를 역순으로 넘기면 실제로는
    더 최신인 버전으로의 forward rollback이 통과했다. 대신 history 각 항목의
    `approvedAt`을 실제로 파싱해 시간순으로 비교한다. current/target이 모두 유일한
    버전으로 계보에 있는지도 함께 검증한다.

    [리뷰 P1, 3차] `target_environment`도 비어있지 않은 허용 환경 값(`production`/
    `staging`/`development`)이어야 한다 — 임의 문자열을 그대로 기록하지 않는다.

    배포와 롤백은 별도 작업으로 기록한다(반환값은 current/target을 바꾸지 않고
    새 액션 레코드만 만든다).
    """
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")
    if (
        not isinstance(target_environment, str)
        or not target_environment.strip()
        or target_environment not in _ALLOWED_TARGET_ENVIRONMENTS
    ):
        raise ValueError(
            f"target_environment는 다음 중 하나여야 합니다: {sorted(_ALLOWED_TARGET_ENVIRONMENTS)} "
            f"(전달값: {target_environment!r})."
        )
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
    if not approved_history:
        raise ValueError(
            "approved_history(신뢰 가능한 승인 계보 또는 canonical registry)가 "
            "필수입니다 — 호출자가 전달한 current/target 객체 자체의 approvedAt은 "
            "신뢰하지 않습니다."
        )

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

    return {
        "action": "rollback",
        "fromVersion": current["version"],
        "toVersion": target["version"],
        "targetApprovedAt": target_approved_at_raw,
        "reason": reason,
        "targetEnvironment": target_environment,
        "at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# BaselineVersion (FUT-010~011)
# ---------------------------------------------------------------------------


_REQUIRED_BASELINE_ID_FIELDS = ("id", "datasetId", "siteId", "assetId")

# registrationDigest 계산에서 뺀다 — 등록 이후 상태 전이마다 달라지는 키.
_VOLATILE_BASELINE_KEYS = frozenset(
    {
        "status",
        "createdAt",
        "approvedAt",
        "approvedBy",
        "approvalReason",
        "activatedAt",
        "registrationDigest",
    }
)


def _validate_baseline_features(features) -> None:
    """`features`가 `{name: {"mean", "std", "normal_range"}}` 구조에 유한값인지
    검증한다. [리뷰 P1] 문자열 features나 std<=0/NaN이 draft에서 approved/active
    까지 그대로 전이되던 문제를 막는다."""
    if not isinstance(features, dict) or not features:
        raise ValueError(f"features는 비어있지 않은 dict여야 합니다: {features!r}")
    for name, spec in features.items():
        if not isinstance(spec, dict):
            raise ValueError(f"features[{name!r}]는 dict여야 합니다: {spec!r}")
        for label in ("mean", "std"):
            value = spec.get(label)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"features[{name!r}].{label}은 유한한 실수여야 합니다: {value!r}")
        std = spec["std"]
        if std <= 0:
            raise ValueError(f"features[{name!r}].std는 0보다 커야 합니다: {std!r}")
        normal_range = spec.get("normal_range")
        if (
            not isinstance(normal_range, (list, tuple))
            or len(normal_range) != 2
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in normal_range
            )
        ):
            raise ValueError(
                f"features[{name!r}].normal_range는 [lo, hi] 형태의 유한값 쌍이어야 합니다: "
                f"{normal_range!r}"
            )


def _validate_baseline_identity_fields(baseline_version: dict) -> None:
    """[리뷰 P1] `approve`/`activate`가 넘겨받은 객체가 실제로
    `register_baseline_version()`이 만든 것처럼 필수 식별 정보를 갖췄는지
    검증한다 — `status`만 맞춘 임의 객체가 ID 없는 active 기준선이 되는 것을
    막는다."""
    blank = [
        name
        for name in _REQUIRED_BASELINE_ID_FIELDS
        if not isinstance(baseline_version.get(name), str) or not baseline_version[name].strip()
    ]
    if blank:
        raise ValueError(f"다음 필드는 비어있지 않은 문자열이어야 합니다: {blank}")
    _validate_baseline_features(baseline_version.get("features"))


def compute_baseline_registration_digest(baseline_version: dict) -> str:
    """등록 레코드의 불변 필드(id/datasetId/siteId/assetId/timeSegment/features)에
    대한 canonical sha256 — `approve`/`activate`가 등록 이후 변조를 탐지하는 데 쓴다."""
    stable = {k: v for k, v in baseline_version.items() if k not in _VOLATILE_BASELINE_KEYS}
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    등록한다. status="draft"로 시작 — 승인 전에는 배포할 수 없다 (FUT-011).

    [리뷰 P1] baseline_id/dataset_id/site_id/asset_id가 비어 있거나 features가
    구조·유한값을 만족하지 않으면 거부한다 — 등록 시점에 검증된 내용만
    `registrationDigest`로 봉인해, 이후 approve/activate가 이 digest로 변조·
    누락을 탐지할 수 있게 한다."""
    blank = [
        name
        for name, value in (
            ("baseline_id", baseline_id),
            ("dataset_id", dataset_id),
            ("site_id", site_id),
            ("asset_id", asset_id),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if blank:
        raise ValueError(f"다음 필드는 비어있지 않은 문자열이어야 합니다: {blank}")
    _validate_baseline_features(features)

    record = {
        "id": baseline_id,
        "datasetId": dataset_id,
        "siteId": site_id,
        "assetId": asset_id,
        "timeSegment": time_segment,
        "features": copy.deepcopy(features),  # draft 수정이 등록본에 새지 않도록
        "status": "draft",
        "createdAt": _now_iso(),
    }
    record["registrationDigest"] = compute_baseline_registration_digest(record)
    return record


def approve_baseline_version(baseline_version: dict, *, approved_by: str, reason: str) -> dict:
    """[리뷰 P1] `approved_by`도 `reason`과 마찬가지로 필수로 검증한다(이전에는
    검증되지 않아 공백 승인자가 승인 레코드에 그대로 남을 수 있었다). 승인 대상
    객체 자체의 식별 필드·features 구조도 검증하고, `registrationDigest`가
    등록 시점 내용과 일치하는지 대조해 register_baseline_version()을 거치지
    않은(또는 등록 이후 변조된) 객체의 승인을 거부한다."""
    if baseline_version.get("status") != "draft":
        raise ValueError(
            f"draft 상태만 승인할 수 있습니다 (현재 status={baseline_version.get('status')!r})."
        )
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")
    _validate_baseline_identity_fields(baseline_version)

    stored_digest = baseline_version.get("registrationDigest")
    if stored_digest is None:
        raise ValueError(
            "registrationDigest가 없어 승인할 수 없습니다 — register_baseline_version()이 "
            "만든 레코드를 그대로 전달했는지 확인하세요."
        )
    if compute_baseline_registration_digest(baseline_version) != stored_digest:
        raise ValueError(
            "등록 이후 기준선 내용이 변경되어 승인할 수 없습니다 "
            f"(registrationDigest registered={stored_digest!r}, "
            f"current={compute_baseline_registration_digest(baseline_version)!r})."
        )

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
    코드 레벨에서 강제한다.

    [리뷰 P1] `status == "approved"`만으로는 ID·features 없는 임의 객체도
    active로 전이될 수 있었다. 식별 필드·features를 다시 검증하고,
    `registrationDigest`가 등록 시점 내용과 여전히 일치하는지(승인~활성화 사이
    변조되지 않았는지)도 대조한다."""
    if baseline_version.get("status") != "approved":
        raise ValueError(
            f"승인된(approved) 기준선만 배포할 수 있습니다 "
            f"(현재 status={baseline_version.get('status')!r})."
        )
    _validate_baseline_identity_fields(baseline_version)
    stored_digest = baseline_version.get("registrationDigest")
    if stored_digest is None:
        raise ValueError(
            "registrationDigest가 없어 배포할 수 없습니다 — register_baseline_version()이 "
            "만든 레코드를 그대로 전달했는지 확인하세요."
        )
    if compute_baseline_registration_digest(baseline_version) != stored_digest:
        raise ValueError(
            "승인 이후 기준선 내용이 변경되어 배포할 수 없습니다 "
            f"(registrationDigest registered={stored_digest!r}, "
            f"current={compute_baseline_registration_digest(baseline_version)!r})."
        )

    active = copy.deepcopy(baseline_version)
    active["status"] = "active"
    active["activatedAt"] = _now_iso()
    return active
