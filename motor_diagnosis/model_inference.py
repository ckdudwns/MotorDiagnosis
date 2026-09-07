"""Opt-in prepared-feature shadow inference; never changes alarms or approvals."""

import hashlib
import json
import logging
import math
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from . import data, device_lifecycle

LOGGER = logging.getLogger(__name__)
RETENTION_SECONDS = 7 * 86400
MAX_RECORDS = 10000
MAX_PENDING = 1000
MAX_STREAMS = 10000
INPUT_KEYS = {
    "schemaVersion",
    "modelVersion",
    "deviceId",
    "siteId",
    "assetId",
    "sourceId",
    "preprocessingId",
    "sampleRateHz",
    "windowIndex",
    "windowStartSample",
    "windowEndSample",
    "timestamp",
    "features",
}


def invalid(message):
    raise data.ApiError(400, "INVALID_MODEL_INPUT", message)


class ModelInferenceStore:
    def __init__(self, checkpoint, database=":memory:"):
        self.checkpoint = checkpoint
        self.lock = threading.RLock()
        self.processing = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        if str(database) != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database), check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inputs(
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                device TEXT NOT NULL,site TEXT NOT NULL,asset TEXT NOT NULL,
                model TEXT NOT NULL,source TEXT NOT NULL,idx INTEGER NOT NULL,
                captured REAL NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL,
                status TEXT NOT NULL,result TEXT,
                UNIQUE(device,model,source,idx));
            CREATE INDEX IF NOT EXISTS input_queue ON inputs(status,ordinal);
            CREATE INDEX IF NOT EXISTS input_scope ON inputs(site,asset,device,captured);
            CREATE INDEX IF NOT EXISTS input_expiry ON inputs(captured);
            CREATE TABLE IF NOT EXISTS streams(device TEXT,model TEXT,source TEXT,
                context TEXT NOT NULL,last_idx INTEGER NOT NULL,
                PRIMARY KEY(device,model,source));
            CREATE TABLE IF NOT EXISTS identities(device TEXT PRIMARY KEY);
        """)
        self.prune()
        device_lifecycle.register_history(self)

    def start(self):
        self.worker = threading.Thread(
            target=self._run, name="model-shadow", daemon=True
        )
        self.worker.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOGGER.exception(
                    "Shadow result processing failed; durable input retained"
                )
            self.stop.wait(0.25)

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join()
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
                    "Model input history prevents device deletion and ID reuse.",
                )

    def prune(self):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM inputs WHERE captured<?",
                (time.time() - RETENTION_SECONDS,),
            )

    def normalize(self, payload):
        if not isinstance(payload, dict) or set(payload) != INPUT_KEYS:
            invalid(
                "Explicit prepared-feature envelope required; raw/summary telemetry is not a model input."
            )
        if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
            invalid("Unsupported prepared-feature schema")
        if payload["modelVersion"] != self.checkpoint.checksum:
            invalid("The input must pin the selected checkpoint checksum")
        for key in ("deviceId", "siteId", "assetId", "sourceId", "preprocessingId"):
            value = payload[key]
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or any(ord(c) < 32 for c in value)
            ):
                invalid("Invalid " + key)
        for key, maximum in (
            ("windowIndex", 2**31 - 1),
            ("windowStartSample", 2**53 - 1),
            ("windowEndSample", 2**53 - 1),
            ("sampleRateHz", 192000),
        ):
            if type(payload[key]) is not int or not 0 <= payload[key] <= maximum:
                invalid("Invalid " + key)
        if (
            not 0
            < payload["windowEndSample"] - payload["windowStartSample"]
            <= 10000000
            or payload["sampleRateHz"] == 0
        ):
            invalid("Invalid window sample boundaries")
        features = payload["features"]
        if not isinstance(features, dict) or set(features) != set(
            self.checkpoint.names
        ):
            invalid(
                "Feature names must match the checkpoint exactly; missing values are not filled"
            )
        for value in features.values():
            try:
                valid = (
                    type(value) in (int, float)
                    and math.isfinite(value)
                    and abs(value) <= 1e100
                )
            except OverflowError:
                valid = False
            if not valid:
                invalid(
                    "All features must be finite numeric values, not null/bool/string"
                )
        captured = data.parse_rfc3339("timestamp", payload["timestamp"]).timestamp()
        if not time.time() - RETENTION_SECONDS <= captured <= time.time() + 300:
            invalid("Input timestamp is outside the retention/future bound")
        try:
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if len(body.encode("utf-8")) > 16384:
                invalid("Prepared-feature envelope is too large")
        except (TypeError, UnicodeError, ValueError) as exc:
            invalid(str(exc))
        return body, captured

    @device_lifecycle.serialized
    def ingest(self, principal, device_id, payload):
        with data.STORE_LOCK:
            data.principal_can_ingest(principal, device_id)
            if not isinstance(payload, dict) or payload.get("deviceId") != device_id:
                invalid("Route and input device IDs must match")
            data.validate_telemetry_mapping(payload)
        body, captured = self.normalize(payload)
        digest = hashlib.sha256(body.encode()).hexdigest()
        model, source, index = (
            payload["modelVersion"],
            payload["sourceId"],
            payload["windowIndex"],
        )
        context = json.dumps(
            [
                payload[k]
                for k in ("siteId", "assetId", "preprocessingId", "sampleRateHz")
            ]
            + [payload["windowEndSample"] - payload["windowStartSample"]]
        )
        self.prune()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT * FROM inputs WHERE device=? AND model=? AND source=? AND idx=?",
                (device_id, model, source, index),
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise data.ApiError(
                        409,
                        "MODEL_INPUT_CONFLICT",
                        "Input identity already has different content",
                    )
                return {
                    "accepted": True,
                    "duplicate": True,
                    "inputId": previous["id"],
                    "status": previous["status"],
                    "mode": "shadow",
                }, 200
            stream = self.db.execute(
                "SELECT * FROM streams WHERE device=? AND model=? AND source=?",
                (device_id, model, source),
            ).fetchone()
            if stream and (stream["context"] != context or index <= stream["last_idx"]):
                raise data.ApiError(
                    409,
                    "MODEL_SEQUENCE_CONFLICT",
                    "Source context changed or window is older than the durable sequence watermark",
                )
            if not stream and index != 0:
                invalid("A new source starts at windowIndex 0")
            if (
                not stream
                and self.db.execute("SELECT count(*) FROM streams").fetchone()[0]
                >= MAX_STREAMS
            ):
                raise data.ApiError(
                    503,
                    "MODEL_STREAM_LIMIT",
                    "Durable source identity capacity is full",
                )
            if (
                self.db.execute("SELECT count(*) FROM inputs").fetchone()[0]
                >= MAX_RECORDS
                or self.db.execute(
                    "SELECT count(*) FROM inputs WHERE status='queued'"
                ).fetchone()[0]
                >= MAX_PENDING
            ):
                raise data.ApiError(
                    503,
                    "MODEL_QUEUE_FULL",
                    "Shadow queue is full; retry the same input later",
                )
            identity = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO inputs(id,device,site,asset,model,source,idx,captured,digest,body,status) VALUES(?,?,?,?,?,?,?,?,?,?,'queued')",
                (
                    identity,
                    device_id,
                    payload["siteId"],
                    payload["assetId"],
                    model,
                    source,
                    index,
                    captured,
                    digest,
                    body,
                ),
            )
            self.db.execute(
                "INSERT INTO streams VALUES(?,?,?,?,?) ON CONFLICT(device,model,source) DO UPDATE SET last_idx=excluded.last_idx",
                (device_id, model, source, context, index),
            )
            self.db.execute("INSERT OR IGNORE INTO identities VALUES(?)", (device_id,))
        return {
            "accepted": True,
            "duplicate": False,
            "inputId": identity,
            "status": "queued",
            "mode": "shadow",
        }, 202

    def tick(self):
        from ai.ai2.model_runtime import contiguous_matrix

        with self.processing:
            with self.lock:
                row = self.db.execute(
                    "SELECT * FROM inputs WHERE status='queued' ORDER BY ordinal LIMIT 1"
                ).fetchone()
                if row is None:
                    return False
                length = self.checkpoint.sequence_length
                start = row["idx"] // length * length
                group = self.db.execute(
                    "SELECT body,captured FROM inputs WHERE device=? AND model=? AND source=? AND idx BETWEEN ? AND ? ORDER BY idx",
                    (row["device"], row["model"], row["source"], start, row["idx"]),
                ).fetchall()
            result = {
                "mode": "shadow",
                "affectsAlerts": False,
                "domainValidated": False,
                "modelVersion": row["model"],
                "modelType": self.checkpoint.kind,
                "verdict": None,
                "reconstructionError": None,
                "threshold": None,
                "status": "not_evaluated",
                "reason": None,
            }
            if row["model"] != self.checkpoint.checksum:
                result["reason"] = "MODEL_CHANGED"
                result["modelType"] = None
            elif row["idx"] % length != length - 1:
                result.update(status="warming_up", reason="SEQUENCE_INCOMPLETE")
            elif len(group) != length:
                result["reason"] = "SEQUENCE_GAP"
            else:
                windows = [json.loads(item["body"]) for item in group]
                try:
                    matrix = contiguous_matrix(windows, self.checkpoint)
                    period = (
                        windows[0]["windowEndSample"] - windows[0]["windowStartSample"]
                    ) / windows[0]["sampleRateHz"]
                    if any(
                        abs(item["captured"] - group[0]["captured"] - offset * period)
                        > max(1.0, period * 0.02)
                        for offset, item in enumerate(group)
                    ):
                        raise ValueError("Non-contiguous capture timestamps")
                except ValueError:
                    result["reason"] = "SEQUENCE_DISCONTINUOUS"
                else:
                    try:
                        score = self.checkpoint.predict(
                            matrix, list(self.checkpoint.names)
                        )
                        result.update(
                            status="inferred",
                            verdict=score["verdict"][0],
                            reconstructionError=score["errors"][0],
                            threshold=score["threshold"],
                        )
                    except Exception:
                        LOGGER.exception(
                            "Shadow inference failed; no normal verdict produced"
                        )
                        result["reason"] = "INFERENCE_FAILED"
            # Never change telemetry, registry approval, event lifecycle or alert state.
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE inputs SET status=?,result=? WHERE id=? AND status='queued'",
                    (result["status"], json.dumps(result, allow_nan=False), row["id"]),
                )
            return True

    @device_lifecycle.serialized
    def list_device(self, user, device_id):
        data.require_permission(user, "device:read")
        with data.STORE_LOCK:
            device = data.copy_payload(data.get_device(device_id))
        return self.list_for(
            user, device["siteId"], device["assetId"], device_id=device_id
        )

    def list_for(
        self, user, site_id, asset_id, *, device_id=None, start=None, end=None
    ):
        data.require_permission(user, "model:read")
        data.require_permission(user, "telemetry:read")
        data.require_site_access(user, site_id)
        self.prune()
        clauses, params = ["site=?", "asset=?"], [site_id, asset_id]
        for clause, value in (
            ("device=?", device_id),
            ("captured>=?", start),
            ("captured<=?", end),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM inputs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY captured DESC,ordinal DESC LIMIT 20",
                params,
            ).fetchall()
        return {
            "checkpoint": self.checkpoint.describe(),
            "deviceId": device_id,
            "siteId": site_id,
            "assetId": asset_id,
            "inputStatus": "prepared_features_only",
            "rawInputStatus": "not_evaluated",
            "rawInputReason": "PREPROCESSOR_NOT_CONFIGURED",
            "limit": 20,
            "items": [
                {
                    "inputId": row["id"],
                    "timestamp": json.loads(row["body"])["timestamp"],
                    "sourceId": row["source"],
                    "windowIndex": row["idx"],
                    **(
                        json.loads(row["result"])
                        if row["result"]
                        else {
                            "status": "queued",
                            "mode": "shadow",
                            "affectsAlerts": False,
                            "modelVersion": row["model"],
                            "verdict": None,
                        }
                    ),
                }
                for row in rows
            ],
        }
