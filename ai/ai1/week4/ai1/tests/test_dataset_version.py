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
)


def _draft_manifest() -> dict:
    """API 명세서 v1.3 build_manifest() 출력을 흉내낸 draft (신규 필수 필드 포함)."""
    return {
        "id": "DS-TEST-001",
        "name": "test-dataset",
        "status": "draft",
        "source": {
            "type": "external",
            "uri": "https://example.invalid/cwru",
            "license": "test-license",
            "checksum": "sha256:cwru-version-checksum",
        },
        "compatibility": {"signalType": ["vibration"], "samplingRateHz": 12000},
        "labelTaxonomyVersion": "CWRU-FAULT-V1",
        "labelMapping": {"NORMAL": "NORMAL", "FAULT": "ANOMALY"},
        "labelPolicyVersion": "LABEL-POLICY-V2",
        "snapshotSchemaVersion": "2",
        "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
        "splitStrategy": "operating_condition_holdout: ...",
        "holdoutType": "operating_condition",
        "independentHoldout": False,
        "rows": [
            {"sample_id": "A", "common_label": "NORMAL"},
            {"sample_id": "B", "common_label": "ANOMALY"},
        ],
    }


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
        달라도 이전에는 재현 성공으로 오판됐다."""
        frozen = freeze_dataset_version(_draft_manifest())
        other = _draft_manifest()
        other["source"]["checksum"] = "sha256:different-source-checksum"
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

    def test_verify_reproducibility_legacy_ignores_snapshot_identity(self):
        """legacy(v1) 동결본은 snapshot identity 필드가 없으므로 datasetChecksum만
        본다 — 관용 처리를 유지한다."""
        legacy = _legacy_frozen()
        recomputed = _draft_manifest_legacy()
        self.assertTrue(verify_reproducibility(legacy, recomputed))

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
        for drop in ("labelPolicyVersion", "snapshotSchemaVersion"):
            draft = _draft_manifest()
            del draft[drop]
            with self.assertRaises(ValueError, msg=drop):
                freeze_dataset_version(draft)
        draft = _draft_manifest()
        draft["source"].pop("checksum")
        with self.assertRaises(ValueError):
            freeze_dataset_version(draft)


class TestV13FrozenFields(unittest.TestCase):
    def test_freeze_carries_policy_schema_and_snapshot_checksum(self):
        frozen = freeze_dataset_version(_draft_manifest())
        self.assertEqual(frozen["labelPolicyVersion"], "LABEL-POLICY-V2")
        self.assertEqual(frozen["snapshotSchemaVersion"], "2")
        self.assertEqual(frozen["snapshotChecksum"], "sha256:cwru-version-checksum")

    def test_datasetChecksum_calc_unchanged_reproducibility_holds(self):
        frozen = freeze_dataset_version(_draft_manifest())
        self.assertEqual(frozen["datasetChecksum"], compute_dataset_checksum(frozen))
        self.assertTrue(verify_reproducibility(frozen, _draft_manifest()))

    def test_approve_carries_new_fields(self):
        approved = approve_dataset_version(
            freeze_dataset_version(_draft_manifest()),
            approved_by="mgr", reason="검증 완료",
        )
        self.assertEqual(approved["labelPolicyVersion"], "LABEL-POLICY-V2")
        self.assertEqual(approved["snapshotChecksum"], "sha256:cwru-version-checksum")
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
    """이미 동결된 v1(snapshotDigest 없음)은 재동결하지 않고 관용 처리한다."""

    def test_is_legacy_v1_frozen(self):
        self.assertTrue(is_legacy_v1_frozen(_legacy_frozen()))
        self.assertFalse(is_legacy_v1_frozen(freeze_dataset_version(_draft_manifest())))

    def test_v1_frozen_approve_still_works(self):
        approved = approve_dataset_version(
            _legacy_frozen(), approved_by="mgr", reason="기존 승인"
        )
        self.assertEqual(approved["status"], "approved")

    def test_v1_frozen_approve_rejects_row_tamper(self):
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            approve_dataset_version(frozen, approved_by="mgr", reason="x")

    def test_v1_frozen_integrity_falls_back_to_dataset_checksum(self):
        verify_frozen_integrity(_legacy_frozen())  # no raise
        frozen = _legacy_frozen()
        frozen["rows"][0]["common_label"] = "TAMPERED"
        with self.assertRaises(ValueError):
            verify_frozen_integrity(frozen)

    def test_v1_frozen_summary_still_works(self):
        summary = dataset_version_summary(_legacy_frozen())
        self.assertNotIn("rows", summary)


class TestModelVersionLifecycle(unittest.TestCase):
    def test_register_starts_as_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
        )
        self.assertEqual(mv["status"], "registered")

    def test_approve_requires_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="지표 통과", metric_snapshot={"f1": 0.9}
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.9})
        with self.assertRaises(ValueError):
            approve_model_version(
                approved, approved_by="mgr", reason="재승인 시도",
                metric_snapshot={"f1": 0.9},
            )

    def test_metric_snapshot_is_isolated_from_source_metrics(self):
        metrics = {"f1": 0.91, "cm": {"tp": 10, "fp": 1}}
        mv = register_model_version(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics=metrics,
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="지표 통과", metric_snapshot=metrics
        )
        metrics["f1"] = 0.12
        metrics["cm"]["tp"] = 0
        mv["metrics"]["f1"] = 0.0
        self.assertEqual(approved["metricSnapshot"]["f1"], 0.91)
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 10)

    def test_explicit_metric_snapshot_is_deep_copied(self):
        mv = register_model_version(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics={"f1": 0.5},
        )
        snap = {"f1": 0.91, "cm": {"tp": 3}}
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok", metric_snapshot=snap
        )
        snap["cm"]["tp"] = 999
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 3)


class TestRegisterModelVersionValidation(unittest.TestCase):
    """[리뷰 P1] 불완전한 등록 정보가 승인 상태까지 조용히 전이되지 않도록 등록
    단계에서 필수 메타데이터를 검증한다."""

    _VALID_KWARGS = dict(
        version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
        baseline_version="b1", metrics={"f1": 0.9},
    )

    def test_blank_required_strings_rejected(self):
        for field in ("version", "artifact_uri", "dataset_id", "baseline_version"):
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


class TestApproveModelVersionRequiresAuditTrail(unittest.TestCase):
    """[리뷰 P1] 승인은 승인자와 명시적인 지표 스냅샷 없이는 이뤄질 수 없다."""

    def _registered(self):
        return register_model_version(
            version="v1", artifact_uri="file:///v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
        )

    def test_missing_approved_by_rejected(self):
        with self.assertRaises(TypeError):
            approve_model_version(
                self._registered(), reason="ok", metric_snapshot={"f1": 0.9}
            )

    def test_blank_approved_by_rejected(self):
        with self.assertRaises(ValueError):
            approve_model_version(
                self._registered(), approved_by="   ", reason="ok",
                metric_snapshot={"f1": 0.9},
            )

    def test_missing_metric_snapshot_rejected(self):
        with self.assertRaises(TypeError):
            approve_model_version(self._registered(), approved_by="mgr", reason="ok")

    def test_empty_metric_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            approve_model_version(
                self._registered(), approved_by="mgr", reason="ok", metric_snapshot={}
            )

    def test_non_dict_metric_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            approve_model_version(
                self._registered(), approved_by="mgr", reason="ok",
                metric_snapshot="not-a-dict",
            )

    def test_valid_approval_records_approver_and_snapshot(self):
        approved = approve_model_version(
            self._registered(), approved_by="mgr", reason="ok",
            metric_snapshot={"f1": 0.95},
        )
        self.assertEqual(approved["approvedBy"], "mgr")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.95})


class TestRollbackLineage(unittest.TestCase):
    """[리뷰 P1] 롤백 대상은 실제로 current보다 앞선 승인 버전이어야 한다.
    자기 자신·더 최신 버전으로의 '롤백'을 차단한다."""

    def _approved(self, version, when):
        mv = register_model_version(
            version=version, artifact_uri=f"file:///{version}.pt", dataset_id="d",
            baseline_version="b", metrics={"f1": 0.9},
        )
        approved = approve_model_version(
            mv, approved_by="mgr", reason="ok", metric_snapshot={"f1": 0.9}
        )
        approved["approvedAt"] = when  # 결정적 시각으로 고정
        return approved

    def test_rollback_to_earlier_approved_version_ok(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        action = rollback_model_version(
            v2, v1, reason="v2 회귀", target_environment="prod"
        )
        self.assertEqual(action["fromVersion"], "v2")
        self.assertEqual(action["toVersion"], "v1")
        self.assertEqual(action["targetApprovedAt"], "2026-08-01T00:00:00+00:00")

    def test_self_rollback_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(v1, v1, reason="x", target_environment="prod")

    def test_forward_rollback_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(v1, v2, reason="x", target_environment="prod")

    def test_non_approved_target_rejected(self):
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v1_reg = register_model_version(
            version="v1", artifact_uri="a1", dataset_id="d", baseline_version="b",
            metrics={},
        )
        with self.assertRaises(ValueError):  # target(v1)이 approved가 아님
            rollback_model_version(v2, v1_reg, reason="x", target_environment="prod")

    def test_rollback_without_approvedAt_and_no_history_rejected(self):
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        del v2["approvedAt"]  # current에 승인 시각이 없으면 계보 검증 불가
        with self.assertRaises(ValueError):
            rollback_model_version(v2, v1, reason="x", target_environment="prod")

    def test_approved_history_lineage_enforced(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        v3 = self._approved("v3", "2026-08-20T00:00:00+00:00")
        history = [v1, v2, v3]
        # v3 -> v1 (계보상 앞) OK
        rollback_model_version(
            v3, v1, reason="ok", target_environment="prod", approved_history=history
        )
        # v1 -> v3 (계보상 뒤) 거부
        with self.assertRaises(ValueError):
            rollback_model_version(
                v1, v3, reason="x", target_environment="prod", approved_history=history
            )
        # 계보에 없는 target 거부
        stray = self._approved("vX", "2026-07-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v3, stray, reason="x", target_environment="prod",
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
                v1, v3, reason="x", target_environment="prod",
                approved_history=reversed_history,
            )
        # v3 -> v1은 실제로 앞선 버전이므로 배열 순서와 무관하게 여전히 허용.
        rollback_model_version(
            v3, v1, reason="ok", target_environment="prod",
            approved_history=reversed_history,
        )

    def test_duplicate_versions_in_history_rejected(self):
        v1 = self._approved("v1", "2026-08-01T00:00:00+00:00")
        v2 = self._approved("v2", "2026-08-10T00:00:00+00:00")
        dup_v1 = self._approved("v1", "2026-08-15T00:00:00+00:00")
        with self.assertRaises(ValueError):
            rollback_model_version(
                v2, v1, reason="x", target_environment="prod",
                approved_history=[v1, dup_v1],
            )


class TestBaselineVersionLifecycle(unittest.TestCase):
    def test_activate_requires_approved(self):
        bv = register_baseline_version(
            baseline_id="BL-1", dataset_id="DS-1", site_id="SITE-01",
            asset_id="SITE-01-MOT-02", features={},
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
