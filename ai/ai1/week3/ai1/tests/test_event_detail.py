"""
EVENT_DETAIL_01 테스트 — 이벤트 전후 특징량 비교 데이터 구조 검증 (AI-1, 3주차)

합성(fixture) 신호로 핵심 로직(before/after 슬라이싱, 누락 표시, delta 계산)을
항상 검증하고, 실제 CWRU 데이터(97.mat NORMAL -> 105.mat 결함 전환 구간)로도
"전후 특징량이 실제로 갈리는지"를 확인한다.
"""

import os
import sys
import math
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_EVENT_DETAILS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "event_details"))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)

sys.path.insert(0, _EVENT_DETAILS_DIR)
sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from event_detail import build_event_detail, summarize_window_features  # noqa: E402


def _sample_event() -> dict:
    return {"id": "EV-241", "assetId": "SITE-01-MOT-02"}


def _fixture_windows(values: list) -> list:
    """values: 각 윈도우의 kurtosis_mean 값 리스트 -> ordered_windows 형식으로 변환."""
    return [
        {"sample_id": f"W{i:03d}", "features": {"kurtosis_mean": v}}
        for i, v in enumerate(values)
    ]


class TestSummarizeWindowFeatures(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(summarize_window_features([]), {})

    def test_basic_stats(self):
        summary = summarize_window_features(
            [{"rms_mean": 1.0}, {"rms_mean": 2.0}, {"rms_mean": 3.0}]
        )
        self.assertAlmostEqual(summary["rms_mean"]["mean"], 2.0)
        self.assertEqual(summary["rms_mean"]["min"], 1.0)
        self.assertEqual(summary["rms_mean"]["max"], 3.0)
        self.assertEqual(summary["rms_mean"]["n"], 3)

    def test_nan_values_excluded_from_stats(self):
        """NaN이 mean/min/max에 섞이면 이후 계산 전체가 NaN으로 오염되고
        strict JSON 직렬화도 실패한다 — 통계에서 제외해야 한다."""
        summary = summarize_window_features(
            [{"x": 1.0}, {"x": float("nan")}, {"x": 3.0}]
        )
        self.assertAlmostEqual(summary["x"]["mean"], 2.0)
        self.assertEqual(summary["x"]["n"], 2)
        self.assertEqual(summary["x"]["n_invalid"], 1)
        self.assertTrue(math.isfinite(summary["x"]["mean"]))

    def test_inf_values_excluded_from_stats(self):
        summary = summarize_window_features(
            [{"x": 1.0}, {"x": float("inf")}, {"x": 3.0}]
        )
        self.assertAlmostEqual(summary["x"]["mean"], 2.0)
        self.assertEqual(summary["x"]["n_invalid"], 1)

    def test_none_and_non_numeric_values_excluded_from_stats(self):
        summary = summarize_window_features(
            [{"x": 1.0}, {"x": None}, {"x": "oops"}, {"x": True}, {"x": 3.0}]
        )
        self.assertAlmostEqual(summary["x"]["mean"], 2.0)
        self.assertEqual(summary["x"]["n"], 2)
        self.assertEqual(summary["x"]["n_invalid"], 3)

    def test_feature_entirely_invalid_is_excluded_from_summary(self):
        summary = summarize_window_features(
            [{"x": float("nan")}, {"x": float("inf")}, {"x": None}]
        )
        self.assertNotIn("x", summary)


class TestBuildEventDetailSynthetic(unittest.TestCase):
    def test_before_after_split_and_delta(self):
        # 이전 5개는 kurtosis ~0(정상), 이후 5개는 kurtosis ~5(이상)
        windows = _fixture_windows([0.0] * 5 + [5.0] * 5)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=5,
            window_seconds=5 * 0.2,
            window_duration_sec=0.2,
        )

        self.assertEqual(detail["before"]["window_count_available"], 5)
        self.assertEqual(detail["after"]["window_count_available"], 5)
        self.assertFalse(detail["before"]["data_missing"])
        self.assertFalse(detail["after"]["data_missing"])
        self.assertFalse(detail["data_completeness"]["gap_detected"])

        self.assertAlmostEqual(detail["before"]["features"]["kurtosis_mean"]["mean"], 0.0)
        self.assertAlmostEqual(detail["after"]["features"]["kurtosis_mean"]["mean"], 5.0)
        self.assertAlmostEqual(detail["feature_delta"]["kurtosis_mean"]["delta"], 5.0)

        # after의 time_range는 이벤트 발생 시각(0.0)에서 시작해야 함 (수용 기준: 시각 범위 일치)
        self.assertEqual(detail["after"]["time_range"][0], 0.0)

    def test_missing_before_data_flagged_when_event_near_start(self):
        windows = _fixture_windows([0.0] * 3 + [5.0] * 5)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=3,  # before로 쓸 수 있는 윈도우가 3개뿐 (요청은 5개)
            window_seconds=5 * 0.2,
            window_duration_sec=0.2,
        )
        self.assertEqual(detail["before"]["window_count_available"], 3)
        self.assertTrue(detail["before"]["data_missing"])
        self.assertTrue(detail["data_completeness"]["gap_detected"])
        self.assertFalse(detail["after"]["data_missing"])

    def test_pct_change_null_when_before_mean_near_zero(self):
        windows = _fixture_windows([0.0] * 3 + [5.0] * 3)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=3,
            window_seconds=3 * 0.2,
            window_duration_sec=0.2,
        )
        self.assertIsNone(detail["feature_delta"]["kurtosis_mean"]["pct_change"])

    def test_invalid_event_index_raises(self):
        windows = _fixture_windows([0.0] * 3)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=99,
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_event_index_equal_to_length_raises(self):
        """event_index == len(ordered_windows)는 실제 이벤트 윈도우가 없는
        상태이므로 허용되면 안 된다."""
        windows = _fixture_windows([0.0] * 3)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=len(windows),
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_bool_event_index_rejected(self):
        """bool은 int의 서브클래스라 True가 조용히 인덱스 1로 쓰일 수 있다."""
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=True,
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_float_event_index_rejected(self):
        """0.5는 범위 검사(0 <= event_index < len)는 통과하지만 슬라이싱에서
        TypeError가 나므로, 범위 검사 전에 정수 타입을 먼저 검증해야 한다."""
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2.5,
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_string_event_index_rejected(self):
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index="2",
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_zero_window_seconds_rejected(self):
        """0을 넣으면 max(1, ceil(...))이 조용히 1개 윈도우 요청으로 바꿔 응답의
        요청값과 실제 계산이 어긋나므로, 미리 명확한 오류로 거부해야 한다."""
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=0,
                window_duration_sec=0.2,
            )

    def test_negative_window_seconds_rejected(self):
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=-1.0,
                window_duration_sec=0.2,
            )

    def test_nan_window_seconds_rejected(self):
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=float("nan"),
                window_duration_sec=0.2,
            )

    def test_infinite_window_duration_sec_rejected(self):
        """window_duration_sec이 무한대면 <= 0 검사를 통과해 [0.0, inf] 같은
        잘못된 time_range가 조용히 반환된다 — 먼저 유한값인지 검증해야 한다."""
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=1.0,
                window_duration_sec=float("inf"),
            )

    def test_nan_window_duration_sec_rejected(self):
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=1.0,
                window_duration_sec=float("nan"),
            )

    def test_bool_window_duration_sec_rejected(self):
        """True는 <= 0 검사만으로는 1초로 통과돼 버린다 — bool은 숫자 타입에서
        제외해야 한다."""
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=1.0,
                window_duration_sec=True,
            )

    def test_string_window_duration_sec_rejected(self):
        windows = _fixture_windows([0.0] * 5)
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=windows,
                event_index=2,
                window_seconds=1.0,
                window_duration_sec="0.2",
            )

    def test_nan_feature_value_does_not_break_strict_json_serialization(self):
        """NaN/Inf가 mean/delta/pct_change로 전파되면 data_missing=false인
        채로 strict JSON(allow_nan=False) 직렬화가 실패한다 — 응답 전체가
        strict 직렬화 가능해야 한다."""
        import json

        windows = _fixture_windows([0.0] * 3 + [float("nan")] * 2 + [5.0] * 5)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=5,
            window_seconds=5 * 0.2,
            window_duration_sec=0.2,
        )
        json.dumps(detail, allow_nan=False)  # 실패하면 예외 발생

    def test_applied_model_version_from_explicit_argument_is_preserved(self):
        windows = _fixture_windows([0.0] * 5)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=2,
            window_seconds=0.2,
            window_duration_sec=0.2,
            applied_model_version="freq-ae-v1",
        )
        self.assertEqual(detail["applied_model_version"], "freq-ae-v1")

    def test_applied_model_version_falls_back_to_event_field(self):
        """EVENT_DETAIL_01 기능정의는 적용 임계값/모델 버전을 반환해야 하는데,
        판정 당시 이벤트에 modelVersion이 실려 있어도 명시적 인자를 안 넘기면
        상세 응답에서 유실되면 안 된다."""
        event = {"id": "EV-241", "assetId": "SITE-01-MOT-02", "modelVersion": "freq-ae-v2"}
        windows = _fixture_windows([0.0] * 5)
        detail = build_event_detail(
            event=event,
            ordered_windows=windows,
            event_index=2,
            window_seconds=0.2,
            window_duration_sec=0.2,
        )
        self.assertEqual(detail["applied_model_version"], "freq-ae-v2")

    def test_applied_model_version_explicit_argument_overrides_event_field(self):
        event = {"id": "EV-241", "assetId": "SITE-01-MOT-02", "modelVersion": "freq-ae-v2"}
        windows = _fixture_windows([0.0] * 5)
        detail = build_event_detail(
            event=event,
            ordered_windows=windows,
            event_index=2,
            window_seconds=0.2,
            window_duration_sec=0.2,
            applied_model_version="freq-ae-v3",
        )
        self.assertEqual(detail["applied_model_version"], "freq-ae-v3")

    def test_applied_model_version_defaults_to_none(self):
        windows = _fixture_windows([0.0] * 5)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=2,
            window_seconds=0.2,
            window_duration_sec=0.2,
        )
        self.assertIsNone(detail["applied_model_version"])

    def test_empty_ordered_windows_raises(self):
        with self.assertRaises(ValueError):
            build_event_detail(
                event=_sample_event(),
                ordered_windows=[],
                event_index=0,
                window_seconds=0.2,
                window_duration_sec=0.2,
            )

    def test_requested_window_seconds_is_always_covered_via_ceil(self):
        """round()라면 window_seconds/window_duration_sec=4.1 -> 4로 내려가
        요청 구간(0.82s)보다 짧은 0.8s만 반환된다. ceil을 쓰면 5윈도우(1.0s)로
        요청 구간을 항상 포함해야 한다."""
        windows = _fixture_windows([0.0] * 10 + [5.0] * 10)
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=windows,
            event_index=10,
            window_seconds=0.82,
            window_duration_sec=0.2,
        )
        self.assertEqual(detail["before"]["window_count_requested"], 5)
        self.assertGreaterEqual(
            detail["before"]["window_count_requested"] * 0.2, 0.82
        )


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")),
    f"CWRU 실데이터 없음: {os.path.join(_CWRU_DATA_DIR, '97.mat')}",
)
class TestBuildEventDetailRealCwruData(unittest.TestCase):
    """97.mat(NORMAL) -> 105.mat(BEARING_FAULT_INNER) 전환 지점을 합성 이벤트로 보고
    전후 특징량이 실제로 갈리는지 확인한다 (week2에서 확인된 kurtosis 급증 재현).
    """

    @classmethod
    def setUpClass(cls):
        from load_cwru_vibration import load_cwru_dataset
        from extract_features import extract_all_features, FeatureConfig

        records = load_cwru_dataset(_CWRU_DATA_DIR)
        normal = [r for r in records if r["label"] == "NORMAL"]
        fault = [r for r in records if r["source_label"] == "105.mat"]
        cls.event_index = len(normal)

        config = FeatureConfig(sample_rate=normal[0]["sample_rate"])
        ordered = normal + fault
        cls.ordered_windows = [
            {"sample_id": r["sample_id"], "features": extract_all_features(r["signal"], config)}
            for r in ordered
        ]
        cls.window_duration_sec = 2048 / normal[0]["sample_rate"]

    def test_kurtosis_jumps_after_fault_transition(self):
        detail = build_event_detail(
            event=_sample_event(),
            ordered_windows=self.ordered_windows,
            event_index=self.event_index,
            window_seconds=10 * self.window_duration_sec,
            window_duration_sec=self.window_duration_sec,
        )

        self.assertFalse(detail["data_completeness"]["gap_detected"])

        before_kurt = detail["before"]["features"]["kurtosis_mean"]["mean"]
        after_kurt = detail["after"]["features"]["kurtosis_mean"]["mean"]
        delta = detail["feature_delta"]["kurtosis_mean"]["delta"]

        self.assertGreater(
            after_kurt,
            before_kurt,
            "결함 전환 이후 구간의 kurtosis_mean이 이전보다 커야 한다",
        )
        self.assertGreater(delta, 1.0, "kurtosis_mean 변화폭이 뚜렷해야 한다")


if __name__ == "__main__":
    unittest.main()
