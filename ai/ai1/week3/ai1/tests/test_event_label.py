"""
EVENT_LABEL_01 테스트 — 라벨 변경 이력 + 기존 데이터셋 라벨 매핑 검증 (AI-1, 3주차)

라벨 검증/이력 로직은 합성 이벤트로 항상 실행되고, 데이터셋 라벨 매핑은 가능하면
DATA_EXPORT_01(register_dataset.py)의 실제 CWRU 매니페스트 결과로 검증한다
(CWRU 데이터가 없으면 그 부분만 skip).
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_EVENT_LABELS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "event_labels"))
_DATASETS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "datasets"))
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _EVENT_LABELS_DIR)
sys.path.insert(0, _DATASETS_DIR)

from event_label import (  # noqa: E402
    apply_label_change,
    seed_label_from_dataset,
    EVENT_REVIEW_LABELS,
    MAX_NOTE_LENGTH,
)


def _sample_event() -> dict:
    return {
        "id": "EV-241",
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "severity": "critical",
        "eventType": "asset_anomaly_candidate",
        "title": "Cooling pump motor bearing suspect",
        "time": "19:42:12",
        "duration": "48s",
        "score": 92,
        "label": "needs_review",
        "note": None,
    }


class TestApplyLabelChange(unittest.TestCase):
    def test_valid_change_updates_current_fields_and_builds_history(self):
        event = _sample_event()
        updated, history = apply_label_change(
            event,
            new_label="confirmed_anomaly",
            changed_by="operator-01",
            reason="베어링 결함 패턴과 일치",
            new_note="정비팀에 통보",
        )

        self.assertEqual(updated["label"], "confirmed_anomaly")
        self.assertEqual(updated["note"], "정비팀에 통보")
        self.assertIn("reviewedAt", updated)

        self.assertEqual(history["event_id"], "EV-241")
        self.assertEqual(history["previous_label"], "needs_review")
        self.assertEqual(history["new_label"], "confirmed_anomaly")
        self.assertIsNone(history["previous_note"])
        self.assertEqual(history["new_note"], "정비팀에 통보")
        self.assertEqual(history["reason"], "베어링 결함 패턴과 일치")

    def test_original_immutable_fields_untouched(self):
        event = _sample_event()
        updated, _ = apply_label_change(
            event,
            new_label="repair_completed",
            changed_by="operator-02",
            reason="정비 완료 처리",
        )
        for field in (
            "id",
            "siteId",
            "assetId",
            "severity",
            "eventType",
            "title",
            "time",
            "duration",
            "score",
        ):
            self.assertEqual(updated[field], event[field])
        # 원본 event dict 자체는 변경되지 않아야 한다 (복사본을 갱신)
        self.assertEqual(event["label"], "needs_review")

    def test_note_omitted_keeps_previous_note(self):
        event = _sample_event()
        event["note"] = "기존 메모"
        updated, history = apply_label_change(
            event, new_label="needs_review", changed_by="op", reason="재검토 필요"
        )
        self.assertEqual(updated["note"], "기존 메모")
        self.assertEqual(history["new_note"], "기존 메모")

    def test_repeated_changes_accumulate_history_with_correct_previous_label(self):
        event = _sample_event()
        updated1, h1 = apply_label_change(
            event, new_label="confirmed_anomaly", changed_by="op1", reason="1차 확인"
        )
        updated2, h2 = apply_label_change(
            updated1, new_label="repair_completed", changed_by="op2", reason="정비 완료"
        )
        self.assertEqual(h1["previous_label"], "needs_review")
        self.assertEqual(h1["new_label"], "confirmed_anomaly")
        self.assertEqual(h2["previous_label"], "confirmed_anomaly")
        self.assertEqual(h2["new_label"], "repair_completed")

    def test_invalid_label_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(),
                new_label="not_a_real_label",
                changed_by="op",
                reason="아무 이유",
            )

    def test_blank_changed_by_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(),
                new_label="confirmed_anomaly",
                changed_by="   ",
                reason="사유",
            )

    def test_empty_changed_by_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(), new_label="confirmed_anomaly", changed_by="", reason="사유"
            )

    def test_non_string_changed_by_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(), new_label="confirmed_anomaly", changed_by=None, reason="사유"
            )

    def test_blank_reason_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(), new_label="confirmed_anomaly", changed_by="op", reason="   "
            )

    def test_numeric_reason_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(), new_label="confirmed_anomaly", changed_by="op", reason=123
            )

    def test_list_reason_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(),
                new_label="confirmed_anomaly",
                changed_by="op",
                reason=["사유"],
            )

    def test_none_reason_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(), new_label="confirmed_anomaly", changed_by="op", reason=None
            )

    def test_note_too_long_rejected(self):
        with self.assertRaises(ValueError):
            apply_label_change(
                _sample_event(),
                new_label="confirmed_anomaly",
                changed_by="op",
                reason="사유",
                new_note="x" * (MAX_NOTE_LENGTH + 1),
            )

    def test_all_review_labels_are_valid_targets(self):
        for label in EVENT_REVIEW_LABELS:
            event = _sample_event()
            updated, _ = apply_label_change(
                event, new_label=label, changed_by="op", reason="전체 라벨 순회 테스트"
            )
            self.assertEqual(updated["label"], label)


class TestSeedLabelFromDataset(unittest.TestCase):
    def test_normal_maps_to_false_positive(self):
        self.assertEqual(seed_label_from_dataset("NORMAL"), "normal_false_positive")

    def test_anomaly_maps_to_confirmed_anomaly(self):
        self.assertEqual(seed_label_from_dataset("ANOMALY"), "confirmed_anomaly")

    def test_unknown_common_label_rejected(self):
        with self.assertRaises(ValueError):
            seed_label_from_dataset("UNKNOWN")

    def test_seeded_label_is_always_a_valid_review_label(self):
        for common_label in ("NORMAL", "ANOMALY"):
            self.assertIn(seed_label_from_dataset(common_label), EVENT_REVIEW_LABELS)


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestSeedLabelWithRealDatasetManifest(unittest.TestCase):
    """DATA_EXPORT_01이 만든 실제 CWRU 매니페스트의 common_label로 전 행을 시딩해 본다."""

    @classmethod
    def setUpClass(cls):
        from register_dataset import build_manifest

        # CWRU는 라벨당 자산이 1개뿐이라 기본 3-way 비율은 InsufficientAssetGroupsError를
        # 낸다 (의도된 동작) — 여기서는 라벨 매핑 시딩만 검증하면 되므로 train 전용으로 생성한다.
        cls.manifest = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            split_ratios={"train": 1.0, "validation": 0.0, "test": 0.0},
            seed=42,
        )

    def test_every_row_seeds_to_a_valid_event_label(self):
        seeded = {seed_label_from_dataset(row["common_label"]) for row in self.manifest["rows"]}
        self.assertTrue(seeded <= EVENT_REVIEW_LABELS)
        # NORMAL(97.mat)과 ANOMALY(105/118/130.mat)가 모두 있으므로 두 종류 모두 나와야 한다
        self.assertEqual(seeded, {"normal_false_positive", "confirmed_anomaly"})


if __name__ == "__main__":
    unittest.main()
