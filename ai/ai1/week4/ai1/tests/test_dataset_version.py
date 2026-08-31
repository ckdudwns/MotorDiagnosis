"""
DATASET_MODEL_01 테스트 — 데이터셋/모델/기준선 버전 상태 머신 검증 (AI-1, 4주차)

상태 전이 규칙은 합성 매니페스트로 항상 검증하고, "동일 데이터셋 버전을 재현할 수
있다"(수용 기준)는 가능하면 실제 CWRU 데이터로 week3 build_manifest()를 두 번
실행해 체크섬이 같은지 확인한다.
"""

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
    compute_dataset_checksum,
    dataset_version_summary,
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
    return {
        "id": "DS-TEST-001",
        "status": "draft",
        "labelMapping": {"NORMAL": "NORMAL", "FAULT": "ANOMALY"},
        "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
        "rows": [
            {"sample_id": "A", "common_label": "NORMAL"},
            {"sample_id": "B", "common_label": "ANOMALY"},
        ],
    }


class TestDatasetVersionStateMachine(unittest.TestCase):
    def test_freeze_sets_status_and_checksum(self):
        frozen = freeze_dataset_version(_draft_manifest())
        self.assertEqual(frozen["status"], "frozen")
        self.assertIn("datasetChecksum", frozen)
        self.assertIn("frozenAt", frozen)

    def test_freeze_does_not_mutate_original(self):
        draft = _draft_manifest()
        freeze_dataset_version(draft)
        self.assertEqual(draft["status"], "draft")

    def test_freeze_deep_copies_nested_rows(self):
        draft = _draft_manifest()
        frozen = freeze_dataset_version(draft)
        # 동결 후 원본 rows(중첩)를 바꿔도 frozen은 그대로여야 하고,
        # frozen 내용과 저장된 체크섬은 계속 일치해야 한다.
        draft["rows"][0]["common_label"] = "TAMPERED"
        self.assertEqual(frozen["rows"][0]["common_label"], "NORMAL")
        self.assertEqual(
            frozen["datasetChecksum"], compute_dataset_checksum(frozen)
        )

    def test_approve_rejects_post_freeze_mutation(self):
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
        manifest_b = _draft_manifest()  # 내용은 동일, 다른 dict 인스턴스
        frozen = freeze_dataset_version(manifest_a)
        self.assertTrue(verify_reproducibility(frozen, manifest_b))

    def test_verify_reproducibility_false_when_content_differs(self):
        frozen = freeze_dataset_version(_draft_manifest())
        different = _draft_manifest()
        different["split"] = {"train": 0.5, "validation": 0.3, "test": 0.2}
        self.assertFalse(verify_reproducibility(frozen, different))

    def test_summary_excludes_rows(self):
        frozen = freeze_dataset_version(_draft_manifest())
        summary = dataset_version_summary(frozen)
        self.assertNotIn("rows", summary)
        self.assertEqual(summary["status"], "frozen")


class TestModelVersionLifecycle(unittest.TestCase):
    def test_register_starts_as_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file://v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
        )
        self.assertEqual(mv["status"], "registered")

    def test_approve_requires_registered(self):
        mv = register_model_version(
            version="v1", artifact_uri="file://v1.pt", dataset_id="DS-1",
            baseline_version="b1", metrics={"f1": 0.9},
        )
        approved = approve_model_version(mv, reason="지표 통과")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["metricSnapshot"], {"f1": 0.9})
        with self.assertRaises(ValueError):
            approve_model_version(approved, reason="재승인 시도")

    def test_rollback_requires_approved_target(self):
        v1 = approve_model_version(
            register_model_version(
                version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
                metrics={},
            ),
            reason="초기 승인",
        )
        v2_registered = register_model_version(
            version="v2", artifact_uri="a2", dataset_id="d", baseline_version="b",
            metrics={},
        )
        with self.assertRaises(ValueError):
            # v2가 아직 registered일 뿐 approved가 아니므로 롤백 대상이 될 수 없음
            rollback_model_version(v2_registered, v2_registered, reason="문제 발생", target_environment="prod")

        action = rollback_model_version(v2_registered, v1, reason="v2 회귀 발생", target_environment="prod")
        self.assertEqual(action["action"], "rollback")
        self.assertEqual(action["toVersion"], "v1")
        self.assertEqual(action["fromVersion"], "v2")

    def test_metric_snapshot_is_isolated_from_source_metrics(self):
        metrics = {"f1": 0.91, "cm": {"tp": 10, "fp": 1}}
        mv = register_model_version(
            version="v1", artifact_uri="a", dataset_id="d", baseline_version="b",
            metrics=metrics,
        )
        approved = approve_model_version(mv, reason="지표 통과")
        # 승인 후 원본/등록본 metrics를 바꿔도 승인 스냅샷은 그대로여야 한다.
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
        approved = approve_model_version(mv, reason="ok", metric_snapshot=snap)
        snap["cm"]["tp"] = 999
        self.assertEqual(approved["metricSnapshot"]["cm"]["tp"], 3)


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
        # draft(원본/등록본) features를 수정해도 승인·active 기준선은 그대로여야 한다.
        features["rms_mean"]["mean"] = 99.0
        bv["features"]["rms_mean"]["std"] = 99.0
        self.assertEqual(approved["features"]["rms_mean"]["mean"], 0.05)
        self.assertEqual(active["features"]["rms_mean"]["std"], 0.01)


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestReproducibilityWithRealCwruData(unittest.TestCase):
    """같은 seed로 register_dataset.build_manifest()를 두 번 실행해 동결 시점
    체크섬이 재현되는지 확인한다 (DATASET_MODEL_01 수용 기준)."""

    def test_same_seed_reproduces_checksum(self):
        from register_dataset import build_manifest

        manifest_1 = build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)
        frozen = freeze_dataset_version(manifest_1)

        manifest_2 = build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)
        self.assertTrue(verify_reproducibility(frozen, manifest_2))

    def test_different_split_config_changes_checksum(self):
        from register_dataset import build_manifest

        # CWRU 16파일은 크기가 전부 달라 group_split이 seed에 의존하지 않는다.
        # 대신 분할 비율을 바꾸면(체크섬 payload에 포함) 다른 버전이어야 한다.
        manifest_1 = build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)
        frozen = freeze_dataset_version(manifest_1)

        manifest_other = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            seed=42,
            split_ratios={"train": 0.5, "validation": 0.3, "test": 0.2},
        )
        self.assertFalse(verify_reproducibility(frozen, manifest_other))


if __name__ == "__main__":
    unittest.main()
