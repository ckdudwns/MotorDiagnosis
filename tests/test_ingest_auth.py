"""Production machine-auth boundaries; isolated files and loopback HTTP only."""

import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from motor_diagnosis import auth_config, data, ingest_auth, mqtt_service
from motor_diagnosis.server import create_server
from tests.auth_fixtures import credentials, install_production_auth
from tests.test_week2_backend import telemetry_payload


class IngestAuthTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.addCleanup(data.reset_runtime_state)
        self.env = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "INGEST_TOKENS_FILE": "",
                "DEVICE_HEALTH_TOKEN": "",
                "MQTT_INGEST_TOKEN": "",
                "MQTT_INGEST_TOKEN_FILE": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_production_auth(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "ingest-tokens.json"
        self.device = "ingest-" + "a" * 43
        self.mqtt = "ingest-" + "b" * 43
        self.rows = [
            {
                "id": "esp32",
                "kind": "device",
                "deviceIds": ["DEV-01-GEN-01"],
                "tokenSha256": ingest_auth.token_hash(self.device),
            },
            {
                "id": "mqtt",
                "kind": "mqtt",
                "deviceIds": ["DEV-01-GEN-01"],
                "tokenSha256": ingest_auth.token_hash(self.mqtt),
            },
        ]

    def save(self, rows=None):
        auth_config.write_private_json(
            self.path,
            {
                "schemaVersion": 1,
                "credentials": self.rows if rows is None else rows,
            },
            create=not self.path.exists(),
        )
        os.environ["INGEST_TOKENS_FILE"] = str(self.path)

    def error(self, status, function, *args):
        with self.assertRaises(data.ApiError) as raised:
            function(*args)
        self.assertEqual(raised.exception.status, status)
        return raised.exception

    def start(self):
        self.server = create_server(
            "127.0.0.1", 0, demo_enabled=False, auto_alerts=False
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)

        self.addCleanup(close)

    def request(self, path, token, payload=None, method="POST"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(
                method,
                path,
                None if method == "GET" else json.dumps(payload or {}),
                {
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_all_factory_tokens_blocked_even_with_demo_flag_or_health_collision(self):
        for environment in ("production", " Production ", "PRODUCTION"):
            for token in data.TELEMETRY_SERVICE_TOKENS:
                with (
                    self.subTest(environment=environment, token=token),
                    patch.dict(
                        os.environ,
                        {
                            "APP_ENV": environment,
                            "DEMO_ENABLED": "true",
                            "DEVICE_HEALTH_TOKEN": token,
                        },
                    ),
                ):
                    self.error(401, data.telemetry_principal_for_token, token)

    def test_development_factory_tokens_remain_available(self):
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            for token, expected in data.TELEMETRY_SERVICE_TOKENS.items():
                self.assertEqual(data.telemetry_principal_for_token(token), expected)

    def test_machine_auth_unset_allows_operator_startup_but_no_machine_key(self):
        self.start()
        self.assertEqual(self.request("/api/health", "", method="GET")[0], 200)
        self.error(401, data.telemetry_principal_for_token, self.device)
        login = data.authenticate(credentials("system"))
        self.assertEqual(
            data.telemetry_principal_for_token(login["session"]["token"])["type"],
            "user",
        )

    def test_configured_missing_file_blocks_start_before_opening_database(self):
        os.environ["INGEST_TOKENS_FILE"] = str(self.path)
        with patch("motor_diagnosis.server.configure_runtime_state") as configure:
            self.assertEqual(
                self.error(503, create_server, "127.0.0.1", 0).code,
                "INGEST_AUTH_UNAVAILABLE",
            )
            configure.assert_not_called()

    def test_device_scope_and_permissions_are_fixed_and_no_hash_is_returned(self):
        self.save()
        principal = data.telemetry_principal_for_token(self.device)
        self.assertEqual(principal["allowedDeviceIds"], ["DEV-01-GEN-01"])
        self.assertEqual(
            principal["permissions"], ["telemetry:ingest", "device-health:write"]
        )
        self.assertNotIn("token", json.dumps(principal).lower())
        data.principal_can_ingest(principal, "DEV-01-GEN-01")
        self.error(403, data.principal_can_ingest, principal, "DEV-01-MOT-02")
        self.error(403, data.update_device_health, principal, "DEV-01-MOT-02", {})
        self.error(
            403,
            data.principal_can_submit_telemetry_labels,
            principal,
            {"scenarioLabel": "normal"},
        )
        self.error(
            403,
            data.report_service_dependency,
            principal,
            "mqtt",
            {"status": "healthy"},
        )

    def test_mqtt_quarantine_enforces_topic_scope_before_any_storage(self):
        self.save()
        principal = data.telemetry_principal_for_token(self.mqtt)
        for topic in (
            "devices/DEV-01-MOT-02/telemetry",
            "unknown",
            "/devices/DEV-01-GEN-01/telemetry",
            "devices/dev-01-gen-01/telemetry",
            "devices/DEV-01-GEN-01//telemetry",
        ):
            with self.subTest(topic=topic):
                self.error(
                    403,
                    data.quarantine_mqtt_message,
                    principal,
                    {
                        "topic": topic,
                        "reason": "INVALID_JSON",
                        "message": "invalid",
                        "payload": "{",
                    },
                )
        self.assertEqual(data.QUARANTINED_DEVICE_MESSAGES, [])
        self.error(
            403,
            data.report_service_dependency,
            principal,
            "smtp",
            {"status": "healthy"},
        )
        self.error(403, data.update_device_health, principal, "DEV-01-GEN-01", {})

    def test_revocation_rotation_and_scope_changes_are_read_without_restart(self):
        self.save()
        data.telemetry_principal_for_token(self.device)
        self.rows[0]["deviceIds"] = ["DEV-01-MOT-02"]
        self.save()
        self.assertEqual(
            data.telemetry_principal_for_token(self.device)["allowedDeviceIds"],
            ["DEV-01-MOT-02"],
        )
        replacement = "ingest-" + "c" * 43
        self.rows[0]["tokenSha256"] = ingest_auth.token_hash(replacement)
        self.save()
        self.error(401, data.telemetry_principal_for_token, self.device)
        data.telemetry_principal_for_token(replacement)
        self.save([])
        with patch.dict(os.environ, {"DEVICE_HEALTH_TOKEN": replacement}):
            self.error(401, data.telemetry_principal_for_token, replacement)

    def test_corrupt_or_lost_configuration_fails_closed_without_leaking_contents(self):
        self.save()
        self.path.write_text('{"private":"do-not-echo-this",', encoding="utf-8")
        for token in (self.device, "invalid", "한글"):
            error = self.error(503, data.telemetry_principal_for_token, token)
            self.assertNotIn("do-not-echo-this", str(error))
        self.path.unlink()
        self.error(503, data.telemetry_principal_for_token, self.device)

    def test_schema_disallows_permission_injection_wildcards_duplicates_and_plaintext(
        self,
    ):
        invalid = [
            {"schemaVersion": True, "credentials": []},
            {
                "schemaVersion": 1,
                "credentials": [dict(self.rows[0], permissions=["*"])],
            },
            {
                "schemaVersion": 1,
                "credentials": [dict(self.rows[0], token=self.device)],
            },
        ]
        for key, value in (
            ("kind", "admin"),
            ("kind", []),
            ("deviceIds", ["*"]),
            ("deviceIds", ["DEV-01-GEN-01", "DEV-01-MOT-02"]),
            ("deviceIds", []),
            ("deviceIds", [{}]),
            ("tokenSha256", "bad"),
            ("id", ""),
        ):
            invalid.append(
                {
                    "schemaVersion": 1,
                    "credentials": [dict(self.rows[0], **{key: value})],
                }
            )
        invalid.extend(
            [
                {"schemaVersion": 1, "credentials": [self.rows[0], self.rows[0]]},
                {
                    "schemaVersion": 1,
                    "credentials": [
                        self.rows[0],
                        dict(self.rows[1], tokenSha256=self.rows[0]["tokenSha256"]),
                    ],
                },
            ]
        )
        for payload in invalid:
            with (
                self.subTest(payload=payload),
                self.assertRaises(ingest_auth.IngestAuthError),
            ):
                ingest_auth.validate_credentials(payload)

    def test_shared_private_reader_rejects_duplicate_keys_and_relative_path(self):
        self.path.write_text(
            '{"schemaVersion":1,"schemaVersion":1,"credentials":[]}', encoding="utf-8"
        )
        self.path.chmod(0o600)
        for path in (self.path, Path("relative.json"), self.directory):
            with self.assertRaises(ingest_auth.IngestAuthError):
                ingest_auth.read_credentials(path)

    def test_every_ingest_http_route_rejects_factory_tokens_before_domain_writes(self):
        self.save()
        self.start()
        paths = [
            "/api/telemetry/ingest",
            "/api/telemetry/bulk",
            "/api/telemetry/quarantine",
            "/api/health/dependencies/mqtt",
            "/api/devices/DEV-01-GEN-01/health",
            "/api/devices/DEV-01-GEN-01/analysis",
            "/api/devices/DEV-01-GEN-01/model-inputs",
            "/api/devices/DEV-01-GEN-01/model-raw-inputs",
        ]
        for token in data.TELEMETRY_SERVICE_TOKENS:
            for path in paths:
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.request(path, token)[0], 401)
            self.assertEqual(
                self.request(
                    "/api/devices/DEV-01-GEN-01/analysis/pending", token, method="GET"
                )[0],
                401,
            )
        self.assertEqual(data.TELEMETRY_RECORDS, [])
        self.assertEqual(data.QUARANTINED_DEVICE_MESSAGES, [])

    def test_real_http_machine_ingest_health_mqtt_and_operator_isolation(self):
        self.save()
        self.start()
        status, body = self.request(
            "/api/telemetry/ingest", self.device, telemetry_payload()
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(
            self.request(
                "/api/devices/DEV-01-GEN-01/health",
                self.device,
                {"reportedAt": data.now_iso()},
            )[0],
            200,
        )
        self.assertEqual(
            self.request("/api/devices/DEV-01-MOT-02/health", self.device)[0], 403
        )
        self.assertEqual(
            self.request(
                "/api/health/dependencies/mqtt", self.mqtt, {"status": "healthy"}
            )[0],
            200,
        )
        self.assertEqual(
            self.request(
                "/api/telemetry/quarantine",
                self.mqtt,
                {
                    "topic": "devices/DEV-01-GEN-01/telemetry",
                    "reason": "INVALID_JSON",
                    "message": "invalid",
                    "payload": "{",
                },
            )[0],
            201,
        )
        self.assertEqual(self.request("/api/sites", self.device, method="GET")[0], 401)
        self.assertEqual(
            self.request(
                "/api/devices/DEV-01-GEN-01/configuration/pending",
                self.device,
                method="GET",
            )[0],
            401,
        )

    def test_foreign_single_bulk_and_analysis_requests_do_not_write_device_history(
        self,
    ):
        self.save()
        self.start()
        foreign = telemetry_payload(deviceId="DEV-01-MOT-02", assetId="SITE-01-MOT-02")
        self.assertEqual(
            self.request("/api/telemetry/ingest", self.device, foreign)[0], 403
        )
        self.assertEqual(
            self.request(
                "/api/telemetry/bulk",
                self.device,
                {"items": [telemetry_payload(), foreign]},
            )[0],
            403,
        )
        self.assertEqual(
            self.request("/api/devices/DEV-01-MOT-02/analysis", self.device)[0], 403
        )
        self.assertEqual(
            self.request(
                "/api/devices/DEV-01-MOT-02/analysis/pending", self.device, method="GET"
            )[0],
            403,
        )
        self.assertEqual(data.TELEMETRY_RECORDS, [])
        self.assertEqual(data.QUARANTINED_DEVICE_MESSAGES, [])
        self.assertEqual(data.TELEMETRY_METRICS["requests"], 0)
        self.assertEqual(
            self.request(
                "/api/telemetry/bulk", self.device, {"items": [telemetry_payload()]}
            )[0],
            200,
        )

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = ingest_auth.main(list(args))
        return status, output.getvalue()

    def test_cli_add_rotate_revoke_check_preserve_other_credentials_and_never_print_secrets(
        self,
    ):
        client = self.directory / "device.ingest-token.json"
        args = ["--file", str(self.path), "--id", "esp32"]
        status, output = self.cli(
            "add",
            *args,
            "--kind",
            "device",
            "--device",
            "DEV-01-GEN-01",
            "--token-file",
            str(client),
        )
        self.assertEqual(status, 0, output)
        original = ingest_auth.read_client_token(client)
        self.assertNotIn(original, output)
        self.assertNotIn(original, self.path.read_text())
        self.assertEqual(self.cli("check", "--file", str(self.path))[0], 0)
        self.assertEqual(
            self.cli(
                "add",
                *args,
                "--kind",
                "device",
                "--device",
                "DEV-01-GEN-01",
                "--token-file",
                str(client),
            )[0],
            1,
        )
        second = self.directory / "second.ingest-token.json"
        self.assertEqual(
            self.cli(
                "add",
                "--file",
                str(self.path),
                "--id",
                "mqtt",
                "--kind",
                "mqtt",
                "--device",
                "DEV-01-MOT-02",
                "--token-file",
                str(second),
            )[0],
            0,
        )
        before = copy.deepcopy(ingest_auth.read_credentials(self.path)[1])
        rotated = self.directory / "rotated.ingest-token.json"
        status, output = self.cli("rotate", *args, "--token-file", str(rotated))
        self.assertEqual(status, 0, output)
        self.assertNotEqual(ingest_auth.read_client_token(rotated), original)
        self.assertNotIn(ingest_auth.read_client_token(rotated), output)
        self.assertEqual(ingest_auth.read_credentials(self.path)[1], before)
        self.assertEqual(self.cli("revoke", *args)[0], 0)
        self.assertEqual(ingest_auth.read_credentials(self.path), [before])

    def test_cli_never_overwrites_client_files_and_rolls_back_on_server_save_failure(
        self,
    ):
        self.save()
        original = self.path.read_bytes()
        client = self.directory / "client.ingest-token.json"
        client.write_text("existing", encoding="utf-8")
        args = [
            "rotate",
            "--file",
            str(self.path),
            "--id",
            "esp32",
            "--token-file",
            str(client),
        ]
        self.assertEqual(self.cli(*args)[0], 1)
        self.assertEqual(client.read_text(), "existing")
        self.assertEqual(self.path.read_bytes(), original)
        client.unlink()
        with patch(
            "motor_diagnosis.auth_config.os.replace",
            side_effect=OSError("simulated disk failure"),
        ):
            self.assertEqual(self.cli(*args)[0], 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(client.exists())
        self.assertFalse(list(self.directory.glob(".auth-update-*")))

    def test_cli_rejects_git_targets_and_concurrent_updates(self):
        client = self.directory / "client.ingest-token.json"
        args = [
            "add",
            "--file",
            str(self.path),
            "--id",
            "esp32",
            "--kind",
            "device",
            "--device",
            "DEV-01-GEN-01",
            "--token-file",
            str(client),
        ]
        lock = self.path.with_name(self.path.name + ".lock")
        lock.touch()
        self.assertEqual(self.cli(*args)[0], 1)
        self.assertTrue(lock.exists())
        self.assertFalse(client.exists())
        lock.unlink()
        (self.directory / ".git").mkdir()
        self.assertEqual(self.cli(*args)[0], 1)
        self.assertFalse(self.path.exists())

    def test_configuration_survives_a_fresh_process(self):
        self.save()
        code = "from motor_diagnosis.ingest_auth import configured_credentials; assert len(configured_credentials()) == 2"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mqtt_parser_requires_explicit_production_secret_and_supports_private_file(
        self,
    ):
        os.environ.pop("MQTT_INGEST_TOKEN", None)
        for arguments in ([], ["--ingest-token", "demo-mqtt-ingest-token"]):
            with (
                patch.object(sys, "argv", ["mqtt", *arguments]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                mqtt_service.parse_args()
        client = self.directory / "mqtt.ingest-token.json"
        auth_config.write_private_json(client, {"token": self.mqtt}, create=True)
        with patch.object(sys, "argv", ["mqtt", "--ingest-token-file", str(client)]):
            self.assertEqual(mqtt_service.parse_args().ingest_token, self.mqtt)
        client.unlink()
        with (
            patch.object(sys, "argv", ["mqtt", "--ingest-token-file", str(client)]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            mqtt_service.parse_args()


if __name__ == "__main__":
    unittest.main()
