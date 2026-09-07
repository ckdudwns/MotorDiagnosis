"""Scoped machine credentials. Backend files contain hashes, never bearer tokens."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sys

from .auth_config import (
    AuthConfigurationError,
    HEX_64,
    IDENTIFIER,
    read_private_json,
    write_private_json,
)

CONFIG_ERROR = "INGEST_TOKENS_FILE must contain a valid private credential file."
TOKEN = re.compile(r"ingest-[A-Za-z0-9_-]{43}")
DEVICE = re.compile(r"[A-Z0-9][A-Z0-9_.-]{0,62}")


class IngestAuthError(ValueError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def validate_credentials(payload: object) -> list[dict]:
    def reject():
        raise IngestAuthError(CONFIG_ERROR)

    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "credentials"}
        or type(payload["schemaVersion"]) is not int
        or payload["schemaVersion"] != 1
        or not isinstance(payload["credentials"], list)
        or len(payload["credentials"]) > 100
    ):
        reject()
    ids, hashes = set(), set()
    for row in payload["credentials"]:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "kind",
            "deviceIds",
            "tokenSha256",
        }:
            reject()
        if (
            not isinstance(row["id"], str)
            or not IDENTIFIER.fullmatch(row["id"])
            or row["id"] in ids
            or row["kind"] not in ("device", "mqtt")
            or not isinstance(row["tokenSha256"], str)
            or not HEX_64.fullmatch(row["tokenSha256"])
            or row["tokenSha256"] in hashes
        ):
            reject()
        devices = row["deviceIds"]
        if (
            not isinstance(devices, list)
            or not 1 <= len(devices) <= 100
            or (row["kind"] == "device" and len(devices) != 1)
            or any(not isinstance(d, str) or not DEVICE.fullmatch(d) for d in devices)
            or len(set(devices)) != len(devices)
        ):
            reject()
        ids.add(row["id"])
        hashes.add(row["tokenSha256"])
    return payload["credentials"]


def read_credentials(path: str | Path) -> list[dict]:
    try:
        return validate_credentials(read_private_json(path))
    except (AuthConfigurationError, ValueError, TypeError, RecursionError):
        raise IngestAuthError(CONFIG_ERROR) from None


def configured_credentials() -> list[dict]:
    path = os.environ.get("INGEST_TOKENS_FILE", "").strip()
    # Unset means machine ingestion is not provisioned, not factory-token fallback.
    return read_credentials(path) if path else []


def principal_for_token(token: str) -> dict | None:
    rows = configured_credentials()  # Fail closed even for a malformed token.
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    digest = token_hash(token)
    for row in rows:
        if hmac.compare_digest(digest, row["tokenSha256"]):
            permissions = ["telemetry:ingest"]
            if row["kind"] == "device":
                permissions.append("device-health:write")
            else:
                permissions.extend(["telemetry:quarantine", "service-health:write"])
            return {
                "id": "service-ingest-" + row["id"],
                "type": "service",
                "permissions": permissions,
                "allowedDeviceIds": list(row["deviceIds"]),
                "allowedDependencyIds": ["mqtt"] if row["kind"] == "mqtt" else [],
            }
    return None


def read_client_token(path: str | Path) -> str:
    try:
        payload = read_private_json(path)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"token"}
            or not isinstance(payload["token"], str)
            or not TOKEN.fullmatch(payload["token"])
        ):
            raise ValueError()
        return payload["token"]
    except (ValueError, TypeError, RecursionError):
        raise IngestAuthError(
            "A valid private client token file is required."
        ) from None


def _private_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.parent.is_dir()
        or any((p / ".git").exists() for p in path.resolve().parents)
    ):
        raise ValueError(
            "Use an absolute non-symlink path in a private directory outside Git."
        )
    if os.name == "posix":
        info = path.parent.stat()
        if info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise ValueError(
                "Use a private parent directory (mode 700, owned by this user or root)."
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("add", "rotate", "revoke", "check"))
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--id")
    parser.add_argument("--kind", choices=("device", "mqtt"))
    parser.add_argument("--device", action="append")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    lock = None
    try:
        _private_path(args.file)
        if args.command == "check":
            read_credentials(args.file)
            print("Ingest credentials are valid. No secrets displayed.")
            return 0
        if not args.id or not IDENTIFIER.fullmatch(args.id):
            raise ValueError("Specify a valid --id.")
        lock_path = args.file.with_name(args.file.name + ".lock")
        lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        create = not args.file.exists()
        rows = [] if create and args.command == "add" else read_credentials(args.file)
        row = next((r for r in rows if r["id"] == args.id), None)
        if args.command == "add":
            if row is not None:
                raise ValueError("Credential already exists; use rotate.")
            row = {"id": args.id, "kind": args.kind, "deviceIds": args.device}
            rows.append(row)
        elif row is None:
            raise ValueError("Credential not found; no changes made.")
        elif args.kind is not None or args.device is not None:
            raise ValueError(
                "rotate/revoke preserve the existing scope; omit --kind and --device."
            )
        payload = {"schemaVersion": 1, "credentials": rows}
        if args.command == "revoke":
            if args.token_file is not None:
                raise ValueError(
                    "revoke does not modify client files; omit --token-file."
                )
            rows.remove(row)
            validate_credentials(payload)
            write_private_json(args.file, payload, create=False)
        else:
            if args.token_file is None:
                raise ValueError(
                    "Specify a new private --token-file for the client secret."
                )
            _private_path(args.token_file)
            if args.token_file.resolve() in (args.file.resolve(), lock_path.resolve()):
                raise ValueError("Client and server paths must be different.")
            token = "ingest-" + secrets.token_urlsafe(32)
            row["tokenSha256"] = token_hash(token)
            validate_credentials(payload)
            # Do not overwrite a client secret or activate a key not saved for its client.
            write_private_json(args.token_file, {"token": token}, create=True)
            try:
                write_private_json(args.file, payload, create=create)
            except BaseException:
                args.token_file.unlink(missing_ok=True)
                raise
        print("Ingest credentials saved. No secrets displayed.")
        return 0
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"Ingest credential setup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            os.close(lock)
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
