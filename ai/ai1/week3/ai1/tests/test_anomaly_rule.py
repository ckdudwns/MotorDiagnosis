"""
ANOMALY_RULE_01 테스트 — 임계값·히스테리시스·설비별 기준선 레지스트리 검증 (AI-1, 3주차)

합성(fixture) 스트림으로 히스테리시스/레지스트리 로직을 항상 검증하고, 실제 CWRU
데이터(97.mat NORMAL 119윈도우 -> 105/118/130.mat 결함 177윈도우)로 week2가 산출한
baseline.json을 그대로 써서 "정상 구간에서는 이벤트가 거의/전혀 안 생기고, 결함
구간 진입 시 실제로 이상 판정되는지"를 확인한다.
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANOMALY_RULES_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "anomaly_rules"))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
_CWRU_DATA_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
)
_BASELINE_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "dataset", "baseline.json")
)

sys.path.insert(0, _ANOMALY_RULES_DIR)
sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from anomaly_rule import (  # noqa: E402
    AnomalyRuleConfig,
    AssetBaselineRegistry,
    evaluate_feature_stream,
    evaluate_asset_stream,
)
from validate_features import load_baseline  # noqa: E402


def _fake_baseline() -> dict:
    return {
        "meta": {"generated_at": "2026-01-01T00:00:00+00:00"},
        "features": {"x": {"mean": 0.0, "std": 1.0}},
    }


class TestAnomalyRuleConfig(unittest.TestCase):
    def test_sigma_exit_must_be_less_than_sigma_enter(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(sigma_enter=2.0, sigma_exit=3.0)

    def test_min_consecutive_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(min_consecutive_enter=0)

    def test_nan_sigma_enter_rejected(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(sigma_enter=float("nan"))

    def test_negative_sigma_exit_rejected(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(sigma_exit=-1.0, sigma_enter=3.0)

    def test_infinite_sigma_enter_rejected(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(sigma_enter=float("inf"))

    def test_bool_sigma_enter_rejected(self):
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(sigma_enter=True)

    def test_float_min_consecutive_enter_rejected(self):
        """1.5는 `< 1` 검사만으로는 통과된다 — 정수 타입인지 먼저 검증해야 한다."""
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(min_consecutive_enter=1.5)

    def test_bool_min_consecutive_exit_rejected(self):
        """bool은 int의 서브클래스라 `True >= 1` 검사를 통과해 버린다."""
        with self.assertRaises(ValueError):
            AnomalyRuleConfig(min_consecutive_exit=True)


class TestAssetBaselineRegistry(unittest.TestCase):
    def test_resolve_prefers_asset_id_over_asset_type_over_default(self):
        registry = AssetBaselineRegistry()
        default_baseline = {"tag": "default", "features": {"x": {"mean": 0.0, "std": 1.0}}}
        motor_baseline = {"tag": "MOTOR", "features": {"x": {"mean": 0.0, "std": 1.0}}}
        specific_baseline = {
            "tag": "SITE-01-MOT-02",
            "features": {"x": {"mean": 0.0, "std": 1.0}},
        }

        registry.register(default_baseline, is_default=True)
        registry.register(motor_baseline, asset_type="MOTOR")
        registry.register(specific_baseline, asset_id="SITE-01-MOT-02")

        self.assertEqual(
            registry.resolve(asset_id="SITE-01-MOT-02", asset_type="MOTOR")["baseline"]["tag"],
            "SITE-01-MOT-02",
        )
        self.assertEqual(
            registry.resolve(asset_id="SITE-01-PUMP-04", asset_type="MOTOR")["baseline"]["tag"],
            "MOTOR",
        )
        self.assertEqual(
            registry.resolve(asset_id="UNKNOWN", asset_type="UNKNOWN")["baseline"]["tag"],
            "default",
        )

    def test_resolve_without_any_registration_raises(self):
        registry = AssetBaselineRegistry()
        with self.assertRaises(KeyError):
            registry.resolve(asset_id="X")

    def test_register_rejects_nan_mean(self):
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": float("nan"), "std": 1.0}}}
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

    def test_register_rejects_nan_std(self):
        """NaN std는 check_outliers()의 low/high도 NaN으로 만들어, 어떤 값과
        비교해도 False가 되므로 모든 윈도우가 조용히 NORMAL 처리된다 —
        등록 시점에 막아야 한다."""
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": float("nan")}}}
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

    def test_register_rejects_infinite_std(self):
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": float("inf")}}}
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

    def test_register_rejects_negative_std(self):
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": -1.0}}}
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

    def test_register_rejects_empty_features(self):
        registry = AssetBaselineRegistry()
        with self.assertRaises(ValueError):
            registry.register({"features": {}}, is_default=True)

    def test_register_rejects_missing_features_key(self):
        registry = AssetBaselineRegistry()
        with self.assertRaises(ValueError):
            registry.register({}, is_default=True)

    def test_register_rejects_non_numeric_mean(self):
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": "0.0", "std": 1.0}}}
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

    def test_corrupted_baseline_would_have_masked_extreme_values(self):
        """등록 검증이 없다면 NaN std baseline은 극단값 윈도우도 전부
        NORMAL로 판정한다는 것을 직접 재현해, 검증이 실제 탐지 실패를
        막는다는 것을 보여준다."""
        baseline = {"features": {"x": {"mean": 0.0, "std": float("nan")}}}
        registry = AssetBaselineRegistry()
        with self.assertRaises(ValueError):
            registry.register(baseline, is_default=True)

        # 레지스트리를 우회해 evaluate_feature_stream에 직접 손상된 baseline을
        # 넘기면(검증이 없다면 실제로 벌어졌을 상황) 극단값도 NORMAL로 잡힌다.
        windows = [{"x": 1e9}] * 5
        result = evaluate_feature_stream(windows, baseline, AnomalyRuleConfig())
        self.assertEqual(result["events"], [], "NaN std baseline은 fail-open으로 이어진다")

    def test_mutating_baseline_after_register_does_not_affect_registered_entry(self):
        """검증을 통과한 baseline 객체를 그대로 저장하면, 등록 후 호출자가
        원본 dict를 변경(예: std를 NaN으로)했을 때 그 변경이 이미 등록된
        항목까지 오염시켜 검증을 우회한다 — register()는 deepcopy로 등록
        시점의 값을 스냅샷으로 고정해야 한다."""
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": 1.0}}}
        registry.register(baseline, is_default=True)

        # 등록 후 원본 dict를 변경 — 등록된 항목이 이 변경에 영향을 받으면
        # 안 된다.
        baseline["features"]["x"]["std"] = float("nan")

        resolved = registry.resolve(asset_id="anything")
        self.assertEqual(resolved["baseline"]["features"]["x"]["std"], 1.0)

        # 큰 이상값을 흘려도 등록된(오염되지 않은) baseline 기준으로 정상
        # 탐지가 계속 동작해야 한다.
        windows = [{"x": 1e9}] * 5
        result = evaluate_feature_stream(windows, resolved["baseline"], resolved["config"])
        self.assertGreater(
            len(result["events"]), 0, "등록 후 원본 변경이 등록된 baseline을 오염시켰다"
        )

    def test_mutating_config_after_register_does_not_affect_registered_entry(self):
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": 1.0}}}
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)
        registry.register(baseline, config=config, is_default=True)

        config.min_consecutive_enter = 999

        resolved = registry.resolve(asset_id="anything")
        self.assertEqual(resolved["config"].min_consecutive_enter, 2)

    def test_mutating_resolved_entry_does_not_leak_to_other_assets(self):
        """resolve()가 내부 entry를 그대로 돌려주면, 조회 결과를 그 자리에서
        수정(예: sigma_enter 조정)했을 때 등록된 상태 자체가 오염된다.
        특히 asset_type/default로 등록한 entry는 여러 설비가 같은 객체를
        공유하므로, 한 설비의 조회 결과를 고치면 별도로 재등록하지 않은
        다른 설비에도 그 변경이 전파된다 — 실제로 MOTOR 기본 설정을
        조회해 sigma_enter를 11로 바꾸면 재등록하지 않은 다른 설비도
        10sigma 이벤트를 놓치는 것을 재현한다."""
        registry = AssetBaselineRegistry()
        baseline = {"features": {"x": {"mean": 0.0, "std": 1.0}}}
        registry.register(
            baseline,
            config=AnomalyRuleConfig(
                sigma_enter=10.0, sigma_exit=5.0,
                min_consecutive_enter=1, min_consecutive_exit=1,
            ),
            asset_type="MOTOR",
        )

        resolved_for_a = registry.resolve(asset_type="MOTOR")
        resolved_for_a["config"].sigma_enter = 11.0  # A 설비 전용으로 조정한다고 착각하기 쉬움
        resolved_for_a["baseline"]["features"]["x"]["mean"] = 999.0

        resolved_for_b = registry.resolve(asset_type="MOTOR")
        self.assertEqual(
            resolved_for_b["config"].sigma_enter,
            10.0,
            "B 설비 조회 결과가 A 설비의 조회 결과 수정에 영향을 받았습니다",
        )
        self.assertEqual(
            resolved_for_b["baseline"]["features"]["x"]["mean"],
            0.0,
            "B 설비 조회 결과가 A 설비의 조회 결과 수정에 영향을 받았습니다",
        )

        # 등록되지 않은(재등록 없이 MOTOR 기본을 그대로 쓰는) 설비도
        # 10sigma 이벤트를 그대로 잡아야 한다 — A 설비 조회 결과 수정으로
        # 임계값이 11sigma로 올라가 이벤트를 놓치면 안 된다.
        result = evaluate_asset_stream(
            "SITE-B-MOT-01", [{"x": 10.0}], registry, asset_type="MOTOR"
        )
        self.assertEqual(
            len(result["events"]), 1, "다른 설비 조회 결과 수정이 이 설비의 판정에 전파됐다"
        )


class TestHysteresisSuppressesSingleSpike(unittest.TestCase):
    def test_single_window_spike_does_not_trigger_event(self):
        baseline = _fake_baseline()
        # 정상(0.0) 사이에 딱 1윈도우만 큰 스파이크(20.0) -> min_consecutive_enter=2면 진입 못함
        windows = [{"x": 0.0}] * 5 + [{"x": 20.0}] + [{"x": 0.0}] * 5
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)

        result = evaluate_feature_stream(windows, baseline, config)
        self.assertEqual(result["events"], [])

    def test_sustained_spike_triggers_event_and_recovers(self):
        baseline = _fake_baseline()
        windows = [{"x": 0.0}] * 5 + [{"x": 20.0}] * 4 + [{"x": 0.0}] * 5
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)

        result = evaluate_feature_stream(windows, baseline, config)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["start_index"], 5)
        self.assertIsNotNone(event["end_index"])
        self.assertEqual(event["baseline_version"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(event["config_version"], "v1")

    def test_event_stays_open_when_stream_ends_during_anomaly(self):
        baseline = _fake_baseline()
        windows = [{"x": 0.0}] * 5 + [{"x": 20.0}] * 4
        result = evaluate_feature_stream(windows, baseline, AnomalyRuleConfig())
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["events"][0]["end_index"])

    def test_entry_buildup_max_deviation_is_preserved_in_event(self):
        """진입 대기 구간(consecutive_over)에서 관측된 최대 편차가 이벤트 생성 시
        유실되면 안 된다 — 10sigma 다음 4sigma로 진입해도 이벤트의
        max_deviation_sigma는 4가 아니라 10이어야 한다."""
        baseline = _fake_baseline()
        windows = [{"x": 0.0}] * 3 + [{"x": 10.0}, {"x": 4.0}] + [{"x": 0.0}] * 5
        config = AnomalyRuleConfig(
            sigma_enter=3.0, sigma_exit=2.0, min_consecutive_enter=2, min_consecutive_exit=2
        )

        result = evaluate_feature_stream(windows, baseline, config)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["max_deviation_sigma"], 10.0)
        self.assertEqual(result["events"][0]["start_index"], 3)

    def test_invalid_window_does_not_close_anomaly_event(self):
        """check_outliers()는 NaN/Inf 특징값을 건너뛰어 _max_deviation_sigma()가
        0.0(=NORMAL)을 반환한다 — 예전 로직대로면 진행 중인 이벤트가 NaN
        윈도우 2개만으로 조기 종료됐다. 무효 윈도우는 consecutive_under에
        포함되면 안 되므로 이벤트가 계속 열려 있어야 한다."""
        baseline = _fake_baseline()
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)
        windows = (
            [{"x": 0.0}] * 5
            + [{"x": 20.0}] * 4  # 이상 진입
            + [{"x": float("nan")}] * 3  # 무효 윈도우 - 조기 종료를 유발하면 안 됨
        )
        result = evaluate_feature_stream(windows, baseline, config)

        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(
            result["events"][0]["end_index"],
            "무효 윈도우 때문에 이벤트가 조기 종료되면 안 된다",
        )
        invalid_states = [w for w in result["window_states"] if w["state"] == "INVALID"]
        self.assertEqual(len(invalid_states), 3)

    def test_invalid_window_breaks_entry_consecutive_count(self):
        """[이상, INVALID, 이상]은 연속 2회 진입으로 합산되면 안 된다.
        INVALID는 consecutive_over를 끊어야 하므로 min_consecutive_enter=2에서는
        이벤트가 아예 생기지 않아야 한다 (이전 버그: continue가 카운터를 보존해
        연속 2회로 오판하고 start_index가 INVALID 윈도우를 가리켰다)."""
        baseline = _fake_baseline()
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)
        windows = (
            [{"x": 0.0}] * 5
            + [{"x": 20.0}]  # 이상 스파이크 1회
            + [{"x": float("nan")}]  # 무효 윈도우 - 진입 카운터를 끊어야 함
            + [{"x": 20.0}]  # 이상 스파이크 1회 (앞의 스파이크와 연속이 아님)
            + [{"x": 0.0}] * 5
        )
        result = evaluate_feature_stream(windows, baseline, config)
        self.assertEqual(
            result["events"], [], "INVALID로 갈라진 두 스파이크가 연속 진입으로 합산되면 안 된다"
        )

    def test_invalid_window_breaks_exit_consecutive_count(self):
        """[이상 진입, 정상, INVALID, 정상]은 연속 2회 복귀로 합산되면 안 된다.
        ANOMALY 상태의 INVALID는 consecutive_under를 끊어야 한다 (이전 버그:
        continue가 카운터를 보존해 연속 2회 복귀로 오판하고 end_index가
        INVALID 윈도우를 가리켰다)."""
        baseline = _fake_baseline()
        config = AnomalyRuleConfig(min_consecutive_enter=2, min_consecutive_exit=2)
        windows = (
            [{"x": 0.0}] * 5
            + [{"x": 20.0}] * 4  # 이상 진입 및 유지
            + [{"x": 0.0}]  # 복귀 후보 1회
            + [{"x": float("nan")}]  # 무효 윈도우 - 복귀 카운터를 끊어야 함
            + [{"x": 0.0}]  # 복귀 후보 1회 (앞의 후보와 연속이 아님)
        )
        result = evaluate_feature_stream(windows, baseline, config)
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(
            result["events"][0]["end_index"],
            "INVALID로 갈라진 두 복귀 후보가 연속 복귀로 합산되어 이벤트가 조기 종료되면 안 된다",
        )

    def test_inf_feature_value_flagged_invalid_not_normal(self):
        baseline = _fake_baseline()
        windows = [{"x": float("inf")}]
        result = evaluate_feature_stream(windows, baseline, AnomalyRuleConfig())
        self.assertEqual(result["window_states"][0]["state"], "INVALID")
        self.assertEqual(result["events"], [])

    def test_missing_required_feature_flagged_invalid(self):
        baseline = _fake_baseline()  # "x"가 필수 특징값
        windows = [{}]
        result = evaluate_feature_stream(windows, baseline, AnomalyRuleConfig())
        self.assertEqual(result["window_states"][0]["state"], "INVALID")

    def test_hysteresis_prevents_flicker_near_boundary(self):
        """진입(3sigma)과 복귀(2sigma) 임계값 사이(2.5sigma)를 오가는 값은,
        일단 이상에 진입하면 그 사이값만으로는 복귀하지 않아야 한다(깜빡임 방지).
        """
        baseline = {
            "meta": {"generated_at": "t"},
            "features": {"x": {"mean": 0.0, "std": 1.0}},
        }
        config = AnomalyRuleConfig(
            sigma_enter=3.0, sigma_exit=2.0, min_consecutive_enter=1, min_consecutive_exit=1
        )
        # 3.5sigma로 진입 후, 2.5sigma(진입 미만이지만 복귀 기준보다는 큼)를 오래 유지
        windows = [{"x": 3.5}] + [{"x": 2.5}] * 5
        result = evaluate_feature_stream(windows, baseline, config)
        # 2.5sigma는 sigma_exit(2.0)보다 커서 복귀 조건을 만족하지 못하므로 이벤트가 계속 열려 있어야 함
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["events"][0]["end_index"])


@unittest.skipUnless(
    os.path.exists(os.path.join(_CWRU_DATA_DIR, "97.mat")) and os.path.exists(_BASELINE_PATH),
    f"CWRU 실데이터 또는 baseline.json 없음: {_CWRU_DATA_DIR}, {_BASELINE_PATH}",
)
class TestAnomalyRuleWithRealCwruData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from load_cwru_vibration import load_cwru_dataset
        from extract_features import extract_all_features, FeatureConfig

        records = load_cwru_dataset(_CWRU_DATA_DIR)
        cls.normal = [r for r in records if r["label"] == "NORMAL"]
        cls.fault = [r for r in records if r["label"] != "NORMAL"]
        self_config = FeatureConfig(sample_rate=cls.normal[0]["sample_rate"])

        cls.ordered_features = [
            extract_all_features(r["signal"], self_config) for r in cls.normal + cls.fault
        ]
        cls.n_normal = len(cls.normal)
        cls.baseline = load_baseline(_BASELINE_PATH)

    def test_default_config_detects_event_within_fault_segment(self):
        result = evaluate_feature_stream(
            self.ordered_features, self.baseline, AnomalyRuleConfig()
        )
        self.assertGreater(len(result["events"]), 0, "결함 구간에서 이상 이벤트가 하나도 안 잡힘")

        first_event = result["events"][0]
        # 히스테리시스로 최대 min_consecutive_enter-1만큼 지연될 수 있으므로 약간의 여유를 둔다
        self.assertGreaterEqual(first_event["start_index"], self.n_normal - 2)

    def test_hysteresis_keeps_event_count_low_despite_100pct_raw_outlier_rate(self):
        """week2 결과: 결함 177윈도우 전부가 개별적으로는 이상치로 플래그됐다.
        히스테리시스가 없다면 매 윈도우 이벤트가 열렸다 닫혔다 할 수 있지만,
        연속조건+복귀임계값 덕분에 이벤트 수는 적게 유지돼야 한다.
        """
        result = evaluate_feature_stream(
            self.ordered_features, self.baseline, AnomalyRuleConfig()
        )
        self.assertLessEqual(
            len(result["events"]), 5, "히스테리시스가 걸려도 이벤트가 과도하게 쪼개짐"
        )

    def test_asset_registry_integration_multi_asset(self):
        registry = AssetBaselineRegistry()
        registry.register(self.baseline, asset_type="MOTOR", is_default=True)
        # 향후 설비 확장 예시: 다른 설비 ID에 별도 기준선을 등록해도 서로 간섭하지 않음
        other_baseline = {
            "meta": {"generated_at": "other"},
            "features": {"rms_mean": {"mean": 0.0, "std": 1.0}},
        }
        registry.register(other_baseline, asset_id="SITE-02-PUMP-01")

        result = evaluate_asset_stream(
            "SITE-01-MOT-02", self.ordered_features, registry, asset_type="MOTOR"
        )
        self.assertGreater(len(result["events"]), 0)
        for event in result["events"]:
            self.assertEqual(event["asset_id"], "SITE-01-MOT-02")
            self.assertEqual(event["baseline_version"], self.baseline["meta"]["generated_at"])

        # 다른 설비는 다른 기준선을 적용받는다 (여기서는 전부 0이라 정상범위를 벗어나지 않음)
        flat_windows = [{"rms_mean": 0.0}] * 10
        other_result = evaluate_asset_stream(
            "SITE-02-PUMP-01", flat_windows, registry
        )
        self.assertEqual(other_result["events"], [])


if __name__ == "__main__":
    unittest.main()
