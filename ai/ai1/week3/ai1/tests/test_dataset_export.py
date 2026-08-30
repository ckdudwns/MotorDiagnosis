"""
DATA_EXPORT_01 테스트 — CWRU 데이터로 매니페스트/CSV/XLSX 내보내기 검증 (AI-1, 3주차)

실제 CWRU 데이터(ai/ai1/week1/ai1/data/external/cwru/*.mat)가 있을 때만 실행되며,
없으면 unittest.skipUnless로 명시적으로 skip 처리한다 (week2 test_week2_pipeline.py와
동일한 패턴).
"""

import os
import sys
import csv
import io
import codecs
import contextlib
import json
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASETS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "datasets"))
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _DATASETS_DIR)

from register_dataset import (  # noqa: E402
    build_manifest,
    group_split,
    compute_version_checksum,
    compute_feature_output_fingerprint,
    validate_split_ratios,
    sha256_of_file,
    DATASET_LABEL_MAPPING,
    LABEL_TAXONOMY_VERSION,
    FEATURE_PIPELINE_VERSION,
    InsufficientAssetGroupsError,
)
from export_dataset import export_dataset  # noqa: E402
import extract_features as _extract_features_module  # noqa: E402
from extract_features import FeatureConfig, compute_mfcc  # noqa: E402


def _cwru_data_available() -> bool:
    return os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat"))


CWRU_SKIP_REASON = f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}"


def _grouped_records(label: str, group_sizes: dict) -> list:
    """label에 대해 {source_label: window_count} 형태로 합성 레코드를 만든다."""
    records = []
    for source_label, count in group_sizes.items():
        records.extend(
            {"label": label, "source_label": source_label} for _ in range(count)
        )
    return records


class TestGroupSplitSynthetic(unittest.TestCase):
    """합성 레코드로 그룹 분할 로직만 검증 — 실데이터 불필요."""

    def _groups_used_per_split(self, records, splits):
        """split(train/validation/test)별로 등장한 source_label 집합을 모은다."""
        by_split: dict = {"train": set(), "validation": set(), "test": set()}
        for rec, split in zip(records, splits):
            by_split[split].add(rec["source_label"])
        return by_split

    def test_no_group_appears_in_more_than_one_split(self):
        records = _grouped_records(
            "NORMAL", {"a.mat": 20, "b.mat": 15, "c.mat": 10, "d.mat": 5}
        ) + _grouped_records(
            "BEARING_FAULT_INNER", {"e.mat": 12, "f.mat": 9, "g.mat": 6}
        )
        splits = group_split(records, seed=1)

        self.assertEqual(len(splits), len(records))
        self.assertTrue(set(splits) <= {"train", "validation", "test"})

        by_split = self._groups_used_per_split(records, splits)
        all_groups_seen = by_split["train"] | by_split["validation"] | by_split["test"]
        # 각 그룹(source_label)은 정확히 하나의 split에서만 등장해야 한다 (누수 없음)
        for group in all_groups_seen:
            memberships = sum(group in s for s in by_split.values())
            self.assertEqual(
                memberships, 1, f"그룹 {group!r}이 두 개 이상의 split에 걸쳐 있습니다."
            )

    def test_every_required_split_gets_at_least_one_group_when_enough_groups_exist(self):
        records = _grouped_records(
            "NORMAL", {"a.mat": 20, "b.mat": 15, "c.mat": 10}
        )
        splits = group_split(records, seed=1)
        by_split = self._groups_used_per_split(records, splits)
        for split_name in ("train", "validation", "test"):
            self.assertGreater(
                len(by_split[split_name]), 0, f"{split_name} split에 그룹이 배정되지 않았습니다."
            )

    def test_split_is_deterministic_given_seed(self):
        records = _grouped_records("NORMAL", {"a.mat": 10, "b.mat": 6, "c.mat": 4})
        self.assertEqual(
            group_split(records, seed=7), group_split(records, seed=7)
        )

    def test_insufficient_groups_raises_instead_of_shuffling_windows(self):
        """CWRU처럼 라벨당 그룹(자산)이 1개뿐이면, 윈도우를 섞는 대신
        명시적으로 데이터 부족 오류를 내야 한다."""
        records = _grouped_records("NORMAL", {"only.mat": 50})
        with self.assertRaises(InsufficientAssetGroupsError):
            group_split(records, seed=1)

    def test_two_groups_is_still_insufficient_for_three_way_split(self):
        records = _grouped_records("NORMAL", {"a.mat": 50, "b.mat": 50})
        with self.assertRaises(InsufficientAssetGroupsError):
            group_split(records, seed=1)

    def test_train_only_ratio_succeeds_with_a_single_group(self):
        records = _grouped_records("NORMAL", {"only.mat": 50})
        splits = group_split(
            records, ratios={"train": 1.0, "validation": 0.0, "test": 0.0}, seed=1
        )
        self.assertEqual(set(splits), {"train"})


class TestComputeVersionChecksum(unittest.TestCase):
    def _files(self):
        return {"97.mat": {"sha256": "sha256:aaa", "label": "NORMAL"}}

    def _checksum(self, **overrides):
        kwargs = dict(
            source_files=self._files(),
            window_size=2048,
            hop_size=2048,
            split_ratios={"train": 1.0},
            seed=42,
            label_taxonomy_version=LABEL_TAXONOMY_VERSION,
            label_mapping=DATASET_LABEL_MAPPING,
            feature_config=FeatureConfig(sample_rate=12000),
            feature_output_fingerprint="sha256:fixed-fingerprint",
            feature_pipeline_version=FEATURE_PIPELINE_VERSION,
        )
        kwargs.update(overrides)
        return compute_version_checksum(**kwargs)

    def test_deterministic_given_same_inputs(self):
        self.assertEqual(self._checksum(), self._checksum())

    def test_changes_when_window_size_changes(self):
        self.assertNotEqual(self._checksum(), self._checksum(window_size=4096))

    def test_changes_when_split_ratios_change(self):
        changed = self._checksum(split_ratios={"train": 0.5, "test": 0.5})
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_label_taxonomy_version_changes(self):
        changed = self._checksum(label_taxonomy_version="CWRU-FAULT-V2")
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_label_mapping_changes(self):
        """DATASET_LABEL_MAPPING을 재정의(예: NORMAL -> ANOMALY)해 정규화 결과가
        바뀌면, 원본 파일/window/hop/분할/seed가 같아도 체크섬이 달라져야 한다 —
        그래야 같은 id가 서로 다른 학습 데이터를 가리키는 상황을 막을 수 있다."""
        relabeled = dict(DATASET_LABEL_MAPPING)
        relabeled["NORMAL"] = "ANOMALY"
        changed = self._checksum(label_mapping=relabeled)
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_source_label_changes_with_same_sha256(self):
        """같은 sha256이라도 파일에 배정된 source label(known_label)이 바뀌면
        정규화 산출물(known_label/common_label)이 달라지므로 체크섬도 달라져야
        한다 — 이전에는 payload에서 label이 빠져 sha256만 같으면 동일했다."""
        relabeled_files = {"97.mat": {"sha256": "sha256:aaa", "label": "BEARING_FAULT_INNER"}}
        changed = self._checksum(source_files=relabeled_files)
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_feature_config_changes(self):
        """frame_length/hop_length/n_mfcc/band_edges 중 하나라도 바뀌면 특징값
        산출물이 달라지므로 체크섬도 달라져야 한다."""
        changed = self._checksum(feature_config=FeatureConfig(sample_rate=12000, n_mfcc=20))
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_feature_pipeline_version_changes(self):
        changed = self._checksum(feature_pipeline_version="week2.extract_all_features.v2")
        self.assertNotEqual(self._checksum(), changed)

    def test_changes_when_feature_output_fingerprint_changes(self):
        """librosa 유무처럼 소스 코드/설정 어디에도 드러나지 않는 실행 환경
        차이(MFCC 0벡터 폴백 등)로 실제 산출된 특징값이 달라지면, 다른 입력이
        전부 같아도 fingerprint를 통해 체크섬이 달라져야 한다."""
        changed = self._checksum(feature_output_fingerprint="sha256:different-fingerprint")
        self.assertNotEqual(self._checksum(), changed)


class TestComputeFeatureOutputFingerprint(unittest.TestCase):
    def test_deterministic_given_same_rows(self):
        rows = [{"sample_id": "97_0000", "mfcc_1": -151.41, "rms_mean": 0.05}]
        self.assertEqual(
            compute_feature_output_fingerprint(rows),
            compute_feature_output_fingerprint(rows),
        )

    def test_changes_when_mfcc_values_differ_like_librosa_fallback(self):
        """librosa가 있으면 실제 MFCC 값이, 없으면 0벡터가 나온다(같은 원본/설정).
        두 실행의 rows가 이 지점에서만 달라지는 상황을 재현한다."""
        rows_with_librosa = [{"sample_id": "97_0000", "mfcc_1": -151.41}]
        rows_without_librosa = [{"sample_id": "97_0000", "mfcc_1": 0.0}]
        self.assertNotEqual(
            compute_feature_output_fingerprint(rows_with_librosa),
            compute_feature_output_fingerprint(rows_without_librosa),
        )

    def test_changes_when_row_order_differs(self):
        rows_a = [{"sample_id": "a"}, {"sample_id": "b"}]
        rows_b = [{"sample_id": "b"}, {"sample_id": "a"}]
        self.assertNotEqual(
            compute_feature_output_fingerprint(rows_a),
            compute_feature_output_fingerprint(rows_b),
        )


class TestMfccFallbackWarningIsEncodingSafe(unittest.TestCase):
    """안내된 설치 방법(루트 requirements.txt + week3 requirements.txt)만으로는
    librosa가 설치되지 않는다. 이 상태로 register_dataset.py를 실행하면
    extract_all_features() -> compute_mfcc()의 librosa 미설치 폴백 경고가
    출력되는데, 이 메시지의 em dash(—)는 Windows 기본 콘솔 코드페이지인
    cp949로 표현할 수 없어 print()가 UnicodeEncodeError를 내며 rows 생성과
    fingerprint 계산 전에 실행이 통째로 중단됐다. cp949로 강제 인코딩되는
    스트림에 실제로 출력해 이 크래시가 재발하지 않는지 확인한다."""

    def test_fallback_warning_does_not_crash_on_cp949_console(self):
        cp949_stream = codecs.getwriter("cp949")(io.BytesIO())
        with mock.patch.object(_extract_features_module, "_HAS_LIBROSA", False):
            with contextlib.redirect_stdout(cp949_stream):
                mfcc = compute_mfcc(np.zeros(4096), FeatureConfig())
        self.assertTrue(np.all(mfcc == 0.0))


class TestValidateSplitRatios(unittest.TestCase):
    def test_default_ratios_are_valid(self):
        validate_split_ratios({"train": 0.7, "validation": 0.2, "test": 0.1})

    def test_missing_key_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 0.8, "validation": 0.2})

    def test_extra_key_rejected(self):
        """holdout처럼 실제로 쓰이지 않는 추가 키가 섞여 있으면, group_split
        결과는 정상 3-way 구성과 동일한데도 checksum payload에 그 키가 포함돼
        다른 dataset id가 생기므로 미리 거부해야 한다."""
        with self.assertRaises(ValueError):
            validate_split_ratios(
                {"train": 0.7, "validation": 0.2, "test": 0.1, "holdout": 0.0}
            )

    def test_empty_dict_rejected_not_replaced_with_default(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({})

    def test_sum_greater_than_one_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 0.8, "validation": 0.3, "test": 0.1})

    def test_sum_less_than_one_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 0.5, "validation": 0.2, "test": 0.1})

    def test_all_zero_rejected_instead_of_raising_max_iterable_empty(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 0.0, "validation": 0.0, "test": 0.0})

    def test_negative_ratio_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 1.2, "validation": -0.1, "test": -0.1})

    def test_ratio_above_one_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": 1.5, "validation": -0.5, "test": 0.0})

    def test_non_numeric_ratio_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": "0.7", "validation": 0.2, "test": 0.1})

    def test_bool_ratio_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": True, "validation": 0.0, "test": 0.0})

    def test_nan_ratio_rejected(self):
        with self.assertRaises(ValueError):
            validate_split_ratios({"train": float("nan"), "validation": 0.2, "test": 0.1})

    def test_group_split_rejects_invalid_ratios_before_max_call(self):
        """전부 0인 비율은 InsufficientAssetGroupsError(그룹 부족)가 아니라,
        group_split 내부의 max() 빈 시퀀스 오류보다 먼저 명확한 입력 검증
        오류로 걸러져야 한다."""
        records = _grouped_records("NORMAL", {"a.mat": 10, "b.mat": 10})
        with self.assertRaises(ValueError) as ctx:
            group_split(
                records, ratios={"train": 0.0, "validation": 0.0, "test": 0.0}, seed=1
            )
        self.assertNotIsInstance(ctx.exception, InsufficientAssetGroupsError)

    def test_build_manifest_rejects_invalid_ratios_before_loading_data(self):
        """CWRU 실데이터 유무와 무관하게, build_manifest는 데이터 로딩보다 먼저
        split_ratios를 검증해야 한다 (data_dir이 없어도 ValueError가 나야 함)."""
        with self.assertRaises(ValueError):
            build_manifest(
                data_dir=os.path.join(_THIS_DIR, "__no_such_dir__"),
                split_ratios={"train": 0.8, "validation": 0.3, "test": 0.1},
            )

    def test_group_split_empty_dict_rejected_not_replaced_with_default(self):
        """split_ratios={}는 `ratios or DEFAULT`의 truthiness 때문에 기본값으로
        치환되면 안 된다 — 명시적으로 빈 dict를 넘겼다면 필수 키 누락으로
        거부해야지, 조용히 기본 3-way 분할을 실행하면 안 된다."""
        records = _grouped_records("NORMAL", {"a.mat": 10, "b.mat": 10})
        with self.assertRaises(ValueError):
            group_split(records, ratios={}, seed=1)

    def test_build_manifest_empty_dict_rejected_not_replaced_with_default(self):
        with self.assertRaises(ValueError):
            build_manifest(
                data_dir=os.path.join(_THIS_DIR, "__no_such_dir__"), split_ratios={}
            )


class TestExportDatasetSynthetic(unittest.TestCase):
    """CWRU 실데이터 없이도 openpyxl XLSX 내보내기 자체를 검증하는 합성 매니페스트 테스트."""

    def _synthetic_manifest(self):
        rows = [
            {
                "sample_id": "97_0000",
                "source_file": "97.mat",
                "known_label": "NORMAL",
                "common_label": "NORMAL",
                "split": "train",
                "sample_rate_hz": 12000,
                "rpm": 1797,
                "rms_mean": 0.05,
            },
            {
                "sample_id": "105_0000",
                "source_file": "105.mat",
                "known_label": "BEARING_FAULT_INNER",
                "common_label": "ANOMALY",
                "split": "test",
                "sample_rate_hz": 12000,
                "rpm": 1797,
                "rms_mean": 0.42,
            },
        ]
        return {
            "id": "DS-CWRU-VIBRATION-19700101-deadbeefcafe",
            "name": "cwru-bearing-vibration-v1",
            "source": {
                "type": "external",
                "uri": "https://example.invalid/cwru",
                "license": "test",
                "checksum": "sha256:deadbeef",
                "files": {"97.mat": {"sha256": "sha256:aaa", "label": "NORMAL"}},
            },
            "compatibility": {
                "signalType": ["vibration"],
                "samplingRateHz": 12000,
                "units": {"vibration": "g"},
                "operatingConditions": {"rpmRange": [1797, 1797], "load": "test"},
            },
            "labelTaxonomyVersion": "CWRU-FAULT-V1",
            "labelMapping": DATASET_LABEL_MAPPING,
            "split": {"train": 0.5, "validation": 0.0, "test": 0.5},
            "splitStrategy": "test",
            "status": "draft",
            "reason": "synthetic test",
            "createdAt": "1970-01-01T00:00:00+00:00",
            "rowCount": len(rows),
            "splitCounts": {"train": 1, "validation": 0, "test": 1},
            "rows": rows,
        }

    def test_export_produces_xlsx_with_manifest_and_rows_sheets(self):
        from openpyxl import load_workbook

        manifest = self._synthetic_manifest()
        tmp_dir = tempfile.mkdtemp(prefix="ai1_week3_dataset_export_synthetic_")
        try:
            result = export_dataset(manifest, tmp_dir)
            self.assertTrue(os.path.exists(result["xlsx_path"]))

            wb = load_workbook(result["xlsx_path"])
            self.assertIn("manifest", wb.sheetnames)
            self.assertIn("rows", wb.sheetnames)
            self.assertEqual(wb["rows"].max_row - 1, manifest["rowCount"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@unittest.skipUnless(_cwru_data_available(), CWRU_SKIP_REASON)
class TestRegisterManifestRejectsLeakyDefaultSplit(unittest.TestCase):
    """실제 CWRU 데이터는 라벨당 자산(원본 파일)이 1개뿐이라, 기본 3-way 비율로는
    그룹을 쪼개지 않고 리크 없는 분할을 만들 수 없다 — build_manifest가 이를
    조용히 window 셔플로 얼버무리지 않고 명시적으로 실패하는지 확인한다."""

    def test_default_ratios_raise_insufficient_asset_groups(self):
        from register_dataset import InsufficientAssetGroupsError

        with self.assertRaises(InsufficientAssetGroupsError):
            build_manifest(data_dir=_CWRU_DATA_DIR, seed=42)


@unittest.skipUnless(_cwru_data_available(), CWRU_SKIP_REASON)
class TestRegisterAndExportRealCwruData(unittest.TestCase):
    """실제 CWRU 데이터로 매니페스트 생성 → CSV/XLSX 내보내기까지 전체 흐름을 검증.

    라벨당 자산이 1개뿐이라 validation/test로 쪼갤 독립 그룹이 없으므로,
    여기서는 train 전용 비율로 파이프라인 자체(체크섬/라벨매핑/내보내기)를 검증한다.
    실제 자산이 여러 개 확보되면 기본 3-way 비율로 전환한다.
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            split_ratios={"train": 1.0, "validation": 0.0, "test": 0.0},
            seed=42,
        )
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

    def test_id_and_source_checksum_change_with_split_config(self):
        other = build_manifest(
            data_dir=_CWRU_DATA_DIR,
            split_ratios={"train": 1.0, "validation": 0.0, "test": 0.0},
            seed=1,  # 다른 seed -> 다른 버전 체크섬/ID여야 함 (같은 날짜라도 충돌 없음)
        )
        self.assertIn("checksum", self.manifest["source"])
        self.assertNotEqual(self.manifest["source"]["checksum"], other["source"]["checksum"])
        self.assertNotEqual(self.manifest["id"], other["id"])
        self.assertIn(self.manifest["source"]["checksum"].split(":", 1)[1][:12], self.manifest["id"])

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
