"""
DATA_EXPORT_01 테스트 — CWRU 데이터로 매니페스트/CSV/XLSX 내보내기 검증 (AI-1, 3주차)

실제 CWRU 데이터(ai/ai1/week1/ai1/data/external/cwru/*.mat)가 있을 때만 실행되며,
없으면 unittest.skipUnless로 명시적으로 skip 처리한다 (week2 test_week2_pipeline.py와
동일한 패턴).
"""

import os
import sys
import csv
import json
import shutil
import tempfile
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASETS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "datasets"))
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _DATASETS_DIR)

from register_dataset import (  # noqa: E402
    build_manifest,
    stratified_split,
    sha256_of_file,
    DATASET_LABEL_MAPPING,
)
from export_dataset import export_dataset  # noqa: E402


def _cwru_data_available() -> bool:
    return os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat"))


CWRU_SKIP_REASON = f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}"


class TestStratifiedSplitSynthetic(unittest.TestCase):
    """합성 레코드로 분할 로직만 검증 — 실데이터 불필요."""

    def test_split_counts_match_group_size_per_label(self):
        records = [{"label": "NORMAL"} for _ in range(10)] + [
            {"label": "BEARING_FAULT_INNER"} for _ in range(7)
        ]
        splits = stratified_split(records, seed=1)

        self.assertEqual(len(splits), len(records))

        normal_splits = splits[:10]
        fault_splits = splits[10:]
        self.assertEqual(len(normal_splits), 10)
        self.assertEqual(len(fault_splits), 7)
        for group in (normal_splits, fault_splits):
            self.assertTrue(set(group) <= {"train", "validation", "test"})

    def test_split_is_deterministic_given_seed(self):
        records = [{"label": "NORMAL"} for _ in range(20)]
        self.assertEqual(
            stratified_split(records, seed=7), stratified_split(records, seed=7)
        )


@unittest.skipUnless(_cwru_data_available(), CWRU_SKIP_REASON)
class TestRegisterAndExportRealCwruData(unittest.TestCase):
    """실제 CWRU 데이터로 매니페스트 생성 → CSV/XLSX 내보내기까지 전체 흐름을 검증."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)
        cls.tmp_dir = tempfile.mkdtemp(prefix="ai1_week3_dataset_export_")
        cls.export_result = export_dataset(cls.manifest, cls.tmp_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def test_row_count_matches_manifest(self):
        self.assertEqual(self.manifest["rowCount"], len(self.manifest["rows"]))
        self.assertGreater(self.manifest["rowCount"], 0)

    def test_split_counts_sum_to_row_count(self):
        total = sum(self.manifest["splitCounts"].values())
        self.assertEqual(total, self.manifest["rowCount"])

    def test_checksums_match_recomputed_values(self):
        for filename, info in self.manifest["source"]["files"].items():
            recomputed = sha256_of_file(os.path.join(_CWRU_DATA_DIR, filename))
            self.assertEqual(info["sha256"], recomputed)

    def test_label_mapping_applied_to_every_row(self):
        for row in self.manifest["rows"]:
            expected = DATASET_LABEL_MAPPING[row["known_label"]]
            self.assertEqual(row["common_label"], expected)

    def test_exported_csv_row_count_matches_manifest(self):
        with open(self.export_result["csv_path"], newline="", encoding="utf-8") as f:
            csv_rows = list(csv.DictReader(f))
        self.assertEqual(len(csv_rows), self.manifest["rowCount"])

    def test_exported_manifest_json_excludes_rows_and_has_artifact_refs(self):
        with open(self.export_result["manifest_path"], encoding="utf-8") as f:
            exported = json.load(f)
        self.assertNotIn("rows", exported)
        self.assertIn("artifactRefs", exported)
        self.assertTrue(os.path.exists(exported["artifactRefs"]["csv"]))
        self.assertTrue(os.path.exists(exported["artifactRefs"]["xlsx"]))

    def test_xlsx_has_manifest_and_rows_sheets(self):
        from openpyxl import load_workbook

        wb = load_workbook(self.export_result["xlsx_path"])
        self.assertIn("manifest", wb.sheetnames)
        self.assertIn("rows", wb.sheetnames)
        rows_sheet = wb["rows"]
        # 헤더 1행 + 데이터 행 수
        self.assertEqual(rows_sheet.max_row - 1, self.manifest["rowCount"])


if __name__ == "__main__":
    unittest.main()
