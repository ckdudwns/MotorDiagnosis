"""Allowlisted device configuration, authentication, durability and HTTP contract."""

from __future__ import annotations

import http.client
import json
import os
import runpy
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from motor_diagnosis import data, remote_config as config
from motor_diagnosis.server import create_server
from tests.auth_fixtures import credentials, install_production_auth

DEVICE = "DEV-01-MOT-02"
TOKEN = "fixture-config-device-one-" + "A" * 32
OTHER_TOKEN = "fixture-config-device-two-" + "B" * 32
FIXTURE = os.environ.get("IOT_CONFIG_FIXTURE_EXE", "")


class ConfigSetup(unittest.TestCase):
    def setUp(self):
        install_production_auth(self)
        data.close_runtime_state()
        data.reset_runtime_state()
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DEVICE_CONFIG_TOKENS_JSON": json.dumps(
                    {DEVICE: TOKEN, "DEV-01-GEN-01": OTHER_TOKEN}
                ),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.users = {}
        self.tokens = {}
        for username in ("operator", "admin", "system"):
            login = data.authenticate(credentials(username))
            self.tokens[username] = login["session"]["token"]
            self.users[username] = data.current_user_for_token(self.tokens[username])
        self.admin = self.users["admin"]

    def tearDown(self):
        data.close_runtime_state()
        data.reset_runtime_state()

    def request_payload(self, version=0, **settings):
        return {
            "expectedVersion": version,
            "settings": {**config.DEFAULT_SETTINGS, **settings},
            "reason": "Remote configuration fixture",
        }

    def issue(self, version=0, **settings):
        return config.request_configuration(
            self.admin, DEVICE, self.request_payload(version, **settings)
        )

    def result(self, command, status="applied", **changes):
        return {
            "version": command["version"],
            "commandId": command["commandId"],
            "status": status,
            "settings": command["settings"] if status == "applied" else None,
            "errorCode": None if status == "applied" else "storage_failure",
            **changes,
        }

    def assertApiError(self, code, function, *args):
        with self.assertRaises(data.ApiError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)

    def register_target(self, device_id, site_id, asset_id):
        """Use the public registration path, not direct edits of model globals."""
        data.create_site(self.admin, {"id": site_id, "name": "ID contract site"})
        data.create_asset(
            self.admin,
            site_id,
            {
                "id": asset_id,
                "assetCode": "MOT.01",
                "name": "ID contract motor",
                "ratedRpm": 1500,
                "installLocation": "Contract test bay",
                "baseline": {
                    "status": "ready",
                    "capturedAt": "2026-09-06T00:00:00Z",
                    "vibrationRmsMmS": 1.0,
                    "acousticDb": 50.0,
                    "sampleCount": 10,
                },
            },
        )
        data.update_rollout_plan(
            self.admin,
            site_id,
            {
                "networkProfileId": "A",
                "targetAssetIds": [asset_id],
                "installPriority": "high",
                "configurationType": "direct",
                "gatewayRequired": False,
                "note": "ID contract fixture",
            },
        )
        return data.create_device(
            self.admin,
            site_id,
            {
                "id": device_id,
                "assetId": asset_id,
                "certificateId": "CERT-CONTRACT",
                "certificateFingerprint": "fingerprint-contract",
                "certificateIssuedAt": "2026-09-06T00:00:00Z",
                "certificateExpiresAt": "2099-09-06T00:00:00Z",
            },
        )


class RemoteConfigTest(ConfigSetup):
    def test_initial_read_does_not_create_or_claim_applied_configuration(self):
        view = config.configuration_for(self.admin, DEVICE)
        self.assertEqual(view["siteId"], data.get_device(DEVICE)["siteId"])
        self.assertEqual(view["assetId"], data.get_device(DEVICE)["assetId"])
        self.assertEqual(view["version"], 0)
        self.assertIsNone(view["desired"])
        self.assertIsNone(view["lastApplied"])
        self.assertIsNone(config.pending_configuration(TOKEN, DEVICE)["desired"])
        self.assertEqual(data.DEVICE_CONFIGS, {})

    def test_publication_and_poll_are_not_application_acknowledgements(self):
        command = self.issue(measurementIntervalMs=6000, replayBatchSize=2)
        for _ in range(3):
            pending = config.pending_configuration(TOKEN, DEVICE)
            self.assertEqual(pending["desired"]["settings"], command["settings"])
            view = config.configuration_for(self.admin, DEVICE)
            self.assertEqual(view["desired"]["state"], "pending")
            self.assertIsNone(view["lastApplied"])
        self.assertEqual(len(data.AUDIT_LOGS), 1)

    def test_only_complete_integer_allowlist_in_conservative_bounds_is_accepted(self):
        invalid = [None, {}, [], {**config.DEFAULT_SETTINGS, "wifiPassword": "x"}]
        for key in config.DEFAULT_SETTINGS:
            for value in (None, True, "4", 4.0, -1, 0, 2**32, [], {}):
                invalid.append({**config.DEFAULT_SETTINGS, key: value})
        invalid += [
            {"measurementIntervalMs": 2999, "replayBatchSize": 4},
            {"measurementIntervalMs": 60001, "replayBatchSize": 4},
            {"measurementIntervalMs": 3000, "replayBatchSize": 5},
        ]
        for settings in invalid:
            with self.subTest(settings=settings):
                self.assertApiError(
                    "INVALID_DEVICE_CONFIG",
                    config.request_configuration,
                    self.admin,
                    DEVICE,
                    {**self.request_payload(), "settings": settings},
                )
        self.assertEqual(data.DEVICE_CONFIGS, {})
        self.assertEqual(data.AUDIT_LOGS, [])
        self.issue(measurementIntervalMs=60000, replayBatchSize=1)

    def test_request_requires_reason_and_expected_version(self):
        for key in ("reason", "expectedVersion", "settings"):
            payload = self.request_payload()
            del payload[key]
            self.assertApiError(
                "INVALID_DEVICE_CONFIG",
                config.request_configuration,
                self.admin,
                DEVICE,
                payload,
            )
        for value in (True, "0", 0.0, -1):
            self.assertApiError(
                "INVALID_DEVICE_CONFIG",
                config.request_configuration,
                self.admin,
                DEVICE,
                {**self.request_payload(), "expectedVersion": value},
            )

    def test_read_only_operator_cannot_publish_and_site_scope_is_enforced(self):
        with self.assertRaises(data.ApiError) as error:
            config.request_configuration(
                self.users["operator"], DEVICE, self.request_payload()
            )
        self.assertEqual(error.exception.status, 403)
        denied = {**self.admin, "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError) as error:
            config.configuration_for(denied, DEVICE)
        self.assertEqual(error.exception.status, 403)

    def test_tokens_are_device_specific_and_cannot_be_replaced_by_admin_or_ingest(self):
        for token in (
            "",
            self.tokens["system"],
            "demo-telemetry-ingest-token",
            "demo-device-health-token",
            "x\ud800",
        ):
            with self.subTest(token=token), self.assertRaises(data.ApiError) as error:
                config.pending_configuration(token, DEVICE)
            self.assertEqual(error.exception.status, 401)
        self.assertApiError(
            "DEVICE_CONFIG_FORBIDDEN", config.pending_configuration, OTHER_TOKEN, DEVICE
        )
        self.assertApiError(
            "DEVICE_CONFIG_FORBIDDEN",
            config.pending_configuration,
            TOKEN,
            "DEV-01-GEN-01",
        )

    def test_missing_or_ambiguous_provisioning_fails_closed_without_exposing_secrets(
        self,
    ):
        values = (
            "[]",
            "{",
            json.dumps({DEVICE: "short"}),
            json.dumps({DEVICE: TOKEN, "DEV-01-GEN-01": TOKEN}),
            '{"' + DEVICE + '":"' + TOKEN + '","' + DEVICE + '":"' + OTHER_TOKEN + '"}',
        )
        for value in values:
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"DEVICE_CONFIG_TOKENS_JSON": value}),
            ):
                with self.assertRaises(data.ApiError) as error:
                    config.pending_configuration(TOKEN, DEVICE)
                self.assertEqual(error.exception.status, 503)
                self.assertNotIn(TOKEN, error.exception.message)
        with patch.dict(os.environ, {"DEVICE_CONFIG_TOKENS_JSON": "{}"}):
            self.assertApiError(
                "AUTH_REQUIRED", config.pending_configuration, TOKEN, DEVICE
            )

    def test_inactive_revoked_and_remapped_devices_cannot_receive_old_commands(self):
        self.issue()
        original_view = config.configuration_for(self.admin, DEVICE)
        device = data.get_device(DEVICE)
        for field, value in (
            ("mappingStatus", "inactive"),
            ("certificateStatus", "revoked"),
        ):
            previous = device[field]
            device[field] = value
            self.assertApiError(
                "DEVICE_CONFIG_INACTIVE", config.pending_configuration, TOKEN, DEVICE
            )
            device[field] = previous
        device["assetId"] = "SITE-01-GEN-01"
        self.assertApiError(
            "DEVICE_CONFIG_SCOPE_CHANGED", config.pending_configuration, TOKEN, DEVICE
        )
        view = config.configuration_for(self.admin, DEVICE)
        self.assertTrue(view["scopeChanged"])
        self.assertEqual(view["siteId"], device["siteId"])
        self.assertEqual(view["assetId"], "SITE-01-GEN-01")
        self.assertIsNone(view["desired"])
        self.assertIsNone(view["lastApplied"])
        self.assertEqual(original_view["assetId"], original_view["desired"]["assetId"])
        self.assertNotEqual(original_view["assetId"], view["assetId"])
        self.assertEqual(config.configuration_history(self.admin, DEVICE)["items"], [])
        self.issue(1)
        self.assertEqual(
            config.pending_configuration(TOKEN, DEVICE)["assetId"], "SITE-01-GEN-01"
        )

    def test_concurrent_requests_use_compare_and_swap(self):
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                return self.issue()["version"]
            except data.ApiError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, [1, "DEVICE_CONFIG_VERSION_CONFLICT"])
        self.assertEqual(len(data.AUDIT_LOGS), 1)

    def test_old_report_cannot_overwrite_new_desired_or_applied_version(self):
        old = self.issue()
        new = self.issue(1, measurementIntervalMs=9000)
        self.assertApiError(
            "DEVICE_CONFIG_SUPERSEDED",
            config.report_configuration,
            TOKEN,
            DEVICE,
            self.result(old),
        )
        self.assertEqual(
            config.configuration_for(self.admin, DEVICE)["desired"]["state"], "pending"
        )
        config.report_configuration(TOKEN, DEVICE, self.result(new))
        self.assertEqual(
            config.configuration_for(self.admin, DEVICE)["lastApplied"]["version"], 2
        )

    def test_result_binds_version_command_and_settings_and_retries_are_idempotent(self):
        command = self.issue()
        self.assertApiError(
            "DEVICE_CONFIG_SUPERSEDED",
            config.report_configuration,
            TOKEN,
            DEVICE,
            self.result(command, commandId="f" * 32),
        )
        self.assertApiError(
            "DEVICE_CONFIG_RESULT_MISMATCH",
            config.report_configuration,
            TOKEN,
            DEVICE,
            self.result(
                command, settings={**command["settings"], "replayBatchSize": 1}
            ),
        )
        for _ in range(3):
            response = config.report_configuration(TOKEN, DEVICE, self.result(command))
            self.assertTrue(response["accepted"])
        self.assertEqual(len(data.AUDIT_LOGS), 2)
        self.assertEqual(
            config.configuration_for(self.admin, DEVICE)["desired"]["state"], "applied"
        )

    def test_storage_failure_can_recover_but_late_failure_cannot_downgrade_applied(
        self,
    ):
        command = self.issue()
        config.report_configuration(TOKEN, DEVICE, self.result(command, "failed"))
        self.assertIsNone(config.configuration_for(self.admin, DEVICE)["lastApplied"])
        config.report_configuration(TOKEN, DEVICE, self.result(command))
        self.assertApiError(
            "DEVICE_CONFIG_RESULT_STALE",
            config.report_configuration,
            TOKEN,
            DEVICE,
            self.result(command, "failed"),
        )

    def test_result_payload_validation_has_no_side_effect(self):
        command = self.issue()
        for change in (
            {"status": []},
            {"settings": None},
            {"version": True},
            {"errorCode": "storage_failure"},
            {"status": "failed"},
            {"status": "failed", "settings": None, "errorCode": "secret details"},
        ):
            with self.subTest(change=change), self.assertRaises(data.ApiError):
                config.report_configuration(
                    TOKEN, DEVICE, {**self.result(command), **change}
                )
        self.assertEqual(
            config.configuration_for(self.admin, DEVICE)["desired"]["state"], "pending"
        )

    def test_history_is_bounded_and_applied_and_pending_versions_are_distinct(self):
        command = self.issue()
        config.report_configuration(TOKEN, DEVICE, self.result(command))
        for version in range(1, 103):
            self.issue(version)
        history = config.configuration_history(self.admin, DEVICE)
        self.assertEqual(len(history["items"]), config.HISTORY_LIMIT)
        self.assertEqual(history["items"][0]["version"], 103)
        self.assertEqual(
            config.configuration_for(self.admin, DEVICE)["lastApplied"]["version"], 1
        )

    def test_configuration_history_prevents_device_id_reuse(self):
        self.issue()
        self.assertApiError(
            "DEVICE_HAS_CONFIG_HISTORY", data.delete_device, self.admin, DEVICE
        )

    def test_restart_preserves_desired_applied_and_audit_but_never_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "runtime.sqlite3")
            data.configure_runtime_state(database)
            command = self.issue()
            config.report_configuration(TOKEN, DEVICE, self.result(command))
            self.issue(1, replayBatchSize=2)
            before = config.configuration_for(self.admin, DEVICE)
            self.assertNotIn(TOKEN, json.dumps(data._runtime_state_payload()))
            data.close_runtime_state()
            data.reset_runtime_state()
            data.configure_runtime_state(database)
            try:
                self.assertEqual(config.configuration_for(self.admin, DEVICE), before)
                self.assertEqual(len(data.AUDIT_LOGS), 3)
                self.assertEqual(
                    config.pending_configuration(TOKEN, DEVICE)["desired"]["version"], 2
                )
            finally:
                data.close_runtime_state()

    def test_failed_storage_rolls_back_publication_and_applied_result(self):
        with tempfile.TemporaryDirectory() as directory:
            data.configure_runtime_state(str(Path(directory) / "runtime.sqlite3"))
            try:
                with patch.object(
                    data._RUNTIME_STATE_STORE,
                    "save",
                    side_effect=sqlite3.OperationalError("fixture lock"),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.issue()
                self.assertEqual(data.DEVICE_CONFIGS, {})
                command = self.issue()
                with patch.object(
                    data._RUNTIME_STATE_STORE,
                    "save",
                    side_effect=sqlite3.OperationalError("fixture lock"),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        config.report_configuration(TOKEN, DEVICE, self.result(command))
                self.assertEqual(
                    config.configuration_for(self.admin, DEVICE)["desired"]["state"],
                    "pending",
                )
                config.report_configuration(TOKEN, DEVICE, self.result(command))
            finally:
                data.close_runtime_state()

    def test_loading_pre_extension_state_clears_unrelated_in_memory_configuration(self):
        snapshot = data._runtime_state_payload()
        del snapshot["deviceConfigs"]
        self.issue()
        data._restore_runtime_state(snapshot)
        self.assertEqual(data.DEVICE_CONFIGS, {})

    def test_schema_upgrade_blocks_rollback_and_redeployment_keeps_generation(self):
        legacy_type = runpy.run_path(
            str(Path(__file__).parent / "fixtures" / "runtime_store_v1.py")
        )["RuntimeStateStore"]
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "runtime.sqlite3")
            command = self.issue()
            config.report_configuration(TOKEN, DEVICE, self.result(command))
            legacy = legacy_type(database)
            try:
                # PR25 originally wrote the new field with the old schema number.
                legacy.save(data._runtime_state_payload(), data.now_iso())
                data.reset_runtime_state()
                data.configure_runtime_state(database)
                before = config.configuration_for(self.admin, DEVICE)
                data.close_runtime_state()
                with self.assertRaisesRegex(
                    ValueError, "Unsupported runtime state schema 2"
                ):
                    legacy.load()
                # Even an already-running old writer cannot drop the field.
                old_checkpoint = data._runtime_state_payload()
                old_checkpoint.pop("deviceConfigs")
                with self.assertRaises(sqlite3.IntegrityError):
                    legacy.save(old_checkpoint, data.now_iso())
                data.reset_runtime_state()
                data.configure_runtime_state(database)
                self.assertEqual(config.configuration_for(self.admin, DEVICE), before)
                self.assertEqual(before["desired"]["commandId"], command["commandId"])
                self.assertEqual(before["lastApplied"]["version"], 1)
                self.assertEqual(self.issue(1)["version"], 2)
            finally:
                data.close_runtime_state()
                legacy.close()

    def test_unsupported_registered_ids_fail_before_configuration_publication(self):
        for field in ("deviceId", "siteId", "assetId"):
            for invalid in (
                "X" * 64,
                "BAD/ID",
                "BAD ID",
                "한글ID",
                "-LEADING",
                "BAD\x00ID",
            ):
                with self.subTest(field=field, value=invalid):
                    data.reset_runtime_state()
                    ids = {
                        "deviceId": "DEV-CONTRACT",
                        "siteId": "SITE.CONTRACT",
                        "assetId": "MOT.CONTRACT",
                    }
                    ids[field] = invalid
                    self.register_target(ids["deviceId"], ids["siteId"], ids["assetId"])
                    audit = data.copy_payload(data.AUDIT_LOGS)
                    self.assertApiError(
                        "DEVICE_CONFIG_UNSUPPORTED_ID",
                        config.request_configuration,
                        self.admin,
                        ids["deviceId"],
                        self.request_payload(),
                    )
                    self.assertEqual(data.DEVICE_CONFIGS, {})
                    self.assertEqual(data.AUDIT_LOGS, audit)


class RemoteConfigHttpTest(ConfigSetup):
    def setUp(self):
        super().setUp()
        self.server = create_server(
            "127.0.0.1", 0, auto_alerts=False, demo_enabled=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        super().tearDown()

    def http(self, suffix="", *, method="GET", body=None, token=None, device_id=DEVICE):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(
                method,
                f"/api/devices/{device_id}/configuration{suffix}",
                json.dumps(body) if body is not None else None,
                {
                    "Authorization": "Bearer "
                    + (self.tokens["admin"] if token is None else token),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_routes_separate_human_publication_from_device_poll_and_result(self):
        status, command = self.http(
            method="PUT", body=self.request_payload(measurementIntervalMs=6000)
        )
        self.assertEqual(status, 200, command)
        self.assertEqual(self.http("/pending")[0], 401)
        status, pending = self.http("/pending", token=TOKEN)
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["desired"]["commandId"], command["commandId"])
        self.assertEqual(
            self.http("/result", method="POST", body=self.result(command))[0], 401
        )
        status, report = self.http(
            "/result", method="POST", body=self.result(command), token=TOKEN
        )
        self.assertEqual(status, 200, report)
        self.assertTrue(report["accepted"])
        self.assertEqual(self.http()[1]["lastApplied"]["version"], 1)
        self.assertEqual(len(self.http("/history")[1]["items"]), 1)

    def test_read_only_write_and_stale_edit_are_rejected_over_http(self):
        self.assertEqual(
            self.http(
                method="PUT", body=self.request_payload(), token=self.tokens["operator"]
            )[0],
            403,
        )
        self.assertEqual(self.http(method="PUT", body=self.request_payload())[0], 200)
        status, error = self.http(method="PUT", body=self.request_payload())
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "DEVICE_CONFIG_VERSION_CONFLICT")

    @unittest.skipUnless(
        FIXTURE and Path(FIXTURE).is_file(),
        "Build native remote-config fixture and set IOT_CONFIG_FIXTURE_EXE",
    )
    def test_real_backend_command_native_cpp_apply_reboot_and_result_contract(self):
        self.http(
            method="PUT",
            body=self.request_payload(
                measurementIntervalMs=7000,
                replayBatchSize=2,
                healthReportIntervalMs=120000,
            ),
        )
        status, pending = self.http("/pending", token=TOKEN)
        self.assertEqual(status, 200)
        completed = subprocess.run(
            [FIXTURE, "--roundtrip-json"],
            input=json.dumps(pending),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        applied, rebooted = map(json.loads, completed.stdout.splitlines())
        self.assertEqual(applied, rebooted)
        status, result = self.http("/result", method="POST", body=rebooted, token=TOKEN)
        self.assertEqual(status, 200, result)
        self.assertEqual(
            self.http()[1]["lastApplied"]["settings"],
            {
                "measurementIntervalMs": 7000,
                "replayBatchSize": 2,
                "healthReportIntervalMs": 120000,
            },
        )

    @unittest.skipUnless(
        FIXTURE and Path(FIXTURE).is_file(),
        "Build native remote-config fixture and set IOT_CONFIG_FIXTURE_EXE",
    )
    def test_registered_id_boundaries_roundtrip_through_native_cpp_and_reboot(self):
        for device_id, site_id, asset_id in (
            ("DEV.DOT_01", "SITE.DOT", "SITE.DOT-MOT.01"),
            ("D", "S", "A"),
            ("D" + "._-9" * 15 + "ZZ", "S" + "." * 62, "A" + "_" * 62),
        ):
            with self.subTest(ids=(device_id, site_id, asset_id)):
                self.register_target(device_id, site_id, asset_id)
                with patch.dict(
                    os.environ,
                    {"DEVICE_CONFIG_TOKENS_JSON": json.dumps({device_id: TOKEN})},
                ):
                    status, command = self.http(
                        method="PUT", body=self.request_payload(), device_id=device_id
                    )
                    self.assertEqual(status, 200, command)
                    status, pending = self.http(
                        "/pending", token=TOKEN, device_id=device_id
                    )
                    self.assertEqual(status, 200, pending)
                    completed = subprocess.run(
                        [FIXTURE, "--roundtrip-json", device_id, site_id, asset_id],
                        input=json.dumps(pending),
                        text=True,
                        capture_output=True,
                        check=True,
                        timeout=10,
                    )
                    applied, rebooted = map(json.loads, completed.stdout.splitlines())
                    self.assertEqual(applied, rebooted)
                    status, result = self.http(
                        "/result",
                        method="POST",
                        body=rebooted,
                        token=TOKEN,
                        device_id=device_id,
                    )
                    self.assertEqual(status, 200, result)
                    self.assertEqual(
                        self.http(device_id=device_id)[1]["lastApplied"]["version"], 1
                    )


class HealthIntervalContractTest(ConfigSetup):
    def test_v2_health_value_survives_application_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            data.configure_runtime_state(path)
            command = self.issue(healthReportIntervalMs=120000)
            self.assertEqual(
                config.pending_configuration(TOKEN, DEVICE)["schemaVersion"], 2
            )
            config.report_configuration(TOKEN, DEVICE, self.result(command))
            data.close_runtime_state()
            data.configure_runtime_state(path)
            restored = config.pending_configuration(TOKEN, DEVICE)
            self.assertEqual(
                restored["desired"]["settings"]["healthReportIntervalMs"], 120000
            )
            self.assertEqual(restored["desired"]["commandId"], command["commandId"])
            data.close_runtime_state()

    def test_legacy_command_and_report_do_not_invent_health_interval(self):
        payload = self.request_payload()
        del payload["settings"]["healthReportIntervalMs"]
        command = config.request_configuration(self.admin, DEVICE, payload)
        self.assertEqual(
            config.pending_configuration(TOKEN, DEVICE)["schemaVersion"], 1
        )
        config.report_configuration(TOKEN, DEVICE, self.result(command))
        with self.assertRaises(data.ApiError):
            config.report_configuration(
                TOKEN,
                DEVICE,
                self.result(
                    command,
                    settings={**command["settings"], "healthReportIntervalMs": 30000},
                ),
            )

    def test_health_bounds_reject_without_advancing_version(self):
        for value in (9999, 300001, True, None, 30000.5, "30000"):
            with self.subTest(value=value), self.assertRaises(data.ApiError):
                self.issue(healthReportIntervalMs=value)
        self.assertIsNone(config.pending_configuration(TOKEN, DEVICE)["desired"])


if __name__ == "__main__":
    unittest.main()
