"""Immutable model/baseline metadata. Registration never trains or deploys a model."""

from __future__ import annotations

import math
import re
from ipaddress import ip_address
from urllib.parse import urlsplit

from . import data

LIMITATIONS = (
    "Pretrained baseline only. Fault classification and RUL performance on the target "
    "motor are not guaranteed before field validation and calibration."
)


def _text(payload, key, *, maximum=2000):
    text = data.required_text(payload, key)
    if len(text) > maximum:
        raise data.ApiError(400, "INVALID_MODEL_METADATA", f"{key} is too long.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise data.ApiError(
            400, "INVALID_MODEL_METADATA", f"{key} contains invalid Unicode."
        ) from exc
    return text


def _valid_artifact_host(host):
    if not host:
        return False
    try:
        ip_address(host)
        return True
    except ValueError:
        if re.fullmatch(r"[\d.]+", host):
            return False
    try:
        host = host.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return False
    return len(host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    )


def _valid_artifact_uri(artifact):
    # urlsplit strips some control characters, so reject them before parsing.
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in artifact
    ):
        return False
    try:
        parsed = urlsplit(artifact)
        port = parsed.port  # Access validates numeric syntax and the 0..65535 bound.
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or not parsed.path
        ):
            return False
        if parsed.scheme == "file":
            return (
                parsed.path.startswith("/")
                and port is None
                and (not parsed.netloc or _valid_artifact_host(parsed.hostname))
            )
        if not _valid_artifact_host(parsed.hostname):
            return False
        if parsed.scheme == "https":
            return port is None or 1 <= port <= 65535
        if parsed.scheme == "s3":
            return port is None and ":" not in parsed.hostname
    except ValueError:
        return False
    return False


def _numbers(value, field, *, depth=0):
    if depth > 5:
        raise data.ApiError(
            400, "INVALID_MODEL_METADATA", f"{field} is too deeply nested."
        )
    if isinstance(value, dict) and value and len(value) <= 100:
        result = {}
        for key, item in value.items():
            normalized = _text({"key": key}, "key", maximum=100)
            if normalized in result:
                raise data.ApiError(
                    400, "INVALID_MODEL_METADATA", f"Duplicate {field} key."
                )
            result[normalized] = _numbers(
                item, f"{field}.{normalized}", depth=depth + 1
            )
        return result
    if isinstance(value, list) and value and len(value) <= 256:
        return [_numbers(item, field, depth=depth + 1) for item in value]
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            if math.isfinite(value) and abs(value) <= 1e100:
                return value
        except OverflowError:
            pass
    raise data.ApiError(
        400, "INVALID_MODEL_METADATA", f"{field} requires finite numeric values."
    )


def _frozen_dataset(user, dataset_id):
    dataset = data.dataset_version_for(user, dataset_id)
    if dataset["status"] != "frozen":
        raise data.ApiError(
            409, "DATASET_NOT_FROZEN", "A frozen dataset version is required."
        )
    return dataset


def _visible(user, row):
    allowed = set(user.get("allowedSiteIds", []))
    return "*" in allowed or set(row["scopeSiteIds"]).issubset(allowed)


def _require_visible(user, row):
    for site_id in row["scopeSiteIds"]:
        data.require_site_access(user, site_id)


def create_baseline_version(user, payload):
    data.require_permission(user, "baseline:write")
    if set(payload) - {
        "datasetId",
        "siteId",
        "assetId",
        "timeSegment",
        "features",
        "status",
    }:
        raise data.ApiError(400, "INVALID_MODEL_METADATA", "Unknown baseline fields.")
    dataset_id = _text(payload, "datasetId", maximum=100)
    site_id = _text(payload, "siteId", maximum=100).upper()
    asset_id = _text(payload, "assetId", maximum=100).upper()
    if payload.get("status", "draft") != "draft":
        raise data.ApiError(
            400, "INVALID_BASELINE_STATUS", "Only draft registration is supported."
        )
    features = payload.get("features")
    if not isinstance(features, dict):
        raise data.ApiError(
            400, "INVALID_MODEL_METADATA", "features must be an object."
        )
    features = _numbers(features, "features")
    segment = (
        _text(payload, "timeSegment", maximum=100) if "timeSegment" in payload else None
    )
    with data.STORE_LOCK:
        dataset = _frozen_dataset(user, dataset_id)
        data.get_asset(site_id, asset_id)
        data.require_site_access(user, site_id)
        scope = sorted(set(data._dataset_site_ids(dataset)) | {site_id})
        record = {
            "version": f"BASELINE-{len(data.BASELINE_VERSIONS)+1:05d}",
            "datasetId": dataset["id"],
            "datasetSnapshot": dataset,
            "siteId": site_id,
            "assetId": asset_id,
            "scopeSiteIds": scope,
            "timeSegment": segment,
            "features": features,
            "status": "draft",
            "approvalStatus": "pending",
            "deployed": False,
            "createdAt": data.now_iso(),
            "createdBy": user["id"],
            "limitations": LIMITATIONS,
        }
        data.BASELINE_VERSIONS.append(record)
        data.append_audit_log(
            user,
            "baseline.create",
            "baseline",
            record["version"],
            None,
            record,
            "Baseline metadata registered (not deployed)",
            site_ids=scope,
        )
        return data.copy_payload(record)


def create_model_version(user, payload):
    data.require_permission(user, "model:write")
    if set(payload) - {
        "version",
        "artifactUri",
        "datasetId",
        "baselineVersion",
        "metrics",
        "domainGap",
        "fieldCalibrationPlan",
        "errorCases",
    }:
        raise data.ApiError(
            400,
            "INVALID_MODEL_METADATA",
            "Unknown model fields; approval and deployment are not registration options.",
        )
    version = _text(payload, "version", maximum=80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise data.ApiError(400, "INVALID_MODEL_VERSION", "version must be URL-safe.")
    artifact = _text(payload, "artifactUri")
    if not _valid_artifact_uri(artifact):
        raise data.ApiError(
            400,
            "INVALID_ARTIFACT_URI",
            "Use an HTTPS, S3 or file reference with a valid host and path, "
            "without credentials; only HTTPS permits a port (1..65535).",
        )
    dataset_id = _text(payload, "datasetId", maximum=100)
    baseline_version = _text(payload, "baselineVersion", maximum=100)
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise data.ApiError(400, "INVALID_MODEL_METADATA", "metrics must be an object.")
    metrics = _numbers(metrics, "metrics")
    report = {}
    for field in ("domainGap", "fieldCalibrationPlan"):
        report[field] = _text(payload, field) if field in payload else None
    errors = payload.get("errorCases", [])
    if not isinstance(errors, list) or len(errors) > 100:
        raise data.ApiError(
            400,
            "INVALID_MODEL_METADATA",
            "errorCases must be a list of at most 100 strings.",
        )
    report["errorCases"] = [_text({"case": value}, "case") for value in errors]
    with data.STORE_LOCK:
        dataset = _frozen_dataset(user, dataset_id)
        baseline = next(
            (
                item
                for item in data.BASELINE_VERSIONS
                if item["version"] == baseline_version
            ),
            None,
        )
        if not baseline:
            raise data.ApiError(
                404, "BASELINE_NOT_FOUND", "Baseline version was not found."
            )
        _require_visible(user, baseline)
        if baseline["datasetId"] != dataset["id"]:
            raise data.ApiError(
                409,
                "MODEL_DATASET_MISMATCH",
                "Model and baseline must reference the same dataset version.",
            )
        if any(
            item["version"].casefold() == version.casefold()
            for item in data.MODEL_VERSIONS
        ):
            raise data.ApiError(
                409, "MODEL_VERSION_EXISTS", "Model versions are immutable."
            )
        record = {
            "version": version,
            "artifactUri": artifact,
            "datasetId": dataset["id"],
            "datasetSnapshot": dataset,
            "baselineVersion": baseline_version,
            "baselineSnapshot": data.copy_payload(baseline),
            "metrics": metrics,
            **report,
            "status": "draft",
            "approvalStatus": "pending",
            "deploymentStatus": "not_deployed",
            "scopeSiteIds": list(baseline["scopeSiteIds"]),
            "createdAt": data.now_iso(),
            "createdBy": user["id"],
            "limitations": LIMITATIONS,
            "artifactVerified": False,
        }
        data.MODEL_VERSIONS.append(record)
        data.append_audit_log(
            user,
            "model.create",
            "model",
            version,
            None,
            record,
            "Model metadata registered (artifact not executed or verified)",
            site_ids=record["scopeSiteIds"],
        )
        return data.copy_payload(record)


def versions_for(
    user, kind, *, version="", site_id="", asset_id="", status="", page=1, size=50
):
    data.require_permission(user, f"{kind}:read")
    if site_id:
        data.require_site_access(user, site_id)
    if status and status != "draft":
        raise data.ApiError(
            400,
            "INVALID_VERSION_STATUS",
            "Only draft versions are supported in this MVP.",
        )
    with data.STORE_LOCK:
        records = data.MODEL_VERSIONS if kind == "model" else data.BASELINE_VERSIONS
        if version:
            record = next(
                (item for item in records if item["version"] == version), None
            )
            if not record:
                raise data.ApiError(404, "VERSION_NOT_FOUND", "Version was not found.")
            _require_visible(user, record)
            return data.copy_payload(record)
        rows = [
            data.copy_payload(row)
            for row in records
            if _visible(user, row)
            and (
                not site_id
                or row.get("siteId", row.get("baselineSnapshot", {}).get("siteId"))
                == site_id
            )
            and (
                not asset_id
                or row.get("assetId", row.get("baselineSnapshot", {}).get("assetId"))
                == asset_id
            )
        ]
    rows.reverse()
    return {
        "items": rows[(page - 1) * size : page * size],
        "page": page,
        "size": size,
        "total": len(rows),
    }
