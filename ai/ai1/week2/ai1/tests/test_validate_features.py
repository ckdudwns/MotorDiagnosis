"""
validate_features.py 단위 테스트 (AI-1, PR #6 리뷰 반영)

CWRU 실데이터 없이도 항상 실행되는 순수 단위 테스트. unittest 표준 프레임워크를
사용해 `python -m unittest discover`로 자동 수집된다.

리뷰에서 지적된 3가지 취약점을 각각 검증한다:
1. 빈 특징값 dict가 "유효"로 통과되던 문제
2. baseline에 정의된 필수 키가 없어도 통과되던 문제
3. 문자열/bool 값이 숫자처럼 통과되던 문제 (bool은 int의 서브클래스라는 함정 포함)
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "feature_extraction"
    ),
)

from validate_features import (  # noqa: E402
    is_valid_number,
    validate_features,
    check_missing_or_invalid,
)


FAKE_BASELINE = {
    "features": {
        "rms_mean": {"mean": 0.07374, "std": 0.00196},
        "kurtosis_mean": {"mean": -0.24415, "std": 0.13095},
    }
}


class TestIsValidNumber(unittest.TestCase):
    def test_int_and_float_are_valid(self):
        self.assertTrue(is_valid_number(1))
        self.assertTrue(is_valid_number(1.5))
        self.assertTrue(is_valid_number(-3.2))

    def test_bool_is_not_a_valid_number(self):
        # bool은 int의 서브클래스라 isinstance(True, (int, float))가 True로 나오는 함정.
        self.assertFalse(is_valid_number(True))
        self.assertFalse(is_valid_number(False))

    def test_string_is_not_a_valid_number(self):
        self.assertFalse(is_valid_number("0.074"))
        self.assertFalse(is_valid_number("not_a_number"))

    def test_none_is_not_a_valid_number(self):
        self.assertFalse(is_valid_number(None))


class TestEmptyFeaturesRejected(unittest.TestCase):
    """케이스 1: 빈 특징값 dict는 거부되어야 한다."""

    def test_empty_dict_is_invalid(self):
        report = validate_features({}, FAKE_BASELINE)
        self.assertFalse(report["is_valid"])
        reasons = {item["reason"] for item in report["missing_or_invalid"]}
        self.assertIn("empty_features", reasons)

    def test_empty_dict_without_baseline_is_invalid(self):
        # baseline이 없어도 빈 dict 자체는 거부되어야 한다.
        report = validate_features({}, baseline=None)
        self.assertFalse(report["is_valid"])
        reasons = {item["reason"] for item in report["missing_or_invalid"]}
        self.assertIn("empty_features", reasons)


class TestMissingRequiredKeyRejected(unittest.TestCase):
    """케이스 2: baseline에 정의된 필수 키가 하나라도 없으면 거부되어야 한다."""

    def test_missing_one_required_key(self):
        # baseline에는 rms_mean, kurtosis_mean 두 키가 정의되어 있는데 하나만 제공.
        report = validate_features({"rms_mean": 0.074}, FAKE_BASELINE)
        self.assertFalse(report["is_valid"])
        missing_keys = {
            item["feature"]
            for item in report["missing_or_invalid"]
            if item["reason"] == "missing_key"
        }
        self.assertIn("kurtosis_mean", missing_keys)

    def test_all_required_keys_present_passes_this_check(self):
        report = validate_features(
            {"rms_mean": 0.074, "kurtosis_mean": -0.2}, FAKE_BASELINE
        )
        missing_key_reasons = [
            item
            for item in report["missing_or_invalid"]
            if item["reason"] == "missing_key"
        ]
        self.assertEqual(missing_key_reasons, [])

    def test_no_baseline_skips_required_key_check(self):
        # baseline이 없으면 "필수 키" 개념 자체가 없으므로 이 검사는 건너뛴다.
        report = validate_features({"rms_mean": 0.074}, baseline=None)
        missing_key_reasons = [
            item
            for item in report["missing_or_invalid"]
            if item["reason"] == "missing_key"
        ]
        self.assertEqual(missing_key_reasons, [])


class TestNonNumericValuesRejected(unittest.TestCase):
    """케이스 3: 문자열/bool 값은 숫자로 취급하지 않고 거부해야 한다."""

    def test_string_value_rejected(self):
        report = validate_features(
            {"rms_mean": "0.074", "kurtosis_mean": -0.2}, FAKE_BASELINE
        )
        self.assertFalse(report["is_valid"])
        invalid_type_features = {
            item["feature"]
            for item in report["missing_or_invalid"]
            if item["reason"] == "invalid_type"
        }
        self.assertIn("rms_mean", invalid_type_features)

    def test_bool_value_rejected(self):
        # bool은 int의 서브클래스이므로 별도 처리 없이는 숫자로 오인될 수 있다.
        report = validate_features(
            {"rms_mean": True, "kurtosis_mean": -0.2}, FAKE_BASELINE
        )
        self.assertFalse(report["is_valid"])
        invalid_type_features = {
            item["feature"]
            for item in report["missing_or_invalid"]
            if item["reason"] == "invalid_type"
        }
        self.assertIn("rms_mean", invalid_type_features)

    def test_bool_value_not_silently_accepted_as_number(self):
        issues = check_missing_or_invalid({"rms_mean": False})
        reasons = [item["reason"] for item in issues if item["feature"] == "rms_mean"]
        self.assertEqual(reasons, ["invalid_type"])


class TestValidFeaturesPass(unittest.TestCase):
    """회귀 방지: 정상 케이스는 여전히 통과해야 한다."""

    def test_valid_numeric_features_pass(self):
        report = validate_features(
            {"rms_mean": 0.0745, "kurtosis_mean": -0.2}, FAKE_BASELINE
        )
        self.assertTrue(report["is_valid"])
        self.assertEqual(report["missing_or_invalid"], [])

    def test_nan_and_inf_still_rejected(self):
        report = validate_features(
            {"rms_mean": float("nan"), "kurtosis_mean": float("inf")}, FAKE_BASELINE
        )
        self.assertFalse(report["is_valid"])
        reasons = {item["reason"] for item in report["missing_or_invalid"]}
        self.assertIn("nan", reasons)
        self.assertIn("inf", reasons)


if __name__ == "__main__":
    unittest.main()
