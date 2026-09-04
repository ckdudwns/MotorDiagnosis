"""
CWRU 로더 테스트 — 키 선택/RPM 폴백/파일 매핑 검증 (AI-1, 1주차)

원본 특이사항(99.mat의 X098/X099 중복, 98/99.mat RPM 키 없음)을 합성 mat dict로
항상 검증하고, 실제 16개 .mat이 있으면 로드·라벨·RPM·그룹 구성까지 확인한다.
"""

import os
import sys
import unittest

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "scripts"))
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "data", "external", "cwru")
)

sys.path.insert(0, _SCRIPTS_DIR)

import load_cwru_vibration as loader  # noqa: E402
from load_cwru_vibration import (  # noqa: E402
    FILE_LABEL_MAP,
    LOAD_RPM_BY_FILE,
    SPECIMEN_BY_FILE,
    LOAD_HP_BY_FILE,
    _base_id_from_path,
    _find_de_time_key,
    load_mat_signal,
    load_mat_signal_and_rpm,
    load_cwru_dataset,
)


class TestFindDeTimeKey(unittest.TestCase):
    def test_prefers_key_matching_file_number(self):
        mat = {"X098_DE_time": [1], "X099_DE_time": [2], "X099_FE_time": [3]}
        self.assertEqual(_find_de_time_key(mat, "99"), "X099_DE_time")
        self.assertEqual(_find_de_time_key(mat, "98"), "X098_DE_time")

    def test_falls_back_to_first_de_time_when_no_prefix_match(self):
        mat = {"X097_DE_time": [1], "X097_FE_time": [2]}
        self.assertEqual(_find_de_time_key(mat, "999"), "X097_DE_time")

    def test_falls_back_to_fe_time_when_no_de_channel(self):
        mat = {"X097_FE_time": [1]}
        self.assertEqual(_find_de_time_key(mat, "97"), "X097_FE_time")

    def test_raises_when_no_signal_key(self):
        with self.assertRaises(KeyError):
            _find_de_time_key({"ans": [1]}, "97")

    def test_base_id_from_path(self):
        self.assertEqual(_base_id_from_path("/x/y/99.mat"), "99")


class TestRpmFallback(unittest.TestCase):
    def setUp(self):
        self._real_loadmat = loader.sio.loadmat if loader.sio else None

    def tearDown(self):
        if loader.sio:
            loader.sio.loadmat = self._real_loadmat

    def _patch_loadmat(self, mat_dict):
        loader.sio.loadmat = lambda path: dict(mat_dict)

    @unittest.skipIf(loader.sio is None, "scipy 미설치")
    def test_missing_rpm_key_filled_from_load_condition(self):
        self._patch_loadmat({"X098_DE_time": np.zeros(4096)})
        _, rpm = load_mat_signal_and_rpm(os.path.join(_CWRU_DATA_DIR, "98.mat"))
        self.assertEqual(rpm, 1772.0)

        self._patch_loadmat(
            {"X098_DE_time": np.zeros(4096), "X099_DE_time": np.ones(4096)}
        )
        _, rpm = load_mat_signal_and_rpm(os.path.join(_CWRU_DATA_DIR, "99.mat"))
        self.assertEqual(rpm, 1750.0)

    @unittest.skipIf(loader.sio is None, "scipy 미설치")
    def test_real_rpm_key_wins_over_table(self):
        self._patch_loadmat({"X097_DE_time": np.zeros(4096), "X097RPM": [[1796]]})
        _, rpm = load_mat_signal_and_rpm(os.path.join(_CWRU_DATA_DIR, "97.mat"))
        self.assertEqual(rpm, 1796.0)

    @unittest.skipIf(loader.sio is None, "scipy 미설치")
    def test_99_picks_x099_not_duplicate_x098(self):
        self._patch_loadmat(
            {
                "X098_DE_time": np.zeros(4096),
                "X099_DE_time": np.arange(4096, dtype=float),
            }
        )
        signal = load_mat_signal(os.path.join(_CWRU_DATA_DIR, "99.mat"))
        np.testing.assert_array_equal(signal, np.arange(4096, dtype=float))


class TestFileLabelMap(unittest.TestCase):
    def test_40_entries_10_physical_specimens(self):
        self.assertEqual(len(FILE_LABEL_MAP), 40)
        self.assertEqual(len(SPECIMEN_BY_FILE), 40)
        # 10 물리 specimen: NORMAL 1개(건강 베어링) + IR/Ball/OR 각 3개(0.007/0.014/0.021").
        by_label_specimens = {}
        for filename, label in FILE_LABEL_MAP.items():
            by_label_specimens.setdefault(label, set()).add(SPECIMEN_BY_FILE[filename])
        self.assertEqual(len(by_label_specimens["NORMAL"]), 1)
        for fault in ("BEARING_FAULT_INNER", "BEARING_FAULT_BALL", "BEARING_FAULT_OUTER"):
            self.assertEqual(len(by_label_specimens[fault]), 3, fault)
        # specimen 하나당 정확히 4개 파일(0/1/2/3 HP).
        files_per_specimen = {}
        for filename, specimen in SPECIMEN_BY_FILE.items():
            files_per_specimen.setdefault(specimen, []).append(filename)
        self.assertTrue(all(len(v) == 4 for v in files_per_specimen.values()))

    def test_load_hp_covers_0_to_3_per_specimen(self):
        hp_per_specimen = {}
        for filename, specimen in SPECIMEN_BY_FILE.items():
            hp_per_specimen.setdefault(specimen, set()).add(LOAD_HP_BY_FILE[filename])
        for specimen, hps in hp_per_specimen.items():
            self.assertEqual(hps, {0, 1, 2, 3}, specimen)

    def test_every_file_has_a_load_rpm_fallback(self):
        for filename in FILE_LABEL_MAP:
            self.assertIn(filename.replace(".mat", ""), LOAD_RPM_BY_FILE)


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {_CWRU_DATA_DIR}",
)
class TestLoadRealCwruData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_cwru_dataset(_CWRU_DATA_DIR)

    def test_specimens_per_label_normal_one_faults_three(self):
        by_label = {}
        for rec in self.records:
            by_label.setdefault(rec["label"], set()).add(rec["specimen_id"])
        self.assertEqual(by_label["NORMAL"], {"CWRU-NORMAL-BASELINE"})
        for fault in ("BEARING_FAULT_INNER", "BEARING_FAULT_BALL", "BEARING_FAULT_OUTER"):
            self.assertEqual(len(by_label[fault]), 3, f"{fault}: {sorted(by_label[fault])}")

    def test_records_carry_specimen_id_and_load_hp(self):
        for rec in self.records:
            self.assertIn(rec["specimen_id"], SPECIMEN_BY_FILE.values())
            self.assertIn(rec["load_hp"], (0, 1, 2, 3))
            self.assertEqual(rec["specimen_id"], SPECIMEN_BY_FILE[rec["source_label"]])

    def test_every_record_has_a_plausible_rpm(self):
        for rec in self.records:
            self.assertIsNotNone(rec["rpm"], rec["source_label"])
            self.assertTrue(1680 <= rec["rpm"] <= 1810, (rec["source_label"], rec["rpm"]))
        # 98/99.mat은 RPM 키가 없어 부하조건 폴백값(1772/1750)을 쓴다.
        filled = {r["rpm"] for r in self.records if r["source_label"] in ("98.mat", "99.mat")}
        self.assertEqual(filled, {1772.0, 1750.0})

    def test_98_and_99_are_distinct_signals(self):
        s98 = load_mat_signal(os.path.join(_CWRU_DATA_DIR, "98.mat"))
        s99 = load_mat_signal(os.path.join(_CWRU_DATA_DIR, "99.mat"))
        self.assertFalse(np.array_equal(s98[: min(len(s98), len(s99))], s99[: min(len(s98), len(s99))]))


if __name__ == "__main__":
    unittest.main()
