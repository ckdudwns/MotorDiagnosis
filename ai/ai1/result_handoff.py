"""Opt-in, immutable AI1 result outbox. No model loading, approval or deployment.

Run from the repository root: python -m ai.ai1.result_handoff --pending FILE
Credentials come only from AI1_RESULT_URL / AI1_RESULT_TOKEN, never the outbox.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, url2pathname

from ai.ai1.week4.ai1.dataset_versions.dataset_version import verify_frozen_integrity
from ai.ai1.week4.ai1.dataset_versions.model_version import ModelVersionRegistry


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def build_submission(report, frozen, target):
    verify_frozen_integrity(frozen)
    required = {"producerId", "datasetId", "datasetFingerprint", "baselineVersion"}
    if (
        not isinstance(target, dict)
        or set(target) - (required | {"artifactUri"})
        or not required <= set(target)
    ):
        raise ValueError(
            "Target requires producerId, datasetId, datasetFingerprint and baselineVersion"
        )
    if any(
        not isinstance(target[key], str) or not target[key].strip() for key in required
    ):
        raise ValueError("Target fields must be nonempty strings")
    if (
        report.get("status") != "completed"
        or report.get("datasetId") != frozen["id"]
        or report.get("datasetSnapshotDigest") != frozen["snapshotDigest"]
    ):
        raise ValueError(
            "A completed report linked to this exact frozen dataset is required"
        )
    summary = report["metrics"]
    selected = [
        row for row in report["candidates"] if row["name"] == summary["bestCandidate"]
    ]
    if (
        len(selected) != 1
        or not selected[0].get("metrics")
        or summary.get("selectionCriterion") != "validation_f1"
        or type(summary.get("independentHoldout")) is not bool
    ):
        raise ValueError(
            "One validation-selected, test-evaluated candidate is required"
        )
    candidate = selected[0]
    uri = urlsplit(candidate["artifactUri"])
    if (
        uri.scheme != "file"
        or uri.netloc not in ("", "localhost")
        or uri.query
        or uri.fragment
    ):
        raise ValueError(
            "Verify the training artifact from a local file URI before handoff"
        )
    artifact = Path(url2pathname(uri.path))
    actual = hashlib.sha256()
    with artifact.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(block)
    if "sha256:" + actual.hexdigest() != candidate["artifactChecksum"]:
        raise ValueError("Training artifact bytes do not match the report checksum")
    version = report["id"] + "-" + candidate["name"]
    metrics = {
        "validation": candidate["validationMetrics"],
        "test": candidate["metrics"],
    }
    artifact_uri = target.get("artifactUri", candidate["artifactUri"])
    # Reuse AI1's canonical registration validation; backend approval is separate.
    ModelVersionRegistry().register(
        version=version,
        artifact_uri=artifact_uri,
        dataset_id=target["datasetId"],
        baseline_version=target["baselineVersion"],
        metrics=metrics,
        artifact_checksum=candidate["artifactChecksum"],
    )
    errors = report["errorCases"].get(candidate["name"])
    if (
        not isinstance(errors, dict)
        or set(errors) != {"false_positives", "false_negatives"}
        or any(not isinstance(rows, list) for rows in errors.values())
        or any(not isinstance(row, dict) for rows in errors.values() for row in rows)
    ):
        raise ValueError(
            "Selected candidate needs false_positives/false_negatives lists"
        )
    error_rows = [
        {"category": category, "evidence": row}
        for category, rows in errors.items()
        for row in rows
    ]
    payload = {
        **{key: target[key] for key in required},
        "jobId": report["id"],
        "version": version,
        "artifactUri": artifact_uri,
        "artifactChecksum": candidate["artifactChecksum"],
        "metrics": metrics,
        "domainGap": _json_text(report["domainGap"]),
        "fieldCalibrationPlan": _json_text(report["fieldCalibrationPlan"]),
        "errorCases": [_json_text(row) for row in error_rows],
        "datasetSourceChecksum": frozen["source"]["checksum"],
        "ai1DatasetId": frozen["id"],
        "ai1SnapshotDigest": frozen["snapshotDigest"],
        "reportChecksum": digest(report),
        "candidate": candidate["name"],
        "independentHoldout": summary["independentHoldout"],
        "evaluation": summary["evaluation"],
    }
    canonical(payload)
    return payload


def store_immutable(path, value):
    """Publish a fully fsynced JSON file without overwriting an existing intent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical(value)
    descriptor, temporary = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise ValueError(
                    "An existing outbox/receipt has different content; use a new job"
                )
    finally:
        os.unlink(temporary)


def configured_transport():
    url, token = os.environ.get("AI1_RESULT_URL", ""), os.environ.get(
        "AI1_RESULT_TOKEN", ""
    )
    parsed = urlsplit(url)
    local_http = (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        and os.environ.get("AI1_RESULT_ALLOW_LOCAL_HTTP") == "true"
    )
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/ai1/results"
        or any(character.isspace() for character in url)
    ):
        raise ValueError(
            "Configure an HTTPS AI1_RESULT_URL; loopback HTTP requires explicit development opt-in"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError(
            "Configure a dedicated AI1_RESULT_TOKEN (not a user/device token)"
        )
    return url, token


def prepare_handoff(report, frozen, target, pending_path):
    url, _ = configured_transport()
    payload = build_submission(report, frozen, target)
    pending = {
        "schemaVersion": 1,
        "url": url,
        "submissionDigest": digest(payload),
        "payload": payload,
    }
    if len(canonical(pending)) > 262144:
        raise ValueError("Result metadata exceeds the 256 KiB outbox limit")
    store_immutable(pending_path, pending)
    return Path(pending_path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_pending(pending_path, *, attempts=3, sleep=time.sleep):
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError("Use one to five bounded attempts")
    url, token = configured_transport()
    pending_path = Path(pending_path)
    if pending_path.stat().st_size > 262144:
        raise ValueError("Outbox is too large")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if (
        set(pending) != {"schemaVersion", "url", "submissionDigest", "payload"}
        or type(pending["schemaVersion"]) is not int
        or pending["schemaVersion"] != 1
        or pending["url"] != url
        or pending["submissionDigest"] != digest(pending["payload"])
    ):
        raise ValueError("Outbox integrity or configured destination changed")
    payload = pending["payload"]
    opener = build_opener(NoRedirect())
    for attempt in range(attempts):
        retryable = True
        try:
            request = Request(
                url,
                data=canonical(payload),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with opener.open(request, timeout=10) as response:
                status, body = response.status, response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Oversized handoff acknowledgement")
            ack = json.loads(body)
            if (
                not isinstance(ack, dict)
                or not isinstance(ack.get("model"), dict)
                or not isinstance(ack["model"].get("submission"), dict)
            ):
                raise ValueError("Invalid handoff acknowledgement structure")
            model = ack.get("model", {})
            source = model.get("submission", {})
            if (
                status not in (200, 201)
                or ack.get("accepted") is not True
                or type(ack.get("duplicate")) is not bool
                or ack["duplicate"] != (status == 200)
                or ack.get("submissionDigest") != pending["submissionDigest"]
                or any(
                    model.get(key) != payload[key]
                    for key in (
                        "version",
                        "artifactChecksum",
                        "datasetId",
                        "baselineVersion",
                    )
                )
                or any(
                    source.get(key) != payload[key] for key in ("producerId", "jobId")
                )
                or source.get("digest") != pending["submissionDigest"]
            ):
                raise ValueError(
                    "Handoff acknowledgement does not match the pending result"
                )
            receipt = {
                "version": payload["version"],
                "producerId": payload["producerId"],
                "jobId": payload["jobId"],
                "submissionDigest": pending["submissionDigest"],
            }
            store_immutable(str(pending_path) + ".receipt.json", receipt)
            return receipt
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            error.close()
        except (URLError, TimeoutError, OSError):
            pass
        if not retryable or attempt + 1 == attempts:
            raise RuntimeError(
                "AI1 handoff was not acknowledged; the immutable outbox is retained. Check configuration/server and retry it without retraining."
            ) from None
        sleep(min(2**attempt, 4))


def main():
    parser = argparse.ArgumentParser(
        description="Retry a prepared AI1 result, without training or approval"
    )
    parser.add_argument("--pending", required=True)
    parser.add_argument(
        "--report", help="Prepare from an existing completed report, without retraining"
    )
    parser.add_argument("--manifest")
    parser.add_argument("--target")
    args = parser.parse_args()
    if any((args.report, args.manifest, args.target)):
        if not all((args.report, args.manifest, args.target)):
            parser.error("--report, --manifest and --target must be supplied together")
        report, frozen, target = [
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in (args.report, args.manifest, args.target)
        ]
        prepare_handoff(report, frozen, target, args.pending)
    receipt = send_pending(args.pending)
    print(f"Registered for human review: {receipt['version']} (not deployed)")


if __name__ == "__main__":
    main()
