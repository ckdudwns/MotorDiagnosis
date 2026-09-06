"""Device-observed HTTP quality windows; independent, idempotent SQLite store.

These are observations, not delivery guarantees or telemetry/label inputs.
An entire window is attributed to its end time. Long/offline windows are
explicitly flagged when they cross a requested bucket boundary, never split
into invented per-minute counts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import data, device_lifecycle
from .device_credentials import device_for_token, supported_identifier

SCHEMA_VERSION = 1
STORAGE_SCHEMA_VERSION = 2
RETENTION_DAYS = 30
MAX_INTEGER = 2**53 - 1
MAX_BUCKETS = 1000
COUNTERS = (
    "attempts",
    "retries",
    "replayAttempts",
    "acknowledged",
    "transportFailures",
    "retryableResponses",
    "configurationFailures",
    "rejectedPackets",
    "ackLatencyTotalMs",
    "ackLatencyMaxMs",
    "bufferSamples",
    "bufferDepthSum",
    "bufferDepthMax",
    "bufferDepthLast",
    "bufferCapacity",
    "bufferDropped",
    "wifiSamples",
    "offlineSamples",
)
ADDITIVE = tuple(
    key
    for key in COUNTERS
    if key
    not in {"ackLatencyMaxMs", "bufferDepthMax", "bufferDepthLast", "bufferCapacity"}
)


def _invalid(message):
    raise data.ApiError(400, "INVALID_COMMUNICATION_QUALITY", message)


def _integer(value, name, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(f"{name} must be an integer in [{minimum}, {maximum}].")
    return value


def _timestamp(value, name):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", value)
        is None
    ):
        _invalid(f"{name} must be an RFC3339 UTC timestamp with at most milliseconds.")
    parsed = data.parse_rfc3339(name, value)
    delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _iso(epoch_ms):
    return data.format_rfc3339(
        datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=epoch_ms)
    )


def _scope(device_id, *, user=None, active=False):
    # Release the master-state lock before waiting on the independent metrics DB.
    # Stored site/asset IDs are immutable capture provenance, not mutable joins.
    with data.STORE_LOCK:
        device = data.get_device(device_id)
        if user is not None:
            data.require_permission(user, "device:read")
            data.require_site_access(user, device["siteId"])
        if active and (
            device.get("mappingStatus") != "active"
            or device.get("certificateStatus") != "registered"
        ):
            raise data.ApiError(
                409,
                "COMMUNICATION_QUALITY_INACTIVE",
                "An active registered device is required.",
            )
        scope = {
            "deviceId": device["id"],
            "siteId": device["siteId"],
            "assetId": device["assetId"],
        }
        if active and not all(supported_identifier(value) for value in scope.values()):
            raise data.ApiError(
                409,
                "COMMUNICATION_QUALITY_UNSUPPORTED_ID",
                "The target ID is unsupported by the firmware.",
            )
        return scope


def normalize_report(payload, scope, now_ms):
    fields = {
        "schemaVersion",
        "deviceId",
        "siteId",
        "assetId",
        "transport",
        "bootId",
        "windowId",
        "startedAt",
        "endedAt",
        "startUptimeMs",
        "endUptimeMs",
        "metrics",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        _invalid("The report has missing or unsupported fields.")
    if (
        _integer(payload["schemaVersion"], "schemaVersion") != SCHEMA_VERSION
        or payload["transport"] != "http"
    ):
        _invalid("Only schemaVersion 1 and the HTTP transport are supported.")
    if any(payload[key] != value for key, value in scope.items()):
        raise data.ApiError(
            409,
            "COMMUNICATION_QUALITY_SCOPE_MISMATCH",
            "Report scope does not match the current device mapping.",
        )
    if (
        not isinstance(payload["bootId"], str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["bootId"]) is None
    ):
        _invalid("bootId must contain 32 lowercase hexadecimal characters.")
    _integer(payload["windowId"], "windowId", 1, 2**32 - 1)
    start_up = _integer(payload["startUptimeMs"], "startUptimeMs")
    end_up = _integer(payload["endUptimeMs"], "endUptimeMs", start_up + 1)
    start = _timestamp(payload["startedAt"], "startedAt")
    end = _timestamp(payload["endedAt"], "endedAt")
    # A single UTC anchor plus monotonic uptime maps the whole boot. Do not
    # silently adjust device timestamps on retry, restart, or ingestion.
    if start < 0 or end - start < 60_000 or end - start != end_up - start_up:
        _invalid("UTC and monotonic window durations must match and be positive.")
    if end > now_ms + 300_000:
        _invalid("The window end is more than five minutes in the future.")
    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(COUNTERS):
        _invalid("All supported metric fields, and no extra fields, are required.")
    metrics = {key: _integer(metrics[key], key) for key in COUNTERS}
    m = metrics
    failures = sum(
        m[key]
        for key in (
            "transportFailures",
            "retryableResponses",
            "configurationFailures",
            "rejectedPackets",
        )
    )
    if (
        m["attempts"] != m["acknowledged"] + failures
        or m["retries"] > m["attempts"]
        or m["replayAttempts"] > m["attempts"]
    ):
        _invalid("Attempt, ACK, failure and retry counts are inconsistent.")
    if m["acknowledged"] == 0:
        if m["ackLatencyTotalMs"] or m["ackLatencyMaxMs"]:
            _invalid("Latency must be zero when no valid ACK was observed.")
    elif (
        not m["ackLatencyMaxMs"]
        <= m["ackLatencyTotalMs"]
        <= m["ackLatencyMaxMs"] * m["acknowledged"]
    ):
        _invalid("ACK latency totals and maximum are inconsistent.")
    if (
        m["bufferCapacity"] < 1
        or m["bufferCapacity"] > 1_000_000
        or m["bufferSamples"] < 1
        or not m["bufferDepthLast"] <= m["bufferDepthMax"] <= m["bufferCapacity"]
        or not m["bufferDepthMax"]
        <= m["bufferDepthSum"]
        <= m["bufferDepthMax"] * m["bufferSamples"]
        or m["offlineSamples"] > m["wifiSamples"]
    ):
        _invalid("Buffer or Wi-Fi observations are inconsistent.")
    normalized = {
        **payload,
        "startedAt": _iso(start),
        "endedAt": _iso(end),
        "metrics": metrics,
    }
    return normalized, start, end


def _aggregate(reports):
    result = {key: 0 for key in ADDITIVE}
    result.update(
        windowCount=len(reports),
        ackLatencyMaxMs=None,
        bufferDepthMax=None,
        bufferDepthLast=None,
        bufferCapacity=None,
        observedDurationMs=0,
    )
    for report in reports:
        m = report["metrics"]
        for key in ADDITIVE:
            result[key] += m[key]
        result["ackLatencyMaxMs"] = (
            max(result["ackLatencyMaxMs"] or 0, m["ackLatencyMaxMs"])
            if m["acknowledged"]
            else result["ackLatencyMaxMs"]
        )
        result["bufferDepthMax"] = max(
            result["bufferDepthMax"] or 0, m["bufferDepthMax"]
        )
        result["bufferDepthLast"] = m["bufferDepthLast"]
        result["bufferCapacity"] = m["bufferCapacity"]
        result["observedDurationMs"] += report["endUptimeMs"] - report["startUptimeMs"]
    result["failures"] = result["attempts"] - result["acknowledged"]
    result["hasData"] = bool(reports)
    result["failureRatePct"] = (
        round(100 * result["failures"] / result["attempts"], 3)
        if result["attempts"]
        else None
    )
    result["retryRatePct"] = (
        round(100 * result["retries"] / result["attempts"], 3)
        if result["attempts"]
        else None
    )
    result["ackLatencyMeanMs"] = (
        round(result["ackLatencyTotalMs"] / result["acknowledged"], 3)
        if result["acknowledged"]
        else None
    )
    result["bufferDepthSampleMean"] = (
        round(result["bufferDepthSum"] / result["bufferSamples"], 3)
        if result["bufferSamples"]
        else None
    )
    result["firstWindowStartedAt"] = min(
        (r["startedAt"] for r in reports), default=None
    )
    result["lastWindowEndedAt"] = reports[-1]["endedAt"] if reports else None
    return result


class CommunicationQualityStore:
    @device_lifecycle.serialized
    def __init__(self, database=":memory:"):
        if database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, timeout=0.25, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            if database != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=FULL")
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in self._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables - {"communication_windows", "communication_devices"}:
                    raise ValueError(
                        "Communication quality requires a separate database file."
                    )
                if version not in (0, 1, STORAGE_SCHEMA_VERSION):
                    raise ValueError("Unsupported communication quality schema.")
                self._db.execute("""CREATE TABLE IF NOT EXISTS communication_windows (
                    device_id TEXT NOT NULL, boot_id TEXT NOT NULL, window_id INTEGER NOT NULL,
                    site_id TEXT NOT NULL, asset_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL, start_uptime INTEGER NOT NULL, end_uptime INTEGER NOT NULL,
                    digest TEXT NOT NULL, payload TEXT NOT NULL, received_ms INTEGER NOT NULL,
                    PRIMARY KEY(device_id, boot_id, window_id))""")
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS quality_scope_time ON communication_windows(device_id, site_id, asset_id, end_ms)"
                )
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS quality_retention ON communication_windows(end_ms)"
                )
                self._db.execute("""CREATE TABLE IF NOT EXISTS communication_devices (
                    device_id TEXT PRIMARY KEY NOT NULL)""")
                if version < STORAGE_SCHEMA_VERSION:
                    # Backfill before pruning: even expired v1 windows reserve
                    # their device identity. Raw-data retention is independent.
                    self._db.execute("""INSERT OR IGNORE INTO communication_devices
                        SELECT DISTINCT device_id FROM communication_windows""")
                self._db.execute(f"PRAGMA user_version={STORAGE_SCHEMA_VERSION}")
            self.prune()
        except BaseException:
            self._db.close()
            raise
        device_lifecycle.register_history(self)

    def check_device_deletion(self, device_id):
        try:
            with self._lock:
                exists = self._db.execute(
                    "SELECT 1 FROM communication_devices WHERE device_id=?",
                    (device_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise data.ApiError(
                503,
                "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
                "Device history could not be checked; retry without deleting the device.",
            ) from error
        if exists:
            raise data.ApiError(
                409,
                "DEVICE_HAS_COMMUNICATION_QUALITY_HISTORY",
                "A device with communication quality history cannot be deleted or reused; deactivate it instead.",
            )

    @staticmethod
    def now_ms():
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def prune(self, now_ms=None):
        cutoff = (
            self.now_ms() if now_ms is None else now_ms
        ) - RETENTION_DAYS * 86_400_000
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM communication_windows WHERE end_ms<=?", (cutoff,)
            ).rowcount

    @device_lifecycle.serialized
    def ingest(self, token, device_id, payload):
        device_for_token(
            token,
            device_id,
            environment="DEVICE_QUALITY_TOKENS_JSON",
            error_prefix="COMMUNICATION_QUALITY",
        )
        scope = _scope(device_id, active=True)
        now = self.now_ms()
        report, start, end = normalize_report(payload, scope, now)
        encoded = json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        identity = (device_id, report["bootId"], report["windowId"])
        response = {
            "accepted": True,
            "deviceId": device_id,
            "bootId": report["bootId"],
            "windowId": report["windowId"],
        }
        cutoff = now - RETENTION_DAYS * 86_400_000
        try:
            with self._lock, self._db:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "DELETE FROM communication_windows WHERE end_ms<=?", (cutoff,)
                )
                previous = self._db.execute(
                    "SELECT digest FROM communication_windows WHERE device_id=? AND boot_id=? AND window_id=?",
                    identity,
                ).fetchone()
                if previous:
                    if previous["digest"] != digest:
                        raise data.ApiError(
                            409,
                            "COMMUNICATION_QUALITY_CONFLICT",
                            "The same report identity has different content.",
                        )
                    return {**response, "duplicate": True, "disposition": "stored"}, 200
                if end <= cutoff:
                    # A retained device outbox must be able to retire an expired
                    # report without resurrecting it in an otherwise empty DB.
                    return {
                        **response,
                        "duplicate": False,
                        "disposition": "expired",
                    }, 200
                overlap = self._db.execute(
                    """SELECT 1 FROM communication_windows
                    WHERE device_id=? AND boot_id=? AND start_uptime<? AND end_uptime>? LIMIT 1""",
                    (
                        device_id,
                        report["bootId"],
                        report["endUptimeMs"],
                        report["startUptimeMs"],
                    ),
                ).fetchone()
                if overlap:
                    raise data.ApiError(
                        409,
                        "COMMUNICATION_QUALITY_OVERLAP",
                        "Reported windows overlap within one boot.",
                    )
                self._db.execute(
                    "INSERT INTO communication_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        *identity,
                        scope["siteId"],
                        scope["assetId"],
                        start,
                        end,
                        report["startUptimeMs"],
                        report["endUptimeMs"],
                        digest,
                        encoded,
                        now,
                    ),
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO communication_devices(device_id) VALUES (?)",
                    (device_id,),
                )
            return {**response, "duplicate": False, "disposition": "stored"}, 201
        except sqlite3.Error as error:
            raise data.ApiError(
                503,
                "COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE",
                "Quality storage is temporarily unavailable; retry the same report.",
            ) from error

    def query(self, user, device_id, query):
        scope = _scope(device_id, user=user)
        if set(query) - {"from", "to", "bucketSeconds"} or any(
            len(values) != 1 for values in query.values()
        ):
            _invalid("Use single from, to and bucketSeconds query parameters only.")
        now = self.now_ms()
        start = (
            _timestamp(query["from"][0], "from")
            if "from" in query
            else now - 86_400_000
        )
        end = _timestamp(query["to"][0], "to") if "to" in query else now
        bucket_value = query.get("bucketSeconds", ["3600"])[0]
        if bucket_value not in {"60", "300", "3600", "86400"}:
            _invalid("bucketSeconds must be 60, 300, 3600 or 86400.")
        width = int(bucket_value) * 1000
        first = start // width * width
        if (
            start < 0
            or end <= start
            or end - start > RETENTION_DAYS * 86_400_000
            or (end - 1 - first) // width + 1 > MAX_BUCKETS
        ):
            _invalid(
                "Use a positive range of at most 30 days and at most 1000 buckets."
            )
        with self._lock, self._db:
            self._db.execute("BEGIN")
            rows = self._db.execute(
                """SELECT payload, start_ms, end_ms FROM communication_windows
                WHERE device_id=? AND site_id=? AND asset_id=? AND end_ms>? AND end_ms<=? AND end_ms>?
                ORDER BY end_ms, boot_id, window_id""",
                (
                    device_id,
                    scope["siteId"],
                    scope["assetId"],
                    start,
                    end,
                    now - RETENTION_DAYS * 86_400_000,
                ),
            ).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault((row["end_ms"] - 1) // width * width, []).append(row)
        items = []
        for bucket in range(first, end, width):
            window_rows = groups.get(bucket, [])
            items.append(
                {
                    "from": _iso(max(start, bucket)),
                    "to": _iso(min(end, bucket + width)),
                    **_aggregate([json.loads(row["payload"]) for row in window_rows]),
                    "crossBoundaryWindows": sum(
                        row["start_ms"] < max(start, bucket) for row in window_rows
                    ),
                }
            )
        return {
            **scope,
            "transport": "http",
            "source": "device_observed",
            "from": _iso(start),
            "to": _iso(end),
            "bucketSeconds": int(bucket_value),
            "retentionDays": RETENTION_DAYS,
            "attribution": "whole_window_at_end",
            "rangeSemantics": "from_exclusive_to_inclusive_window_end",
            "summary": _aggregate([json.loads(row["payload"]) for row in rows]),
            "items": items,
        }

    @device_lifecycle.serialized
    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            device_lifecycle.unregister_history(self)
