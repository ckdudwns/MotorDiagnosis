"""
DATASET_MODEL_01 테스트 — 데이터셋/모델/기준선 버전 상태 머신 검증 (AI-1, 4주차)

상태 전이 규칙은 합성 매니페스트로 항상 검증하고, "동일 데이터셋 버전을 재현할 수
있다"(수용 기준)는 가능하면 실제 CWRU 데이터로 week3 build_manifest()를 두 번
실행해 체크섬이 같은지 확인한다.
"""

import copy
import os
import sys
import unittest
from unittest import mock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASET_VERSIONS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
_WEEK3_DATASETS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week3", "ai1", "datasets")
)
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)
_WEEK2_BASELINE_JSON = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "dataset", "baseline.json")
)

sys.path.insert(0, _DATASET_VERSIONS_DIR)
sys.path.insert(0, _WEEK3_DATASETS_DIR)

from dataset_version import (  # noqa: E402
    freeze_dataset_version,
    DatasetVersionRegistry,
    verify_reproducibility,
    verify_frozen_integrity,
    compute_dataset_checksum,
    compute_snapshot_digest,
    compute_rows_fingerprint,
    compute_source_checksum,
    dataset_version_summary,
    is_legacy_v1_frozen,
)
from model_version import (  # noqa: E402
    ModelVersionRegistry,
    BaselineVersionRegistry,
    compute_registration_digest,
    compute_approval_digest,
    compute_baseline_registration_digest,
    compute_baseline_approval_digest,
)


def _checksum_inputs() -> dict:
    """register_dataset.build_manifest()가 채우는 checksumInputs를 흉내낸다 —
    compute_source_checksum()이 source.checksum/id를 재계산하는 데 쓰는 나머지
    입력(rows/labelMapping만으로는 재현 불가능한 것들)."""
    return {
        "windowSize": 2048,
        "hopSize": 2048,
        "seed": 42,
        "featurePipelineVersion": "test.pipeline.v1",
        "featureConfig": {
            "sampleRate": 12000,
            "frameLength": 2048,
            "hopLength": 512,
            "nMfcc": 13,
            "bandEdges": [0, 500, 1000, 2000, 4000, 8000],
        },
        "splitStrategyKey": "operating_condition_holdout",
    }


def _draft_manifest() -> dict:
    """API 명세서 v1.3 build_manifest() 출력을 흉내낸 draft (신규 필수 필드 포함)."""
    rows = [
        {"sample_id": "A", "common_label": "NORMAL"},
        {"sample_id": "B", "common_label": "ANOMALY"},
    ]
    manifest = {
        "id": "DS-TEST-001-000000000000",
        "name": "test-dataset",
        "status": "draft",
        "source": {
            "type": "external",
            "uri": "https://example.invalid/cwru",
            "license": "test-license",
            "files": {"97.mat": {"sha256": "sha256:aaa", "label": "NORMAL"}},
            "checksum": None,  # 아래에서 실제 재계산 가능한 값으로 채운다.
        },
        "compatibility": {"signalType": ["vibration"], "samplingRateHz": 12000},
        "labelTaxonomyVersion": "CWRU-FAULT-V1",
        "labelMapping": {"NORMAL": "NORMAL", "FAULT": "ANOMALY"},
        "labelPolicyVersion": "LABEL-POLICY-V2",
        "snapshotSchemaVersion": "2",
        # rows에서 다시 계산 가능한 fingerprint. build_manifest()가 실제로 채우는
        # 값을 흉내낸다 — freeze_dataset_version()이 이 값을 rows와 대조한다.
        "featureOutputFingerprint": compute_rows_fingerprint(rows),
        "checksumInputs": _checksum_inputs(),
        "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
        "splitStrategy": "operating_condition_holdout: ...",
        "holdoutType": "operating_condition",
        "independentHoldout": False,
        "rows": rows,
    }
    # build_manifest()처럼 source.checksum과 id suffix를 canonical 재계산 값으로
    # 맞춘다 — freeze_dataset_version()이 이를 draft 필드만으로 재검증한다.
    manifest["source"]["checksum"] = compute_source_checksum(manifest)
    checksum_suffix = manifest["source"]["checksum"].split(":", 1)[1][:12]
    manifest["id"] = f"DS-TEST-001-{checksum_suffix}"
    return manifest


def _draft_manifest_legacy() -> dict:
    """v1.3 필드가 없는 구형 draft — 신규 동결이 거부되어야 한다."""
    return {
        "id": "DS-LEGACY-001",
        "status": "draft",
        "labelMapping": {"NORMAL": "NORMAL", "FAULT": "ANOMALY"},
        "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
        "rows": [{"sample_id": "A", "common_label": "NORMAL"}],
    }


def _legacy_frozen() -> dict:
    """이미 커밋된 구(v1) 동결본 — snapshotDigest/v1.3 필드 없음. approve/summary는
    관용 처리해야 한다."""
    manifest = _draft_manifest_legacy()
    frozen = copy.deepcopy(manifest)
    frozen["status"] = "frozen"
    frozen["datasetChecksum"] = compute_dataset_checksum(manifest)
    frozen["frozenAt"] = "2026-08-01T00:00:00+00:00"
    return frozen


class TestDatasetVersionStateMachine(unittest.TestCase):
    def test_freeze_sets_status_and_checksum(self):
        frozen = freeze_dataset_version(_draft_manifest())
        self.assertEqual(frozen["status"], "frozen")
        self.assertIn("datasetChecksum", frozen)
        self.assertIn("frozenAt", frozen)
        self.assertIn("snapshotDigest", frozen)

    def test_freeze_does_not_mutate_original(self):
        draft = _draft_manifest()
        freeze_dataset_version(draft)
        self.assertEqual(draft["status"], "draft")
        self.assertNotIn("snapshotDigest", draft)

    def test_freeze_deep_copies_nested_rows(self):
        draft = _draft_manifest()
        frozen = freeze_dataset_version(draft)
        draft["rows"][0]["common_label"] = "TAMPERED"
        self.assertEqual(frozen["rows"][0]["common_label"], "NORMAL")
        self.assertEqual(frozen["datasetChecksum"], compute_dataset_checksum(frozen))
        self.assertEqual(frozen["snapshotDigest"], compute_snapshot_digest(frozen))

    def test_approve_deep_copies_nested_rows(self):
        """[리뷰 P1, 6차] freeze()/approve()가 반환하는 두 사본은 서로 독립적이다 —
        freeze()가 반환한 사본을 변조해도 registry.approve()가 반환하는 승인
        레코드에는 영향이 없다(registry 내부 canonical 레코드는 애초에 그 사본과
        별개다 — TestApproveDatasetVersionBoundToCanonicalRegistration 참고)."""
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="검증 완료")
        frozen["rows"][0]["common_label"] = "CHANGED"
        self.assertEqual(approved["rows"][0]["common_label"], "NORMAL")

    def test_freeze_non_draft_rejected(self):
        frozen = freeze_dataset_version(_draft_manifest())
        with self.assertRaises(ValueError):
            freeze_dataset_version(frozen)  # 이미 frozen -> 재동결 불가

    def test_approve_requires_freeze_first(self):
        """draft는 아직 어떤 레지스트리에도 동결(freeze)된 적이 없으므로, 그
        id로 approve()를 호출해도 조회되지 않는다 — draft를 바로 승인하는
        경로는 없다."""
        draft = _draft_manifest()
        registry = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry.approve(draft["id"], approved_by="mgr", reason="검증 완료")

    def test_approve_after_freeze(self):
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="검증 완료")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["approvalReason"], "검증 완료")

    def test_approve_blank_reason_rejected(self):
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        with self.assertRaises(ValueError):
            registry.approve(frozen["id"], approved_by="mgr", reason="   ")

    def test_approve_blank_approved_by_rejected(self):
        """[리뷰 P2] approved_by가 공백만 있는 문자열이어도 reason과 동일하게
        거부해야 한다 — `not "   "`는 False라서 예전에는 통과했다."""
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        with self.assertRaises(ValueError):
            registry.approve(frozen["id"], approved_by="   ", reason="ok")

    def test_checksum_changes_when_rows_change(self):
        manifest_a = _draft_manifest()
        manifest_b = _draft_manifest()
        manifest_b["rows"][0]["sample_id"] = "CHANGED"
        self.assertNotEqual(
            compute_dataset_checksum(manifest_a), compute_dataset_checksum(manifest_b)
        )

    def test_verify_reproducibility_true_for_identical_content(self):
        manifest_a = _draft_manifest()
        manifest_b = _draft_manifest()  # 내용 동일, 다른 dict 인스턴스
        frozen = freeze_dataset_version(manifest_a)
        self.assertTrue(verify_reproducibility(frozen, manifest_b))

    def test_verify_reproducibility_false_when_content_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        different = _draft_manifest()
        different["split"] = {"train": 0.5, "validation": 0.3, "test": 0.2}
        self.assertFalse(verify_reproducibility(frozen, different))

    def test_verify_reproducibility_false_when_source_checksum_differs(self):
        """[리뷰 P1] rows/labelMapping/split만 같으면 원본(source) checksum이
        달라도 이전에는 재현 성공으로 오판됐다. [리뷰 P1, 3차] source.checksum
        필드 그 자체가 아니라 그 값을 결정하는 실제 내용(원본 파일 sha256)이
        달라야 한다 — checksumInputs가 있으면 recomputed_manifest의 필드값은
        신뢰하지 않고 내용으로 재계산하기 때문이다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["source"]["files"]["97.mat"]["sha256"] = "sha256:different-file-hash"
        other["source"]["checksum"] = compute_source_checksum(other)  # 내용에 맞춰 재계산
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_label_policy_version_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["labelPolicyVersion"] = "LABEL-POLICY-V9"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_snapshot_schema_version_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["snapshotSchemaVersion"] = "99"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_legacy_ignores_snapshot_identity_with_trusted_legacy(self):
        """호출자가 trusted_legacy=True를 명시하면 legacy(v1) 동결본은
        datasetChecksum만 본다 — 관용 처리를 유지한다."""
        legacy = _legacy_frozen()
        recomputed = _draft_manifest_legacy()
        self.assertTrue(
            verify_reproducibility(legacy, recomputed, trusted_legacy=True)
        )

    def test_summary_excludes_rows(self):
        frozen = freeze_dataset_version(_draft_manifest())
        summary = dataset_version_summary(frozen)
        self.assertNotIn("rows", summary)
        self.assertEqual(summary["status"], "frozen")

    def test_summary_deep_copies_nested_objects_frozen(self):
        frozen = freeze_dataset_version(_draft_manifest())
        summary = dataset_version_summary(frozen)
        summary["labelMapping"]["NORMAL"] = "TAMPERED"
        summary["split"]["train"] = 0.999
        self.assertEqual(frozen["labelMapping"]["NORMAL"], "NORMAL")
        self.assertEqual(frozen["split"]["train"], 0.7)
        self.assertEqual(frozen["datasetChecksum"], compute_dataset_checksum(frozen))

    def test_summary_deep_copies_nested_objects_approved(self):
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="검증 완료")
        summary = dataset_version_summary(approved)
        summary["labelMapping"]["FAULT"] = "TAMPERED"
        self.assertEqual(approved["labelMapping"]["FAULT"], "ANOMALY")
        self.assertEqual(approved["datasetChecksum"], compute_dataset_checksum(approved))


class TestFreezeRequiresV13Fields(unittest.TestCase):
    """[리뷰 P1] v1.3 필드가 없는 신규 draft의 동결을 차단한다 — 이미 동결된 v1과
    지금 새로 동결하는 것은 다른 요구사항이다."""

    def test_legacy_draft_freeze_rejected(self):
        with self.assertRaises(ValueError):
            freeze_dataset_version(_draft_manifest_legacy())

    def test_each_required_field_missing_rejects(self):
        for drop in (
            "labelPolicyVersion",
            "snapshotSchemaVersion",
            "featureOutputFingerprint",
            "checksumInputs",
        ):
            draft = _draft_manifest()
            del draft[drop]
            with self.assertRaises(ValueError, msg=drop):
                freeze_dataset_version(draft)
        draft = _draft_manifest()
        draft["source"].pop("checksum")
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)


class TestFreezeRejectsStaleFeatureOutputFingerprint(unittest.TestCase):
    """[리뷰 P1, 2차] build_manifest() 이후 rows/라벨이 바뀐 draft를 동결하면
    (재계산되지 않은) 예전 source.checksum/id가 새 rows에 그대로 붙는다 — 서로
    다른 rows를 가진 두 데이터셋이 같은 id/snapshotChecksum으로 승인될 수 있었다.
    featureOutputFingerprint가 현재 rows와 대조되어 이를 막아야 한다."""

    def test_rows_changed_after_build_rejects_freeze(self):
        draft = _draft_manifest()
        draft["rows"][0]["common_label"] = "TAMPERED"  # build 이후 라벨 변경
        # id/source.checksum은 (실수로든 의도적으로든) 예전 값 그대로 방치.
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)

    def test_two_different_drafts_cannot_share_id_via_stale_fingerprint(self):
        """서로 다른 rows를 가진 두 draft가 같은 id/source.checksum을 그대로
        복사해 왔다면(예: 후처리 스크립트가 rows만 바꾸고 id는 안 바꿈), 최소한
        하나는 동결이 거부되어야 한다 — featureOutputFingerprint는 원본 rows
        기준으로 한 번만 유효하다."""
        draft_a = _draft_manifest()
        draft_b = _draft_manifest()
        draft_b["rows"][1]["common_label"] = "NORMAL"  # 다른 내용, 같은 id/checksum/fingerprint 신고

        frozen_a = freeze_dataset_version(draft_a)  # draft_a는 자기 rows와 일치 -> 통과
        self.assertEqual(frozen_a["datasetChecksum"], compute_dataset_checksum(draft_a))
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft_b)  # draft_b는 신고된 fingerprint와 불일치 -> 거부

    def test_untampered_draft_still_freezes(self):
        freeze_dataset_version(_draft_manifest())  # no raise


class TestFreezeRecomputesFullCanonicalIdentity(unittest.TestCase):
    """[리뷰 P1, 3차] rows 변경 후 fingerprint만 재계산하거나(현재 rows와의 대조는
    통과), fingerprint에는 안 들어가지만 checksum 계산에는 들어가는 필드
    (labelMapping 등)만 바꿔도, 예전 source.checksum/id를 그대로 승계해 동결·
    승인될 수 있었다. freeze는 draft가 가진 필드만으로 source.checksum과 id
    suffix를 독립적으로 재계산해 대조해야 한다."""

    def test_rows_changed_and_fingerprint_recomputed_to_match_still_rejected(self):
        """공격자가 rows를 바꾸고 featureOutputFingerprint도 새 rows에 맞게
        재계산해 신고하면(freeze의 fingerprint-vs-rows 대조는 통과) — 하지만
        id/source.checksum은 예전 rows 기준 값 그대로 방치했다면 여전히 거부돼야
        한다."""
        from dataset_version import compute_rows_fingerprint as _fp

        draft = _draft_manifest()
        draft["rows"][0]["common_label"] = "TAMPERED"
        draft["featureOutputFingerprint"] = _fp(draft["rows"])  # 새 rows와는 일치
        # id/source.checksum은 원래 rows 기준 값 그대로(재계산 안 함).
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)

    def test_unused_label_mapping_change_with_stale_checksum_rejected(self):
        """featureOutputFingerprint는 rows만으로 결정되므로 labelMapping을 바꿔도
        변하지 않는다 — 하지만 source.checksum 계산에는 labelMapping이 들어가므로,
        labelMapping만 바꾸고 id/checksum을 그대로 두면 checksum이 실제로는
        더 이상 이 draft 내용을 반영하지 않는다."""
        draft = _draft_manifest()
        draft["labelMapping"] = dict(draft["labelMapping"], FAULT="OTHER")
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)

    def test_checksum_inputs_change_with_stale_declared_checksum_rejected(self):
        """window_size 등 checksumInputs만 바꾸고 source.checksum/id를 그대로 두면
        거부돼야 한다 — rows/fingerprint가 그대로여도 checksum이 반영해야 할
        입력이 달라졌다."""
        draft = _draft_manifest()
        draft["checksumInputs"]["windowSize"] = 4096
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)

    def test_id_suffix_mismatched_with_source_checksum_rejected(self):
        draft = _draft_manifest()
        draft["id"] = "DS-TEST-001-000000000000"  # source.checksum과 무관한 suffix
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)

    def test_verify_reproducibility_rejects_manifest_whose_checksum_field_is_forged_to_match(self):
        """recomputed_manifest의 실제 내용(checksumInputs)은 frozen과 다른데,
        source.checksum 필드만 frozen과 같은 값으로 위조하면 — 필드값만 비교하던
        예전 방식은 "재현 성공"으로 오판했다. checksumInputs가 있으면 필드값을
        믿지 않고 각자 내용으로 독립 재계산해 대조해야 한다."""
        frozen = freeze_dataset_version(_draft_manifest())
        forged = _draft_manifest()
        forged["checksumInputs"]["windowSize"] = 4096  # 실제 내용이 다름
        forged["source"]["checksum"] = frozen["source"]["checksum"]  # 필드만 위조해 맞춤
        self.assertFalse(verify_reproducibility(frozen, forged))

    def test_verify_reproducibility_rejects_when_recomputed_checksum_inputs_deleted_and_content_changed(self):
        """[리뷰 P1, 4차] recomputed_manifest에서 checksumInputs를 통째로 지우고
        samplingRateHz/splitStrategy를 바꿔도, 예전에는 (양쪽 중 하나라도
        checksumInputs가 없으면) 필드값 비교로 폴백해 "재현 성공"으로 오판했다.
        비-legacy 경로에서는 양쪽 모두 checksumInputs가 있어야 한다 — 하나라도
        없으면 무조건 실패로 처리한다."""
        frozen = freeze_dataset_version(_draft_manifest())
        recomputed = _draft_manifest()
        # source.checksum 필드는 예전 값 그대로 둔 채(재계산 안 함) 실제 내용만 바꾼다.
        recomputed["compatibility"]["samplingRateHz"] = 48000
        recomputed["splitStrategy"] = "operating_condition_holdout: TAMPERED"
        del recomputed["checksumInputs"]
        self.assertFalse(verify_reproducibility(frozen, recomputed))

    def test_verify_reproducibility_rejects_when_frozen_checksum_inputs_deleted(self):
        """checksumInputs가 없는 쪽이 frozen이면, frozen 자체의 무결성 검증
        (snapshotDigest — checksumInputs를 지운 것 자체가 매니페스트 내용 변경)에서
        먼저 걸린다."""
        frozen = freeze_dataset_version(_draft_manifest())
        del frozen["checksumInputs"]
        recomputed = _draft_manifest()
        with self.assertRaises(ValueError):
            verify_reproducibility(frozen, recomputed)

    def test_verify_reproducibility_false_when_source_license_differs(self):
        """[리뷰 P1, 5차] source.license는 datasetChecksum에도
        compute_source_checksum() payload에도 들어가지 않으므로, 이 필드만
        바뀌면 이전에는 여전히 재현 성공으로 오판됐다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["source"]["license"] = "different-license"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_name_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["name"] = "different-dataset-name"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_sampling_rate_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["compatibility"]["samplingRateHz"] = 48000
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_top_level_split_strategy_differs(self):
        """checksumInputs.splitStrategyKey(내부 재계산 입력)와는 별개인, 사람이
        읽는 최상위 splitStrategy 문자열."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["splitStrategy"] = "operating_condition_holdout: TAMPERED-TEXT"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_raises_when_frozen_manifest_itself_tampered(self):
        """[리뷰 P1, 4차] verify_reproducibility는 recomputed와 비교하기 전에
        frozen_manifest 자체의 무결성(snapshotDigest)부터 검증해야 한다 —
        그렇지 않으면 변조된 frozen과 우연히 일치하는 recomputed를 "재현
        성공"으로 오판할 수 있다."""
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["source"]["license"] = "TAMPERED-AFTER-FREEZE"  # snapshotDigest와 불일치
        recomputed = _draft_manifest()
        with self.assertRaises(ValueError):
            verify_reproducibility(frozen, recomputed)

    def test_verify_reproducibility_false_when_int_field_type_changes_to_float(self):
        """[리뷰 P1, 6차] 파이썬 dict 비교(`==`)에서 `12000 == 12000.0`이 참이라,
        recomputed_manifest에서 samplingRateHz를 정수에서 실수로 **타입만** 바꿔도
        이전에는 (canonical snapshot을 dict로 비교했으므로) 재현 성공으로
        오판됐다 — 반면 `compute_snapshot_digest()`는 JSON 직렬화 기준이라
        `12000`과 `12000.0`을 다른 값으로 낸다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["compatibility"]["samplingRateHz"] = 12000.0
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_bool_field_type_changes_to_int(self):
        """[리뷰 P1, 6차] `False == 0`이 파이썬에서 참이라, independentHoldout을
        `False`에서 정수 `0`으로 타입만 바꿔도 이전에는 재현 성공으로 오판됐다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["independentHoldout"] = 0  # False와 "값"은 같지만 타입이 다름
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_recomputed_id_is_totally_unrelated(self):
        """[리뷰 P1, 6차] id 전체를 비교에서 빼면, 내용은 완전히 동일한
        recomputed_manifest의 id만 checksum과 무관한 문자열로 바꿔도 재현 성공으로
        오판됐다 — id의 마지막 12자리 16진수 suffix는 각자 재계산한
        source.checksum suffix와 일치해야 한다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["id"] = "TOTALLY-UNRELATED-ID"
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_false_when_id_suffix_is_not_valid_hex_format(self):
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["id"] = "DS-TEST-001-NOTVALIDHEX12"  # 12자지만 16진수가 아님
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_verify_reproducibility_true_when_only_id_prefix_differs(self):
        """id의 checksum suffix(마지막 "-" 뒤 12자리 16진수)만 각자 내용과 일치하면,
        그 앞의 이름·날짜 부분이 서로 달라도(재실행 날짜가 바뀌는 정상적인 경우)
        재현 성공으로 처리해야 한다 — suffix가 아닌 부분까지 비교하면 안 된다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        suffix = other["id"].rsplit("-", 1)[-1]
        other["id"] = f"DS-TEST-001-DIFFERENT-BUILD-DATE-{suffix}"
        self.assertTrue(verify_reproducibility(frozen, other))


class TestV13FrozenFields(unittest.TestCase):
    def test_freeze_carries_policy_schema_and_snapshot_checksum(self):
        draft = _draft_manifest()
        frozen = freeze_dataset_version(draft)
        self.assertEqual(frozen["labelPolicyVersion"], "LABEL-POLICY-V2")
        self.assertEqual(frozen["snapshotSchemaVersion"], "2")
        self.assertEqual(frozen["snapshotChecksum"], draft["source"]["checksum"])

    def test_datasetChecksum_calc_unchanged_reproducibility_holds(self):
        frozen = freeze_dataset_version(_draft_manifest())
        self.assertEqual(frozen["datasetChecksum"], compute_dataset_checksum(frozen))
        self.assertTrue(verify_reproducibility(frozen, _draft_manifest()))

    def test_approve_carries_new_fields(self):
        draft = _draft_manifest()
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(draft)
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="검증 완료")
        self.assertEqual(approved["labelPolicyVersion"], "LABEL-POLICY-V2")
        self.assertEqual(approved["snapshotChecksum"], draft["source"]["checksum"])
        self.assertEqual(approved["snapshotDigest"], compute_snapshot_digest(approved))


class TestSnapshotDigestProtectsIntegrity(unittest.TestCase):
    """[리뷰 P1] datasetChecksum(rows+labelMapping+split)만으로는 freeze 이후
    source.license/checksum, samplingRate, 정책 버전 변조를 잡지 못한다.
    snapshotDigest는 매니페스트 전체 불변 필드를 보호한다.

    [리뷰 P1, 6차] 승인은 이제 `DatasetVersionRegistry`가 소유한 canonical 레코드만
    id로 조회해 이뤄진다(`TestApproveDatasetVersionBoundToCanonicalRegistration`
    참고) — 호출자가 들고 있는 frozen 사본을 변조해도 승인 대상 자체에는 영향이
    없다. 그래서 이 클래스는 그 방어를 실제로 수행하는 `verify_frozen_integrity()`
    (학습·배포 등 소비자가 임의 소스에서 읽어온 매니페스트를 스스로 재검증할 때
    쓰는 함수이자, `DatasetVersionRegistry.approve()`가 내부적으로 의존하는 바로
    그 검증)를 직접 대상으로 삼는다."""

    def _tamper_and_expect_reject(self, mutate):
        frozen = freeze_dataset_version(_draft_manifest())
        mutate(frozen)
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_source_license_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f["source"].__setitem__("license", "CHANGED")
        )

    def test_source_checksum_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f["source"].__setitem__("checksum", "sha256:other")
        )

    def test_sampling_rate_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f["compatibility"].__setitem__("samplingRateHz", 48000)
        )

    def test_label_policy_version_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f.__setitem__("labelPolicyVersion", "LABEL-POLICY-V9")
        )

    def test_snapshot_schema_version_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f.__setitem__("snapshotSchemaVersion", "99")
        )

    def test_split_strategy_tamper_rejected(self):
        self._tamper_and_expect_reject(
            lambda f: f.__setitem__("independentHoldout", True)
        )

    def test_untampered_frozen_passes_integrity_and_approves(self):
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        verify_frozen_integrity(frozen)  # no raise
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="ok")
        self.assertEqual(approved["status"], "approved")

    def test_deleting_snapshot_digest_does_not_bypass_integrity_check(self):
        """[리뷰 P1] snapshotDigest 필드를 지워서 v1(legacy) 관용 경로로 강등시키는
        우회를 차단한다. v1.3 동결본(snapshotSchemaVersion 보유)에서 digest만
        지우고 source.license를 변조해도 무결성 검증이 이를 잡아내야 한다
        (datasetChecksum은 rows/labelMapping/split만 보므로 license 변조를 못
        잡는다 — 예전에는 이 경로로 승인이 통과했다)."""
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["source"]["license"] = "CHANGED-BY-ATTACKER"
        del frozen["snapshotDigest"]

        self.assertFalse(is_legacy_v1_frozen(frozen))
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_deleting_snapshot_digest_without_other_tamper_still_rejected(self):
        """digest 삭제 자체만으로도(다른 필드 변조 없이) v1.3 동결본은 거부돼야
        한다 — "무결성 검증값이 없다"는 사실 자체가 거부 사유다."""
        frozen = freeze_dataset_version(_draft_manifest())
        del frozen["snapshotDigest"]
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_deleting_both_schema_version_and_digest_still_rejected(self):
        """[리뷰 P1, 2차] snapshotSchemaVersion과 snapshotDigest를 **함께** 지워도
        legacy 관용 경로로 강등되지 않는다. legacy 취급은 이제 매니페스트 필드가
        아니라 호출자가 명시하는 `trusted_legacy`로만 결정되므로, 기본값(False)에서는
        두 필드를 모두 지운 v1.3 레코드도 무조건 거부된다."""
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["source"]["license"] = "CHANGED-BY-ATTACKER"
        del frozen["snapshotSchemaVersion"]
        del frozen["snapshotDigest"]

        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)  # trusted_legacy 기본값(False)

    def test_trusted_legacy_true_is_required_for_legacy_fallback(self):
        """호출자가 `trusted_legacy=True`를 명시적으로 전달할 때만 legacy(v1)
        완화 검증(datasetChecksum 폴백)이 적용된다."""
        legacy = _legacy_frozen()
        with self.assertRaises(ValueError):
            verify_frozen_integrity(legacy)  # 기본값 False -> snapshotDigest 없음 거부
        verify_frozen_integrity(legacy, trusted_legacy=True)  # no raise


class TestApproveDatasetVersionBoundToCanonicalRegistration(unittest.TestCase):
    """[리뷰 P1, 6차] 승인을 canonical 동결 레코드에 결속한다 — `DatasetVersionRegistry`
    (model_version.py의 `ModelVersionRegistry`/`BaselineVersionRegistry`와 동일한
    신뢰 경계).

    이전에는 `approve_dataset_version()`이 공개 함수라서, 호출자가
    `freeze_dataset_version()`(v1.3 필수 필드·featureOutputFingerprint·
    source.checksum 교차검증)을 실제로 거친 적 없는 임의 dict도 — 공개 함수인
    `compute_dataset_checksum()`/`compute_snapshot_digest()`로 **자기 자신과만**
    일관된 checksum/digest를 계산해 채워 넣기만 하면 — 승인을 통과시킬 수 있었다.
    `DatasetVersionRegistry`는 승인 대상을 인스턴스 자신이 `freeze()`(또는
    `adopt_legacy_frozen()`)를 실제로 거쳐 소유하고 있는 저장소에서 `id` 문자열로만
    조회하므로, 그런 위조가 애초에 구조적으로 불가능하다."""

    def test_approving_never_frozen_id_rejected(self):
        """이 레지스트리의 `freeze()`를 거친 적 없는 id는 어떤 문자열을 대도
        조회되지 않는다 — canonical 저장소에 없으면 그걸로 끝이다."""
        registry = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry.approve("never-frozen-id", approved_by="mgr", reason="ok")

    def test_hand_crafted_frozen_object_with_forged_digest_never_reaches_registry(self):
        """[리뷰 P1] 호출자가 v1.3 필수 필드를 전부 채우고
        `compute_dataset_checksum()`/`compute_snapshot_digest()`(둘 다 공개 함수)로
        **자기 자신과만** 일관된 checksum/digest를 계산해 채운 임의 dict를 아무리
        정교하게 만들어도 — `verify_frozen_integrity()` 자기-일관성 검사 자체는
        통과하더라도 — `DatasetVersionRegistry.approve()`는 그 dict 자체를 인자로
        받지 않는다(오직 `id` 문자열만 받는다). `freeze()`를 실제로 거친 적이
        없으므로 이 레지스트리에 그 dict를 주입할 방법이 없다."""
        forged = _draft_manifest()
        forged["status"] = "frozen"
        forged["frozenAt"] = "2026-01-01T00:00:00+00:00"
        forged["snapshotChecksum"] = forged["source"]["checksum"]
        forged["datasetChecksum"] = compute_dataset_checksum(forged)
        forged["snapshotDigest"] = compute_snapshot_digest(forged)
        # 자기-일관성 자체는 통과한다 — 그런데도 registry.freeze()를 거친 적이 없다.
        verify_frozen_integrity(forged)  # no raise

        registry = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry.approve(forged["id"], approved_by="mgr", reason="ok")

    def test_mutating_returned_frozen_copy_does_not_affect_registry_state(self):
        """[리뷰 P1, 6차] `freeze()`가 반환하는 값은 깊은 복사본이다 — 호출자가
        반환값을 변조해도(id/rows 등을 바꿔도) 레지스트리 내부 상태는 전혀
        영향받지 않는다. 원래 id는 정상적으로 승인되고, 변조된 값이 가리키는
        id는(이 레지스트리의 freeze()를 거친 적 없으므로) 승인할 수 없다."""
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        original_id = frozen["id"]
        frozen["id"] = "id-tampered"
        frozen["rows"][0]["common_label"] = "TAMPERED"

        with self.assertRaises(ValueError):
            registry.approve("id-tampered", approved_by="mgr", reason="ok")

        approved = registry.approve(original_id, approved_by="mgr", reason="ok")
        self.assertEqual(approved["rows"][0]["common_label"], "NORMAL")

    def test_registry_lookup_approves_canonical_entry(self):
        registry = DatasetVersionRegistry()
        frozen = registry.freeze(_draft_manifest())
        approved = registry.approve(frozen["id"], approved_by="mgr", reason="ok")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["rows"], frozen["rows"])

    def test_registry_isolated_between_instances(self):
        """서로 다른 `DatasetVersionRegistry` 인스턴스는 완전히 독립된 저장소다 —
        한 인스턴스에 동결된 데이터셋을 다른 인스턴스에서 승인할 수 없다."""
        registry_a = DatasetVersionRegistry()
        frozen = registry_a.freeze(_draft_manifest())
        registry_b = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry_b.approve(frozen["id"], approved_by="mgr", reason="ok")

    def test_freeze_rejects_duplicate_id(self):
        """[리뷰 P1, 6차] 같은 id를 이 레지스트리에 두 번 동결하면 조용히 덮어써
        감사 이력이 끊긴다 — 명시적으로 거부해야 한다."""
        registry = DatasetVersionRegistry()
        draft = _draft_manifest()
        registry.freeze(draft)
        with self.assertRaises(ValueError):
            registry.freeze(copy.deepcopy(draft))


class TestFrozenIntegrityVerification(unittest.TestCase):
    """[리뷰 P1] verify_frozen_integrity: 학습·배포처럼 동결본을 입력으로 쓰는 쪽이
    status만 보지 말고 전체 snapshot 무결성을 재검증해야 한다."""

    def test_passes_for_untampered_frozen(self):
        verify_frozen_integrity(freeze_dataset_version(_draft_manifest()))  # no raise

    def test_raises_on_row_tamper(self):
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_raises_on_metadata_tamper(self):
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["source"]["license"] = "CHANGED"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_raises_for_non_frozen(self):
        with self.assertRaises(ValueError):
            verify_frozen_integrity(_draft_manifest())  # draft


class TestLegacyV1FrozenTolerance(unittest.TestCase):
    """이미 동결된 v1(snapshotDigest 없음)은 재동결(`freeze()`)하지 않고,
    `DatasetVersionRegistry.adopt_legacy_frozen()`으로 가져와야만 승인할 수 있다
    (리뷰 P1, 2차 — legacy 여부는 매니페스트 필드로 추정하지 않는다. 리뷰 P1, 6차 —
    adopt하지 않은 legacy 레코드는 어떤 id를 대도 승인 대상으로 조회되지 않는다)."""

    def test_is_legacy_v1_frozen(self):
        # 정보성 추정일 뿐 — 무결성 검증의 신뢰 판단에는 쓰이지 않는다.
        self.assertTrue(is_legacy_v1_frozen(_legacy_frozen()))
        self.assertFalse(is_legacy_v1_frozen(freeze_dataset_version(_draft_manifest())))

    def test_v1_frozen_approve_still_works_via_adopt_legacy_frozen(self):
        registry = DatasetVersionRegistry()
        legacy = registry.adopt_legacy_frozen(_legacy_frozen())
        approved = registry.approve(
            legacy["id"], approved_by="mgr", reason="기존 승인", trusted_legacy=True
        )
        self.assertEqual(approved["status"], "approved")

    def test_v1_frozen_approve_without_trusted_legacy_rejected(self):
        # trusted_legacy를 명시하지 않으면 v1.3 엄격 검증(snapshotDigest 필수)이
        # 적용돼 legacy 레코드도 거부된다 — 매니페스트 필드로 legacy를 봐주지 않는다.
        registry = DatasetVersionRegistry()
        legacy = registry.adopt_legacy_frozen(_legacy_frozen())
        with self.assertRaises(ValueError):
            registry.approve(legacy["id"], approved_by="mgr", reason="x")

    def test_adopt_legacy_frozen_rejects_row_tamper(self):
        """`adopt_legacy_frozen()`도 `verify_frozen_integrity(trusted_legacy=True)`를
        거치므로, 자기 자신의 datasetChecksum과 어긋나는(rows가 변조된) 레코드는
        저장소에 들어갈 수조차 없다."""
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        registry = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry.adopt_legacy_frozen(frozen)

    def test_v1_frozen_integrity_falls_back_to_dataset_checksum_with_trusted_legacy(self):
        verify_frozen_integrity(_legacy_frozen(), trusted_legacy=True)  # no raise
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen, trusted_legacy=True)

    def test_v1_frozen_summary_still_works(self):
        summary = dataset_version_summary(_legacy_frozen())
        self.assertNotIn("rows", summary)

    def test_adopt_legacy_frozen_requires_non_blank_id(self):
        legacy = _legacy_frozen()
        legacy["id"] = "   "
        registry = DatasetVersionRegistry()
        with self.assertRaises(ValueError):
            registry.adopt_legacy_frozen(legacy)

    def test_adopt_legacy_frozen_rejects_duplicate_id(self):
        registry = DatasetVersionRegistry()
        registry.adopt_legacy_frozen(_legacy_frozen())
        with self.assertRaises(ValueError):
            registry.adopt_legacy_frozen(_legacy_frozen())


class TestTrustedLegacyRequiresActualBool(unittest.TestCase):
    """[리뷰 P1] trusted_legacy는 실제 bool만 허용한다 — 문자열 "false"는
    파이썬에서 truthy라서, 예전에는 legacy 완화 경로가 잘못 켜져 snapshotDigest
    없는 변조 레코드의 검증·승인·재현성 확인이 통과했다."""

    def test_approve_rejects_string_false(self):
        registry = DatasetVersionRegistry()
        legacy = registry.adopt_legacy_frozen(_legacy_frozen())
        with self.assertRaises(TypeError):
            registry.approve(
                legacy["id"], approved_by="mgr", reason="x", trusted_legacy="false"
            )

    def test_verify_frozen_integrity_rejects_string_false(self):
        legacy = _legacy_frozen()
        with self.assertRaises(TypeError):
            verify_frozen_integrity(legacy, trusted_legacy="false")

    def test_verify_reproducibility_rejects_string_false(self):
        legacy = _legacy_frozen()
        recomputed = _draft_manifest_legacy()
        with self.assertRaises(TypeError):
            verify_reproducibility(legacy, recomputed, trusted_legacy="false")

    def test_approve_rejects_int_one_as_truthy_stand_in(self):
        registry = DatasetVersionRegistry()
        legacy = registry.adopt_legacy_frozen(_legacy_frozen())
        with self.assertRaises(TypeError):
            registry.approve(legacy["id"], approved_by="mgr", reason="x", trusted_legacy=1)


class TestModelVersionLifecycle(unittest.TestCase):
    def test_register_starts_as_registered(self):
        registry = ModelVersionRegistry()
        mv = registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        self.assertEqual(mv["status"], "registered")
        self.assertEqual(mv["artifactChecksum"], "sha256:" + "a" * 64)
        self.assertIn("registrationDigest", mv)

    def test_approve_requires_registered(self):
        registry = ModelVersionRegistry()
        registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        approved = registry.approve(
            "v1", approved_by="mgr", reason="지표 통과", metric_snapshot={"f1": 0.9},
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.9})
        self.assertIn("approvalDigest", approved)
        with self.assertRaises(ValueError):
            registry.approve(
                "v1", approved_by="mgr", reason="재승인 시도",
                metric_snapshot={"f1": 0.9},
            )

    def test_metric_snapshot_is_isolated_from_source_metrics(self):
        metrics = {"f1": 0.91, "cm": {"tp": 10, "fp": 1}}
        registry = ModelVersionRegistry()
        registry.register(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics=metrics, artifact_checksum="sha256:" + "a" * 64,
        )
        approved = registry.approve(
            "v1", approved_by="mgr", reason="지표 통과", metric_snapshot=metrics,
        )
        metrics["f1"] = 0.12
        metrics["cm"]["tp"] = 0
        self.assertEqual(approved["metricSnapshot"]["f1"], 0.91)
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 10)

    def test_explicit_metric_snapshot_is_deep_copied(self):
        registry = ModelVersionRegistry()
        registry.register(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics={"f1": 0.5}, artifact_checksum="sha256:" + "a" * 64,
        )
        snap = {"f1": 0.91, "cm": {"tp": 3}}
        approved = registry.approve(
            "v1", approved_by="mgr", reason="ok", metric_snapshot=snap,
        )
        snap["cm"]["tp"] = 999
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 3)


class TestRegisterModelVersionValidation(unittest.TestCase):
    """[리뷰 P1] 불완전한 등록 정보가 승인 상태까지 조용히 전이되지 않도록 등록
    단계에서 필수 메타데이터를 검증한다."""

    _VALID_KWARGS = dict(
        version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
        baseline_version="b1", metrics={"f1": 0.9},
        artifact_checksum="sha256:" + "a" * 64,
    )

    def test_blank_required_strings_rejected(self):
        for field in (
            "version", "artifact_uri", "dataset_id", "baseline_version", "artifact_checksum",
        ):
            for blank in ("", "   "):
                kwargs = dict(self._VALID_KWARGS)
                kwargs[field] = blank
                with self.assertRaises(ValueError, msg=f"{field}={blank!r}"):
                    ModelVersionRegistry().register(**kwargs)

    def test_none_metrics_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["metrics"] = None
        with self.assertRaises(ValueError):
            ModelVersionRegistry().register(**kwargs)

    def test_non_dict_metrics_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["metrics"] = "not-a-dict"
        with self.assertRaises(ValueError):
            ModelVersionRegistry().register(**kwargs)

    def test_malformed_artifact_checksum_rejected(self):
        """[리뷰 P2] "not-a-sha256" 같은 값도 비어있지 않은 문자열이라는 이유로
        그대로 등록됐다 — sha256 hexdigest 형식(`sha256:` + 64자리 16진수)인지도
        검증해야 한다."""
        for bad in (
            "not-a-sha256",
            "sha256:aaa",  # 너무 짧음
            "sha256:" + "a" * 63,  # 63자(한 글자 부족)
            "sha256:" + "a" * 65,  # 65자(한 글자 초과)
            "sha256:" + "g" * 64,  # 16진수가 아닌 문자 포함
            "md5:" + "a" * 32,  # 다른 알고리즘 접두사
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # 접두사 없음
            "sha256:" + "a" * 64 + "\n",  # [리뷰 P2, 2차] 트레일링 개행 — re.match는 통과시켰다
        ):
            kwargs = dict(self._VALID_KWARGS)
            kwargs["artifact_checksum"] = bad
            with self.assertRaises(ValueError, msg=f"artifact_checksum={bad!r}"):
                ModelVersionRegistry().register(**kwargs)

    def test_valid_sha256_artifact_checksum_accepted(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["artifact_checksum"] = "sha256:" + "0123456789abcdef" * 4
        mv = ModelVersionRegistry().register(**kwargs)
        self.assertEqual(mv["artifactChecksum"], "sha256:" + "0123456789abcdef" * 4)

    def test_uppercase_hex_checksum_normalized_to_lowercase(self):
        """[리뷰 P2, 2차] 등록 시 대문자 16진수를 그대로 저장하면, 추론 단계
        (`score_from_artifact`)가 파일 바이트로 계산한 `hexdigest()`(항상 소문자)와
        문자열이 달라 정상 아티팩트가 거부된다. 저장 전에 소문자로 정규화해야 한다."""
        kwargs = dict(self._VALID_KWARGS)
        kwargs["artifact_checksum"] = "sha256:" + "ABCDEF0123456789" * 4
        mv = ModelVersionRegistry().register(**kwargs)
        self.assertEqual(mv["artifactChecksum"], "sha256:" + "abcdef0123456789" * 4)

    def test_registration_digest_matches_recompute(self):
        mv = ModelVersionRegistry().register(**self._VALID_KWARGS)
        self.assertEqual(mv["registrationDigest"], compute_registration_digest(mv))

    def test_duplicate_version_registration_rejected(self):
        """[리뷰 P1, 5차] 같은 버전을 이 레지스트리에 두 번 등록하면(다른 내용으로도)
        조용히 덮어써 감사 이력이 끊긴다 — 명시적으로 거부해야 한다."""
        registry = ModelVersionRegistry()
        registry.register(**self._VALID_KWARGS)
        with self.assertRaises(ValueError):
            registry.register(**self._VALID_KWARGS)


class TestApproveModelVersionRequiresAuditTrail(unittest.TestCase):
    """[리뷰 P1] 승인은 승인자와 명시적인 지표 스냅샷 없이는 이뤄질 수 없다."""

    def _registered(self, registry=None):
        registry = registry or ModelVersionRegistry()
        registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        return registry

    def test_missing_approved_by_rejected(self):
        registry = self._registered()
        with self.assertRaises(TypeError):
            registry.approve("v1", reason="ok", metric_snapshot={"f1": 0.9})

    def test_blank_approved_by_rejected(self):
        registry = self._registered()
        with self.assertRaises(ValueError):
            registry.approve("v1", approved_by="   ", reason="ok", metric_snapshot={"f1": 0.9})

    def test_missing_metric_snapshot_rejected(self):
        registry = self._registered()
        with self.assertRaises(TypeError):
            registry.approve("v1", approved_by="mgr", reason="ok")

    def test_empty_metric_snapshot_rejected(self):
        registry = self._registered()
        with self.assertRaises(ValueError):
            registry.approve("v1", approved_by="mgr", reason="ok", metric_snapshot={})

    def test_non_dict_metric_snapshot_rejected(self):
        registry = self._registered()
        with self.assertRaises(ValueError):
            registry.approve(
                "v1", approved_by="mgr", reason="ok", metric_snapshot="not-a-dict",
            )

    def test_non_finite_metric_snapshot_value_rejected(self):
        """[리뷰 P2] metric_snapshot 내부(중첩 포함) 수치가 NaN/Inf면 거부한다 —
        JSON 직렬화 실패나 잘못된 승인 근거로 남는 것을 막는다."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            registry = self._registered()
            with self.assertRaises(ValueError, msg=f"f1={bad!r}"):
                registry.approve(
                    "v1", approved_by="mgr", reason="ok", metric_snapshot={"f1": bad},
                )
            registry2 = self._registered()
            with self.assertRaises(ValueError, msg=f"nested f1={bad!r}"):
                registry2.approve(
                    "v1", approved_by="mgr", reason="ok",
                    metric_snapshot={"cm": {"f1": bad}},
                )

    def test_valid_approval_records_approver_and_snapshot(self):
        registry = self._registered()
        approved = registry.approve(
            "v1", approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.95},
        )
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.95})


class TestApproveModelVersionBoundToCanonicalRegistration(unittest.TestCase):
    """[리뷰 P1, 3차·4차·5차] 승인을 canonical 등록 레코드에 결속한다.

    이전에는 `registry`가 호출자가 만든 평범한 list라서, 임의 레코드에
    `compute_registration_digest()`(공개 함수)로 직접 계산한 digest를 채워
    넣고 `registry=[그 레코드]`로 넘기면 "자기 서명"이 되어 승인이 통과했다.
    `ModelVersionRegistry`는 승인 대상을 인스턴스 자신이 소유한 저장소에서
    `version` 문자열로만 조회하므로, 그런 위조가 애초에 구조적으로 불가능하다."""

    def test_approving_unregistered_version_rejected(self):
        """이 레지스트리의 `register()`를 거치지 않은 버전은 어떤 문자열을
        대도 조회되지 않는다 — canonical 저장소에 없으면 그걸로 끝이다."""
        registry = ModelVersionRegistry()
        with self.assertRaises(ValueError):
            registry.approve(
                "never-registered", approved_by="mgr", reason="ok",
                metric_snapshot={"f1": 0.9},
            )

    def test_hand_crafted_object_with_forged_digest_never_reaches_registry(self):
        """[리뷰 P1, 5차] 호출자가 `compute_registration_digest()`로 직접 계산한
        digest를 채운 임의 dict를 아무리 정교하게 만들어도, `ModelVersionRegistry`의
        `approve()`는 그 dict 자체를 인자로 받지 않는다(오직 `version` 문자열만
        받는다) — 그 dict를 레지스트리에 주입할 방법이 없으므로 "자기 서명" 공격이
        성립하지 않는다."""
        forged = {
            "version": "v-forged", "artifactUri": "file:///forged.pt",
            "artifactChecksum": "sha256:" + "f" * 64, "datasetId": "d",
            "baselineVersion": "b", "metrics": {"f1": 0.99},
            "status": "registered", "createdAt": "2026-01-01T00:00:00+00:00",
        }
        forged["registrationDigest"] = compute_registration_digest(forged)

        registry = ModelVersionRegistry()
        # forged 레코드는 이 레지스트리의 register()를 거친 적이 없다 —
        # approve()에 forged 자체를 넘기는 API 자체가 없으므로, version
        # 문자열로만 조회를 시도할 수 있고 당연히 실패한다.
        with self.assertRaises(ValueError):
            registry.approve(
                forged["version"], approved_by="mgr", reason="ok",
                metric_snapshot={"f1": 0.9},
            )

    def test_mutating_returned_record_does_not_affect_registry_state(self):
        """[리뷰 P1, 5차] `register()`가 반환하는 값은 깊은 복사본이다 — 호출자가
        반환값을 변조해도(예: version/artifactChecksum을 바꿔도) 레지스트리
        내부 상태는 전혀 영향받지 않는다. 원래 버전은 정상적으로 승인되고,
        변조된 값이 가리키는 버전은(등록된 적 없으므로) 승인할 수 없다."""
        registry = ModelVersionRegistry()
        mv = registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        mv["version"] = "v1-tampered"
        mv["artifactChecksum"] = "sha256:" + "9" * 64

        with self.assertRaises(ValueError):
            registry.approve(
                "v1-tampered", approved_by="mgr", reason="ok",
                metric_snapshot={"f1": 0.9},
            )
        approved = registry.approve(
            "v1", approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
        )
        self.assertEqual(approved["artifactUri"], "file:///v1.pt")
        self.assertEqual(approved["artifactChecksum"], "sha256:" + "a" * 64)

    def test_registry_lookup_approves_canonical_entry(self):
        registry = ModelVersionRegistry()
        registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        approved = registry.approve(
            "v1", approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
        )
        self.assertEqual(approved["artifactUri"], "file:///v1.pt")

    def test_registry_isolated_between_instances(self):
        """서로 다른 `ModelVersionRegistry` 인스턴스는 완전히 독립된 저장소다 —
        한 인스턴스에 등록된 버전을 다른 인스턴스에서 승인할 수 없다."""
        registry_a = ModelVersionRegistry()
        registry_a.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        registry_b = ModelVersionRegistry()
        with self.assertRaises(ValueError):
            registry_b.approve(
                "v1", approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
            )


class TestRollbackLineage(unittest.TestCase):
    """[리뷰 P1] 롤백 대상은 실제로 current보다 앞선 승인 버전이어야 한다.
    자기 자신·더 최신 버전으로의 '롤백'을 차단한다.

    [리뷰 P1, 5차] 계보는 이제 `ModelVersionRegistry` 자신이 보유한 전체 상태
    에서 나온다 — 별도의 `approved_history` 인자를 넘길 필요도, 넘길 방법도
    없다. 승인 시각은 `model_version._now_iso`를 결정적으로 패치해 제어한다
    (production API에 approvedAt을 직접 지정하는 우회 경로를 열어두지 않기
    위해서다 — 그런 경로 자체가 과거에 이 모듈이 겪은 위조 공격이었다)."""

    def _approve_at(self, registry, version, when):
        with mock.patch("model_version._now_iso", return_value=when):
            return registry.approve(
                version, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
            )

    def _registry_with_approved(self, versions_and_times):
        """[(version, approvedAt), ...] 순서로 등록·승인된 레지스트리를 만든다."""
        registry = ModelVersionRegistry()
        for version, when in versions_and_times:
            registry.register(
                version=version, artifact_uri=f"file:///{version}.pt", dataset_id="d",
                baseline_version="b", metrics={"f1": 0.9},
                artifact_checksum="sha256:" + "a" * 64,
            )
            self._approve_at(registry, version, when)
        return registry

    def test_rollback_to_earlier_approved_version_ok(self):
        registry = self._registry_with_approved(
            [("v1", "2026-08-01T00:00:00+00:00"), ("v2", "2026-08-10T00:00:00+00:00")]
        )
        action = registry.rollback(
            "v2", "v1", reason="v2 회귀", target_environment="production",
        )
        self.assertEqual(action["fromVersion"], "v2")
        self.assertEqual(action["toVersion"], "v1")
        self.assertEqual(action["targetApprovedAt"], "2026-08-01T00:00:00+00:00")

    def test_self_rollback_rejected(self):
        registry = self._registry_with_approved([("v1", "2026-08-01T00:00:00+00:00")])
        with self.assertRaises(ValueError):
            registry.rollback("v1", "v1", reason="x", target_environment="production")

    def test_forward_rollback_rejected(self):
        registry = self._registry_with_approved(
            [("v1", "2026-08-01T00:00:00+00:00"), ("v2", "2026-08-10T00:00:00+00:00")]
        )
        with self.assertRaises(ValueError):
            registry.rollback("v1", "v2", reason="x", target_environment="production")

    def test_non_approved_target_rejected(self):
        registry = self._registry_with_approved([("v2", "2026-08-10T00:00:00+00:00")])
        registry.register(
            version="v1", artifact_uri="a1", dataset_id="d", baseline_version="b",
            metrics={}, artifact_checksum="sha256:" + "b" * 64,
        )
        with self.assertRaises(ValueError):  # target(v1)이 approved가 아님
            registry.rollback("v2", "v1", reason="x", target_environment="production")

    def test_rollback_target_never_registered_rejected(self):
        """[리뷰 P1, 5차] 예전 `approved_history`가 필수 인자였던 것과 같은
        보호를 이제는 레지스트리 자체가 구조적으로 제공한다 — 이 레지스트리에
        등록조차 된 적 없는 버전은 롤백 대상이 될 수 없다."""
        registry = self._registry_with_approved([("v2", "2026-08-10T00:00:00+00:00")])
        with self.assertRaises(ValueError):
            registry.rollback("v2", "v1", reason="x", target_environment="production")

    def test_empty_registry_rollback_rejected(self):
        registry = ModelVersionRegistry()
        with self.assertRaises(ValueError):
            registry.rollback("v2", "v1", reason="x", target_environment="production")

    def test_blank_target_environment_rejected(self):
        registry = self._registry_with_approved(
            [("v1", "2026-08-01T00:00:00+00:00"), ("v2", "2026-08-10T00:00:00+00:00")]
        )
        with self.assertRaises(ValueError):
            registry.rollback("v2", "v1", reason="x", target_environment="")

    def test_unrecognized_target_environment_rejected(self):
        """[리뷰 P1, 3차] target_environment는 허용된 환경 값이어야 한다 — 임의
        문자열을 그대로 기록하지 않는다."""
        registry = self._registry_with_approved(
            [("v1", "2026-08-01T00:00:00+00:00"), ("v2", "2026-08-10T00:00:00+00:00")]
        )
        with self.assertRaises(ValueError):
            registry.rollback(
                "v2", "v1", reason="x", target_environment="not-a-real-env",
            )

    def test_approved_history_lineage_enforced(self):
        registry = self._registry_with_approved(
            [
                ("v1", "2026-08-01T00:00:00+00:00"),
                ("v2", "2026-08-10T00:00:00+00:00"),
                ("v3", "2026-08-20T00:00:00+00:00"),
            ]
        )
        # v3 -> v1 (계보상 앞) OK
        registry.rollback("v3", "v1", reason="ok", target_environment="production")
        # v1 -> v3 (계보상 뒤) 거부
        with self.assertRaises(ValueError):
            registry.rollback("v1", "v3", reason="x", target_environment="production")
        # 이 레지스트리에 없는 target 거부
        with self.assertRaises(ValueError):
            registry.rollback("v3", "vX", reason="x", target_environment="production")

    def test_registration_order_does_not_affect_lineage_only_approved_at_does(self):
        """[리뷰 P1] 계보 순서 판정은 등록·승인 호출 순서가 아니라 각 항목의
        실제 `approvedAt`을 UTC로 파싱해 비교해야 한다. v3을 v1보다 먼저
        등록·승인해도(호출 순서 역전) approvedAt 자체는 v1이 더 이르므로
        v3 -> v1 롤백은 여전히 허용되고 v1 -> v3는 여전히 거부돼야 한다."""
        registry = ModelVersionRegistry()
        # 호출 순서: v3 먼저 등록·승인(이른 시각으로), 그다음 v1(늦은 시각으로).
        registry.register(
            version="v3", artifact_uri="file:///v3.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        self._approve_at(registry, "v3", "2026-08-20T00:00:00+00:00")
        registry.register(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9},
            artifact_checksum="sha256:" + "a" * 64,
        )
        self._approve_at(registry, "v1", "2026-08-01T00:00:00+00:00")

        # v3 -> v1: 호출 순서로는 v1이 나중에 등록·승인됐지만, 실제
        # approvedAt은 v1이 더 이르므로 허용돼야 한다.
        registry.rollback("v3", "v1", reason="ok", target_environment="production")
        # v1 -> v3: approvedAt 기준 forward rollback -> 거부.
        with self.assertRaises(ValueError):
            registry.rollback("v1", "v3", reason="x", target_environment="production")


class TestBaselineVersionLifecycle(unittest.TestCase):
    _FEATURES = {"rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03, 0.07]}}

    def test_activate_requires_approved(self):
        registry = BaselineVersionRegistry()
        registry.register(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features=self._FEATURES,
        )
        with self.assertRaises(ValueError):
            registry.activate("BL-1")

        registry.approve("BL-1", approved_by="mgr", reason="검증 통과")
        active = registry.activate("BL-1")
        self.assertEqual(active["status"], "active")

    def test_features_are_isolated_through_register_approve_activate(self):
        features = {"rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03, 0.07]}}
        registry = BaselineVersionRegistry()
        bv = registry.register(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features=features,
        )
        registry.approve("BL-1", approved_by="mgr", reason="ok")
        active = registry.activate("BL-1")
        features["rms_mean"]["mean"] = 99.0
        bv["features"]["rms_mean"]["std"] = 99.0
        self.assertEqual(active["features"]["rms_mean"]["mean"], 0.05)
        self.assertEqual(active["features"]["rms_mean"]["std"], 0.01)

    def test_registration_digest_matches_recompute(self):
        bv = BaselineVersionRegistry().register(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features=self._FEATURES,
        )
        self.assertEqual(
            bv["registrationDigest"], compute_baseline_registration_digest(bv)
        )


class TestBaselineVersionValidation(unittest.TestCase):
    """[리뷰 P1] 빈 baseline/dataset/site/asset ID, 구조가 잘못된 features, 공백
    승인자가 draft에서 approved/active까지 조용히 전이되지 않도록 검증한다."""

    _VALID_KWARGS = dict(
        baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
        asset_id="SITE-01-MOT-02",
        features={"rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03, 0.07]}},
    )

    def test_blank_id_fields_rejected(self):
        for field in ("baseline_id", "dataset_id", "site_id", "asset_id"):
            for blank in ("", "   "):
                kwargs = dict(self._VALID_KWARGS)
                kwargs[field] = blank
                with self.assertRaises(ValueError, msg=f"{field}={blank!r}"):
                    BaselineVersionRegistry().register(**kwargs)

    def test_non_dict_features_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = "not-a-dict"
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().register(**kwargs)

    def test_empty_features_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {}
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().register(**kwargs)

    def test_negative_or_nan_std_rejected(self):
        """[리뷰 P1] 음수·NaN std는 명백히 잘못된 통계이므로 계속 거부한다."""
        for bad_std in (-0.01, float("nan")):
            kwargs = dict(self._VALID_KWARGS)
            kwargs["features"] = {
                "rms_mean": {"mean": 0.05, "std": bad_std, "normal_range": [0.03, 0.07]}
            }
            with self.assertRaises(ValueError, msg=f"std={bad_std!r}"):
                BaselineVersionRegistry().register(**kwargs)

    def test_zero_std_is_accepted(self):
        """[리뷰 P1] 저장소의 실제 week2 baseline.json은 rms_std/kurtosis_std/
        spectral_rolloff처럼 모든 윈도우에서 값이 상수라 std가 정확히 0.0인
        특징을 포함한다(기존 판정 로직도 near-zero std 특징은 건너뛴다) — std==0을
        거부하면 그 실제 baseline을 등록할 수 없었다. 음수만 거부하고 0은
        소비자(판정 로직) 정책에 맡긴다."""
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "spectral_rolloff": {
                "mean": 1066.40625, "std": 0.0, "normal_range": [1066.40625, 1066.40625]
            }
        }
        bv = BaselineVersionRegistry().register(**kwargs)
        self.assertEqual(bv["features"]["spectral_rolloff"]["std"], 0.0)

    def test_non_finite_mean_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": float("inf"), "std": 0.01, "normal_range": [0.03, 0.07]}
        }
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().register(**kwargs)

    def test_malformed_normal_range_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03]}
        }
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().register(**kwargs)

    def test_blank_approved_by_rejected(self):
        registry = BaselineVersionRegistry()
        registry.register(**self._VALID_KWARGS)
        with self.assertRaises(ValueError):
            registry.approve("BL-1", approved_by="   ", reason="ok")

    def test_activating_unregistered_baseline_rejected(self):
        """[리뷰 P1, 5차] status만 "approved"로 맞춘 임의 dict를 만들어도,
        `BaselineVersionRegistry.activate()`는 그 dict 자체를 인자로 받지
        않는다(오직 `baseline_id` 문자열만 받는다) — 이 레지스트리에 등록된
        적 없는 id는 조회조차 되지 않는다."""
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().activate("never-registered")

    def test_mutating_returned_record_does_not_affect_registry_state(self):
        """[리뷰 P1, 5차] `register()`가 반환하는 값은 깊은 복사본이다 — 호출자가
        반환값의 status/features를 직접 바꿔도(예: approve()를 거친 것처럼 status만
        "approved"로 바꾸거나 approve() 이후 features를 변조해도) 레지스트리 내부
        상태는 전혀 영향받지 않는다."""
        registry = BaselineVersionRegistry()
        bv = registry.register(**self._VALID_KWARGS)
        bv["status"] = "approved"  # 반환값만 변조 — 내부 상태는 여전히 draft
        with self.assertRaises(ValueError):
            registry.activate("BL-1")

        approved = registry.approve("BL-1", approved_by="mgr", reason="ok")
        approved["features"]["rms_mean"]["std"] = 999.0  # 반환값만 변조
        active = registry.activate("BL-1")  # 내부 상태는 손상되지 않았으므로 정상 진행
        self.assertEqual(active["features"]["rms_mean"]["std"], 0.01)

    def test_approval_digest_matches_recompute(self):
        registry = BaselineVersionRegistry()
        registry.register(**self._VALID_KWARGS)
        approved = registry.approve("BL-1", approved_by="mgr", reason="ok")
        self.assertEqual(
            approved["approvalDigest"], compute_baseline_approval_digest(approved)
        )

    def test_internal_store_is_not_reachable_under_the_naive_attribute_name(self):
        """[리뷰 확인 요청] `approve()`가 "approved" 상태를 저장소에 기록하는
        유일한 경로인지 확인하는 질문에 대한 답: 내부 저장소는 `self.__entries`
        (이름 맹글링 → `_BaselineVersionRegistry__entries`)로 선언돼 있어,
        `registry._entries`처럼 바로 짐작 가는 이름으로는 클래스 바깥에서 조회도
        변조도 할 수 없다(`AttributeError`). 파이썬에 진짜 접근 제어는 없으므로
        완전한 방어는 아니지만(맹글링된 실제 이름을 알면 여전히 접근 가능),
        "다른 setter나 직접 상태를 꽂아넣을 수 있는 경로"가 우연히/평범하게는
        없다는 것을 보장한다."""
        registry = BaselineVersionRegistry()
        registry.register(**self._VALID_KWARGS)
        with self.assertRaises(AttributeError):
            registry._entries  # noqa: B018 — 의도적으로 접근 시도, 실패해야 함

    def test_only_approve_writes_approved_status_into_the_store(self):
        """`BaselineVersionRegistry`의 공개 메서드는 `register`(status="draft"만
        기록)/`approve`/`activate`/`get`(읽기 전용) 네 개뿐이다 — "approved"
        상태를 저장소에 기록하는 코드 경로는 `approve()` 안의 단 한 곳
        (`self.__entries[baseline_id] = approved`)뿐이며, `register()`는
        `status` 인자를 아예 받지 않으므로 등록 시점에 "approved"를 주입할
        방법이 없다."""
        public_methods = {
            name
            for name in dir(BaselineVersionRegistry)
            if not name.startswith("_") and callable(getattr(BaselineVersionRegistry, name))
        }
        self.assertEqual(public_methods, {"register", "approve", "activate", "get"})

        import inspect

        register_params = set(inspect.signature(BaselineVersionRegistry.register).parameters)
        self.assertNotIn("status", register_params)
        self.assertNotIn("registrationDigest", register_params)
        self.assertNotIn("approvalDigest", register_params)

    def test_normal_range_lower_bound_above_upper_bound_rejected(self):
        """[리뷰 P2] normal_range의 두 값이 유한한지만 확인하고 lo<=hi는
        검증하지 않으면 [1.0, -1.0]처럼 뒤집힌 범위도 등록된다."""
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [1.0, -1.0]}
        }
        with self.assertRaises(ValueError):
            BaselineVersionRegistry().register(**kwargs)


@unittest.skipUnless(
    os.path.exists(_WEEK2_BASELINE_JSON),
    f"week2 baseline.json 없음: {_WEEK2_BASELINE_JSON}",
)
class TestRegisterRealWeek2Baseline(unittest.TestCase):
    """[리뷰 P1] 저장소의 실제 week2/ai1/dataset/baseline.json을 그대로 등록할 수
    있어야 한다. 이 파일은 rms_std/kurtosis_std/spectral_rolloff처럼 모든 윈도우에서
    값이 상수라 표본표준편차가 정확히 0.0인 특징을 포함한다 — std==0을 거부하던
    예전 검증에서는 등록 자체가 실패했다(기존 판정 로직은 이런 near-zero std
    특징을 건너뛰고 계속 판정하므로, 등록을 막을 이유가 없다)."""

    def _load_features(self) -> dict:
        import json

        with open(_WEEK2_BASELINE_JSON, encoding="utf-8") as f:
            data = json.load(f)
        # baseline.json의 feature 항목은 mean/std/min/max/normal_range를 갖는다 —
        # BaselineVersionRegistry.register()는 mean/std/normal_range만 검증하고
        # min/max 등 추가 키는 그대로 통과시킨다.
        return data["features"]

    def test_real_baseline_registers_successfully(self):
        features = self._load_features()
        self.assertIn("rms_std", features)
        self.assertEqual(features["rms_std"]["std"], 0.0)

        bv = BaselineVersionRegistry().register(
            baseline_id="BL-CWRU-NORMAL-1",
            dataset_id="DS-CWRU-VIBRATION-week2-baseline",
            site_id="SITE-01",
            asset_id="SITE-01-MOT-01",
            features=features,
        )
        self.assertEqual(bv["status"], "draft")
        self.assertEqual(bv["features"]["rms_std"]["std"], 0.0)
        self.assertEqual(bv["features"]["rms_mean"]["mean"], features["rms_mean"]["mean"])

    def test_real_baseline_can_be_approved_and_activated(self):
        registry = BaselineVersionRegistry()
        registry.register(
            baseline_id="BL-CWRU-NORMAL-1",
            dataset_id="DS-CWRU-VIBRATION-week2-baseline",
            site_id="SITE-01",
            asset_id="SITE-01-MOT-01",
            features=self._load_features(),
        )
        registry.approve("BL-CWRU-NORMAL-1", approved_by="mgr", reason="week2 baseline 승인")
        active = registry.activate("BL-CWRU-NORMAL-1")
        self.assertEqual(active["status"], "active")


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestReproducibilityWithRealCwruData(unittest.TestCase):
    """같은 조건으로 build_manifest()를 두 번 실행해 동결 시점 체크섬이 재현되는지
    확인한다 (DATASET_MODEL_01 수용 기준). 기본 specimen_group은 CWRU에서 실패하므로
    operating_condition_holdout으로 검증한다."""

    def test_same_inputs_reproduce_checksum(self):
        from register_dataset import build_manifest

        m1 = build_manifest(
            data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
        )
        frozen = freeze_dataset_version(m1)
        m2 = build_manifest(
            data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
        )
        self.assertTrue(verify_reproducibility(frozen, m2))

    def test_different_window_size_changes_checksum(self):
        from register_dataset import build_manifest

        frozen = freeze_dataset_version(
            build_manifest(
                data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
            )
        )
        other = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            window_size=1024,
            hop_size=1024,
            split_strategy="operating_condition_holdout",
        )
        self.assertFalse(verify_reproducibility(frozen, other))

    def test_operating_condition_holdout_ignores_requested_split_ratios_in_checksum(self):
        """[리뷰 P1] operating_condition_split은 요청 split_ratios를 무시하고 부하
        tier로 배정을 고정한다. 매니페스트/체크섬도 실제 rows를 반영해야 하므로,
        무의미한(무시되는) split_ratios를 다르게 넘겨도 실제 데이터가 같으면 같은
        체크섬 — 재현성 판정이 흔들리면 안 된다(이전에는 무시된 입력이 체크섬에
        그대로 들어가 실제로 같은 데이터가 다른 버전으로 판정됐다)."""
        from register_dataset import build_manifest

        m1 = build_manifest(
            data_dir=_CWRU_DATA_DIR, split_strategy="operating_condition_holdout"
        )
        m2 = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            split_ratios={"train": 1.0, "validation": 0.0, "test": 0.0},
            split_strategy="operating_condition_holdout",
        )
        self.assertEqual(m1["split"], m2["split"])
        self.assertTrue(
            verify_reproducibility(freeze_dataset_version(m1), m2)
        )


if __name__ == "__main__":
    unittest.main()
