"""Explicit non-demo operator accounts for production-mode integration tests."""

import copy
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

from motor_diagnosis import auth_config


def credentials(username="admin"):
    return {"username": username, "password": username + "-test-passphrase-2026"}


@lru_cache(maxsize=1)
def _accounts():
    users = []
    for index, (username, role) in enumerate(
        (("operator", "A"), ("admin", "B"), ("system", "C")), 1
    ):
        salt = str(index) * 64
        users.append(
            {
                "id": "production-" + username,
                "username": username,
                "name": username,
                "role": role,
                "allowedSiteIds": (
                    ["SITE-01", "SITE-02", "SITE-03", "SITE-04"]
                    if role == "A"
                    else ["*"]
                ),
                "passwordSalt": salt,
                "passwordIterations": auth_config.ITERATIONS,
                "passwordHash": auth_config.password_hash(
                    credentials(username)["password"], salt
                ),
            }
        )
    return users


def accounts():
    return copy.deepcopy(_accounts())


def install_production_auth(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / "auth-users.json"
    auth_config.write_accounts(path, accounts(), create=True)
    environment = patch.dict(os.environ, {"AUTH_USERS_FILE": str(path)})
    environment.start()
    test.addCleanup(environment.stop)
    return path


def install_ingest_auth(test, *, devices=("DEV-01-MOT-02",), kind="device"):
    from motor_diagnosis import ingest_auth

    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / "ingest-tokens.json"
    token = (
        "ingest-" + "t" * 43
    )  # Test-only; production CLI uses secrets.token_urlsafe.
    payload = {
        "schemaVersion": 1,
        "credentials": [
            {
                "id": "test-device",
                "kind": kind,
                "deviceIds": list(devices),
                "tokenSha256": ingest_auth.token_hash(token),
            }
        ],
    }
    ingest_auth.validate_credentials(payload)
    auth_config.write_private_json(path, payload, create=True)
    environment = patch.dict(os.environ, {"INGEST_TOKENS_FILE": str(path)})
    environment.start()
    test.addCleanup(environment.stop)
    return token
