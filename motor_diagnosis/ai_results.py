"""AI1 result handoff and explicit human review, never model deployment."""

import hashlib
import hmac
import json
import os
import re

from . import data, model_registry
from .device_credentials import TOKEN_PATTERN, supported_identifier


def digest(value):
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (ValueError, TypeError, RecursionError) as error:
        raise data.ApiError(
            400, "INVALID_AI_RESULT", "Result evidence must be finite JSON data."
        ) from error
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def text(payload, field, maximum=2000):
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise data.ApiError(
            400,
            "INVALID_AI_RESULT",
            f"{field} must be a nonempty string of at most {maximum} characters.",
        )
    return model_registry._text(payload, field, maximum=maximum)


def checksum(payload, field):
    value = text(payload, field, 71)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise data.ApiError(
            400, "INVALID_AI_RESULT", f"{field} must be a lowercase SHA-256 checksum."
        )
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def producer_for_token(token, producer_id):
    if not isinstance(token, str) or not re.fullmatch(TOKEN_PATTERN, token):
        raise data.ApiError(
            401, "AI_RESULT_AUTH_REQUIRED", "A dedicated AI1 result token is required."
        )
    try:
        configured = json.loads(
            os.environ.get("AI1_RESULT_PRODUCERS_JSON", "{}"),
            object_pairs_hook=unique_object,
        )
        if not isinstance(configured, dict):
            raise ValueError("Invalid producers")
        secrets = set()
        for name, config in configured.items():
            if (
                not supported_identifier(name)
                or not isinstance(config, dict)
                or set(config) != {"token", "allowedSiteIds"}
            ):
                raise ValueError("Invalid producer")
            secret, sites = config["token"], config["allowedSiteIds"]
            if (
                not isinstance(secret, str)
                or not re.fullmatch(TOKEN_PATTERN, secret)
                or secret in secrets
            ):
                raise ValueError("Invalid token")
            secrets.add(secret)
            if (
                not isinstance(sites, list)
                or not sites
                or not all(supported_identifier(site) for site in sites)
                or len(set(sites)) != len(sites)
            ):
                raise ValueError("Explicit site scope is required")
    except (TypeError, ValueError) as error:
        raise data.ApiError(
            503,
            "AI_RESULT_AUTH_UNAVAILABLE",
            "AI1 result authentication is not correctly configured.",
        ) from error
    for name, config in configured.items():
        if hmac.compare_digest(token.encode("ascii"), config["token"].encode("ascii")):
            if name != producer_id:
                raise data.ApiError(
                    403,
                    "AI_RESULT_PRODUCER_FORBIDDEN",
                    "The token belongs to another producer.",
                )
            return {
                "id": f"ai1:{name}",
                "username": name,
                "name": f"AI1 {name}",
                "role": "AI1",
                "allowedSiteIds": list(config["allowedSiteIds"]),
            }
    raise data.ApiError(
        401, "AI_RESULT_AUTH_REQUIRED", "A dedicated AI1 result token is required."
    )


MODEL_FIELDS = {
    "version",
    "artifactUri",
    "datasetId",
    "baselineVersion",
    "metrics",
    "domainGap",
    "fieldCalibrationPlan",
    "errorCases",
}
EVIDENCE_FIELDS = {
    "producerId",
    "jobId",
    "artifactChecksum",
    "datasetSourceChecksum",
    "datasetFingerprint",
    "ai1DatasetId",
    "ai1SnapshotDigest",
    "reportChecksum",
    "candidate",
    "independentHoldout",
    "evaluation",
}


def registration_digest(record):
    # Fixed immutable content: later review fields are not registration evidence.
    keys = MODEL_FIELDS | {
        "artifactChecksum",
        "datasetSnapshot",
        "baselineSnapshot",
        "scopeSiteIds",
        "submission",
        "createdAt",
        "createdBy",
    }
    return digest({key: record.get(key) for key in sorted(keys)})


def submit_result(token, payload):
    if not isinstance(payload, dict):
        raise data.ApiError(400, "INVALID_AI_RESULT", "A result object is required.")
    user = producer_for_token(token, payload.get("producerId"))
    if set(payload) != MODEL_FIELDS | EVIDENCE_FIELDS:
        raise data.ApiError(
            400,
            "INVALID_AI_RESULT",
            "Missing or unsupported result fields; approval is never an input.",
        )
    evidence = {
        key: text(
            payload,
            key,
            (
                100
                if key in {"producerId", "jobId", "candidate", "ai1DatasetId"}
                else 2000
            ),
        )
        for key in EVIDENCE_FIELDS - {"independentHoldout"}
    }
    for key in (
        "artifactChecksum",
        "datasetSourceChecksum",
        "datasetFingerprint",
        "ai1SnapshotDigest",
        "reportChecksum",
    ):
        evidence[key] = checksum(payload, key)
    if type(payload["independentHoldout"]) is not bool:
        raise data.ApiError(
            400, "INVALID_AI_RESULT", "independentHoldout must be a boolean."
        )
    evidence["independentHoldout"] = payload["independentHoldout"]
    model = {key: payload[key] for key in MODEL_FIELDS}
    # Validate numeric leaves before hashing; reject NaN and unbounded structures.
    model_registry._numbers(model["metrics"], "metrics")
    submission_digest = digest(payload)
    with data.STORE_LOCK:
        previous = next(
            (
                row
                for row in data.MODEL_VERSIONS
                if row.get("submission", {}).get("producerId") == evidence["producerId"]
                and row["submission"].get("jobId") == evidence["jobId"]
            ),
            None,
        )
        if previous:
            model_registry._require_visible(user, previous)
            if previous["submission"]["digest"] != submission_digest:
                raise data.ApiError(
                    409,
                    "AI_RESULT_CONFLICT",
                    "The same producer/job identity has different content.",
                )
            return {
                "accepted": True,
                "duplicate": True,
                "submissionDigest": submission_digest,
                "model": data.copy_payload(previous),
            }, 200
        dataset = model_registry._frozen_dataset(user, model["datasetId"])
        if (
            dataset.get("source", {}).get("checksum")
            != evidence["datasetSourceChecksum"]
            or dataset.get("versionFingerprint") != evidence["datasetFingerprint"]
        ):
            raise data.ApiError(
                409,
                "AI_RESULT_DATASET_MISMATCH",
                "The training source checksum and backend frozen dataset fingerprint must match.",
            )
        # Existing model registration owns dataset/baseline validation and audit.
        created = model_registry.create_model_version(user, model)
        record = next(
            row for row in data.MODEL_VERSIONS if row["version"] == created["version"]
        )
        record.update(
            artifactChecksum=evidence["artifactChecksum"],
            submission={**evidence, "digest": submission_digest},
            approvalRevision=0,
            reviewHistory=[],
        )
        record["registrationDigest"] = registration_digest(record)
        data.append_audit_log(
            user,
            "model.handoff",
            "model",
            record["version"],
            None,
            record,
            "AI1 result received for human review; not deployed or server-verified",
            site_ids=record["scopeSiteIds"],
        )
        result = {
            "accepted": True,
            "duplicate": False,
            "submissionDigest": submission_digest,
            "model": data.copy_payload(record),
        }
    return result, 201


def review_model(user, version, payload):
    data.require_permission(user, "model:review")
    if not isinstance(payload, dict) or set(payload) != {
        "decision",
        "reason",
        "requestId",
        "expectedRevision",
        "registrationDigest",
    }:
        raise data.ApiError(
            400,
            "INVALID_MODEL_REVIEW",
            "A decision, reason, request ID and reviewed revision/digest are required.",
        )
    decision = payload["decision"]
    if decision not in ("approve", "reject"):
        raise data.ApiError(
            400, "INVALID_MODEL_REVIEW", "decision must be approve or reject."
        )
    reason = text(payload, "reason", 1000)
    request_id = text(payload, "requestId", 80)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id)
        or type(payload["expectedRevision"]) is not int
        or payload["expectedRevision"] < 0
    ):
        raise data.ApiError(
            400, "INVALID_MODEL_REVIEW", "Invalid request ID or expected revision."
        )
    expected_digest = checksum(payload, "registrationDigest")
    request_digest = digest({**payload, "actorId": user["id"]})
    with data.STORE_LOCK:
        record = next(
            (row for row in data.MODEL_VERSIONS if row["version"] == version), None
        )
        if record is None:
            raise data.ApiError(
                404, "VERSION_NOT_FOUND", "Model version was not found."
            )
        model_registry._require_visible(user, record)
        if not record.get("submission") or not record.get("artifactChecksum"):
            raise data.ApiError(
                409,
                "MODEL_NOT_REVIEWABLE",
                "This legacy metadata lacks a linked AI1 result and checksum; submit a new version.",
            )
        if record["createdBy"] == user["id"]:
            raise data.ApiError(
                403,
                "MODEL_SELF_REVIEW_FORBIDDEN",
                "The submitter cannot approve or reject their own result.",
            )
        if registration_digest(record) != record.get("registrationDigest"):
            raise data.ApiError(
                409,
                "MODEL_EVIDENCE_CHANGED",
                "Stored registration evidence is inconsistent.",
            )
        previous = next(
            (row for row in record["reviewHistory"] if row["requestId"] == request_id),
            None,
        )
        if previous:
            if previous["requestDigest"] != request_digest:
                raise data.ApiError(
                    409,
                    "MODEL_REVIEW_CONFLICT",
                    "The review request ID has different content or actor.",
                )
            return data.copy_payload(record)
        if (
            record["approvalRevision"] != payload["expectedRevision"]
            or expected_digest != record["registrationDigest"]
        ):
            raise data.ApiError(
                409, "MODEL_REVIEW_STALE", "Reload the current result before reviewing."
            )
        if record["approvalStatus"] != "pending":
            raise data.ApiError(
                409,
                "MODEL_REVIEW_FINAL",
                "This result is already reviewed; corrections require a new model version.",
            )
        before = data.copy_payload(record)
        status = "approved" if decision == "approve" else "rejected"
        reviewed_at = data.now_iso()
        entry = {
            "requestId": request_id,
            "requestDigest": request_digest,
            "revision": record["approvalRevision"] + 1,
            "decision": decision,
            "reason": reason,
            "reviewedAt": reviewed_at,
            "reviewedBy": user["id"],
            "registrationDigest": expected_digest,
            "metricSnapshot": data.copy_payload(record["metrics"]),
            "artifactChecksum": record["artifactChecksum"],
            "datasetId": record["datasetId"],
            "baselineVersion": record["baselineVersion"],
        }
        record.update(
            status=status,
            approvalStatus=status,
            approvalRevision=entry["revision"],
            reviewedAt=reviewed_at,
            reviewedBy=user["id"],
        )
        record["reviewHistory"].append(entry)
        # Human acceptance does not verify remote file bytes or deploy anything.
        data.append_audit_log(
            user,
            f"model.{decision}",
            "model",
            version,
            before,
            record,
            reason,
            site_ids=record["scopeSiteIds"],
        )
        result = data.copy_payload(record)
    return result
