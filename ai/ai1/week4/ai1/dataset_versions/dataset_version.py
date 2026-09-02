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
    재현성 검증(수용 기준)이 의미가 있다).

    v1 호환용. 신규 동결본은 아래 compute_snapshot_digest()가 매니페스트 전체
    불변 필드를 보호한다."""
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


# freeze 시점에 고정되지 않는(실행·상태 전이마다 달라지는) 키. digest 계산에서 뺀다.
_VOLATILE_MANIFEST_KEYS = frozenset(
    {
        "status",
        "createdAt",
        "frozenAt",
        "approvedAt",
        "approvedBy",
        "approvalReason",
        "datasetChecksum",
        "snapshotDigest",
    }
)


def compute_snapshot_digest(manifest: dict) -> str:
    """동결 시점 매니페스트의 **모든 불변 필드**에 대한 canonical sha256.

    `datasetChecksum`(rows+labelMapping+split)만으로는 freeze 이후 `source.license`,
    `source.checksum`, `compatibility.samplingRateHz`, `labelPolicyVersion`,
    `snapshotSchemaVersion`, `splitStrategy` 등을 바꿔도 검증을 통과한다. 이 digest는
    휘발성 키(_VOLATILE_MANIFEST_KEYS)를 뺀 매니페스트 전체를 해시해, 동결 이후 어떤
    불변 필드가 바뀌어도 반드시 달라진다. approve/학습 시작 시 재계산·비교한다.
    """
    stable = {k: v for k, v in manifest.items() if k not in _VOLATILE_MANIFEST_KEYS}
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_legacy_v1_frozen(frozen_manifest: dict) -> bool:
    """매니페스트 내용만 보고 v1(legacy) 동결본"처럼 보이는지" 추정한다 — 정보성
    보조 함수일 뿐, 무결성 검증의 신뢰 판단에는 쓰지 않는다.

    [리뷰 P1, 2차] 매니페스트는 호출자가 자유롭게 수정 가능한 데이터다. 처음에는
    "snapshotDigest 없음"을 legacy 판정 기준으로 썼는데, digest 필드만 지우면
    legacy로 오인됐다. 그래서 "snapshotSchemaVersion 없음"으로 바꿨더니, 이번에는
    schemaVersion과 digest를 **함께** 지우면 여전히 legacy로 오인됐다 — 매니페스트
    안의 어떤 필드 조합을 기준으로 삼아도 공격자가 그 필드들을 함께 지우면 우회된다.
    그래서 `verify_frozen_integrity`/`approve_dataset_version`/`verify_reproducibility`는
    이 함수가 아니라, 호출자가 매니페스트 **바깥의** 신뢰 가능한 저장소(최초 동결
    시점에 별도로 기록해 둔 스키마 버전 메타데이터 등)에서 확인한 사실을 실어 보내는
    `trusted_legacy` 인자로만 legacy를 인정한다(기본값 False = 항상 v1.3 엄격 검증).
    """
    return "snapshotSchemaVersion" not in frozen_manifest


def compute_rows_fingerprint(rows: list) -> str:
    """rows 배열(실제 산출된 특징값) 자체의 canonical sha256.

    week3 `register_dataset.compute_feature_output_fingerprint()`와 동일한
    알고리즘을 이 모듈 안에서 독립적으로 재구현한다 — week4의 이 모듈은 특정
    데이터셋 소스(CWRU)에 결합된 week3 모듈을 import하지 않는다는 기존 설계를
    유지하면서도, freeze 시점에 draft가 스스로 신고한 `featureOutputFingerprint`가
    실제 rows와 여전히 일치하는지 대조하는 데 쓴다.
    """
    payload = [{name: row[name] for name in sorted(row)} for row in rows]
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def freeze_dataset_version(manifest: dict) -> dict:
    """status가 draft인 매니페스트를 frozen으로 전이한다. 원본은 변경하지 않는다.

    **신규 동결은 API 명세서 v1.3을 준수해야 한다** — `labelPolicyVersion`,
    `snapshotSchemaVersion`, `source.checksum`, `featureOutputFingerprint`가 없으면
    거부한다. 이미 커밋된 구(v1) frozen 산출물은 이 함수를 다시 거치지 않으며,
    approve/verify/summary가 `trusted_legacy=True`를 명시적으로 받을 때만 관용
    처리한다.

    [리뷰 P1, 2차] `build_manifest()`가 만든 draft의 `source.checksum`(및 거기서
    파생된 `id`)은 원본 파일·전처리 설정·**실제 산출된 rows**로 계산된다. 그런데
    `freeze_dataset_version`은 이 값을 재검증 없이 그대로 복사만 했다 — draft를
    만든 뒤 `id`/`source.checksum`은 그대로 둔 채 `rows`(라벨 등)만 바꿔도 동결이
    통과해서, 서로 다른 rows(그래서 다른 `datasetChecksum`)를 가진 두 데이터셋이
    같은 `id`·`snapshotChecksum`으로 승인될 수 있었다. `featureOutputFingerprint`는
    rows만으로 결정되는 값이라(원본 파일 접근 불필요) draft가 신고한 값과 현재
    rows에서 다시 계산한 값을 여기서 대조해, build 이후 rows가 바뀐 draft의 동결을
    거부한다.
    """
    if manifest.get("status") != "draft":
        raise ValueError(
            f"draft 상태만 동결할 수 있습니다 (현재 status={manifest.get('status')!r})."
        )

    missing = [
        key
        for key in ("labelPolicyVersion", "snapshotSchemaVersion", "featureOutputFingerprint")
        if not manifest.get(key)
    ]
    if not (manifest.get("source") or {}).get("checksum"):
        missing.append("source.checksum")
    if missing:
        raise ValueError(
            "신규 데이터셋 동결에는 API 명세서 v1.3 필드가 필수입니다 "
            f"(누락: {missing}). 3주차 build_manifest()가 채운 draft를 사용하세요. "
            "이미 동결된 v1 데이터셋은 재동결하지 않습니다."
        )

    current_fingerprint = compute_rows_fingerprint(manifest.get("rows") or [])
    if current_fingerprint != manifest["featureOutputFingerprint"]:
        raise ValueError(
            "draft의 featureOutputFingerprint가 현재 rows와 일치하지 않습니다 "
            f"(신고={manifest['featureOutputFingerprint']!r}, "
            f"실제={current_fingerprint!r}). build_manifest() 이후 rows/라벨이 "
            "변경된 것으로 보입니다 — id/source.checksum도 이 rows를 정직하게 "
            "반영하지 않을 수 있으니 매니페스트를 다시 생성하세요."
        )

    # 얕은 dict()는 중첩 rows 리스트를 원본과 공유한다 — 동결 후 원본 rows를
    # 변형하면 frozen 데이터도 함께 바뀌는데 datasetChecksum은 그대로라, 변조된
    # 데이터가 검증을 통과해 버린다. 깊은 복사로 동결 시점 내용을 독립 보존한다.
    frozen = copy.deepcopy(manifest)
    frozen["status"] = "frozen"
    frozen["datasetChecksum"] = compute_dataset_checksum(manifest)  # v1 호환용
    # snapshotChecksum은 3주차 compute_version_checksum() 결과(source.checksum)를 재사용
    # — id 재현성용. snapshotDigest는 매니페스트 전체 불변 필드 변조 탐지용(approve/학습에서 재검증).
    frozen["labelPolicyVersion"] = manifest["labelPolicyVersion"]
    frozen["snapshotSchemaVersion"] = manifest["snapshotSchemaVersion"]
    frozen["snapshotChecksum"] = manifest["source"]["checksum"]
    frozen["frozenAt"] = _now_iso()
    frozen["snapshotDigest"] = compute_snapshot_digest(frozen)
    return frozen


def approve_dataset_version(
    frozen_manifest: dict, *, approved_by: str, reason: str, trusted_legacy: bool = False
) -> dict:
    """status가 frozen인 데이터셋 버전만 승인할 수 있다.

    `trusted_legacy=True`는 매니페스트 안의 어떤 필드로도 추정하지 않는다 — 호출자가
    매니페스트 바깥의 신뢰 가능한 저장소에서 "이건 v1.3 이전에 이미 동결된 레코드다"를
    확인했을 때만 명시적으로 전달해야 한다(리뷰 P1, 2차). 기본값(False)에서는 언제나
    v1.3 엄격 검증(`snapshotDigest` 필수)을 적용하므로, schema/digest가 없는 신규
    레코드는 legacy로 봐주지 않고 무조건 거부한다.
    """
    if frozen_manifest.get("status") != "frozen":
        raise ValueError(
            f"frozen 상태만 승인할 수 있습니다 (현재 status={frozen_manifest.get('status')!r})."
        )
    if not approved_by:
        raise ValueError("approved_by는 필수입니다.")
    if not reason or not reason.strip():
        raise ValueError("reason은 필수입니다.")

    # 동결 이후 내용이 바뀌지 않았는지 승인 직전에 다시 검증한다.
    # (1) v1 호환: rows+labelMapping+split 체크섬.
    current_checksum = compute_dataset_checksum(frozen_manifest)
    if current_checksum != frozen_manifest.get("datasetChecksum"):
        raise ValueError(
            "동결 이후 매니페스트 내용이 변경되어 승인할 수 없습니다 "
            f"(frozen={frozen_manifest.get('datasetChecksum')!r}, "
            f"current={current_checksum!r})."
        )
    # (2) v2: 매니페스트 전체 불변 필드 digest — source.license/checksum, samplingRate,
    #     labelPolicyVersion, splitStrategy 등 freeze 이후 변조까지 잡는다.
    if not trusted_legacy:
        stored_digest = frozen_manifest.get("snapshotDigest")
        if stored_digest is None:
            raise ValueError(
                "snapshotDigest가 없어 승인할 수 없습니다 (무결성 검증을 우회할 수 "
                "없습니다). legacy(v1) 레코드라면 신뢰 가능한 외부 저장소로 확인한 "
                "뒤 trusted_legacy=True를 명시적으로 전달하세요."
            )
        current_digest = compute_snapshot_digest(frozen_manifest)
        if current_digest != stored_digest:
            raise ValueError(
                "동결 이후 매니페스트 불변 필드가 변경되어 승인할 수 없습니다 "
                f"(snapshotDigest frozen={stored_digest!r}, current={current_digest!r})."
            )

    approved = copy.deepcopy(frozen_manifest)
    approved["status"] = "approved"
    approved["approvedBy"] = approved_by
    approved["approvalReason"] = reason
    approved["approvedAt"] = _now_iso()
    return approved


def verify_frozen_integrity(frozen_manifest: dict, *, trusted_legacy: bool = False) -> None:
    """동결본이 동결 시점 이후 변조되지 않았는지 검증한다. 불일치면 ValueError.

    학습·배포처럼 동결본을 입력으로 쓰는 쪽이 status=="frozen"만 확인하지 말고 이걸
    호출해야 한다 — 그래야 freeze 이후 row 값이 바뀌었는데 같은 datasetId로 학습이
    완료되는 상황을 막는다.

    `trusted_legacy=True`는 매니페스트 안의 필드로 추정하지 않는다 — 호출자가
    매니페스트 바깥의 신뢰 가능한 저장소에서 확인한 사실을 명시적으로 전달할
    때만 legacy(v1) 완화 검증(datasetChecksum 폴백)을 적용한다(리뷰 P1, 2차).
    기본값(False)에서는 schema/digest가 없는 레코드를 무조건 거부한다.
    """
    if frozen_manifest.get("status") not in ("frozen", "approved"):
        raise ValueError(
            f"frozen/approved 상태가 아닙니다 (status={frozen_manifest.get('status')!r})."
        )
    if trusted_legacy:
        stored_checksum = frozen_manifest.get("datasetChecksum")
        if stored_checksum is None:
            raise ValueError("동결본에 무결성 검증값(snapshotDigest/datasetChecksum)이 없습니다.")
        current = compute_dataset_checksum(frozen_manifest)
        if current != stored_checksum:
            raise ValueError(
                "동결 이후 매니페스트 rows/labelMapping/split이 변조되었습니다 "
                f"(datasetChecksum frozen={stored_checksum!r}, current={current!r})."
            )
        return
    stored_digest = frozen_manifest.get("snapshotDigest")
    if stored_digest is None:
        raise ValueError(
            "snapshotDigest가 없습니다 — 무결성 검증을 우회할 수 없습니다. legacy(v1) "
            "레코드라면 신뢰 가능한 외부 저장소로 확인한 뒤 trusted_legacy=True를 "
            "명시적으로 전달하세요."
        )
    current = compute_snapshot_digest(frozen_manifest)
    if current != stored_digest:
        raise ValueError(
            "동결 이후 매니페스트가 변조되었습니다 "
            f"(snapshotDigest frozen={stored_digest!r}, current={current!r})."
        )


def verify_reproducibility(
    frozen_manifest: dict, recomputed_manifest: dict, *, trusted_legacy: bool = False
) -> bool:
    """동일 조건(같은 seed)으로 다시 만든 draft 매니페스트가 frozen 시점과 같은
    체크섬을 내는지 검증한다 (DATASET_MODEL_01 수용 기준: "동일 데이터셋 버전을
    재현할 수 있다").

    [리뷰 P1] datasetChecksum은 rows+labelMapping+split만 본다 — source(원본 파일)
    checksum이나 labelPolicyVersion/snapshotSchemaVersion이 다른 데이터셋도 rows만
    우연히 같으면 "재현 성공"으로 오판될 수 있다. v1.3 동결본은 이 snapshot
    identity까지 함께 검증한다. `trusted_legacy=True`(매니페스트 바깥에서 확인한
    사실)일 때만 이 추가 검증을 생략한다(리뷰 P1, 2차 — 매니페스트 필드로는
    legacy 여부를 추정하지 않는다).
    """
    if frozen_manifest["datasetChecksum"] != compute_dataset_checksum(recomputed_manifest):
        return False
    if trusted_legacy:
        return True

    for field in ("labelPolicyVersion", "snapshotSchemaVersion"):
        if frozen_manifest.get(field) != recomputed_manifest.get(field):
            return False
    frozen_source_checksum = frozen_manifest.get("snapshotChecksum") or (
        frozen_manifest.get("source") or {}
    ).get("checksum")
    recomputed_source_checksum = (recomputed_manifest.get("source") or {}).get("checksum")
    if frozen_source_checksum != recomputed_source_checksum:
        return False
    return True


def dataset_version_summary(manifest: dict) -> dict:
    """GET /api/datasets/{id} 응답에 가까운, rows를 뺀 요약 뷰.

    얕은 dict comprehension은 labelMapping·split 같은 중첩 객체를 원본과 공유한다
    — 호출자가 요약 결과의 labelMapping만 바꿔도 frozen·approved 매니페스트의
    라벨 매핑이 함께 변하는데 status/datasetChecksum은 그대로라, 승인 내용과
    체크섬이 조용히 어긋난다. 깊은 복사로 요약 뷰를 원본과 완전히 분리한다
    (rows를 이미 뺐으므로 비용도 작다).
    """
    return copy.deepcopy({k: v for k, v in manifest.items() if k != "rows"})
