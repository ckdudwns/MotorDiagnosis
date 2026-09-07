"""Read-only base21 field archive and run-separated normal-study preparation.

Standard-library only. Never labels a machine normal from a model prediction.
This archives existing base21 storage; it cannot manufacture spectral66 inputs.
"""

import argparse
import collections
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

PROFILE = "mcc5-vibration-800hz-xyz-v1"
QUALITY = {
    "valid",
    "fifo_overrun",
    "sensor_unavailable",
    "sample_gap",
    "clipped",
    "constant_axis",
    "processing_overflow",
}
MAX_ROWS = 100000
# Two timestamps quantized to whole seconds can differ from uptime by <1s.
# This allowance is cumulative, never multiplied by the number of windows.
TIMESTAMP_UPTIME_TOLERANCE_US = 1000000
WINDOW_KEYS = set(
    "schemaVersion deviceId siteId assetId bootId windowIndex timestamp startUptimeUs sampleRateHz sampleCount profileId axes unit quality features".split()
)
STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def timestamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError(
            "Use a strict timestamp with timezone; 24:00 and leap seconds rejected"
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def decode(text):
    def invalid(value):
        raise ValueError("Invalid JSON number: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def dump(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def template(path):
    dump(
        path,
        {
            "schemaVersion": 1,
            "sessionId": "",
            "collectionRunId": "",
            "conditionId": "",
            "deviceId": "DEV-01-MOT-02",
            "siteId": "",
            "assetId": "",
            "start": "",
            "end": "",
            "operatingCondition": "",
            "firmwareVersion": "",
            "mountingDescription": "",
            "axisMapping": "",
            "gConversionEvidence": "",
            "normalReview": {
                "status": "pending",
                "reviewer": "",
                "reviewedAt": "",
                "basis": "",
            },
            "qualityLimits": {
                "minimumValidSeconds": 300,
                "maxMissingWindows": 0,
                "maxInvalidWindows": 0,
                "maxTimingErrorUs": None,
            },
            "notes": "Pilot 300s target is not statistical sufficiency. Fill IDs/scope from current device mapping. Agree timing tolerance; inspect normal state independently. One collectionRunId is a whole independent run, not a file chunk.",
        },
    )


def metadata(value):
    if (
        not isinstance(value, dict)
        or type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != 1
    ):
        raise ValueError("Unsupported session metadata")
    for key in (
        "sessionId",
        "collectionRunId",
        "conditionId",
        "deviceId",
        "siteId",
        "assetId",
    ):
        if not isinstance(value.get(key), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", value[key]
        ):
            raise ValueError("Fill session field: " + key)
    for key in (
        "operatingCondition",
        "firmwareVersion",
        "mountingDescription",
        "axisMapping",
        "gConversionEvidence",
    ):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("Fill measurement provenance: " + key)
    start, end = timestamp(value.get("start")), timestamp(value.get("end"))
    if not 0 < end - start <= 6 * 3600:
        raise ValueError("One export interval must be positive and at most six hours")
    limits = value.get("qualityLimits", {})
    if not isinstance(limits, dict):
        raise ValueError("Quality limits must be an object")
    for key in ("maxMissingWindows", "maxInvalidWindows"):
        if type(limits.get(key)) is not int or not 0 <= limits[key] <= MAX_ROWS:
            raise ValueError("Invalid quality limit: " + key)
    seconds = limits.get("minimumValidSeconds")
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 21600
    ):
        raise ValueError("Invalid minimumValidSeconds")
    timing = limits.get("maxTimingErrorUs")
    if timing is not None and (type(timing) is not int or not 0 <= timing < 640000):
        raise ValueError("Invalid explicit timing tolerance")
    review = value.get("normalReview", {})
    if not isinstance(review, dict):
        raise ValueError("Normal review must be an object")
    if review.get("status") not in ("pending", "confirmed_normal", "rejected"):
        raise ValueError("Unknown human normal review state")
    if review["status"] == "confirmed_normal":
        if any(
            not isinstance(review.get(k), str) or not review[k].strip()
            for k in ("reviewer", "basis")
        ):
            raise ValueError("Human normal review needs reviewer and independent basis")
        timestamp(review.get("reviewedAt"))
    return start, end


def features21(values):
    if not isinstance(values, list) or len(values) != 21:
        raise ValueError("Require base21, not four telemetry summaries or spectral66")
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1e12
        for v in values
    ):
        raise ValueError("Invalid feature number")
    for i in (0, 7, 14):
        rms, peak, kurtosis = values[i : i + 3]
        if rms <= 1e-8 or peak < rms * (1 - 1e-9) or kurtosis < 1 - 1e-9:
            raise ValueError("Invalid base21 RMS/peak/Pearson kurtosis")


def validate_window(window, meta, start, end):
    if not isinstance(window, dict):
        raise ValueError("Window must be an object")
    if set(window) != WINDOW_KEYS:
        raise ValueError("Unexpected or missing window fields")
    for key in ("deviceId", "siteId", "assetId"):
        if window.get(key) != meta[key]:
            raise ValueError("Session scope mismatch: " + key)
    if (
        type(window.get("schemaVersion")) is not int
        or window["schemaVersion"] != 1
        or window.get("profileId") != PROFILE
        or window.get("axes") != ["X", "Y", "Z"]
        or window.get("unit") != "g"
        or type(window.get("sampleRateHz")) is not int
        or window["sampleRateHz"] != 800
    ):
        raise ValueError("Require exact base21 800Hz XYZ g contract")
    if not isinstance(window.get("bootId"), str) or not re.fullmatch(
        r"[0-9a-f]{32}", window["bootId"]
    ):
        raise ValueError("Invalid bootId")
    for key, maximum in (
        ("windowIndex", 2**31 - 1),
        ("startUptimeUs", 2**53 - 1),
        ("sampleCount", 512),
    ):
        if type(window.get(key)) is not int or not 0 <= window[key] <= maximum:
            raise ValueError("Invalid window integer: " + key)
    captured = timestamp(window.get("timestamp"))
    if captured < start - 1e-6 or captured + 0.640 > end + 1e-6:
        raise ValueError("Window is not fully inside the session interval")
    quality = window.get("quality")
    if not isinstance(quality, str) or quality not in QUALITY:
        raise ValueError("Unknown measurement quality")
    if quality == "valid":
        if window["sampleCount"] != 512:
            raise ValueError("Valid window requires 512 samples")
        features21(window.get("features"))
    elif window.get("features") is not None:
        raise ValueError("Invalid measurements must not contain filled features")
    return captured


def inspect(meta, path):
    start, end = metadata(meta)
    windows, identities, streams, qualities = 0, set(), {}, collections.Counter()
    missing, max_error, first, last = 0, 0, None, None
    anchors, max_clock_error = {}, 0
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if len(line) > 128000 or not line.strip():
                raise ValueError("Invalid/oversized NDJSON line")
            w = decode(line)
            captured = validate_window(w, meta, start, end)
            key = (w["deviceId"], w["bootId"], w["windowIndex"])
            if key in identities:
                raise ValueError("Duplicate window identity")
            identities.add(key)
            previous = streams.get(w["bootId"])
            captured_us = round(captured * 1000000)
            anchor_uptime, anchor_timestamp = anchors.setdefault(
                w["bootId"], (w["startUptimeUs"], captured_us)
            )
            max_clock_error = max(
                max_clock_error,
                abs(
                    (captured_us - anchor_timestamp)
                    - (w["startUptimeUs"] - anchor_uptime)
                ),
            )
            if previous is not None:
                delta = w["windowIndex"] - previous[0]
                if (
                    delta <= 0
                    or w["startUptimeUs"] <= previous[1]
                    or captured < previous[2]
                ):
                    raise ValueError("Out-of-order sequence/clock")
                missing += delta - 1
                max_error = max(
                    max_error, abs(w["startUptimeUs"] - previous[1] - delta * 640000)
                )
            streams[w["bootId"]] = (w["windowIndex"], w["startUptimeUs"], captured)
            qualities[w["quality"]] += 1
            first = captured if first is None else min(first, captured)
            last = captured if last is None else max(last, captured)
            windows += 1
            if windows > MAX_ROWS:
                raise ValueError("Export exceeds bounded session size")
    valid = qualities["valid"]
    limits = meta["qualityLimits"]
    reasons = []
    if meta["normalReview"]["status"] != "confirmed_normal":
        reasons.append("HUMAN_NORMAL_REVIEW_REQUIRED")
    if len(streams) != 1:
        reasons.append("REQUIRE_ONE_BOOT_PER_SESSION")
    if windows < 2:
        reasons.append("INSUFFICIENT_TIMING_PAIRS")
    if max_clock_error > TIMESTAMP_UPTIME_TOLERANCE_US:
        reasons.append("TIMESTAMP_UPTIME_MISMATCH")
    if valid * 0.640 < limits["minimumValidSeconds"]:
        reasons.append("INSUFFICIENT_VALID_DURATION")
    if windows - valid > limits["maxInvalidWindows"]:
        reasons.append("INVALID_WINDOW_LIMIT")
    if missing > limits["maxMissingWindows"]:
        reasons.append("MISSING_WINDOW_LIMIT")
    if limits["maxTimingErrorUs"] is None:
        reasons.append("TIMING_TOLERANCE_NOT_AGREED")
    elif max_error > limits["maxTimingErrorUs"]:
        reasons.append("TIMING_TOLERANCE_EXCEEDED")
    # Counts alone cannot prove coverage. Check UTC edges and the independent
    # uptime span too; second-precision UTC gets a fixed quantization allowance.
    uptime_span = max(
        (streams[boot][1] - anchor[0] + 640000 for boot, anchor in anchors.items()),
        default=0,
    ) / 1000000
    edge_tolerance = 0.640 + TIMESTAMP_UPTIME_TOLERANCE_US / 1000000
    if (windows < max(0, math.floor((end - start) / 0.640) - 1)
            or first is None or first - start > edge_tolerance
            or end - (last + 0.640) > edge_tolerance
            or uptime_span + 2 * 0.640 < end - start - 0.000001):
        reasons.append("INCOMPLETE_REQUESTED_INTERVAL")
    return {
        "sessionId": meta["sessionId"],
        "collectionRunId": meta["collectionRunId"],
        "conditionId": meta["conditionId"],
        "windows": windows,
        "validWindows": valid,
        "validSeconds": valid * 0.640,
        "missingWindowsInsideStream": missing,
        "maxAdjacentTimingErrorUs": max_error,
        "maxTimestampUptimeErrorUs": max_clock_error,
        "timestampUptimeToleranceUs": TIMESTAMP_UPTIME_TOLERANCE_US,
        "validSecondsBasis": "quality-valid window count x 0.640; not verified wall-clock duration",
        "qualityCounts": dict(qualities),
        "observedSpanSeconds": 0 if first is None else last - first + 0.640,
        "observedUptimeSpanSeconds": uptime_span,
        "requestedIntervalBoundaryUncertaintyWindows": 1,
        "passesCollectionChecks": not reasons,
        "blockingReasons": reasons,
        "spectral66Ready": False,
        "spectral66Reason": "BASE21_CANNOT_RECONSTRUCT_EXTRA_RAW_SPECTRAL_FEATURES",
        "fieldValidated": False,
        "normalLabelSource": "human session review, never model verdict",
    }, identities


def export(database, session_file, output):
    database, session_file, output = map(Path, (database, session_file, output))
    if output.exists():
        raise FileExistsError("Use a new archive directory")
    meta = decode(session_file.read_text(encoding="utf-8"))
    start, end = metadata(meta)
    if not database.is_file():
        raise FileNotFoundError("Provide the actual existing vibration window database")
    # mode=ro prevents accidentally creating or modifying the source database.
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        cursor = connection.execute(
            "SELECT body FROM vibration_windows WHERE device=? AND site=? AND asset=? AND captured>=? AND captured<=? ORDER BY captured,ordinal",
            (
                meta["deviceId"],
                meta["siteId"],
                meta["assetId"],
                start - 1e-6,
                end - 0.640 + 1e-6,
            ),
        )
        output.mkdir(parents=True)
        with (output / "windows.ndjson").open(
            "x", encoding="utf-8", newline="\n"
        ) as target:
            for index, (body,) in enumerate(cursor):
                if index >= MAX_ROWS or len(body) > 128000:
                    raise ValueError("Oversized source session")
                validate_window(decode(body), meta, start, end)
                target.write(body.strip() + "\n")
    dump(output / "session.json", meta)
    report, _ = inspect(meta, output / "windows.ndjson")
    report.update(
        {
            "metadataSha256": digest(output / "session.json"),
            "windowsSha256": digest(output / "windows.ndjson"),
            "source": "read-only SQLite snapshot of vibration_windows.body; no model analysis/credentials exported",
        }
    )
    dump(output / "audit.json", report)
    return report


def check_archive(folder):
    folder = Path(folder)
    meta = decode((folder / "session.json").read_text(encoding="utf-8"))
    original = decode((folder / "audit.json").read_text(encoding="utf-8"))
    if original["metadataSha256"] != digest(folder / "session.json") or original[
        "windowsSha256"
    ] != digest(folder / "windows.ndjson"):
        raise ValueError("Archive hash mismatch")
    report, identities = inspect(meta, folder / "windows.ndjson")
    return meta, original, report, identities


def build(sessions, output, seed=42):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a new immutable collection plan directory")
    records, seen_windows, seen_sessions, runs_by_condition = (
        [],
        set(),
        set(),
        collections.defaultdict(set),
    )
    run_context = {}
    for folder in map(Path, sessions):
        meta, original, report, identities = check_archive(folder)
        if not report["passesCollectionChecks"]:
            raise ValueError(
                "Session not ready: "
                + meta["sessionId"]
                + " "
                + ",".join(report["blockingReasons"])
            )
        if meta["sessionId"] in seen_sessions or seen_windows & identities:
            raise ValueError(
                "Duplicated session or overlapping device/boot/window records"
            )
        seen_sessions.add(meta["sessionId"])
        seen_windows.update(identities)
        run = meta["collectionRunId"]
        context = (
            meta["deviceId"],
            meta["siteId"],
            meta["assetId"],
            meta["conditionId"],
            meta["mountingDescription"],
            meta["axisMapping"],
            meta["firmwareVersion"],
            meta["gConversionEvidence"],
        )
        if run in run_context and run_context[run] != context:
            raise ValueError(
                "One run changed condition/mounting/scope; review session boundaries"
            )
        run_context[run] = context
        condition = context  # do not silently mix devices or mounting configurations
        runs_by_condition[condition].add(run)
        records.append(
            {
                "sessionId": meta["sessionId"],
                "collectionRunId": run,
                "conditionId": meta["conditionId"],
                "archive": str(folder.resolve()),
                "metadataSha256": original["metadataSha256"],
                "windowsSha256": original["windowsSha256"],
                "validWindows": report["validWindows"],
            }
        )
    if not records:
        raise ValueError("No observed sessions; a template is not collected data")
    assignment = {}
    for run_ids in runs_by_condition.values():
        if len(run_ids) < 2:
            raise ValueError(
                "Each device/mounting/condition needs at least two independent collectionRunIds"
            )
        ordered = sorted(
            run_ids, key=lambda r: hashlib.sha256(f"{seed}:{r}".encode()).hexdigest()
        )
        evaluation_count = max(1, math.ceil(len(ordered) * 0.25))
        for i, run in enumerate(ordered):
            assignment[run] = "evaluation" if i < evaluation_count else "calibration"
    for record in records:
        record["split"] = assignment[record["collectionRunId"]]
    output.mkdir(parents=True)
    result = {
        "schemaVersion": 1,
        "profileId": PROFILE,
        "seed": seed,
        "sessions": records,
        "splitUnit": "whole independently identified collectionRunId, not random640ms windows",
        "normalOnly": True,
        "spectral66Ready": False,
        "fieldValidated": False,
        "limits": [
            "At least two runs is a structural minimum, not statistical sufficiency",
            "Operator must verify that run IDs represent independent recording sessions",
            "This is a split plan, not a model training/calibration execution",
            "Normal data alone cannot validate anomaly recall; keep evaluation sealed until policy is frozen",
        ],
    }
    dump(output / "collection.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("template")
    init.add_argument("--output", required=True)
    save = sub.add_parser("export")
    save.add_argument("--database", required=True)
    save.add_argument("--session", required=True)
    save.add_argument("--output", required=True)
    check = sub.add_parser("check")
    check.add_argument("--archive", required=True)
    plan = sub.add_parser("build")
    plan.add_argument("--sessions", nargs="+", required=True)
    plan.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.action == "template":
        template(args.output)
        print(
            "Empty session template created; no data collected or normal label assigned."
        )
    elif args.action == "export":
        print(
            json.dumps(
                export(args.database, args.session, args.output),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.action == "check":
        print(json.dumps(check_archive(args.archive)[2], ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(build(args.sessions, args.output), ensure_ascii=False, indent=2)
        )
