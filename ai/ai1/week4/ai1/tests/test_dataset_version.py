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

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASET_VERSIONS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
_WEEK3_DATASETS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week3", "ai1", "datasets")
)
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _DATASET_VERSIONS_DIR)
sys.path.insert(0, _WEEK3_DATASETS_DIR)

from dataset_version import (  # noqa: E402
    freeze_dataset_version,
    approve_dataset_version,
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
    register_model_version,
    approve_model_version,
    rollback_model_version,
    register_baseline_version,
    approve_baseline_version,
    activate_baseline_version,
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

    def test_approve_rejects_post_freeze_row_mutation(self):
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["rows"][0]["common_label"] = "TAMPERED"  # 동결 이후 변조
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="검증 완료")

    def test_approve_deep_copies_nested_rows(self):
        frozen = freeze_dataset_version(_draft_manifest())
        approved = approve_dataset_version(frozen, approved_by="mgr", reason="검증 완료")
        frozen["rows"][0]["common_label"] = "CHANGED"
        self.assertEqual(approved["rows"][0]["common_label"], "NORMAL")

    def test_freeze_non_draft_rejected(self):
        frozen = freeze_dataset_version(_draft_manifest())
        with self.assertRaises(ValueError):
            freeze_dataset_version(frozen)  # 이미 frozen -> 재동결 불가

    def test_approve_requires_frozen(self):
        draft = _draft_manifest()
        with self.assertRaises(ValueError):
            approve_dataset_version(draft, approved_by="mgr", reason="검증 완료")

    def test_approve_after_freeze(self):
        frozen = freeze_dataset_version(_draft_manifest())
        approved = approve_dataset_version(frozen, approved_by="mgr", reason="검증 완료")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["approvalReason"], "검증 완료")

    def test_approve_blank_reason_rejected(self):
        frozen = freeze_dataset_version(_draft_manifest())
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="   ")

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
        approved = approve_dataset_version(
            freeze_dataset_version(_draft_manifest()),
            approved_by="mgr", reason="검증 완료",
        )
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
        approved = approve_dataset_version(
            freeze_dataset_version(draft),
            approved_by="mgr", reason="검증 완료",
        )
        self.assertEqual(approved["labelPolicyVersion"], "LABEL-POLICY-V2")
        self.assertEqual(approved["snapshotChecksum"], draft["source"]["checksum"])
        self.assertEqual(approved["snapshotDigest"], compute_snapshot_digest(approved))


class TestSnapshotDigestProtectsApproval(unittest.TestCase):
    """[리뷰 P1] datasetChecksum(rows+labelMapping+split)만으로는 freeze 이후
    source.license/checksum, samplingRate, 정책 버전 변조가 승인을 통과한다.
    snapshotDigest는 매니페스트 전체 불변 필드를 보호한다."""

    def _tamper_and_expect_reject(self, mutate):
        frozen = freeze_dataset_version(_draft_manifest())
        mutate(frozen)
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="검증 완료")

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

    def test_untampered_frozen_still_approves(self):
        frozen = freeze_dataset_version(_draft_manifest())
        approved = approve_dataset_version(frozen, approved_by="mgr", reason="ok")
        self.assertEqual(approved["status"], "approved")

    def test_deleting_snapshot_digest_does_not_bypass_integrity_check(self):
        """[리뷰 P1] snapshotDigest 필드를 지워서 v1(legacy) 관용 경로로 강등시키는
        우회를 차단한다. v1.3 동결본(snapshotSchemaVersion 보유)에서 digest만
        지우고 source.license를 변조해도 승인/무결성 검증이 이를 잡아내야 한다
        (datasetChecksum은 rows/labelMapping/split만 보므로 license 변조를 못
        잡는다 — 예전에는 이 경로로 승인이 통과했다)."""
        frozen = freeze_dataset_version(_draft_manifest())
        frozen["source"]["license"] = "CHANGED-BY-ATTACKER"
        del frozen["snapshotDigest"]

        self.assertFalse(is_legacy_v1_frozen(frozen))
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="ok")

    def test_deleting_snapshot_digest_without_other_tamper_still_rejected(self):
        """digest 삭제 자체만으로도(다른 필드 변조 없이) v1.3 동결본은 거부돼야
        한다 — "무결성 검증값이 없다"는 사실 자체가 거부 사유다."""
        frozen = freeze_dataset_version(_draft_manifest())
        del frozen["snapshotDigest"]
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="ok")

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
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="ok")

    def test_trusted_legacy_true_is_required_for_legacy_fallback(self):
        """호출자가 `trusted_legacy=True`를 명시적으로 전달할 때만 legacy(v1)
        완화 검증(datasetChecksum 폴백)이 적용된다."""
        legacy = _legacy_frozen()
        with self.assertRaises(ValueError):
            verify_frozen_integrity(legacy)  # 기본값 False -> snapshotDigest 없음 거부
        verify_frozen_integrity(legacy, trusted_legacy=True)  # no raise
        approved = approve_dataset_version(
            legacy, approved_by="mgr", reason="ok", trusted_legacy=True
        )
        self.assertEqual(approved["status"], "approved")


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
    """이미 동결된 v1(snapshotDigest 없음)은 재동결하지 않고, 호출자가
    `trusted_legacy=True`를 명시할 때만 관용 처리한다 (리뷰 P1, 2차 — legacy
    여부는 매니페스트 필드로 추정하지 않는다)."""

    def test_is_legacy_v1_frozen(self):
        # 정보성 추정일 뿐 — 무결성 검증의 신뢰 판단에는 쓰이지 않는다.
        self.assertTrue(is_legacy_v1_frozen(_legacy_frozen()))
        self.assertFalse(is_legacy_v1_frozen(freeze_dataset_version(_draft_manifest())))

    def test_v1_frozen_approve_still_works_with_trusted_legacy(self):
        approved = approve_dataset_version(
            _legacy_frozen(), approved_by="mgr", reason="기존 승인", trusted_legacy=True
        )
        self.assertEqual(approved["status"], "approved")

    def test_v1_frozen_approve_without_trusted_legacy_rejected(self):
        # trusted_legacy를 명시하지 않으면 v1.3 엄격 검증(snapshotDigest 필수)이
        # 적용돼 legacy 레코드도 거부된다 — 매니페스트 필드로 legacy를 봐주지 않는다.
        with self.assertRaises(ValueError):
            approve_dataset_version(_legacy_frozen(), approved_by="mgr", reason="x")

    def test_v1_frozen_approve_rejects_row_tamper(self):
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            approve_dataset_version(
                frozen, approved_by="mgr", reason="x", trusted_legacy=True
            )

    def test_v1_frozen_integrity_falls_back_to_dataset_checksum_with_trusted_legacy(self):
        verify_frozen_integrity(_legacy_frozen(), trusted_legacy=True)  # no raise
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen, trusted_legacy=True)

    def test_v1_frozen_summary_still_works(self):
        summary = dataset_version_summary(_legacy_frozen())
        self.assertNotIn("rows", summary)


class TestTrustedLegacyRequiresActualBool(unittest.TestCase):
    """[리뷰 P1] trusted_legacy는 실제 bool만 허용한다 — 문자열 "false"는
    파이썬에서 truthy라서, 예전에는 legacy 완화 경로가 잘못 켜져 snapshotDigest
    없는 변조 레코드의 검증·승인·재현성 확인이 통과했다."""

    def test_approve_rejects_string_false(self):
        legacy = _legacy_frozen()
        with self.assertRaises(TypeError):
            approve_dataset_version(
                legacy, approved_by="mgr", reason="x", trusted_legacy="false"
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
        legacy = _legacy_frozen()
        with self.assertRaises(TypeError):
            approve_dataset_version(legacy, approved_by="mgr", reason="x", trusted_legacy=1)


class TestModelVersionLifecycle(unittest.TestCase):
    def test_register_starts_as_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        self.assertEqual(mv["status"], "registered")
        self.assertEqual(mv["artifactChecksum"], "sha256:aaa")
        self.assertIn("registrationDigest", mv)

    def test_approve_requires_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="지표 통과", metric_snapshot={"f1": 0.9},
            registry=[mv],
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.9})
        self.assertIn("approvalDigest", approved)
        with self.assertRaises(ValueError):
            approve_model_version(
                approved, approved_by="mgr", reason="재승인 시도",
                metric_snapshot={"f1": 0.9}, registry=[mv],
            )

    def test_metric_snapshot_is_isolated_from_source_metrics(self):
        metrics = {"f1": 0.91, "cm": {"tp": 10, "fp": 1}}
        mv = register_model_version(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics=metrics, artifact_checksum="sha256:aaa",
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="지표 통과", metric_snapshot=metrics,
            registry=[mv],
        )
        metrics["f1"] = 0.12
        metrics["cm"]["tp"] = 0
        mv["metrics"]["f1"] = 0.0
        self.assertEqual(approved["metricSnapshot"]["f1"], 0.91)
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 10)

    def test_explicit_metric_snapshot_is_deep_copied(self):
        mv = register_model_version(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics={"f1": 0.5}, artifact_checksum="sha256:aaa",
        )
        snap = {"f1": 0.91, "cm": {"tp": 3}}
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok", metric_snapshot=snap, registry=[mv],
        )
        snap["cm"]["tp"] = 999
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 3)


class TestRegisterModelVersionValidation(unittest.TestCase):
    """[리뷰 P1] 불완전한 등록 정보가 승인 상태까지 조용히 전이되지 않도록 등록
    단계에서 필수 메타데이터를 검증한다."""

    _VALID_KWARGS = dict(
        version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
        baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
    )

    def test_blank_required_strings_rejected(self):
        for field in (
            "version", "artifact_uri", "dataset_id", "baseline_version", "artifact_checksum",
        ):
            for blank in ("", "   "):
                kwargs = dict(self._VALID_KWARGS)
                kwargs[field] = blank
                with self.assertRaises(ValueError, msg=f"{field}={blank!r}"):
                    register_model_version(**kwargs)

    def test_none_metrics_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["metrics"] = None
        with self.assertRaises(ValueError):
            register_model_version(**kwargs)

    def test_non_dict_metrics_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["metrics"] = "not-a-dict"
        with self.assertRaises(ValueError):
            register_model_version(**kwargs)

    def test_registration_digest_matches_recompute(self):
        mv = register_model_version(**self._VALID_KWARGS)
        self.assertEqual(mv["registrationDigest"], compute_registration_digest(mv))


class TestApproveModelVersionRequiresAuditTrail(unittest.TestCase):
    """[리뷰 P1] 승인은 승인자와 명시적인 지표 스냅샷 없이는 이뤄질 수 없다."""

    def _registered(self):
        return register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )

    def test_missing_approved_by_rejected(self):
        mv = self._registered()
        with self.assertRaises(TypeError):
            approve_model_version(
                mv, reason="ok", metric_snapshot={"f1": 0.9}, registry=[mv]
            )

    def test_blank_approved_by_rejected(self):
        mv = self._registered()
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="   ", reason="ok",
                metric_snapshot={"f1": 0.9}, registry=[mv],
            )

    def test_missing_metric_snapshot_rejected(self):
        mv = self._registered()
        with self.assertRaises(TypeError):
            approve_model_version(mv, approved_by="mgr", reason="ok", registry=[mv])

    def test_empty_metric_snapshot_rejected(self):
        mv = self._registered()
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok", metric_snapshot={}, registry=[mv]
            )

    def test_non_dict_metric_snapshot_rejected(self):
        mv = self._registered()
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok",
                metric_snapshot="not-a-dict", registry=[mv],
            )

    def test_non_finite_metric_snapshot_value_rejected(self):
        """[리뷰 P2] metric_snapshot 내부(중첩 포함) 수치가 NaN/Inf면 거부한다 —
        JSON 직렬화 실패나 잘못된 승인 근거로 남는 것을 막는다."""
        mv = self._registered()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError, msg=f"f1={bad!r}"):
                approve_model_version(
                    mv, approved_by="mgr", reason="ok",
                    metric_snapshot={"f1": bad}, registry=[mv],
                )
            with self.assertRaises(ValueError, msg=f"nested f1={bad!r}"):
                approve_model_version(
                    mv, approved_by="mgr", reason="ok",
                    metric_snapshot={"cm": {"f1": bad}}, registry=[mv],
                )

    def test_valid_approval_records_approver_and_snapshot(self):
        mv = self._registered()
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok",
            metric_snapshot={"f1": 0.95}, registry=[mv],
        )
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.95})


class TestApproveModelVersionBoundToCanonicalRegistration(unittest.TestCase):
    """[리뷰 P1, 3차·4차] 승인을 canonical 등록 레코드에 결속한다 — status만
    "registered"인 임의 객체나, 등록 이후 변조된 model_version, registry 없이
    호출자 자신의 registrationDigest만 신뢰하는 경로는 모두 거부돼야 한다."""

    def test_missing_registry_raises_type_error(self):
        """[리뷰 P1, 4차] registry는 이제 필수 인자다 — 생략하면(호출자가 전달한
        model_version 자신의 registrationDigest만으로 승인되는 경로가 아예
        없다) TypeError."""
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        with self.assertRaises(TypeError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9}
            )

    def test_self_computed_registration_digest_no_longer_sufficient(self):
        """[리뷰 P1, 4차] `compute_registration_digest`는 공개 함수라서, 호출자가
        임의로 만든 레코드에 그 함수로 직접 계산한 digest를 채워 넣으면 "자기
        서명"이 되어 registry 없이는(예전 코드에서) 승인됐다. 이제 registry가
        없으면 호출 자체가 TypeError이므로, self-signed 레코드도 이 경로로는
        승인될 수 없다."""
        forged = {
            "version": "v-forged", "artifactUri": "file:///forged.pt",
            "artifactChecksum": "sha256:forged", "datasetId": "d",
            "baselineVersion": "b", "metrics": {"f1": 0.99},
            "status": "registered", "createdAt": "2026-01-01T00:00:00+00:00",
        }
        forged["registrationDigest"] = compute_registration_digest(forged)
        with self.assertRaises(TypeError):
            approve_model_version(
                forged, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9}
            )
        # registry에 자기 자신만 넣어도(실제 canonical 저장소가 아니라 호출자가
        # 즉석에서 지어낸 registry) 여전히 최소한 registrationDigest 자기 일관성은
        # 요구되므로 이 경우는 통과한다 — 이 함수 하나만으로 "등록 증명"을 완전히
        # 대체할 수는 없지만(서명 체계가 없는 이 코드베이스의 근본 한계), 최소한
        # 여기서는 registry를 실제로 조회하도록 강제한다.
        approve_model_version(
            forged, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
            registry=[forged],
        )

    def test_hand_crafted_object_not_found_in_registry_rejected(self):
        """version/artifact/dataset/baseline 정보가 없는(또는 임의로 채운) 객체는
        canonical registry에서 그 version을 찾지 못하면 거부돼야 한다."""
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        forged = {"status": "registered", "version": "", "artifactUri": ""}
        with self.assertRaises(ValueError):
            approve_model_version(
                forged, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
                registry=[mv],
            )

    def test_tampered_version_after_registration_rejected(self):
        """정상 등록 결과의 version/artifactUri를 등록 이후에 바꾸면 registry
        자체의 registrationDigest 자기 일관성 검사에서 거부돼야 한다."""
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        mv["version"] = "v1-tampered"
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
                registry=[mv],
            )

    def test_tampered_artifact_checksum_after_registration_rejected(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        mv["artifactChecksum"] = "sha256:swapped-artifact"
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
                registry=[mv],
            )

    def test_registry_lookup_approves_canonical_entry(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        registry = [mv]
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
            registry=registry,
        )
        self.assertEqual(approved["artifactUri"], "file:///v1.pt")

    def test_registry_rejects_caller_value_that_diverges_from_canonical_entry(self):
        """registry가 주어지면 호출자가 넘긴 model_version이 canonical 등록본과
        내용이 달라도(예: artifactUri를 바꿔 전달) registry의 값을 대체로 쓰지
        않고 거부한다 — 계보 없이는 계보 밖 값을 신뢰하지 않는 rollback과 같은
        패턴."""
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        registry = [mv]
        forged_copy = copy.deepcopy(mv)
        forged_copy["artifactUri"] = "file:///swapped.pt"
        with self.assertRaises(ValueError):
            approve_model_version(
                forged_copy, approved_by="mgr", reason="ok",
                metric_snapshot={"f1": 0.9}, registry=registry,
            )

    def test_registry_rejects_version_not_present_exactly_once(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        with self.assertRaises(ValueError):
            approve_model_version(
                mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9},
                registry=[],  # v1이 registry에 없음
            )


class TestRollbackLineage(unittest.TestCase):
    """[리뷰 P1] 롤백 대상은 실제로 current보다 앞선 승인 버전이어야 한다.
    자기 자신·더 최신 버전으로의 '롤백'을 차단한다."""

    def _approved(self, version, when):
        mv = register_model_version(
            version=version, artifact_uri=f"file:///{version}.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9}, artifact_checksum="sha256:aaa",
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9}, registry=[mv]
        )
        approved["approvedAt"] = when  # 결정적 시각으로 고정
        # approvedAt을 덮어쓰면 approve_model_version()이 실제로 계산한
        # approvalDigest와 어긋난다 — "이 버전이 실제로 `when`에 승인됐다"는
        # 시나리오를 표현하려면 그 시각을 반영해 다시 계산해야 한다(테스트
        # 픽스처 전용 — 실제 코드 경로에서는 approvedAt을 직접 덮어쓰지 않는다).
        approved["approvalDigest"] = compute_approval_digest(approved)
        return approved

    def test_rollback_to_earlier_approved_version_ok(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        action = rollback_model_version(
            v2, v1, reason="v2 회귀", target_environment="production",
            approved_history=[v1, v2],
        )
        self.assertEqual(action["fromVersion"], "v2")
        self.assertEqual(action["toVersion"], "v1")
        self.assertEqual(action["targetApprovedAt"], "2026-08-01T00:00:00+00:00")

    def test_self_rollback_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v1, v1, reason="x", target_environment="production",
                approved_history=[v1],
            )

    def test_forward_rollback_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v1, v2, reason="x", target_environment="production",
                approved_history=[v1, v2],
            )

    def test_non_approved_target_rejected(self):
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v1_reg = register_model_version(
            version="v1", artifact_uri="a1", dataset_id="d", baseline_version="b",
            metrics={}, artifact_checksum="sha256:bbb",
        )
        with self.assertRaises(ValueError):  # target(v1)이 approved가 아님
            rollback_model_version(
                v2, v1_reg, reason="x", target_environment="production",
                approved_history=[v2, v1_reg],
            )

    def test_missing_approved_history_raises_type_error(self):
        """[리뷰 P1, 3차] approved_history는 필수 인자다 — 생략하면(예전처럼
        current/target 객체 자체의 approvedAt을 신뢰하는 폴백 경로로 빠지지 않고)
        호출 자체가 실패해야 한다."""
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        with self.assertRaises(TypeError):
            rollback_model_version(v2, v1, reason="x", target_environment="production")

    def test_forged_approvedAt_without_being_in_history_rejected(self):
        """approved_history를 명시적으로 전달하더라도, registered 상태인 current에
        임의의 approvedAt만 심어 놓고 그 current가 계보에 없으면 여전히 거부돼야
        한다 — 계보 밖 객체 자체의 값을 신뢰하지 않는다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2_unapproved = register_model_version(
            version="v2", artifact_uri="file:///v2.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9}, artifact_checksum="sha256:ccc",
        )
        v2_unapproved["approvedAt"] = "2026-08-10T00:00:00+00:00"  # 위조된 승인 시각
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2_unapproved, v1, reason="x", target_environment="production",
                approved_history=[v1],  # v2는 계보에 없음
            )

    def test_empty_approved_history_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="production", approved_history=[]
            )

    def test_blank_target_environment_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="", approved_history=[v1, v2]
            )

    def test_unrecognized_target_environment_rejected(self):
        """[리뷰 P1, 3차] target_environment는 허용된 환경 값이어야 한다 — 임의
        문자열을 그대로 기록하지 않는다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="not-a-real-env",
                approved_history=[v1, v2],
            )

    def test_approved_history_lineage_enforced(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v3 = self._approved("v3", "2026-08-20T00:00:00+00:00")
        history = [v1, v2, v3]
        # v3 -> v1 (계보상 앞) OK
        rollback_model_version(
            v3, v1, reason="ok", target_environment="production", approved_history=history
        )
        # v1 -> v3 (계보상 뒤) 거부
        with self.assertRaises(ValueError):
            rollback_model_version(
                v1, v3, reason="x", target_environment="production", approved_history=history
            )
        # 계보에 없는 target 거부
        stray = self._approved("vX", "2026-07-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v3, stray, reason="x", target_environment="production",
                approved_history=history,
            )

    def test_reversed_history_array_order_does_not_enable_forward_rollback(self):
        """[리뷰 P1] 계보 순서 판정은 배열 index가 아니라 approvedAt 실값을 써야
        한다. history를 시간 역순으로 넘겨도(예: 캐시·쿼리 정렬이 뒤집힌 경우)
        실제로는 더 최신인 버전으로의 forward rollback을 차단해야 한다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v3 = self._approved("v3", "2026-08-20T00:00:00+00:00")
        reversed_history = [v3, v2, v1]  # 시간 역순(내림차순)으로 전달

        # v1 -> v3: 배열 index로는 v3가 index 0(더 "앞")이라 예전 구현이 통과시켰다.
        # 실제 approvedAt 기준으로는 v3가 v1보다 미래이므로 forward rollback -> 거부.
        with self.assertRaises(ValueError):
            rollback_model_version(
                v1, v3, reason="x", target_environment="production",
                approved_history=reversed_history,
            )
        # v3 -> v1은 실제로 앞선 버전이므로 배열 순서와 무관하게 여전히 허용.
        rollback_model_version(
            v3, v1, reason="ok", target_environment="production",
            approved_history=reversed_history,
        )

    def test_duplicate_versions_in_history_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        dup_v1 = self._approved("v1", "2026-08-15T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="production",
                approved_history=[v1, dup_v1],
            )

    def test_current_not_in_history_rejected_even_with_forged_approvedAt(self):
        """[리뷰 P1, 2차] history가 주어지면 current 자체 객체의 approvedAt으로
        대체하면 안 된다. 실제로는 registered 상태인 current에 approvedAt만
        위조해 넣고, history에는 target(v1)만 전달해도 예전에는 v2->v1 롤백이
        만들어졌다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2_unapproved = register_model_version(
            version="v2", artifact_uri="file:///v2.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9}, artifact_checksum="sha256:ddd",
        )
        self.assertEqual(v2_unapproved["status"], "registered")
        v2_unapproved["approvedAt"] = "2026-08-10T00:00:00+00:00"  # 위조된 승인 시각
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2_unapproved, v1, reason="x", target_environment="production",
                approved_history=[v1],  # v2는 계보에 없음
            )

    def test_current_history_entry_must_be_approved_status(self):
        """history 안의 current 레코드 자체가 승인 상태가 아니면(계보가 변조됐거나
        오염됐다면) 거부한다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        tampered_v2_entry = dict(v2)
        tampered_v2_entry["status"] = "registered"
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="production",
                approved_history=[v1, tampered_v2_entry],
            )

    def test_entirely_fabricated_history_without_real_approval_rejected(self):
        """[리뷰 P1, 4차] 실제 register/approve를 한 번도 거치지 않고, 임의의
        `status`/`approvedAt`만으로 만든 old/new 레코드 두 개를 approved_history로
        전달해도 rollback 액션이 만들어지면 안 된다 — `status == "approved"`
        필드값만으로는 계보를 증명하지 못한다. registrationDigest/approvalDigest가
        없으면 거부해야 한다."""
        fabricated_old = {
            "version": "fab-old", "status": "approved",
            "approvedAt": "2026-01-01T00:00:00+00:00",
        }
        fabricated_new = {
            "version": "fab-new", "status": "approved",
            "approvedAt": "2026-02-01T00:00:00+00:00",
        }
        with self.assertRaises(ValueError):
            rollback_model_version(
                fabricated_new, fabricated_old, reason="x",
                target_environment="production",
                approved_history=[fabricated_old, fabricated_new],
            )

    def test_history_entry_with_tampered_registration_content_rejected(self):
        """계보 항목의 registrationDigest는 있지만(등록은 실제로 했지만) 등록
        이후 내용을 바꾼 경우 — 자기 일관성이 깨져 거부돼야 한다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        tampered_v1 = dict(v1)
        tampered_v1["artifactUri"] = "file:///tampered.pt"  # registrationDigest는 그대로 방치
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, tampered_v1, reason="x", target_environment="production",
                approved_history=[tampered_v1, v2],
            )

    def test_history_entry_with_tampered_approval_content_rejected(self):
        """approvalDigest 계산 이후 approvalReason 등 승인 관련 필드만 바꾸면
        registrationDigest는 그대로 일치해도 approvalDigest 불일치로 거부돼야
        한다."""
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        tampered_v1 = dict(v1)
        tampered_v1["approvalReason"] = "TAMPERED"  # approvalDigest는 그대로 방치
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, tampered_v1, reason="x", target_environment="production",
                approved_history=[tampered_v1, v2],
            )


class TestBaselineVersionLifecycle(unittest.TestCase):
    _FEATURES = {"rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03, 0.07]}}

    def test_activate_requires_approved(self):
        bv = register_baseline_version(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features=self._FEATURES,
        )
        with self.assertRaises(ValueError):
            activate_baseline_version(bv)

        approved = approve_baseline_version(bv, approved_by="mgr", reason="검증 통과")
        active = activate_baseline_version(approved)
        self.assertEqual(active["status"], "active")

    def test_features_are_isolated_through_register_approve_activate(self):
        features = {"rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03, 0.07]}}
        bv = register_baseline_version(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features=features,
        )
        approved = approve_baseline_version(bv, approved_by="mgr", reason="ok")
        active = activate_baseline_version(approved)
        features["rms_mean"]["mean"] = 99.0
        bv["features"]["rms_mean"]["std"] = 99.0
        self.assertEqual(approved["features"]["rms_mean"]["mean"], 0.05)
        self.assertEqual(active["features"]["rms_mean"]["std"], 0.01)

    def test_registration_digest_matches_recompute(self):
        bv = register_baseline_version(
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
                    register_baseline_version(**kwargs)

    def test_non_dict_features_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = "not-a-dict"
        with self.assertRaises(ValueError):
            register_baseline_version(**kwargs)

    def test_empty_features_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {}
        with self.assertRaises(ValueError):
            register_baseline_version(**kwargs)

    def test_non_positive_std_rejected(self):
        for bad_std in (0, -0.01, float("nan")):
            kwargs = dict(self._VALID_KWARGS)
            kwargs["features"] = {
                "rms_mean": {"mean": 0.05, "std": bad_std, "normal_range": [0.03, 0.07]}
            }
            with self.assertRaises(ValueError, msg=f"std={bad_std!r}"):
                register_baseline_version(**kwargs)

    def test_non_finite_mean_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": float("inf"), "std": 0.01, "normal_range": [0.03, 0.07]}
        }
        with self.assertRaises(ValueError):
            register_baseline_version(**kwargs)

    def test_malformed_normal_range_rejected(self):
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [0.03]}
        }
        with self.assertRaises(ValueError):
            register_baseline_version(**kwargs)

    def test_blank_approved_by_rejected(self):
        bv = register_baseline_version(**self._VALID_KWARGS)
        with self.assertRaises(ValueError):
            approve_baseline_version(bv, approved_by="   ", reason="ok")

    def test_hand_crafted_approved_object_without_registration_digest_rejected(self):
        """status만 "approved"로 맞춘 임의 객체는(ID·features 없이도) 예전에는
        activate까지 통과했다 — registrationDigest가 없으면 activate가 거부해야
        한다."""
        forged = {
            "status": "approved", "id": "", "datasetId": "", "siteId": "",
            "assetId": "", "features": {},
        }
        with self.assertRaises(ValueError):
            activate_baseline_version(forged)

    def test_tampered_features_after_approval_rejected_by_activate(self):
        bv = register_baseline_version(**self._VALID_KWARGS)
        approved = approve_baseline_version(bv, approved_by="mgr", reason="ok")
        approved["features"]["rms_mean"]["std"] = 999.0  # 승인 이후 변조
        with self.assertRaises(ValueError):
            activate_baseline_version(approved)

    def test_status_flip_without_calling_approve_rejected_by_activate(self):
        """[리뷰 P1, 4차] register_baseline_version() 결과의 status만
        "approved"로 직접 바꾸면(approve_baseline_version()을 한 번도 호출하지
        않아도) registrationDigest는 등록 시점 content(id/datasetId/siteId/
        assetId/timeSegment/features)만 보므로 그대로 일치해 activate가
        통과했었다. approvalDigest가 없으면 activate가 거부해야 한다."""
        bv = register_baseline_version(**self._VALID_KWARGS)
        bv["status"] = "approved"  # approve_baseline_version()을 거치지 않고 직접 변경
        self.assertEqual(
            bv["registrationDigest"], compute_baseline_registration_digest(bv)
        )  # registrationDigest는 여전히 유효함(변조 아님) — 그래도 거부돼야 한다.
        with self.assertRaises(ValueError):
            activate_baseline_version(bv)

    def test_approval_digest_matches_recompute(self):
        bv = register_baseline_version(**self._VALID_KWARGS)
        approved = approve_baseline_version(bv, approved_by="mgr", reason="ok")
        self.assertEqual(
            approved["approvalDigest"], compute_baseline_approval_digest(approved)
        )

    def test_normal_range_lower_bound_above_upper_bound_rejected(self):
        """[리뷰 P2] normal_range의 두 값이 유한한지만 확인하고 lo<=hi는
        검증하지 않으면 [1.0, -1.0]처럼 뒤집힌 범위도 등록된다."""
        kwargs = dict(self._VALID_KWARGS)
        kwargs["features"] = {
            "rms_mean": {"mean": 0.05, "std": 0.01, "normal_range": [1.0, -1.0]}
        }
        with self.assertRaises(ValueError):
            register_baseline_version(**kwargs)


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
