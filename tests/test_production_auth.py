"""Private credentials, fail-closed production and real HTTP session boundaries."""

import contextlib
import getpass
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import stat
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from motor_diagnosis import auth_config, data
from motor_diagnosis.server import create_server
from tests.auth_fixtures import accounts, credentials, install_production_auth


class ProductionAuthTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.addCleanup(data.reset_runtime_state)
        self.environment = patch.dict(
            os.environ, {"APP_ENV": "production", "AUTH_USERS_FILE": ""}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def install(self):
        self.path = install_production_auth(self)
        return self.path

    def login(self, name="admin"):
        return data.authenticate(credentials(name))

    def assert_api_error(self, code, function, *args):
        with self.assertRaises(data.ApiError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)

    def test_missing_file_blocks_factory_accounts_and_startup_before_database(self):
        self.assert_api_error(
            "AUTH_NOT_CONFIGURED",
            data.authenticate,
            {"username": "admin", "password": "admin123"},
        )
        with patch("motor_diagnosis.server.configure_runtime_state") as configure:
            self.assert_api_error("AUTH_NOT_CONFIGURED", create_server, "127.0.0.1", 0)
            configure.assert_not_called()

    def test_development_without_file_preserves_demo_login_only_there(self):
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            token = data.authenticate({"username": "admin", "password": "admin123"})[
                "session"
            ]["token"]
            self.assertTrue(token.startswith("demo-"))
        self.install()
        self.assert_api_error("INVALID_SESSION", data.current_user_for_token, token)

    def test_configured_accounts_replace_demo_and_keep_roles_and_scope(self):
        self.install()
        for name, role in (("operator", "A"), ("admin", "B"), ("system", "C")):
            with self.subTest(name=name):
                self.assert_api_error(
                    "INVALID_CREDENTIALS",
                    data.authenticate,
                    {"username": name, "password": name + "123"},
                )
                login = self.login(name)
                self.assertEqual(login["user"]["role"], role)
                self.assertTrue(login["session"]["token"].startswith("session-"))
                self.assertNotIn("password", json.dumps(login).lower())
                self.assertEqual(
                    data.current_user_for_token(login["session"]["token"]),
                    login["user"],
                )
        operator = self.login("operator")["user"]
        self.assert_api_error(
            "SITE_FORBIDDEN", data.require_site_access, operator, "SITE-05"
        )

    def test_invalid_config_is_never_replaced_by_demo_even_in_development(self):
        path = self.install()
        token = self.login()["session"]["token"]
        path.write_text('{"secret":"must-not-appear",', encoding="utf-8")
        for environment in ("production", "development"):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, {"APP_ENV": environment}),
            ):
                for function, payload in (
                    (data.authenticate, credentials()),
                    (data.current_user_for_token, token),
                ):
                    with self.assertRaises(data.ApiError) as error:
                        function(payload)
                    self.assertEqual(error.exception.code, "AUTH_NOT_CONFIGURED")
                    self.assertNotIn("must-not-appear", str(error.exception))
        path.unlink()
        self.assert_api_error("AUTH_NOT_CONFIGURED", data.authenticate, credentials())

    def test_password_role_scope_identity_and_removal_revoke_existing_sessions(self):
        path = self.install()
        for change in (
            "passwordHash",
            "role",
            "allowedSiteIds",
            "id",
            "username",
            "remove",
        ):
            with self.subTest(change=change):
                auth_config.write_accounts(path, accounts(), create=False)
                token = self.login()["session"]["token"]
                updated = accounts()
                user = updated[1]
                if change == "passwordHash":
                    user[change] = "f" * 64
                elif change == "role":
                    user[change] = "A"
                elif change == "allowedSiteIds":
                    user[change] = ["SITE-01"]
                elif change == "remove":
                    updated.pop(1)
                else:
                    user[change] += "-changed"
                auth_config.write_accounts(path, updated, create=False)
                self.assert_api_error(
                    "INVALID_SESSION", data.current_user_for_token, token
                )
                self.assertNotIn(token, data.SESSIONS)

    def test_lockout_is_preserved_for_new_operator_usernames(self):
        self.install()
        for _ in range(4):
            self.assert_api_error(
                "INVALID_CREDENTIALS",
                data.authenticate,
                {"username": "system", "password": "wrong"},
            )
        self.assert_api_error(
            "ACCOUNT_LOCKED",
            data.authenticate,
            {"username": "system", "password": "wrong"},
        )
        self.assert_api_error("ACCOUNT_LOCKED", self.login, "system")

    def test_restart_keeps_accounts_but_not_sessions(self):
        self.install()
        old = self.login()["session"]["token"]
        data.reset_runtime_state()
        self.assert_api_error("INVALID_SESSION", data.current_user_for_token, old)
        self.assertEqual(self.login()["user"]["id"], "production-admin")
        program = "from motor_diagnosis import data; print(data.authenticate({'username':'admin','password':'admin-test-passphrase-2026'})['user']['role'])"
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "B")

    def test_http_login_scope_health_and_credential_loss(self):
        path = self.install()
        server = create_server("127.0.0.1", 0, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(url, payload=None, token=""):
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                connection.request(
                    "POST" if payload else "GET",
                    url,
                    json.dumps(payload) if payload else None,
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + token,
                    },
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(request("/api/health")[0], 200)
            self.assertEqual(
                request(
                    "/api/auth/login", {"username": "admin", "password": "admin123"}
                )[0],
                400,
            )
            status, body = request("/api/auth/login", credentials("operator"))
            self.assertEqual(status, 200, body)
            token = body["session"]["token"]
            self.assertEqual(request("/api/sites", token=token)[0], 200)
            self.assertEqual(request("/api/assets?siteId=SITE-05", token=token)[0], 403)
            path.unlink()
            self.assertEqual(request("/api/sites", token=token)[0], 503)
            self.assertEqual(request("/api/auth/login", credentials())[0], 503)
            self.assertEqual(request("/api/health")[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class AccountFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth-users.json"

    def test_schema_rejects_ambiguous_weak_or_factory_configuration(self):
        variants = []
        for key, value in (
            ("role", "ROOT"),
            ("allowedSiteIds", []),
            ("allowedSiteIds", ["*", "SITE-01"]),
            ("passwordIterations", True),
            ("passwordIterations", 100000),
            ("passwordHash", "not-a-hash"),
            ("id", "user-admin"),
        ):
            users = accounts()
            users[1][key] = value
            variants.append({"schemaVersion": 1, "users": users})
        users = accounts()
        users[1]["password"] = "plaintext"
        variants.append({"schemaVersion": 1, "users": users})
        users = accounts()
        users[1]["username"] = "OPERATOR"
        variants.append({"schemaVersion": 1, "users": users})
        users = accounts()
        users[1]["id"] = users[0]["id"]
        variants.append({"schemaVersion": 1, "users": users})
        variants.extend(
            (
                [],
                {"schemaVersion": True, "users": accounts()},
                {"schemaVersion": 1, "users": []},
            )
        )
        for payload in variants:
            with (
                self.subTest(payload_type=type(payload).__name__),
                self.assertRaises(auth_config.AuthConfigurationError),
            ):
                auth_config.validate_accounts(payload)

    def test_private_file_roundtrip_and_duplicate_json_fields(self):
        auth_config.write_accounts(self.path, accounts(), create=True)
        self.assertEqual(auth_config.read_account_file(self.path), accounts())
        self.path.write_text(
            '{"schemaVersion":1,"schemaVersion":1,"users":[]}', encoding="utf-8"
        )
        with self.assertRaises(auth_config.AuthConfigurationError):
            auth_config.read_account_file(self.path)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode enforcement")
    def test_world_readable_file_is_rejected(self):
        auth_config.write_accounts(self.path, accounts(), create=True)
        self.path.chmod(0o644)
        with self.assertRaises(auth_config.AuthConfigurationError):
            auth_config.read_account_file(self.path)

    def test_relative_oversized_and_directory_paths_are_rejected(self):
        self.path.write_bytes(b" " * (auth_config.MAX_FILE_BYTES + 1))
        for path in ("auth-users.json", self.path, self.path.parent):
            with (
                self.subTest(path=str(path)),
                self.assertRaises(auth_config.AuthConfigurationError),
            ):
                auth_config.read_account_file(path)

    def test_replacement_failure_preserves_previous_accounts(self):
        auth_config.write_accounts(self.path, accounts(), create=True)
        before = self.path.read_bytes()
        with patch.object(
            auth_config.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                auth_config.write_accounts(self.path, accounts()[:1], create=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".auth-update-*")), [])

    def test_posix_permission_and_owner_checks(self):
        auth_config.write_accounts(self.path, accounts(), create=True)
        for mode, owner, valid in (
            (0o600, 1000, True),
            (0o600, 0, True),
            (0o644, 1000, False),
            (0o660, 1000, False),
            (0o600, 2000, False),
        ):
            interface = SimpleNamespace(
                name="posix",
                O_RDONLY=os.O_RDONLY,
                open=os.open,
                fdopen=os.fdopen,
                geteuid=lambda: 1000,
                fstat=lambda fd: SimpleNamespace(
                    st_mode=stat.S_IFREG | mode, st_uid=owner, st_size=100
                ),
            )
            with (
                self.subTest(mode=mode, owner=owner),
                patch.object(auth_config, "os", interface),
            ):
                if valid:
                    self.assertEqual(
                        auth_config.read_account_file(self.path), accounts()
                    )
                else:
                    with self.assertRaises(auth_config.AuthConfigurationError):
                        auth_config.read_account_file(self.path)

    def cli(self, command, username="operations-admin"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = auth_config.main(
                [command, "--file", str(self.path), "--username", username]
            )
        return result, output.getvalue()

    def test_cli_init_rotate_and_check_never_print_password_or_overwrite_on_init(self):
        password = "strong-test-passphrase-2026"
        with patch.object(auth_config, "_password_input", return_value=password):
            status, output = self.cli("init")
            self.assertEqual(status, 0, output)
            self.assertNotIn(password, output)
            before = self.path.read_bytes()
            self.assertNotIn(password.encode(), before)
            first = auth_config.read_account_file(self.path)[0]
            status, _ = self.cli("init")
            self.assertEqual(status, 1)
            self.assertEqual(self.path.read_bytes(), before)
        with patch.object(
            auth_config, "_password_input", return_value=password + "-new"
        ):
            status, output = self.cli("passwd")
            self.assertEqual(status, 0, output)
            second = auth_config.read_account_file(self.path)[0]
            self.assertEqual(first["id"], second["id"])
            self.assertNotEqual(first["passwordHash"], second["passwordHash"])
        self.assertEqual(self.cli("check")[0], 0)
        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())

    def test_cli_rejects_git_repository_and_noninteractive_or_echoed_password(self):
        (self.path.parent / ".git").mkdir()
        self.assertEqual(self.cli("init")[0], 1)
        self.assertFalse(self.path.exists())
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(ValueError):
                auth_config._password_input()
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(
                getpass, "getpass", side_effect=getpass.GetPassWarning("echo")
            ),
        ):
            with self.assertRaises(getpass.GetPassWarning):
                auth_config._password_input()

    def test_cli_add_preserves_accounts_and_blocks_duplicate_or_parallel_updates(self):
        auth_config.write_accounts(self.path, accounts(), create=True)
        with patch.object(
            auth_config, "_password_input", return_value="new-user-test-passphrase"
        ):
            status, output = self.cli("add", "new-operator")
            self.assertEqual(status, 0, output)
            updated = auth_config.read_account_file(self.path)
            self.assertEqual(updated[:3], accounts())
            self.assertEqual(updated[3]["username"], "new-operator")
            before = self.path.read_bytes()
            self.assertEqual(self.cli("add", "NEW-OPERATOR")[0], 1)
            self.assertEqual(self.path.read_bytes(), before)
            lock = self.path.with_name(self.path.name + ".lock")
            lock.touch()
            self.assertEqual(self.cli("passwd", "admin")[0], 1)
            self.assertTrue(lock.exists())
            self.assertEqual(self.path.read_bytes(), before)

    def test_cli_rejects_weak_or_mismatched_password(self):
        for first, second in (
            ("admin123", "admin123"),
            ("x" * 15, "y" * 15),
            (" " * 15, " " * 15),
        ):
            with (
                patch.object(sys.stdin, "isatty", return_value=True),
                patch.object(getpass, "getpass", side_effect=(first, second)),
            ):
                with self.assertRaises(ValueError):
                    auth_config._password_input()


if __name__ == "__main__":
    unittest.main()
