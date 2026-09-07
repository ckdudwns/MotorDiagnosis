"""Private, restart-safe account provisioning; never store plaintext passwords."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
import warnings
from pathlib import Path

from .json_validation import validate_json_values

ITERATIONS = 600_000
MAX_FILE_BYTES = 64 * 1024
CONFIG_ERROR = "AUTH_USERS_FILE must contain a valid private account file."
USER_FIELDS = {
    "id",
    "username",
    "name",
    "role",
    "allowedSiteIds",
    "passwordSalt",
    "passwordHash",
    "passwordIterations",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HEX_64 = re.compile(r"[0-9a-f]{64}")


class AuthConfigurationError(ValueError):
    pass


def password_hash(password: str, salt: str, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    ).hex()


def validate_accounts(payload: object) -> list[dict]:
    def reject():
        raise AuthConfigurationError(CONFIG_ERROR)

    try:
        validate_json_values(payload)
    except ValueError:
        reject()
    if not isinstance(payload, dict) or set(payload) != {"schemaVersion", "users"}:
        reject()
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        reject()
    users = payload["users"]
    if not isinstance(users, list) or not 1 <= len(users) <= 100:
        reject()
    ids, usernames = set(), set()
    for user in users:
        if not isinstance(user, dict) or set(user) != USER_FIELDS:
            reject()
        for key in ("id", "username"):
            if not isinstance(user[key], str) or not IDENTIFIER.fullmatch(user[key]):
                reject()
        # Do not attribute new operators' audit history to factory-demo identities.
        if user["id"] in {"user-admin", "user-operator", "user-system"}:
            reject()
        if user["id"] in ids or user["username"].casefold() in usernames:
            reject()
        ids.add(user["id"])
        usernames.add(user["username"].casefold())
        if (
            not isinstance(user["name"], str)
            or not 1 <= len(user["name"].strip()) <= 100
            or any(ord(c) < 32 or ord(c) == 127 for c in user["name"])
            or user["role"] not in ("A", "B", "C")
        ):
            reject()
        sites = user["allowedSiteIds"]
        if not isinstance(sites, list) or not 1 <= len(sites) <= 100:
            reject()
        if any(
            not isinstance(s, str) or (s != "*" and not IDENTIFIER.fullmatch(s))
            for s in sites
        ):
            reject()
        if len(set(sites)) != len(sites) or ("*" in sites and len(sites) != 1):
            reject()
        for key in ("passwordSalt", "passwordHash"):
            if not isinstance(user[key], str) or not HEX_64.fullmatch(user[key]):
                reject()
        if (
            type(user["passwordIterations"]) is not int
            or not ITERATIONS <= user["passwordIterations"] <= 1_000_000
        ):
            reject()
    return users


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthConfigurationError(CONFIG_ERROR)
        result[key] = value
    return result


def read_account_file(path: str | Path) -> list[dict]:
    """Read afresh: revocation/file loss must not reuse a cached account list."""
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise AuthConfigurationError(CONFIG_ERROR)
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise AuthConfigurationError(CONFIG_ERROR)
            if os.name == "posix" and (
                info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())
            ):
                raise AuthConfigurationError(CONFIG_ERROR)
            contents = stream.read(MAX_FILE_BYTES + 1)
        if len(contents) > MAX_FILE_BYTES:
            raise AuthConfigurationError(CONFIG_ERROR)
        payload = json.loads(contents, object_pairs_hook=_unique_object)
        validate_json_values(payload)
        return validate_accounts(payload)
    except (OSError, ValueError, TypeError, RecursionError):
        # Do not expose file contents, hashes, or parser excerpts to HTTP/logs.
        raise AuthConfigurationError(CONFIG_ERROR) from None


def configured_users() -> list[dict] | None:
    path = os.environ.get("AUTH_USERS_FILE", "").strip()
    if path:
        return read_account_file(path)
    if os.environ.get("APP_ENV", "").strip().lower() == "production":
        raise AuthConfigurationError(CONFIG_ERROR)
    return None


def user_fingerprint(user: dict) -> str:
    """Bind sessions to identity, password, role and site scope, including demo mode."""
    return hashlib.sha256(
        json.dumps(
            user, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _password_input() -> str:
    if not sys.stdin.isatty():
        raise ValueError(
            "Run in an interactive terminal; passwords are never command arguments."
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass("New password (15-128 characters): ")
        second = getpass.getpass("Confirm password: ")
    if first != second:
        raise ValueError("Passwords do not match.")
    if not 15 <= len(first) <= 128 or first.isspace():
        raise ValueError("Use a password or passphrase of 15-128 characters.")
    return first


def write_accounts(path: Path, users: list[dict], *, create: bool) -> None:
    """Exclusive create, or atomic replacement. Caller holds the CLI update lock."""
    payload = {"schemaVersion": 1, "users": users}
    validate_accounts(payload)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Account file is too large.")
    if create:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink()
            raise
    else:
        descriptor, name = tempfile.mkstemp(prefix=".auth-update-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "add", "passwd", "check"))
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--username")
    parser.add_argument("--name", default="Operations administrator")
    parser.add_argument("--role", choices=("A", "B", "C"), default="C")
    parser.add_argument("--site", action="append", dest="sites")
    args = parser.parse_args(argv)
    path = args.file
    lock = None
    try:
        if not path.is_absolute() or path.is_symlink():
            raise ValueError(
                "Use an absolute, non-symlink path outside the repository."
            )
        if any((parent / ".git").exists() for parent in path.resolve().parents):
            raise ValueError("Keep account files outside Git repositories.")
        if args.command == "check":
            read_account_file(path)
            print("Account configuration is valid. No credentials displayed.")
            return 0
        if not args.username or not IDENTIFIER.fullmatch(args.username):
            raise ValueError("Specify a valid --username.")
        if not path.parent.is_dir():
            raise ValueError(
                "Create the private parent directory first (mode 700 on Linux)."
            )
        lock_path = path.with_name(path.name + ".lock")
        lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if args.command in ("init", "add"):
            if args.command == "init" and path.exists():
                raise ValueError(
                    "Account file already exists; use passwd to rotate a password."
                )
            users = [] if args.command == "init" else read_account_file(path)
            if any(u["username"].casefold() == args.username.casefold() for u in users):
                raise ValueError("Account already exists; use passwd instead.")
            user = {
                "id": "user-" + uuid.uuid4().hex,
                "username": args.username,
                "name": args.name,
                "role": args.role,
                "allowedSiteIds": args.sites or ["*"],
            }
            users.append(user)
        else:
            users = read_account_file(path)
            user = next((u for u in users if u["username"] == args.username), None)
            if user is None:
                raise ValueError("Account not found; no changes made.")
        password = _password_input()
        user["passwordSalt"] = secrets.token_hex(32)
        user["passwordIterations"] = ITERATIONS
        user["passwordHash"] = password_hash(password, user["passwordSalt"])
        del password
        write_accounts(path, users, create=args.command == "init")
        print("Account configuration saved. No credentials displayed.")
        return 0
    except (
        OSError,
        ValueError,
        getpass.GetPassWarning,
        EOFError,
        KeyboardInterrupt,
    ) as exc:
        # OS errors may reveal local paths but never account contents/passwords.
        print(f"Account setup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            os.close(lock)
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
