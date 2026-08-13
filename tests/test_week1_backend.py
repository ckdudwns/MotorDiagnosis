from __future__ import annotations

import json
import unittest
from io import BytesIO

from motor_diagnosis.data import (
    ApiError,
    authenticate,
    create_asset,
    create_device,
    create_site,
    current_user_for_token,
    delete_asset,
    delete_device,
    delete_site,
    get_asset,
    get_device,
    get_site,
    quarantine_unregistered_device,
    require_site_access,
    reset_runtime_state,
    telemetry_for,
    telemetry_units,
    update_asset,
    update_device,
    update_site,
    visible_sites_for_user,
)
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
    def setUp(self) -> None:
        reset_runtime_state()

    def admin_user(self) -> dict:
        login = authenticate({"username": "admin", "password": "admin123"})
        return current_user_for_token(login["session"]["token"])

    def test_login_returns_server_side_session_and_role_policy(self) -> None:
        first = authenticate({"username": "admin", "password": "admin123"})
        second = authenticate({"username": "admin", "password": "admin123"})

        self.assertEqual(first["user"]["role"], "B")
        self.assertTrue(first["session"]["token"].startswith("demo-"))
        self.assertNotEqual(first["session"]["token"], second["session"]["token"])
        self.assertIn("site:write", first["rolePolicy"]["permissions"])
        self.assertEqual(current_user_for_token(first["session"]["token"])["username"], "admin")

    def test_login_locks_account_after_five_failures(self) -> None:
        for _ in range(4):
            with self.assertRaises(ApiError) as context:
                authenticate({"username": "admin", "password": "bad"})
            self.assertEqual(context.exception.status, 400)
            self.assertEqual(context.exception.code, "INVALID_CREDENTIALS")

        with self.assertRaises(ApiError) as locked_context:
            authenticate({"username": "admin", "password": "bad"})
        self.assertEqual(locked_context.exception.status, 423)
        self.assertEqual(locked_context.exception.code, "ACCOUNT_LOCKED")

        with self.assertRaises(ApiError) as still_locked_context:
            authenticate({"username": "admin", "password": "admin123"})
        self.assertEqual(still_locked_context.exception.status, 423)
        self.assertEqual(still_locked_context.exception.code, "ACCOUNT_LOCKED")

    def test_role_scope_limits_visible_sites_and_direct_access(self) -> None:
        login = authenticate({"username": "operator", "password": "operator123"})
        operator = current_user_for_token(login["session"]["token"])

        visible_sites = visible_sites_for_user(operator)

        self.assertEqual([site["id"] for site in visible_sites], ["SITE-01", "SITE-02", "SITE-03", "SITE-04"])
        with self.assertRaises(ApiError) as context:
            require_site_access(operator, "SITE-05")
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(context.exception.code, "SITE_FORBIDDEN")

    def test_site_asset_device_crud_rules(self) -> None:
        admin = self.admin_user()
        site = create_site(
            admin,
            {
                "id": "SITE-T01",
                "code": "TEST-T01",
                "name": "Test Plant",
                "networkType": "D",
                "targetAssetCount": 1,
            },
        )

        with self.assertRaises(ApiError) as duplicated_site:
            create_site(admin, {"id": "SITE-T01", "code": "TEST-T01-B", "name": "Duplicate", "networkType": "D"})
        self.assertEqual(duplicated_site.exception.code, "SITE_DUPLICATED")

        asset = create_asset(
            admin,
            site["id"],
            {
                "assetCode": "MOT-99",
                "name": "Test Motor",
                "assetType": "motor",
                "ratedRpm": 1500,
                "baseline": {
                    "status": "ready",
                    "capturedAt": "2026-08-13T10:00:00+09:00",
                    "vibrationRmsMmS": 1.21,
                    "acousticDb": 51.4,
                    "sampleCount": 180,
                },
            },
        )
        self.assertEqual(asset["baseline"]["vibrationRmsMmS"], 1.21)

        with self.assertRaises(ApiError) as duplicated_asset:
            create_asset(
                admin,
                site["id"],
                {"id": "SITE-T01-MOT-100", "assetCode": "MOT-99", "name": "Duplicate Motor"},
            )
        self.assertEqual(duplicated_asset.exception.code, "ASSET_CODE_DUPLICATED")

        device = create_device(
            admin,
            site["id"],
            {
                "id": "DEV-T01",
                "assetId": asset["id"],
                "firmware": "edge-0.1.1",
            },
        )
        self.assertEqual(device["mappingHistory"][0]["assetId"], asset["id"])

        with self.assertRaises(ApiError) as duplicated_mapping:
            create_device(admin, site["id"], {"id": "DEV-T02", "assetId": asset["id"]})
        self.assertEqual(duplicated_mapping.exception.code, "DEVICE_ASSET_DUPLICATED")

        second_asset = create_asset(
            admin,
            site["id"],
            {
                "assetCode": "MOT-100",
                "name": "Second Test Motor",
                "assetType": "motor",
                "ratedRpm": 1450,
            },
        )
        inactive_device = create_device(
            admin,
            site["id"],
            {
                "id": "DEV-T02",
                "assetId": second_asset["id"],
                "mappingStatus": "inactive",
                "health": "offline",
            },
        )
        with self.assertRaises(ApiError) as duplicated_activation:
            update_device(admin, inactive_device["id"], {"assetId": asset["id"], "mappingStatus": "active"})
        self.assertEqual(duplicated_activation.exception.code, "DEVICE_ASSET_DUPLICATED")

        updated_device = update_device(admin, device["id"], {"firmware": "edge-0.2.0", "replacementReason": "lab swap"})
        self.assertEqual(updated_device["firmware"], "edge-0.2.0")
        self.assertEqual(updated_device["replacementHistory"][0]["reason"], "lab swap")

        self.assertEqual(get_site(site["id"])["onlineDevices"], 1)
        update_device(admin, device["id"], {"health": "offline"})
        self.assertEqual(get_site(site["id"])["onlineDevices"], 0)

        quarantined = quarantine_unregistered_device({"deviceId": "DEV-UNKNOWN", "payload": {"rssi": -88}})
        self.assertEqual(quarantined["deviceId"], "DEV-UNKNOWN")

        with self.assertRaises(ApiError) as blocked_site_delete:
            delete_site(admin, site["id"])
        self.assertEqual(blocked_site_delete.exception.code, "SITE_HAS_ASSETS")

        with self.assertRaises(ApiError) as blocked_asset_delete:
            delete_asset(admin, site["id"], asset["id"])
        self.assertEqual(blocked_asset_delete.exception.code, "ASSET_HAS_DEVICE")

        self.assertTrue(delete_device(admin, device["id"])["deleted"])
        self.assertTrue(delete_device(admin, inactive_device["id"])["deleted"])
        self.assertTrue(delete_asset(admin, site["id"], asset["id"])["deleted"])
        self.assertTrue(delete_asset(admin, site["id"], second_asset["id"])["deleted"])
        self.assertTrue(delete_site(admin, site["id"])["deleted"])

    def test_numeric_crud_fields_return_400_for_invalid_input(self) -> None:
        admin = self.admin_user()

        with self.assertRaises(ApiError) as invalid_site_number:
            create_site(
                admin,
                {
                    "id": "SITE-NUM",
                    "code": "TEST-NUM",
                    "name": "Bad Number Plant",
                    "networkType": "D",
                    "signalQuality": "not-a-number",
                },
            )
        self.assertEqual(invalid_site_number.exception.status, 400)
        self.assertEqual(invalid_site_number.exception.code, "INVALID_NUMBER")

        with self.assertRaises(ApiError) as invalid_asset_number:
            create_asset(
                admin,
                "SITE-01",
                {
                    "assetCode": "BAD-NUM",
                    "name": "Bad Number Asset",
                    "ratedRpm": "fast",
                },
            )
        self.assertEqual(invalid_asset_number.exception.status, 400)
        self.assertEqual(invalid_asset_number.exception.code, "INVALID_NUMBER")

    def test_failed_updates_do_not_partially_mutate_state(self) -> None:
        admin = self.admin_user()

        original_site = get_site("SITE-01").copy()
        with self.assertRaises(ApiError) as invalid_site_update:
            update_site(admin, "SITE-01", {"name": "Should Not Persist", "signalQuality": "bad"})
        self.assertEqual(invalid_site_update.exception.code, "INVALID_NUMBER")
        self.assertEqual(get_site("SITE-01")["name"], original_site["name"])
        self.assertEqual(get_site("SITE-01")["signalQuality"], original_site["signalQuality"])

        original_asset = get_asset("SITE-01", "SITE-01-MOT-02").copy()
        with self.assertRaises(ApiError) as invalid_asset_update:
            update_asset(
                admin,
                "SITE-01",
                "SITE-01-MOT-02",
                {"name": "Should Not Persist", "ratedRpm": "fast"},
            )
        self.assertEqual(invalid_asset_update.exception.code, "INVALID_NUMBER")
        self.assertEqual(get_asset("SITE-01", "SITE-01-MOT-02")["name"], original_asset["name"])
        self.assertEqual(get_asset("SITE-01", "SITE-01-MOT-02")["ratedRpm"], original_asset["ratedRpm"])

        site = create_site(admin, {"id": "SITE-ATOM", "code": "TEST-ATOM", "name": "Atomic Plant"})
        first_asset = create_asset(admin, site["id"], {"assetCode": "MOT-01", "name": "First Motor"})
        second_asset = create_asset(admin, site["id"], {"assetCode": "MOT-02", "name": "Second Motor"})
        device = create_device(admin, site["id"], {"id": "DEV-ATOM", "assetId": first_asset["id"]})
        original_device = get_device(device["id"]).copy()
        original_history_count = len(original_device["mappingHistory"])
        original_online_count = get_site(site["id"])["onlineDevices"]

        with self.assertRaises(ApiError) as invalid_device_update:
            update_device(
                admin,
                device["id"],
                {
                    "assetId": second_asset["id"],
                    "health": "offline",
                    "lastSeenSecAgo": "late",
                },
            )
        self.assertEqual(invalid_device_update.exception.code, "INVALID_NUMBER")
        current_device = get_device(device["id"])
        self.assertEqual(current_device["assetId"], original_device["assetId"])
        self.assertEqual(current_device["health"], original_device["health"])
        self.assertEqual(len(current_device["mappingHistory"]), original_history_count)
        self.assertEqual(get_site(site["id"])["onlineDevices"], original_online_count)

    def test_unknown_site_is_not_replaced_by_first_site(self) -> None:
        with self.assertRaises(ApiError) as context:
            get_site("SITE-999")

        self.assertEqual(context.exception.status, 404)
        self.assertEqual(context.exception.code, "SITE_NOT_FOUND")

    def test_telemetry_contains_units_and_72_points(self) -> None:
        points = telemetry_for("SITE-01", "SITE-01-MOT-02")
        units = telemetry_units()

        self.assertEqual(len(points), 72)
        self.assertEqual(units["vibrationRmsMmS"], "mm/s RMS")
        self.assertLessEqual({"vibrationRmsMmS", "acousticDb", "anomalyScore"}, set(points[0]))
        self.assertTrue(all(0 <= point["anomalyScore"] <= 100 for point in points))

    def test_error_response_shape_is_safe_json(self) -> None:
        handler = DummyHandler()

        handler.send_error_json(ApiError(404, "SITE_NOT_FOUND", "Site was not found."))

        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(handler._status, 404)
        self.assertEqual(body, {"error": {"code": "SITE_NOT_FOUND", "message": "Site was not found."}})
        self.assertFalse(any("traceback" in str(value).lower() for value in body["error"].values()))
