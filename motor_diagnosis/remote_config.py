"""Device-scoped, allowlisted desired configuration and reported application state."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from typing import Any

from . import data

SCHEMA_VERSION = 1
MAX_VERSION = 2_147_483_647
HISTORY_LIMIT = 100
SETTING_LIMITS = {"measurementIntervalMs": (3000, 60000), "replayBatchSize": (1, 4)}
DEFAULT_SETTINGS = {"measurementIntervalMs": 3000, "replayBatchSize": 4}
ERROR_CODES = {"storage_failure", "invalid_config", "stale_version", "version_conflict"}
IDENTIFIER_PATTERN = r"[A-Z0-9][A-Z0-9_.-]{0,62}"


def _supported_identifier(value: Any) -> bool:
    # Match the firmware's 64-byte NVS fields (63 ASCII bytes plus terminator).
    return (
        isinstance(value, str) and re.fullmatch(IDENTIFIER_PATTERN, value) is not None
    )


def _error(status: int, code: str, message: str):
    raise data.ApiError(status, code, message)


def _integer(value: Any, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _error(
            400,
            "INVALID_DEVICE_CONFIG",
            f"{field} must be an integer in [{minimum}, {maximum}].",
        )
    return value


def normalize_settings(settings: Any) -> dict[str, int]:
    if not isinstance(settings, dict) or set(settings) != set(SETTING_LIMITS):
        _error(
            400,
            "INVALID_DEVICE_CONFIG",
            "Only measurementIntervalMs and replayBatchSize are supported; both are required.",
        )
    return {
        key: _integer(settings[key], *bounds, key)
        for key, bounds in SETTING_LIMITS.items()
    }


def _credentials() -> dict[str, str]:
    """Read normally provisioned secrets; never echo or persist credentials."""
    raw = os.environ.get("DEVICE_CONFIG_TOKENS_JSON", "{}")
    try:

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result

        values = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(values, dict):
            raise ValueError()
        seen = set()
        for device_id, token in values.items():
            if (
                not _supported_identifier(device_id)
                or not isinstance(token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None
                or token in seen
            ):
                raise ValueError()
            seen.add(token)
        return values
    except (ValueError, TypeError):
        _error(
            503,
            "DEVICE_CONFIG_AUTH_UNAVAILABLE",
            "Device configuration credentials are not correctly provisioned.",
        )


def _device_principal(token: str, device_id: str) -> dict[str, str]:
    if (
        not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None
    ):
        _error(
            401, "AUTH_REQUIRED", "A device-specific configuration token is required."
        )
    authenticated = next(
        (
            key
            for key, secret in _credentials().items()
            if hmac.compare_digest(token.encode("utf-8"), secret.encode("ascii"))
        ),
        None,
    )
    if authenticated is None:
        _error(
            401, "AUTH_REQUIRED", "A device-specific configuration token is required."
        )
    if authenticated != device_id:
        _error(
            403, "DEVICE_CONFIG_FORBIDDEN", "The credential belongs to another device."
        )
    return {"id": device_id, "name": device_id, "role": "device"}


def _active_device(device_id: str) -> dict[str, Any]:
    device = data.get_device(device_id)
    if (
        device.get("mappingStatus") != "active"
        or device.get("certificateStatus") != "registered"
    ):
        _error(
            409,
            "DEVICE_CONFIG_INACTIVE",
            "Configuration requires an active, registered device mapping.",
        )
    data.get_site(device["siteId"])
    data.get_asset(device["siteId"], device["assetId"])
    for field, value in (
        ("deviceId", device["id"]),
        ("siteId", device["siteId"]),
        ("assetId", device["assetId"]),
    ):
        if not _supported_identifier(value):
            _error(
                409,
                "DEVICE_CONFIG_UNSUPPORTED_ID",
                f"{field} is unsupported for remote configuration; use 1-63 ASCII characters, "
                "starting with A-Z or 0-9, followed by A-Z, 0-9, dot, underscore or hyphen.",
            )
    return device


def _record(device_id: str) -> dict[str, Any]:
    return data.DEVICE_CONFIGS.get(
        device_id, {"version": 0, "desired": None, "lastApplied": None, "history": []}
    )


def _same_scope(command: dict[str, Any], device: dict[str, Any]) -> bool:
    return (
        command["siteId"] == device["siteId"]
        and command["assetId"] == device["assetId"]
    )


def configuration_for(user: dict[str, Any], device_id: str) -> dict[str, Any]:
    data.require_permission(user, "device:read")
    with data.STORE_LOCK:
        device = data.get_device(device_id)
        data.require_site_access(user, device["siteId"])
        record = _record(device_id)
        desired = record["desired"]
        last_applied = record["lastApplied"]
        return data.copy_payload(
            {
                "deviceId": device_id,
                "version": record["version"],
                "desired": (
                    desired if desired and _same_scope(desired, device) else None
                ),
                "lastApplied": (
                    last_applied
                    if last_applied and _same_scope(last_applied, device)
                    else None
                ),
                "scopeChanged": bool(desired and not _same_scope(desired, device)),
                "defaults": DEFAULT_SETTINGS,
                "limits": {
                    key: {"minimum": lo, "maximum": hi}
                    for key, (lo, hi) in SETTING_LIMITS.items()
                },
            }
        )


def configuration_history(user: dict[str, Any], device_id: str) -> dict[str, Any]:
    data.require_permission(user, "device:read")
    with data.STORE_LOCK:
        device = data.get_device(device_id)
        data.require_site_access(user, device["siteId"])
        rows = [
            row
            for row in reversed(_record(device_id)["history"])
            if _same_scope(row, device)
        ]
        return data.copy_payload({"items": rows, "retainedLimit": HISTORY_LIMIT})


def request_configuration(
    user: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    data.require_permission(user, "parameter:write")
    data.require_permission(user, "device:read")
    if set(payload) != {"expectedVersion", "settings", "reason"}:
        _error(
            400,
            "INVALID_DEVICE_CONFIG",
            "expectedVersion, settings and reason are required; extra fields are not allowed.",
        )
    expected = _integer(payload["expectedVersion"], 0, MAX_VERSION, "expectedVersion")
    settings = normalize_settings(payload["settings"])
    reason = data.required_text(payload, "reason")
    if len(reason) > data.MAX_REASON_LENGTH:
        _error(400, "REASON_TOO_LONG", "reason must be at most 1000 characters.")
    with data.STORE_LOCK:
        device = _active_device(device_id)
        data.require_site_access(user, device["siteId"])
        before = _record(device_id)
        if expected != before["version"]:
            _error(
                409,
                "DEVICE_CONFIG_VERSION_CONFLICT",
                "The configuration changed; reload before submitting.",
            )
        if expected == MAX_VERSION:
            _error(
                409,
                "DEVICE_CONFIG_VERSION_EXHAUSTED",
                "Configuration version space is exhausted.",
            )
        command = {
            "version": expected + 1,
            "commandId": secrets.token_hex(16),
            "settings": settings,
            "state": "pending",
            "result": None,
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "requestedAt": data.now_iso(),
            "requestedBy": user["id"],
            "reason": reason,
        }
        record = data.copy_payload(before)
        record.update(version=expected + 1, desired=command)
        record["history"] = [*record["history"], command][-HISTORY_LIMIT:]
        data.DEVICE_CONFIGS[device_id] = record
        data.append_audit_log(
            user,
            "device-config.request",
            "device",
            device_id,
            before["desired"],
            command,
            reason,
            site_id=device["siteId"],
        )
        # The HTTP result is sent only after STORE_LOCK's durable commit succeeds.
        return data.copy_payload(command)


def pending_configuration(token: str, device_id: str) -> dict[str, Any]:
    _device_principal(token, device_id)
    with data.STORE_LOCK:
        device = _active_device(device_id)
        desired = _record(device_id)["desired"]
        if desired and not _same_scope(desired, device):
            _error(
                409,
                "DEVICE_CONFIG_SCOPE_CHANGED",
                "The device mapping changed; request a new configuration for this mapping.",
            )
        # Keep returning the desired version even after application: reboot/ACK
        # loss can be reconciled without inventing another command or NVS write.
        return data.copy_payload(
            {
                "schemaVersion": SCHEMA_VERSION,
                "deviceId": device_id,
                "siteId": device["siteId"],
                "assetId": device["assetId"],
                "desired": (
                    {key: desired[key] for key in ("version", "commandId", "settings")}
                    if desired
                    else None
                ),
            }
        )


def report_configuration(
    token: str, device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    actor = _device_principal(token, device_id)
    if set(payload) != {"version", "commandId", "status", "settings", "errorCode"}:
        _error(
            400,
            "INVALID_DEVICE_CONFIG_RESULT",
            "The configuration result has missing or unsupported fields.",
        )
    version = _integer(payload["version"], 1, MAX_VERSION, "version")
    command_id = payload["commandId"]
    if (
        not isinstance(command_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", command_id) is None
    ):
        _error(
            400,
            "INVALID_DEVICE_CONFIG_RESULT",
            "commandId must match the issued command.",
        )
    status = payload["status"]
    if not isinstance(status, str) or status not in {"applied", "failed", "rejected"}:
        _error(
            400,
            "INVALID_DEVICE_CONFIG_RESULT",
            "status must be applied, failed or rejected.",
        )
    if status == "applied":
        settings = normalize_settings(payload["settings"])
        if payload["errorCode"] is not None:
            _error(
                400,
                "INVALID_DEVICE_CONFIG_RESULT",
                "Applied results cannot contain an error.",
            )
    else:
        settings = None
        if (
            payload["settings"] is not None
            or not isinstance(payload["errorCode"], str)
            or payload["errorCode"] not in ERROR_CODES
        ):
            _error(
                400,
                "INVALID_DEVICE_CONFIG_RESULT",
                "Failure results require a supported errorCode and null settings.",
            )
    with data.STORE_LOCK:
        device = _active_device(device_id)
        record = _record(device_id)
        desired = record["desired"]
        if (
            not desired
            or version != desired["version"]
            or command_id != desired["commandId"]
            or not _same_scope(desired, device)
        ):
            _error(
                409,
                "DEVICE_CONFIG_SUPERSEDED",
                "The reported command is not current for this device mapping.",
            )
        if settings is not None and settings != desired["settings"]:
            _error(
                409,
                "DEVICE_CONFIG_RESULT_MISMATCH",
                "Applied settings do not match the issued command.",
            )
        if desired["state"] == "applied" and status != "applied":
            _error(
                409,
                "DEVICE_CONFIG_RESULT_STALE",
                "A delayed failure cannot overwrite an applied result.",
            )
        result = {key: payload[key] for key in ("status", "settings", "errorCode")}
        previous = desired["result"]
        if previous is None or any(previous[key] != result[key] for key in result):
            result["receivedAt"] = data.now_iso()
            updated = {**desired, "state": status, "result": result}
            record["desired"] = updated
            record["history"] = [
                updated if row["commandId"] == command_id else row
                for row in record["history"]
            ]
            if status == "applied":
                record["lastApplied"] = data.copy_payload(updated)
            data.append_audit_log(
                actor,
                "device-config.result",
                "device",
                device_id,
                previous,
                result,
                "Device-reported configuration result",
                site_id=device["siteId"],
            )
        return {
            "accepted": True,
            "deviceId": device_id,
            "version": version,
            "commandId": command_id,
            "status": status,
        }
