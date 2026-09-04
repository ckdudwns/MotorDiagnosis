"""
모델·기준선 버전 등록/승인/롤백 (AI-1, 4주차 DATASET_MODEL_01)

API 명세서 v1.3 06_후속개발API의 FUT-005~007(모델 버전 등록/승인/롤백),
FUT-010~011(기준선 버전 조회/등록) 계약을 그대로 데이터 구조로 옮긴다.
근거는 dataset_version_format.md 참고.

[리뷰 P1, 5차] `ModelVersionRegistry`/`BaselineVersionRegistry` — 승인·롤백을
인스턴스가 소유한 canonical 저장소에 결속한다.

이전 버전(자유 함수 `register_model_version`/`approve_model_version`/
`rollback_model_version`, `register_baseline_version`/`approve_baseline_version`/
`activate_baseline_version`)은 등록 이력(`registry`)·승인 계보(`approved_history`)를
호출자가 만들어 넘기는 **평범한 list**로 받았다. `registrationDigest`/
`approvalDigest`는 이 모듈의 공개 함수(`compute_registration_digest` 등)로 누구나
계산할 수 있어서, 호출자가 임의의 레코드에 그 함수로 직접 계산한 digest를 채워
넣고 `registry=[그 레코드]`(또는 `approved_history=[...]`)로 전달하면 "자기 서명"이
되어 승인·롤백이 그대로 통과했다 — "canonical 등록 이력"이라는 이름의 값이
실제로는 호출자가 무엇이든 담을 수 있는 값이었기 때문이다(리뷰 P1, 3차·4차가
digest 정합성까지는 막았지만, "이 digest 쌍을 가진 list를 통째로 지어내는" 공격
자체는 막지 못했다 — 기존 테스트 주석에 "서명 체계가 없는 이 코드베이스의 근본
한계"로 명시돼 있었다).

이 모듈은 그 한계를 파이썬 프로세스 안에서 실제로 가능한 방식으로 해소한다:
등록·승인된 레코드를 **레지스트리 인스턴스 자신**이 비공개로 소유하고(`self.__entries` —
이름 맹글링(`_ClassName__entries`)이 적용되는 이중 밑줄 속성이라, `registry._entries`처럼
바로 짐작 가는 이름으로는 클래스 바깥에서 손댈 수 없다. 파이썬에 진짜 접근 제어는
없으므로 완전한 방어는 아니지만, 우연한/부주의한 직접 조작은 막는다), `approve()`/
`rollback()`/`activate()`는 호출자가 넘긴 dict/list를 검증 입력으로 받지 않는다 —
`version`/`baseline_id` 문자열로 **그 레지스트리 자체**에서 조회한 값만 신뢰한다. 새
`ModelVersionRegistry()`는 빈 저장소이므로, 그 인스턴스의 `register()`를 실제로 거치지
않은 버전은 어떤 문자열을 대도 조회되지 않는다 — "registry=[forged]" 같은 위조가
애초에 구조적으로 불가능하다. `register()`/`approve()`/`get()`은 항상 내부 레코드의
**깊은 복사본**만 반환해, 호출자가 반환값을 수정해도 저장소 내부 상태는 전혀
영향받지 않는다(반환된 dict를 아무리 변조해도 그 변조는 저장소로 되먹임되지 않는다).

롤백 계보도 마찬가지다 — 별도의 `approved_history` 인자를 받지 않는다.
`ModelVersionRegistry`의 각 `version` 키는 유일하므로(같은 버전을 두 번 등록하면
거부한다), 레지스트리 자신이 보유한 모든 항목이 곧 계보다. `rollback()`은
`current_version`/`target_version`을 레지스트리에서 직접 조회해 계보를 구성한다.
"""

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone


# [리뷰 P2] "not-a-sha256" 같은 임의 문자열도 artifactChecksum으로 등록됐다 —
# 실제 sha256 hexdigest 형식(`sha256:` 뒤에 64자리 16진수)인지 등록 시점에
# 검증한다.
_SHA256_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


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
# approvalDigest도 등록 시점에는 존재하지 않는 값이므로 함께 뺀다(그래야 승인된
# 레코드에서 registrationDigest를 재계산해도 등록 시점과 같은 값이 나온다).
_VOLATILE_MODEL_VERSION_KEYS = frozenset(
    {
        "status",
        "createdAt",
        "approvedAt",
        "approvedBy",
        "approvalReason",
        "metricSnapshot",
        "registrationDigest",
        "approvalDigest",
    }
)


def compute_registration_digest(model_version: dict) -> str:
    """등록 레코드의 **모든 불변 필드**(version/artifactUri/artifactChecksum/
    datasetId/baselineVersion/metrics)에 대한 canonical sha256.

    등록 시점에 이 digest를 계산해 레코드에 실어 두고, 승인 시 재계산·대조해
    (`ModelVersionRegistry`가 반환하는 복사본을 넘겨받아 저장한 뒤) 저장소 내부
    상태가 손상되지 않았는지 확인하는 정합성 점검에 쓴다. `ModelVersionRegistry`가
    등록·승인된 레코드를 직접 소유하므로(외부에서 주입할 수 없으므로) 이 값 자체가
    "등록 증명"의 유일한 근거는 아니다 — 등록 증명은 이제 "레지스트리 인스턴스의
    `register()`를 실제로 거쳤는가"라는 구조적 사실에서 나온다.
    """
    stable = {k: v for k, v in model_version.items() if k not in _VOLATILE_MODEL_VERSION_KEYS}
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_approval_digest(approved_model_version: dict) -> str:
    """승인 레코드의 **모든 내용**(등록 불변 필드 + registrationDigest + 승인자·
    사유·시각·metricSnapshot)에 대한 canonical sha256(`status` 자체와 이 필드는
    제외).

    `ModelVersionRegistry.rollback()`이 계보(target/current)를 이 레지스트리
    자신에게서만 조회하므로, "실제로 register→approve 흐름을 거친 레코드인지"는
    이제 이 digest가 아니라 레지스트리 내부 상태 자체가 보장한다. 그래도 등록
    이후 상태 전이 사이의 내부 손상을 잡기 위한 정합성 점검 용도로는 유지한다.
    """
    stable = {
        k: v for k, v in approved_model_version.items() if k not in ("status", "approvalDigest")
    }
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_finite_metric_values(value, *, path: str = "metric_snapshot") -> None:
    """`metric_snapshot`(중첩 dict/list 허용) 안의 모든 수치 leaf가 유한한지 검증한다.

    [리뷰 P2] `f1=NaN` 같은 값이 그대로 승인 레코드에 저장되면 이후 JSON
    직렬화(`allow_nan=False` 기준)가 실패하거나, NaN이 조용히 "통과"로 오판되는
    비교 로직과 결합해 잘못된 승인 근거가 남을 수 있다.
    """
    if isinstance(value, dict):
        for key, sub_value in value.items():
            _validate_finite_metric_values(sub_value, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, sub_value in enumerate(value):
            _validate_finite_metric_values(sub_value, path=f"{path}[{index}]")
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"{path}는 유한한 값이어야 합니다: {value!r}")


def _build_registered_model_version(
    *,
    version: str,
    artifact_uri: str,
    dataset_id: str,
    baseline_version: str,
    metrics: dict,
    artifact_checksum: str,
) -> dict:
    """status="registered" 레코드를 만들고 검증한다
    (`ModelVersionRegistry.register()`의 내부 로직).

    [리뷰 P1] 필수 문자열이 비어 있거나 metrics가 유효한 dict가 아니면 거부한다 —
    불완전한 레코드가 등록·승인 상태까지 조용히 전이되는 것을 막는다.
    `artifact_checksum`(학습 시 저장한 아티팩트의 sha256, `train_and_evaluate.
    _sha256_of_file()` 결과)도 필수로 받아 등록 레코드에 보존한다 — 그래야
    승인·롤백된 모델 버전이 어떤 아티팩트 바이트를 가리키는지 감사할 수 있고,
    `registrationDigest`가 아티팩트 교체까지 탐지한다.

    [리뷰 P2] `artifact_checksum`은 비어있지 않은 문자열인지뿐 아니라
    `sha256:` 뒤에 64자리 16진수가 오는 실제 sha256 hexdigest 형식인지도
    검증한다 — `"not-a-sha256"` 같은 값이 그대로 등록되는 것을 막는다.
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
    if not _SHA256_CHECKSUM_RE.match(artifact_checksum):
        raise ValueError(
            f"artifact_checksum은 'sha256:' 뒤에 64자리 16진수여야 합니다: {artifact_checksum!r}"
        )
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


def _build_approved_model_version(
    entry: dict, *, approved_by: str, reason: str, metric_snapshot: dict
) -> dict:
    """`entry`(레지스트리 내부에 저장된, 실제로 `register()`를 거친 레코드)를
    승인 레코드로 전이한다 (`ModelVersionRegistry.approve()`의 내부 로직).

    [리뷰 P1] approved_by와 metric_snapshot을 모두 명시적으로 필수 요구한다 — 이전엔
    reason만으로 승인이 가능해 승인자가 기록되지 않았고, metric_snapshot을 생략하면
    등록 시점 metrics를 암묵적으로 재사용해 "승인 당시 실제로 검토한 지표"를
    감사할 수 없었다.

    [리뷰 P2] `metric_snapshot` 내부 수치(중첩 포함)가 모두 유한한지도 검증한다.
    """
    if entry.get("status") != "registered":
        raise ValueError(
            f"registered 상태만 승인할 수 있습니다 (현재 status={entry.get('status')!r})."
        )
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")
    if not isinstance(metric_snapshot, dict) or not metric_snapshot:
        raise ValueError(
            f"metric_snapshot은 비어있지 않은 dict로 명시적으로 제출해야 합니다: {metric_snapshot!r}"
        )
    _validate_finite_metric_values(metric_snapshot)

    stored_digest = entry.get("registrationDigest")
    if stored_digest is None or compute_registration_digest(entry) != stored_digest:
        raise ValueError(
            "등록 레코드의 registrationDigest가 내용과 일치하지 않습니다 — 저장소 "
            "내부 상태가 손상된 것으로 보입니다."
        )

    # 얕은 dict()는 중첩 metrics(혼동행렬 등)를 원본과 공유한다 — 승인 후 원본
    # metric_snapshot을 수정하면 승인 당시 근거까지 바뀐다. 깊은 복사한다.
    approved = copy.deepcopy(entry)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["metricSnapshot"] = copy.deepcopy(metric_snapshot)
    approved["approvedAt"] = _now_iso()
    approved["approvalDigest"] = compute_approval_digest(approved)
    return approved


_ALLOWED_TARGET_ENVIRONMENTS = frozenset({"production", "staging", "development"})


class ModelVersionRegistry:
    """모델 버전의 canonical(레지스트리 소유) 저장소 — FUT-005~007.

    모듈 docstring에서 설명한 신뢰 경계를 구현한다: `self.__entries`는 이
    인스턴스 밖으로 절대 노출하지 않고, `register()`/`approve()`만 여기에
    기록한다. `approve()`/`rollback()`은 호출자가 만든 dict/list를 검증
    입력으로 받지 않는다 — `version` 문자열로 이 레지스트리 자체에서 조회한
    값만 신뢰한다.
    """

    def __init__(self):
        self.__entries: dict = {}

    def register(
        self,
        *,
        version: str,
        artifact_uri: str,
        dataset_id: str,
        baseline_version: str,
        metrics: dict,
        artifact_checksum: str,
    ) -> dict:
        """새 모델 버전을 이 저장소에 status="registered"로 기록한다 (FUT-005).

        같은 `version`을 이 레지스트리에 두 번 등록할 수 없다 — 재등록을
        허용하면 "이미 등록된 버전"을 다른 내용으로 조용히 덮어써 감사 이력이
        끊긴다.
        """
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"version은 비어있지 않은 문자열이어야 합니다: {version!r}")
        if version in self.__entries:
            raise ValueError(
                f"버전 {version!r}은 이미 이 저장소에 등록되어 있습니다 — 새 버전 "
                "문자열을 쓰세요(재등록으로 기존 감사 이력을 덮어쓰지 않습니다)."
            )
        record = _build_registered_model_version(
            version=version,
            artifact_uri=artifact_uri,
            dataset_id=dataset_id,
            baseline_version=baseline_version,
            metrics=metrics,
            artifact_checksum=artifact_checksum,
        )
        self.__entries[version] = record
        return copy.deepcopy(record)

    def approve(
        self, version: str, *, approved_by: str, reason: str, metric_snapshot: dict
    ) -> dict:
        """이 저장소에 등록된(registered) 버전만 승인한다 (FUT-006).

        `version`으로 이 레지스트리 자신에게서 등록 레코드를 조회한다 — 호출자가
        레코드 내용을 함께 넘기지 않으므로, 등록된 적 없는 버전이나(존재하지
        않는 `version`) 이미 승인된 버전은 거부된다.
        """
        entry = self.__entries.get(version)
        if entry is None:
            raise ValueError(
                f"버전 {version!r}이 이 저장소에 등록되어 있지 않습니다 — register()를 "
                "먼저 호출하세요."
            )
        approved = _build_approved_model_version(
            entry, approved_by=approved_by, reason=reason, metric_snapshot=metric_snapshot
        )
        self.__entries[version] = approved
        return copy.deepcopy(approved)

    def rollback(
        self,
        current_version: str,
        target_version: str,
        *,
        reason: str,
        target_environment: str,
    ) -> dict:
        """target_version으로 롤백하는 액션 레코드를 만든다 (FUT-007).

        "이전 승인 버전만 롤백 대상 허용" — `current_version`/`target_version`
        모두 이 레지스트리에 **실제로 approve()된** 버전이어야 하고, target이
        current보다 먼저 승인됐어야 한다(`approvedAt`을 UTC로 실제 파싱해
        비교). 계보는 이 레지스트리 자신이 보유한 전체 상태에서 나온다 —
        호출자가 별도로 넘기는 이력 목록은 없다.

        `target_environment`도 비어있지 않은 허용 환경 값(`production`/
        `staging`/`development`)이어야 한다.

        배포와 롤백은 별도 작업으로 기록한다(반환값은 저장소 상태를 바꾸지
        않고 새 액션 레코드만 만든다).
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
        if current_version == target_version:
            raise ValueError(f"자기 자신({target_version})으로는 롤백할 수 없습니다.")

        def _approved_entry(label: str, version: str) -> dict:
            entry = self.__entries.get(version)
            if entry is None:
                raise ValueError(
                    f"{label} 버전 {version!r}이 이 저장소에 등록되어 있지 않습니다."
                )
            if entry.get("status") != "approved":
                raise ValueError(
                    f"{label} 버전 {version!r}은 승인(approved) 상태가 아닙니다 "
                    f"(status={entry.get('status')!r})."
                )
            registration_digest = entry.get("registrationDigest")
            if (
                registration_digest is None
                or compute_registration_digest(entry) != registration_digest
            ):
                raise ValueError(
                    f"{label} 버전 {version!r}의 registrationDigest가 내용과 일치하지 "
                    "않습니다 — 저장소 내부 상태가 손상된 것으로 보입니다."
                )
            approval_digest = entry.get("approvalDigest")
            if approval_digest is None or compute_approval_digest(entry) != approval_digest:
                raise ValueError(
                    f"{label} 버전 {version!r}의 approvalDigest가 내용과 일치하지 "
                    "않습니다 — 저장소 내부 상태가 손상된 것으로 보입니다."
                )
            return entry

        target_entry = _approved_entry("target", target_version)
        current_entry = _approved_entry("current", current_version)

        target_approved_at_raw = target_entry.get("approvedAt")
        target_approved_at_dt = _parse_utc(
            target_approved_at_raw, context=f"target({target_version!r})"
        )
        current_approved_at_dt = _parse_utc(
            current_entry.get("approvedAt"), context=f"current({current_version!r})"
        )

        if target_approved_at_dt >= current_approved_at_dt:
            raise ValueError(
                f"target(approvedAt={target_approved_at_dt.isoformat()})은 current"
                f"(approvedAt={current_approved_at_dt.isoformat()})보다 먼저 승인된 "
                "버전이어야 합니다 (실제 승인 시각 기준)."
            )

        return {
            "action": "rollback",
            "fromVersion": current_version,
            "toVersion": target_version,
            "targetApprovedAt": target_approved_at_raw,
            "reason": reason,
            "targetEnvironment": target_environment,
            "at": _now_iso(),
        }

    def get(self, version: str):
        """`version`의 현재 저장소 상태를 깊은 복사본으로 돌려준다(없으면 None).
        반환값을 수정해도 저장소 내부 상태는 바뀌지 않는다."""
        entry = self.__entries.get(version)
        return copy.deepcopy(entry) if entry is not None else None


# ---------------------------------------------------------------------------
# BaselineVersion (FUT-010~011)
# ---------------------------------------------------------------------------


_REQUIRED_BASELINE_ID_FIELDS = ("id", "datasetId", "siteId", "assetId")

# registrationDigest 계산에서 뺀다 — 등록 이후 상태 전이마다 달라지는 키.
# approvalDigest도 등록 시점에는 존재하지 않으므로 함께 뺀다(승인된/활성화된
# 레코드에서 재계산해도 등록 시점과 같은 registrationDigest가 나오도록).
_VOLATILE_BASELINE_KEYS = frozenset(
    {
        "status",
        "createdAt",
        "approvedAt",
        "approvedBy",
        "approvalReason",
        "activatedAt",
        "registrationDigest",
        "approvalDigest",
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
        # [리뷰 P2] 유한값 쌍인지만 확인하면 [1.0, -1.0]처럼 하한이 상한보다 큰
        # 뒤집힌 범위도 통과한다 — 하한이 상한 이하인지 명시적으로 검증한다.
        if normal_range[0] > normal_range[1]:
            raise ValueError(
                f"features[{name!r}].normal_range의 하한이 상한보다 큽니다: {normal_range!r}"
            )


def compute_baseline_registration_digest(baseline_version: dict) -> str:
    """등록 레코드의 불변 필드(id/datasetId/siteId/assetId/timeSegment/features)에
    대한 canonical sha256 — 등록 시점 이후 저장소 내부 상태가 손상되지 않았는지
    확인하는 정합성 점검에 쓴다(핵심 신뢰 경계는 `BaselineVersionRegistry`
    자신의 소유 저장소가 제공한다)."""
    stable = {k: v for k, v in baseline_version.items() if k not in _VOLATILE_BASELINE_KEYS}
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_baseline_approval_digest(baseline_version: dict) -> str:
    """승인 레코드의 **모든 내용**(등록 불변 필드 + registrationDigest + 승인자·
    사유·시각)에 대한 canonical sha256(`status`/`activatedAt`/이 필드 자체는 제외).
    """
    stable = {
        k: v
        for k, v in baseline_version.items()
        if k not in ("status", "activatedAt", "approvalDigest")
    }
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_registered_baseline_version(
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
    구조·유한값을 만족하지 않으면 거부한다."""
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


def _build_approved_baseline_version(entry: dict, *, approved_by: str, reason: str) -> dict:
    """[리뷰 P1] `approved_by`도 `reason`과 마찬가지로 필수로 검증한다."""
    if entry.get("status") != "draft":
        raise ValueError(
            f"draft 상태만 승인할 수 있습니다 (현재 status={entry.get('status')!r})."
        )
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    stored_digest = entry.get("registrationDigest")
    if stored_digest is None or compute_baseline_registration_digest(entry) != stored_digest:
        raise ValueError(
            "등록 레코드의 registrationDigest가 내용과 일치하지 않습니다 — 저장소 "
            "내부 상태가 손상된 것으로 보입니다."
        )

    # features(mean/std/normal_range)를 참조 공유하면 draft 수정이 승인본·active
    # 기준선에까지 전파된다. 승인 결과를 깊은 복사로 독립시킨다.
    approved = copy.deepcopy(entry)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["approvedAt"] = _now_iso()
    approved["approvalDigest"] = compute_baseline_approval_digest(approved)
    return approved


def _build_active_baseline_version(entry: dict) -> dict:
    """승인된(approved) 기준선만 배포(active)할 수 있다 — "승인 전 배포 금지"를
    코드 레벨에서 강제한다."""
    if entry.get("status") != "approved":
        raise ValueError(
            f"승인된(approved) 기준선만 배포할 수 있습니다 "
            f"(현재 status={entry.get('status')!r})."
        )
    approval_digest = entry.get("approvalDigest")
    if approval_digest is None or compute_baseline_approval_digest(entry) != approval_digest:
        raise ValueError(
            "승인 레코드의 approvalDigest가 내용과 일치하지 않습니다 — 저장소 내부 "
            "상태가 손상된 것으로 보입니다."
        )

    active = copy.deepcopy(entry)
    active["status"] = "active"
    active["activatedAt"] = _now_iso()
    return active


class BaselineVersionRegistry:
    """기준선 버전의 canonical(레지스트리 소유) 저장소 — FUT-010~011.

    `ModelVersionRegistry`와 같은 신뢰 경계를 적용한다: `register()`/
    `approve()`/`activate()`는 호출자가 만든 dict를 검증 입력으로 받지
    않는다 — `baseline_id` 문자열로 이 레지스트리 자체에서 조회한 값만
    신뢰한다.
    """

    def __init__(self):
        self.__entries: dict = {}

    def register(
        self,
        *,
        baseline_id: str,
        dataset_id: str,
        site_id: str,
        asset_id: str,
        features: dict,
        time_segment: str = None,
    ) -> dict:
        """새 기준선을 이 저장소에 status="draft"로 기록한다 (FUT-011)."""
        if not isinstance(baseline_id, str) or not baseline_id.strip():
            raise ValueError(f"baseline_id는 비어있지 않은 문자열이어야 합니다: {baseline_id!r}")
        if baseline_id in self.__entries:
            raise ValueError(
                f"기준선 {baseline_id!r}은 이미 이 저장소에 등록되어 있습니다 — 새 "
                "baseline_id를 쓰세요."
            )
        record = _build_registered_baseline_version(
            baseline_id=baseline_id,
            dataset_id=dataset_id,
            site_id=site_id,
            asset_id=asset_id,
            features=features,
            time_segment=time_segment,
        )
        self.__entries[baseline_id] = record
        return copy.deepcopy(record)

    def approve(self, baseline_id: str, *, approved_by: str, reason: str) -> dict:
        """이 저장소에 등록된(draft) 기준선만 승인한다."""
        entry = self.__entries.get(baseline_id)
        if entry is None:
            raise ValueError(
                f"기준선 {baseline_id!r}이 이 저장소에 등록되어 있지 않습니다 — "
                "register()를 먼저 호출하세요."
            )
        approved = _build_approved_baseline_version(entry, approved_by=approved_by, reason=reason)
        self.__entries[baseline_id] = approved
        return copy.deepcopy(approved)

    def activate(self, baseline_id: str) -> dict:
        """이 저장소에서 승인된(approved) 기준선만 배포(active)한다."""
        entry = self.__entries.get(baseline_id)
        if entry is None:
            raise ValueError(f"기준선 {baseline_id!r}이 이 저장소에 등록되어 있지 않습니다.")
        active = _build_active_baseline_version(entry)
        self.__entries[baseline_id] = active
        return copy.deepcopy(active)

    def get(self, baseline_id: str):
        """`baseline_id`의 현재 저장소 상태를 깊은 복사본으로 돌려준다(없으면
        None). 반환값을 수정해도 저장소 내부 상태는 바뀌지 않는다."""
        entry = self.__entries.get(baseline_id)
        return copy.deepcopy(entry) if entry is not None else None
