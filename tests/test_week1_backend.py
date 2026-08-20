from __future__ import annotations

import json
import unittest
from io import BytesIO

from motor_diagnosis.data import (
    ApiError,
    authenticate,
    create_asset,
    create_device,
    create_install_point,
    create_site,
    current_user_for_token,
    delete_asset,
    delete_device,
    delete_site,
    get_asset,
    get_device,
    get_site,
    install_points_for_asset,
    logout,
    quarantine_unregistered_device,
    require_site_access,
    reset_runtime_state,
    rollout_plan_for,
    telemetry_for,
    telemetry_units,
    update_asset,
    update_device,
    update_install_point,
    update_rollout_plan,
    update_site,
    update_site_network_profile,
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

    def ready_asset_payload(self, asset_code: str, name: str, rated_rpm: int = 1500) -> dict:
        return {
            "assetCode": asset_code,
            "name": name,
            "assetType": "motor",
            "ratedRpm": rated_rpm,
            "installLocation": f"Bay {asset_code}",
            "baseline": {
                "status": "ready",
                "capturedAt": "2026-08-13T10:00:00+09:00",
                "vibrationRmsMmS": 1.21,
                "acousticDb": 51.4,
                "sampleCount": 180,
            },
        }

    def configure_rollout(self, admin: dict, site_id: str, asset_ids: list[str]) -> dict:
        return update_rollout_plan(
            admin,
            site_id,
            {
                "networkProfileId": "A",
                "targetAssetIds": asset_ids,
                "installPriority": "high",
                "configurationType": "direct",
                "gatewayRequired": False,
                "note": "week1 test rollout",
            },
        )

    def certificate_fields(self, suffix: str) -> dict:
        return {
            "certificateId": f"CERT-{suffix}",
            "certificateFingerprint": f"fingerprint-{suffix}",
            "certificateIssuedAt": "2026-08-13T00:00:00Z",
            "certificateExpiresAt": "2027-08-13T00:00:00Z",
        }

    def test_login_returns_server_side_session_and_role_policy(self) -> None:
        first = authenticate({"username": "admin", "password": "admin123"})
        second = authenticate({"username": "admin", "password": "admin123"})

        self.assertEqual(first["user"]["role"], "B")
        self.assertTrue(first["session"]["token"].startswith("demo-"))
        self.assertNotEqual(first["session"]["token"], second["session"]["token"])
        self.assertIn("site:write", first["rolePolicy"]["permissions"])
        self.assertEqual(current_user_for_token(first["session"]["token"])["username"], "admin")

        self.assertEqual(logout(first["session"]["token"]), {"loggedOut": True})
        with self.assertRaises(ApiError) as logged_out_context:
            current_user_for_token(first["session"]["token"])
        self.assertEqual(logged_out_context.exception.code, "INVALID_SESSION")

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
            self.ready_asset_payload("MOT-99", "Test Motor"),
        )
        self.assertEqual(asset["baseline"]["vibrationRmsMmS"], 1.21)

        with self.assertRaises(ApiError) as duplicated_asset:
            create_asset(
                admin,
                site["id"],
                {"id": "SITE-T01-MOT-100", "assetCode": "MOT-99", "name": "Duplicate Motor"},
            )
        self.assertEqual(duplicated_asset.exception.code, "ASSET_CODE_DUPLICATED")

        self.configure_rollout(admin, site["id"], [asset["id"]])

        device = create_device(
            admin,
            site["id"],
            {
                "id": "DEV-T01",
                "assetId": asset["id"],
                "firmware": "edge-0.1.1",
                **self.certificate_fields("T01"),
            },
        )
        self.assertEqual(device["mappingHistory"][0]["assetId"], asset["id"])

        with self.assertRaises(ApiError) as duplicated_mapping:
            create_device(admin, site["id"], {"id": "DEV-T02", "assetId": asset["id"]})
        self.assertEqual(duplicated_mapping.exception.code, "DEVICE_ASSET_DUPLICATED")

        second_asset = create_asset(
            admin,
            site["id"],
            self.ready_asset_payload("MOT-100", "Second Test Motor", 1450),
        )
        self.configure_rollout(admin, site["id"], [asset["id"], second_asset["id"]])
        inactive_device = create_device(
            admin,
            site["id"],
            {
                "id": "DEV-T02",
                "assetId": second_asset["id"],
                "mappingStatus": "inactive",
                "health": "offline",
                **self.certificate_fields("T02"),
            },
        )
        with self.assertRaises(ApiError) as duplicated_activation:
            update_device(admin, inactive_device["id"], {"assetId": asset["id"], "mappingStatus": "active"})
        self.assertEqual(duplicated_activation.exception.code, "DEVICE_ASSET_DUPLICATED")

        updated_device = update_device(admin, device["id"], {"firmware": "edge-0.2.0", "replacementReason": "lab swap"})
        self.assertEqual(updated_device["firmware"], "edge-0.2.0")
        self.assertEqual(updated_device["replacementHistory"][0]["reason"], "lab swap")
        self.assertEqual(updated_device["replacementHistory"][0]["before"]["firmwareVersion"], "edge-0.1.1")
        self.assertEqual(updated_device["replacementHistory"][0]["after"]["firmwareVersion"], "edge-0.2.0")

        certificate_updated = update_device(
            admin,
            device["id"],
            {
                "certificateId": "CERT-T01-ROTATED",
                "certificateFingerprint": "fingerprint-t01-rotated",
                "certificateStatus": "registered",
                "reason": "scheduled certificate rotation",
            },
        )
        self.assertEqual(certificate_updated["certificateId"], "CERT-T01-ROTATED")
        self.assertEqual(certificate_updated["certificateHistory"][0]["before"]["id"], "CERT-T01")
        self.assertEqual(certificate_updated["certificateHistory"][0]["after"]["id"], "CERT-T01-ROTATED")

        with self.assertRaises(ApiError) as revoked_active_update:
            update_device(
                admin,
                device["id"],
                {"certificateStatus": "revoked", "reason": "certificate compromise"},
            )
        self.assertEqual(revoked_active_update.exception.code, "CERTIFICATE_NOT_ACTIVE")
        self.assertEqual(get_device(device["id"])["certificateStatus"], "registered")

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

        for index, non_finite in enumerate((float("nan"), float("inf"), float("-inf")), 1):
            with self.subTest(non_finite=non_finite):
                with self.assertRaises(ApiError) as invalid_coordinate:
                    create_site(
                        admin,
                        {
                            "id": f"SITE-NON-FINITE-{index}",
                            "code": f"NON-FINITE-{index}",
                            "name": "Invalid Coordinate Plant",
                            "latitude": non_finite,
                        },
                    )
                self.assertEqual(invalid_coordinate.exception.status, 400)
                self.assertEqual(invalid_coordinate.exception.code, "INVALID_NUMBER")

        with self.assertRaises(ApiError) as invalid_baseline:
            create_asset(
                admin,
                "SITE-01",
                {
                    "assetCode": "NON-FINITE",
                    "name": "Invalid Baseline Asset",
                    "baselineVibrationRmsMmS": float("nan"),
                },
            )
        self.assertEqual(invalid_baseline.exception.status, 400)
        self.assertEqual(invalid_baseline.exception.code, "INVALID_NUMBER")

    def test_device_mapping_requires_ready_asset_rollout_and_certificate(self) -> None:
        admin = self.admin_user()
        site = create_site(admin, {"id": "SITE-READY", "code": "READY", "name": "Ready Check Plant"})
        asset = create_asset(admin, site["id"], {"assetCode": "MOT-01", "name": "Draft Motor"})
        self.configure_rollout(admin, site["id"], [asset["id"]])

        with self.assertRaises(ApiError) as not_ready:
            create_device(
                admin,
                site["id"],
                {"id": "DEV-READY", "assetId": asset["id"], **self.certificate_fields("READY")},
            )
        self.assertEqual(not_ready.exception.status, 409)
        self.assertEqual(not_ready.exception.code, "ASSET_NOT_READY_FOR_MAPPING")

        update_asset(admin, site["id"], asset["id"], self.ready_asset_payload("MOT-01", "Ready Motor"))
        with self.assertRaises(ApiError) as missing_certificate:
            create_device(admin, site["id"], {"id": "DEV-READY", "assetId": asset["id"]})
        self.assertEqual(missing_certificate.exception.code, "DEVICE_CERTIFICATE_REQUIRED")

        for certificate_status in ("revoked", "expired", "pending"):
            with self.subTest(certificate_status=certificate_status):
                with self.assertRaises(ApiError) as inactive_certificate:
                    create_device(
                        admin,
                        site["id"],
                        {
                            "id": f"DEV-{certificate_status.upper()}",
                            "assetId": asset["id"],
                            "certificateStatus": certificate_status,
                            **self.certificate_fields(certificate_status.upper()),
                        },
                    )
                self.assertEqual(inactive_certificate.exception.status, 409)
                self.assertEqual(inactive_certificate.exception.code, "CERTIFICATE_NOT_ACTIVE")

        with self.assertRaises(ApiError) as expired_certificate:
            create_device(
                admin,
                site["id"],
                {
                    "id": "DEV-EXPIRED-DATE",
                    "assetId": asset["id"],
                    **self.certificate_fields("EXPIRED-DATE"),
                    "certificateExpiresAt": "2000-01-01T00:00:00Z",
                },
            )
        self.assertEqual(expired_certificate.exception.status, 409)
        self.assertEqual(expired_certificate.exception.code, "CERTIFICATE_EXPIRED")

        inactive_device = create_device(
            admin,
            site["id"],
            {
                "id": "DEV-REVOKED-INACTIVE",
                "assetId": asset["id"],
                "mappingStatus": "inactive",
                "certificateStatus": "revoked",
                **self.certificate_fields("REVOKED-INACTIVE"),
            },
        )
        self.assertEqual(inactive_device["certificateStatus"], "revoked")

    def test_rollout_network_and_install_point_changes_are_persisted_with_history(self) -> None:
        admin = self.admin_user()
        site = create_site(admin, {"id": "SITE-PLAN", "code": "PLAN", "name": "Plan Plant"})
        asset = create_asset(admin, site["id"], self.ready_asset_payload("MOT-01", "Plan Motor"))

        rollout = self.configure_rollout(admin, site["id"], [asset["id"]])
        self.assertEqual(rollout["targetAssetIds"], [asset["id"]])
        self.assertTrue(rollout["installationReady"])

        site_network = update_site_network_profile(
            admin,
            site["id"],
            {
                "networkProfileId": "B",
                "grade": "B",
                "directSend": False,
                "gateway": True,
                "offlineSync": True,
                "reason": "Field survey confirmed gateway relay",
            },
        )
        self.assertEqual(site_network["networkProfileId"], "B")
        self.assertTrue(site_network["gateway"])

        install_point = create_install_point(
            admin,
            asset["id"],
            {
                "position": "drive-end bearing housing",
                "orientation": "horizontal X",
                "mountingMethod": "bolt fixed bracket",
                "acousticDirection": "motor cooling fan",
                "ambientNoiseSources": ["adjacent pump"],
                "photoRefs": ["survey://SITE-PLAN/MOT-01/front.jpg"],
            },
        )
        updated = update_install_point(
            admin,
            asset["id"],
            install_point["id"],
            {"orientation": "vertical Z", "reason": "Sensor axis corrected after field verification"},
        )
        self.assertEqual(updated["orientation"], "vertical Z")
        self.assertEqual(updated["changeHistory"][0]["before"]["orientation"], "horizontal X")
        self.assertEqual(updated["changeHistory"][0]["after"]["orientation"], "vertical Z")
        self.assertEqual(len(install_points_for_asset(asset["id"])), 1)

    def test_rollout_configuration_follows_network_profile(self) -> None:
        admin = self.admin_user()

        initial_c = rollout_plan_for("SITE-03")
        initial_d = rollout_plan_for("SITE-07")
        self.assertEqual(initial_c["configurationType"], "store_and_forward")
        self.assertFalse(initial_c["gatewayRequired"])
        self.assertEqual(initial_d["configurationType"], "offline")
        self.assertFalse(initial_d["gatewayRequired"])

        update_site(admin, "SITE-01", {"networkType": "B"})
        profile_b = rollout_plan_for("SITE-01")
        self.assertEqual(profile_b["networkProfileId"], "B")
        self.assertEqual(profile_b["configurationType"], "gateway")
        self.assertTrue(profile_b["gatewayRequired"])

        update_site_network_profile(
            admin,
            "SITE-01",
            {
                "networkProfileId": "C",
                "grade": "C",
                "directSend": False,
                "gateway": False,
                "offlineSync": True,
                "reason": "Bandwidth-limited field network",
            },
        )
        profile_c = rollout_plan_for("SITE-01")
        self.assertEqual(profile_c["networkProfileId"], "C")
        self.assertEqual(profile_c["configurationType"], "store_and_forward")
        self.assertFalse(profile_c["gatewayRequired"])

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
        first_asset = create_asset(admin, site["id"], self.ready_asset_payload("MOT-01", "First Motor"))
        second_asset = create_asset(admin, site["id"], self.ready_asset_payload("MOT-02", "Second Motor"))
        self.configure_rollout(admin, site["id"], [first_asset["id"], second_asset["id"]])
        device = create_device(
            admin,
            site["id"],
            {"id": "DEV-ATOM", "assetId": first_asset["id"], **self.certificate_fields("ATOM")},
        )
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
