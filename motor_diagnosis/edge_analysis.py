"""Bounded, device-scoped analysis storage separate from telemetry checkpoints.

Raw PCM is accepted only for an operator-requested window. Feature frames can
arrive independently of their summary telemetry, including after replay.
"""

import base64
import hashlib
import json
import math
import re
import secrets
import sqlite3
import struct
import threading
import time
from pathlib import Path

from . import data, device_lifecycle

CHANNELS = {
    "vibrationX": (800, 512, "g"),
    "vibrationY": (800, 512, "g"),
    "vibrationZ": (800, 512, "g"),
    "acoustic": (16000, 10240, "pcm24"),
}
RETENTION_SECONDS = 7 * 86400
MAX_BYTES = 256 * 1024 * 1024
MAX_FRAMES_PER_DEVICE = 25000


def invalid(message):
    raise data.ApiError(400, "INVALID_ANALYSIS_FRAME", message)


def normalize_frame(payload, *, enforce_retention=True):
    keys = {
        "schemaVersion",
        "featureVersion",
        "deviceId",
        "siteId",
        "assetId",
        "sequence",
        "timestamp",
        "requestId",
        "channels",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != keys
        or type(payload["schemaVersion"]) is not int
        or payload["schemaVersion"] != 1
        or payload["featureVersion"] != "edge-statistics-v1"
    ):
        invalid("Use the exact analysis v1 envelope and edge-statistics-v1 features.")
    if (
        type(payload["sequence"]) is not int
        or not 1 <= payload["sequence"] <= 0xFFFFFFFF
    ):
        invalid("sequence must identify the original telemetry window.")
    timestamp = data.parse_rfc3339("timestamp", payload["timestamp"])
    epoch = timestamp.timestamp()
    if (
        enforce_retention
        and not time.time() - RETENTION_SECONDS <= epoch <= time.time() + 60
    ):
        invalid("The capture is outside the analysis retention window.")
    for field in ("siteId", "assetId", "deviceId"):
        if (
            not isinstance(payload[field], str)
            or re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,62}", payload[field]) is None
        ):
            invalid("Exact registered IDs are required.")
    channels = payload["channels"]
    if not isinstance(channels, dict) or set(channels) != set(CHANNELS):
        invalid("All four synchronized channels are required.")
    raw_flags = []
    for name, (rate, count, unit) in CHANNELS.items():
        row = channels[name]
        required = {"sampleRateHz", "sampleCount", "unit", "features"}
        if not isinstance(row, dict) or set(row) not in (
            required,
            required | {"samplesFloat32LE"},
        ):
            invalid("Unsupported channel fields.")
        if (
            type(row["sampleRateHz"]) is not int
            or type(row["sampleCount"]) is not int
            or (row["sampleRateHz"], row["sampleCount"], row["unit"])
            != (rate, count, unit)
        ):
            invalid(
                "Sampling rate, count and unit must match the ESP32 analysis profile."
            )
        features = row["features"]
        if not isinstance(features, dict) or set(features) != {
            "rms",
            "peak",
            "kurtosis",
            "bandEnergy",
        }:
            invalid(
                "RMS, absolute peak, Pearson kurtosis and three band energies are required."
            )
        bands = features["bandEnergy"]
        if not isinstance(bands, list) or len(bands) != 3:
            invalid("bandEnergy requires three fixed profile bands.")
        values = [features["rms"], features["peak"], *bands]
        if features["kurtosis"] is not None:
            values.append(features["kurtosis"])
        if any(
            type(value) not in (int, float)
            or not 0 <= value <= 1e30
            or not math.isfinite(value)
            for value in values
        ):
            invalid(
                "Features must be finite and nonnegative; constant-window kurtosis is null."
            )
        raw = "samplesFloat32LE" in row
        raw_flags.append(raw)
        if raw:
            encoded = row["samplesFloat32LE"]
            if (
                not isinstance(encoded, str)
                or len(encoded) != ((count * 4 + 2) // 3) * 4
            ):
                invalid("Invalid raw sample size.")
            try:
                samples = struct.unpack(
                    f"<{count}f", base64.b64decode(encoded, validate=True)
                )
            except (ValueError, struct.error):
                invalid("Raw samples must be float32 little-endian base64.")
            if any(not math.isfinite(value) or abs(value) > 1e9 for value in samples):
                invalid("Raw samples must be finite sensor readings.")
    if any(raw_flags) != all(raw_flags):
        invalid("Partial synchronized waveforms are not accepted.")
    if all(raw_flags):
        if (
            not isinstance(payload["requestId"], str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["requestId"]) is None
        ):
            invalid("Raw samples require an explicit operator request.")
    elif payload["requestId"] is not None:
        invalid("Feature-only frames cannot acknowledge a waveform request.")
    result = data.copy_payload(payload)
    result["timestamp"] = data.format_rfc3339(timestamp)
    return result, epoch, all(raw_flags)


class AnalysisStore:
    def __init__(self, database=":memory:"):
        if str(database) != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(database), check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS frames(id TEXT PRIMARY KEY, device TEXT NOT NULL, site TEXT NOT NULL,
                asset TEXT NOT NULL, sequence INTEGER NOT NULL, captured REAL NOT NULL, raw INTEGER NOT NULL,
                digest TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(device,sequence));
            CREATE INDEX IF NOT EXISTS frames_scope_time ON frames(site,asset,captured);
            CREATE INDEX IF NOT EXISTS frames_device_time ON frames(device,captured);
            CREATE INDEX IF NOT EXISTS frames_expiry ON frames(captured);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,device TEXT NOT NULL,site TEXT NOT NULL,
                asset TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,actor TEXT NOT NULL,
                reason TEXT NOT NULL,frame TEXT);
            CREATE INDEX IF NOT EXISTS requests_device ON requests(device,created);
            CREATE INDEX IF NOT EXISTS requests_expiry ON requests(expires);
            CREATE TABLE IF NOT EXISTS identities(device TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY CHECK(id=1),bytes INTEGER NOT NULL);
            INSERT OR IGNORE INTO usage SELECT 1,coalesce(sum(length(body)),0) FROM frames;
            CREATE TRIGGER IF NOT EXISTS frames_added AFTER INSERT ON frames BEGIN
                UPDATE usage SET bytes=bytes+length(NEW.body) WHERE id=1; END;
            CREATE TRIGGER IF NOT EXISTS frames_removed AFTER DELETE ON frames BEGIN
                UPDATE usage SET bytes=bytes-length(OLD.body) WHERE id=1; END;
        """)
        self.prune()
        device_lifecycle.register_history(self)

    def close(self):
        device_lifecycle.unregister_history(self)
        with self.lock:
            self.db.close()

    def check_device_deletion(self, device_id):
        with self.lock:
            if self.db.execute(
                "SELECT 1 FROM identities WHERE device=?", (device_id,)
            ).fetchone():
                raise data.ApiError(
                    409,
                    "DEVICE_HAS_HISTORY",
                    "Analysis history prevents device deletion and ID reuse.",
                )

    def prune(self):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM frames WHERE captured<?",
                (time.time() - RETENTION_SECONDS,),
            )
            self.db.execute(
                "DELETE FROM requests WHERE expires<?",
                (time.time() - RETENTION_SECONDS,),
            )

    @device_lifecycle.serialized
    def request(self, user, device_id, payload):
        data.require_permission(user, "device:write")
        data.require_permission(user, "telemetry:read")
        if not isinstance(payload, dict) or set(payload) not in (
            {"siteId", "assetId", "reason"},
            {"siteId", "assetId", "reason", "requestId"},
        ):
            invalid("Explicit selected siteId, assetId and reason are required.")
        identity = (
            payload["requestId"] if "requestId" in payload else secrets.token_hex(16)
        )
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[0-9a-f]{32}", identity) is None
        ):
            invalid("requestId must contain 32 lowercase hex characters.")
        reason = data.required_text(payload, "reason")
        if len(reason) > 1000:
            invalid("reason must be at most 1000 characters.")
        with data.STORE_LOCK:
            device = data.copy_payload(data.get_device(device_id))
            data.require_site_access(user, device["siteId"])
            if (
                device["mappingStatus"] != "active"
                or device["certificateStatus"] != "registered"
                or any(payload[k] != device[k] for k in ("siteId", "assetId"))
            ):
                raise data.ApiError(
                    409,
                    "DEVICE_MAPPING_MISMATCH",
                    "Reselect the active device before requesting raw data.",
                )
        self.prune()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            now = time.time()
            previous = self.db.execute(
                "SELECT * FROM requests WHERE id=?", (identity,)
            ).fetchone()
            if previous:
                if (
                    previous["device"],
                    previous["site"],
                    previous["asset"],
                    previous["actor"],
                    previous["reason"],
                ) != (
                    device_id,
                    device["siteId"],
                    device["assetId"],
                    user["id"],
                    reason,
                ):
                    raise data.ApiError(
                        409,
                        "WAVEFORM_REQUEST_CONFLICT",
                        "The request ID belongs to a different intent.",
                    )
                return {
                    "requestId": identity,
                    "siteId": device["siteId"],
                    "assetId": device["assetId"],
                    "status": (
                        "completed"
                        if previous["frame"]
                        else "expired" if previous["expires"] < now else "pending"
                    ),
                    "expiresAtEpoch": previous["expires"],
                    "duplicate": True,
                }
            if self.db.execute(
                "SELECT 1 FROM requests WHERE device=? AND expires>=? AND frame IS NULL",
                (device_id, now),
            ).fetchone():
                raise data.ApiError(
                    409,
                    "WAVEFORM_REQUEST_PENDING",
                    "A capture request is already pending.",
                )
            self.db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,NULL)",
                (
                    identity,
                    device_id,
                    device["siteId"],
                    device["assetId"],
                    now,
                    now + 300,
                    user["id"],
                    reason,
                ),
            )
            self.db.execute("INSERT OR IGNORE INTO identities VALUES(?)", (device_id,))
            return {
                "requestId": identity,
                "siteId": device["siteId"],
                "assetId": device["assetId"],
                "status": "pending",
                "expiresAtEpoch": now + 300,
            }

    @device_lifecycle.serialized
    def pending(self, principal, device_id):
        with data.STORE_LOCK:
            data.principal_can_ingest(principal, device_id)
            device = data.copy_payload(data.get_device(device_id))
            data.validate_telemetry_mapping(
                {
                    "siteId": device["siteId"],
                    "assetId": device["assetId"],
                    "deviceId": device_id,
                }
            )
        with self.lock:
            row = self.db.execute(
                "SELECT id,expires FROM requests WHERE device=? AND site=? AND asset=? AND expires>=? AND frame IS NULL ORDER BY created LIMIT 1",
                (device_id, device["siteId"], device["assetId"], time.time()),
            ).fetchone()
            return {
                "deviceId": device_id,
                "siteId": device["siteId"],
                "assetId": device["assetId"],
                "requestId": row["id"] if row else None,
                "expiresAtEpoch": row["expires"] if row else None,
            }

    @device_lifecycle.serialized
    def ingest(self, principal, device_id, payload):
        with data.STORE_LOCK:
            data.principal_can_ingest(principal, device_id)
            frame, captured, raw = normalize_frame(payload)
            if frame["deviceId"] != device_id:
                invalid("Route and payload device IDs must match.")
            data.validate_telemetry_mapping(frame)
        body = json.dumps(frame, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(body.encode()).hexdigest()
        self.prune()
        with self.lock, self.db:
            # Serialize capacity checks and inserts across independent connections.
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT id,digest FROM frames WHERE device=? AND sequence=?",
                (device_id, frame["sequence"]),
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise data.ApiError(
                        409,
                        "ANALYSIS_SEQUENCE_CONFLICT",
                        "The original capture cannot be replaced.",
                    )
                return {
                    "accepted": True,
                    "duplicate": True,
                    "frameId": previous["id"],
                    "sequence": frame["sequence"],
                }, 200
            request = None
            if raw:
                request = self.db.execute(
                    "SELECT * FROM requests WHERE id=?", (frame["requestId"],)
                ).fetchone()
                if (
                    not request
                    or request["frame"]
                    or (request["device"], request["site"], request["asset"])
                    != (device_id, frame["siteId"], frame["assetId"])
                    or not request["created"] - 1 <= captured <= request["expires"]
                ):
                    raise data.ApiError(
                        409,
                        "WAVEFORM_REQUEST_MISMATCH",
                        "No matching unfulfilled capture request at the capture time.",
                    )
            count = self.db.execute(
                "SELECT count(*) FROM frames WHERE device=?", (device_id,)
            ).fetchone()[0]
            size = self.db.execute("SELECT bytes FROM usage WHERE id=1").fetchone()[0]
            if count >= MAX_FRAMES_PER_DEVICE or size + len(body) > MAX_BYTES:
                raise data.ApiError(
                    503,
                    "ANALYSIS_STORAGE_FULL",
                    "Analysis retention capacity is full; retry without deleting unacknowledged captures.",
                )
            identity = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO frames VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    device_id,
                    frame["siteId"],
                    frame["assetId"],
                    frame["sequence"],
                    captured,
                    int(raw),
                    digest,
                    body,
                ),
            )
            self.db.execute("INSERT OR IGNORE INTO identities VALUES(?)", (device_id,))
            if raw:
                self.db.execute(
                    "UPDATE requests SET frame=? WHERE id=?", (identity, request["id"])
                )
            return {
                "accepted": True,
                "duplicate": False,
                "frameId": identity,
                "sequence": frame["sequence"],
            }, 201

    @device_lifecycle.serialized
    def list_device(self, user, device_id, *, cursor=None):
        data.require_permission(user, "device:read")
        with data.STORE_LOCK:
            device = data.copy_payload(data.get_device(device_id))
        return {
            "deviceId": device_id,
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            **self.list_for(
                user,
                device["siteId"],
                device["assetId"],
                device_id=device_id,
                cursor=cursor,
            ),
        }

    def list_for(
        self,
        user,
        site_id,
        asset_id,
        *,
        device_id=None,
        start=None,
        end=None,
        cursor=None,
    ):
        data.require_permission(user, "telemetry:read")
        data.require_site_access(user, site_id)
        self.prune()
        clauses = ["site=?", "asset=?"]
        params = [site_id, asset_id]
        for clause, value in (
            ("device=?", device_id),
            ("captured>=?", start),
            ("captured<=?", end),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 200:
                    raise ValueError()
                before, identity = json.loads(base64.b64decode(cursor, validate=True))
                if (
                    type(before) not in (int, float)
                    or not 0 < before < 1e12
                    or not isinstance(identity, str)
                    or re.fullmatch(r"[0-9a-f]{32}", identity) is None
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                invalid("Invalid analysis pagination cursor.")
            clauses.append("(captured<? OR (captured=? AND id<?))")
            params.extend([before, before, identity])
        with self.lock:
            rows = self.db.execute(
                "SELECT id,captured,raw,digest,body FROM frames WHERE "
                + " AND ".join(clauses)
                + " ORDER BY captured DESC,id DESC LIMIT 101",
                params,
            ).fetchall()
            next_cursor = (
                base64.b64encode(
                    json.dumps([rows[99]["captured"], rows[99]["id"]]).encode()
                ).decode()
                if len(rows) > 100
                else None
            )
            items = []
            for row in rows[:100]:
                body = json.loads(row["body"])
                for channel in body["channels"].values():
                    channel.pop("samplesFloat32LE", None)
                items.append(
                    {
                        **body,
                        "id": row["id"],
                        "hasWaveform": bool(row["raw"]),
                        "sha256": row["digest"],
                    }
                )
            request_clauses = ["site=?", "asset=?"]
            request_params = [site_id, asset_id]
            for clause, value in (
                ("device=?", device_id),
                ("created>=?", start),
                ("created<=?", end),
            ):
                if value is not None:
                    request_clauses.append(clause)
                    request_params.append(value)
            requests = self.db.execute(
                "SELECT * FROM requests WHERE "
                + " AND ".join(request_clauses)
                + " ORDER BY created DESC LIMIT 20",
                request_params,
            ).fetchall()
            return {
                "items": items,
                "limit": 100,
                "nextCursor": next_cursor,
                "retentionDays": 7,
                "requests": [
                    {
                        "id": row["id"],
                        "deviceId": row["device"],
                        "reason": row["reason"],
                        "frameId": row["frame"],
                        "status": (
                            "completed"
                            if row["frame"]
                            else (
                                "expired" if row["expires"] < time.time() else "pending"
                            )
                        ),
                    }
                    for row in requests
                    if device_id is None or row["device"] == device_id
                ],
            }

    def detail(self, user, identity):
        data.require_permission(user, "telemetry:read")
        self.prune()
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM frames WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise data.ApiError(
                    404, "ANALYSIS_NOT_FOUND", "Capture is missing or expired."
                )
            data.require_site_access(user, row["site"])
            return {
                "id": identity,
                "sha256": row["digest"],
                "frame": json.loads(row["body"]),
                "training": {
                    "labelStatus": "unlabeled",
                    "trainingEligible": False,
                    "pipelineVersion": "edge-statistics-v1",
                },
            }
