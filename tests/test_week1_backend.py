from __future__ import annotations

import json
import unittest
from http import HTTPStatus
from io import BytesIO

from motor_diagnosis.data import ApiError, authenticate, get_site, telemetry_for
from motor_diagnosis.server import AppHandler


class DummyHandler(AppHandler):
    def __init__(self) -> None:
        self._status = None
        self._headers = []
        self.wfile = BytesIO()

    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = code

    def send_header(self, keyword: str, value: str) -> None:
        self._headers.append((keyword.lower(), value))

    def end_headers(self) -> None:
        return None


class Week1BackendTest(unittest.TestCase):
    def test_login_returns_role_policy(self) -> None:
        response = authenticate({"username": "admin", "password": "admin123"})

        self.assertEqual(response["user"]["role"], "B")
        self.assertTrue(response["session"]["token"].startswith("demo-"))
        self.assertIn("site:write", response["rolePolicy"]["permissions"])

    def test_login_rejects_invalid_credentials(self) -> None:
        with self.assertRaises(ApiError) as context:
            authenticate({"username": "admin", "password": "bad"})

        self.assertEqual(context.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(context.exception.code, "INVALID_CREDENTIALS")

    def test_unknown_site_is_not_replaced_by_first_site(self) -> None:
        with self.assertRaises(ApiError) as context:
            get_site("SITE-999")

        self.assertEqual(context.exception.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(context.exception.code, "SITE_NOT_FOUND")

    def test_telemetry_contains_week1_units_and_72_points(self) -> None:
        points = telemetry_for("SITE-01", "SITE-01-MOT-02")

        self.assertEqual(len(points), 72)
        self.assertLessEqual(
            {"vibrationRmsMmS", "acousticDb", "anomalyScore"},
            set(points[0]),
        )
        self.assertTrue(all(0 <= point["anomalyScore"] <= 100 for point in points))

    def test_error_response_shape_is_safe_json(self) -> None:
        handler = DummyHandler()

        handler.send_error_json(ApiError(404, "SITE_NOT_FOUND", "존재하지 않는 발전소입니다."))

        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(handler._status, 404)
        self.assertEqual(
            body,
            {"error": {"code": "SITE_NOT_FOUND", "message": "존재하지 않는 발전소입니다."}},
        )
        self.assertFalse(
            any("traceback" in str(value).lower() for value in body["error"].values())
        )
