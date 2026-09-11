"""Durable, idempotent per-window ingestion, separate from summary telemetry.

Ingestion never waits for a model calculation. Invalid measurements are stored
as unavailable, not normal. Identity tombstones survive retention and restart.
"""

import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from . import data, device_lifecycle
from .window_features import PROFILE_ID, derive, feature_names

LOGGER = logging.getLogger(__name__)
RETENTION = 2 * 86400
MAX_ROWS = 500000
MAX_PENDING = 4096
MAX_STREAMS = 10000
MAX_BATCH = 16
KEYS = {"schemaVersion", "deviceId", "siteId", "assetId", "bootId",
        "windowIndex", "timestamp", "startUptimeUs", "sampleRateHz",
        "sampleCount", "profileId", "axes", "unit", "quality", "features"}
QUALITY = {"valid", "fifo_overrun", "sensor_unavailable", "sample_gap",
           "clipped", "constant_axis", "processing_overflow"}


def reject(message, status=400, code="INVALID_VIBRATION_WINDOW"):
    raise data.ApiError(status, code, message)


def validate_envelope(payload, keys=KEYS, profile=PROFILE_ID, unit="g"):
    if not isinstance(payload, dict) or set(payload) != keys:
        reject("Use the exact versioned vibration-window envelope")
    for key in ("deviceId", "siteId", "assetId"):
        # Registration uses required_text(...).upper(), not a second ID grammar.
        # Require the canonical spelling on the wire to preserve stream identity.
        if payload[key] != data.required_text(payload, key).upper():
            reject("Invalid " + key)
    if not isinstance(payload["bootId"], str) or not re.fullmatch(r"[0-9a-f]{32}", payload["bootId"]):
        reject("bootId must be 32 lowercase hexadecimal characters")
    for key, low, high in (("windowIndex", 0, 2**31 - 1),
                           ("startUptimeUs", 0, 2**53 - 1),
                           ("sampleCount", 0, 512)):
        if type(payload[key]) is not int or not low <= payload[key] <= high:
            reject("Invalid " + key)
    if (type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1
            or type(payload["sampleRateHz"]) is not int or payload["sampleRateHz"] != 800
            or payload["profileId"] != profile or payload["unit"] != unit
            or payload["axes"] != ["X", "Y", "Z"]):
        reject("The 800 Hz XYZ g profile must match exactly")
    quality = payload["quality"]
    if not isinstance(quality, str) or quality not in QUALITY:
        reject("Unknown quality state")
    captured = data.parse_rfc3339("timestamp", payload["timestamp"]).timestamp()
    if not time.time() - RETENTION <= captured <= time.time() + 300:
        reject("Window timestamp outside retention/future bounds")
    return captured


def normalize(payload):
    captured = validate_envelope(payload)
    quality = payload["quality"]
    if quality == "valid":
        if payload["sampleCount"] != 512:
            reject("Valid windows require 512 real samples per axis")
        try:
            derive(payload["features"])
        except (ValueError, OverflowError) as exc:
            reject(str(exc))
    elif payload["features"] is not None:
        reject("Invalid windows must have null features, never zero-filled values")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return body, captured


class VibrationWindowStore:
    normalize = staticmethod(normalize)
    profile_id = PROFILE_ID
    storage_kind = "features21"
    max_rows = None
    max_batch = MAX_BATCH
    list_limit = 100

    def input_names(self):
        return feature_names(self.variant)

    def input_values(self, window):
        return derive(window["features"], self.variant)

    def validate_stream_clock(self, window, captured):
        """Subclass hook, called inside the atomic ingestion transaction."""

    def __init__(self, database=":memory:", *, checkpoint=None, variant="base21"):
        names = feature_names(variant)
        if checkpoint is not None and (
            checkpoint.sequence_length != 1 or checkpoint.kind != "dense_autoencoder"
            or tuple(checkpoint.names) != tuple(names)
        ):
            raise ValueError("Window model must be a single-window dense checkpoint with the exact selected feature order")
        self.checkpoint, self.variant = checkpoint, variant
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
            CREATE TABLE IF NOT EXISTS window_identities(device TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS window_streams(
                device TEXT,boot TEXT,context TEXT NOT NULL,idx INTEGER NOT NULL,
                uptime INTEGER NOT NULL,PRIMARY KEY(device,boot));
            CREATE TABLE IF NOT EXISTS vibration_windows(
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,site TEXT NOT NULL,asset TEXT NOT NULL,
                boot TEXT NOT NULL,idx INTEGER NOT NULL,captured REAL NOT NULL,
                digest TEXT NOT NULL,body TEXT NOT NULL,gap INTEGER NOT NULL,
                status TEXT NOT NULL,result TEXT,
                UNIQUE(device,boot,idx));
            CREATE INDEX IF NOT EXISTS vibration_queue ON vibration_windows(status,ordinal);
            CREATE INDEX IF NOT EXISTS vibration_scope ON vibration_windows(device,site,asset,ordinal);
            CREATE INDEX IF NOT EXISTS vibration_expiry ON vibration_windows(captured);
            CREATE TABLE IF NOT EXISTS window_store_kind(kind TEXT NOT NULL);
        """)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                kind = self.db.execute("SELECT kind FROM window_store_kind").fetchone()
                if kind is not None and kind[0] != self.storage_kind:
                    raise ValueError("Raw and feature window stores require different database files")
                if kind is None:
                    previous = self.db.execute("SELECT body FROM vibration_windows LIMIT 1").fetchone()
                    if previous is not None and json.loads(previous[0]).get("profileId") != self.profile_id:
                        raise ValueError("Existing database has a different window profile")
                    self.db.execute("INSERT INTO window_store_kind VALUES(?)", (self.storage_kind,))
        except Exception:
            self.db.close()
            raise
        device_lifecycle.register_history(self)

    def check_device_deletion(self, device_id):
        with self.lock:
            if self.db.execute("SELECT 1 FROM window_identities WHERE device=?", (device_id,)).fetchone():
                reject("Vibration window history protects this device ID", 409, "DEVICE_HAS_HISTORY")

    def prune(self):
        with self.lock, self.db:
            # Never silently expire accepted but unprocessed work.
            self.db.execute("DELETE FROM vibration_windows WHERE captured<? AND status!='queued'",
                            (time.time() - RETENTION,))

    @device_lifecycle.serialized
    def ingest(self, principal, device_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"windows"}:
            reject("Expected a windows batch")
        windows = payload["windows"]
        if not isinstance(windows, list) or not 1 <= len(windows) <= self.max_batch:
            reject(f"Batch must contain 1 to {self.max_batch} windows")
        normalized = []
        with data.STORE_LOCK:
            data.principal_can_ingest(principal, device_id)
            for window in windows:
                if not isinstance(window, dict) or window.get("deviceId") != device_id:
                    reject("Route/device mismatch")
                normalized.append(self.normalize(window))
                data.validate_telemetry_mapping(window)
        self.prune()
        ack, accepted = [], 0
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute("SELECT count(*) FROM vibration_windows").fetchone()[0]
            pending = self.db.execute("SELECT count(*) FROM vibration_windows WHERE status='queued'").fetchone()[0]
            for window, (body, captured) in zip(windows, normalized):
                boot, index = window["bootId"], window["windowIndex"]
                digest = hashlib.sha256(body.encode()).hexdigest()
                previous = self.db.execute(
                    "SELECT digest FROM vibration_windows WHERE device=? AND boot=? AND idx=?",
                    (device_id, boot, index)).fetchone()
                if previous:
                    if previous[0] != digest:
                        reject("Same window identity has different content", 409, "WINDOW_CONFLICT")
                else:
                    if count + accepted >= (self.max_rows or MAX_ROWS) or pending + accepted >= MAX_PENDING:
                        reject("Window storage/processing capacity reached; retry later", 503, "WINDOW_BACKPRESSURE")
                    context = json.dumps([window[k] for k in ("siteId", "assetId", "profileId")])
                    stream = self.db.execute(
                        "SELECT * FROM window_streams WHERE device=? AND boot=?", (device_id, boot)).fetchone()
                    if stream and stream["context"] != context:
                        reject("Stream context changed or expired/out-of-order window", 409, "WINDOW_SEQUENCE_CONFLICT")
                    lower = upper = None
                    if self.storage_kind == "raw-counts":
                        lower = self.db.execute(
                            "SELECT idx,body FROM vibration_windows WHERE device=? AND boot=? AND idx<? ORDER BY idx DESC LIMIT 1",
                            (device_id, boot, index)).fetchone()
                        upper = self.db.execute(
                            "SELECT idx,body FROM vibration_windows WHERE device=? AND boot=? AND idx>? ORDER BY idx LIMIT 1",
                            (device_id, boot, index)).fetchone()
                        lower_uptime = json.loads(lower["body"])["startUptimeUs"] if lower else None
                        upper_uptime = json.loads(upper["body"])["startUptimeUs"] if upper else None
                        if stream and index == stream["idx"]:
                            reject("Stream context changed or expired/out-of-order window", 409, "WINDOW_SEQUENCE_CONFLICT")
                        previous_uptime = lower_uptime
                        next_uptime = upper_uptime
                        if stream and index > stream["idx"] and previous_uptime is None:
                            previous_uptime = stream["uptime"]
                        if stream and index < stream["idx"] and next_uptime is None:
                            next_uptime = stream["uptime"]
                        if ((previous_uptime is not None and window["startUptimeUs"] <= previous_uptime)
                                or (next_uptime is not None and window["startUptimeUs"] >= next_uptime)):
                            reject("Stream context changed or expired/out-of-order window", 409, "WINDOW_SEQUENCE_CONFLICT")
                    elif stream and (index <= stream["idx"] or window["startUptimeUs"] <= stream["uptime"]):
                        reject("Stream context changed or expired/out-of-order window", 409, "WINDOW_SEQUENCE_CONFLICT")
                    if not stream and self.db.execute("SELECT count(*) FROM window_streams").fetchone()[0] >= MAX_STREAMS:
                        reject("Stream identity capacity reached", 503, "WINDOW_BACKPRESSURE")
                    if self.storage_kind == "raw-counts":
                        gap = index - lower["idx"] - 1 if lower else index
                    else:
                        gap = index - stream["idx"] - 1 if stream else index
                    self.validate_stream_clock(window, captured)
                    self.db.execute(
                        "INSERT INTO vibration_windows(device,site,asset,boot,idx,captured,digest,body,gap,status) VALUES(?,?,?,?,?,?,?,?,?,'queued')",
                        (device_id, window["siteId"], window["assetId"], boot, index, captured, digest, body, gap))
                    self.db.execute("INSERT OR IGNORE INTO window_identities VALUES(?)", (device_id,))
                    if not stream or index > stream["idx"]:
                        self.db.execute("INSERT OR REPLACE INTO window_streams VALUES(?,?,?,?,?)",
                                        (device_id, boot, context, index, window["startUptimeUs"]))
                    accepted += 1
                ack.append({"bootId": boot, "windowIndex": index, "digest": digest})
        return {"deviceId": device_id, "accepted": accepted, "acknowledged": ack}, 202 if accepted else 200

    def start(self):
        self.worker = threading.Thread(target=self._run, name="vibration-windows", daemon=True)
        self.worker.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOGGER.exception("Window processing failed; accepted work retained")
            self.stop.wait(0.25)

    def result_context(self):
        return {"mode": "shadow", "affectsAlerts": False, "domainValidated": False,
                "verdict": None, "variant": self.variant, "modelVersion": None}

    def infer_window(self, values):
        if self.checkpoint is None:
            return {"status": "waiting_model", "reason": "MODEL_NOT_CONFIGURED"}
        prediction = self.checkpoint.predict([values], self.input_names())
        if len(prediction["verdict"]) != 1 or len(prediction["errors"]) != 1:
            raise ValueError("Expected exactly one prediction per window")
        if (type(prediction["verdict"][0]) is not bool
                or not math.isfinite(prediction["errors"][0])
                or not math.isfinite(prediction["threshold"])):
            raise ValueError("Invalid prediction values")
        return {"status": "completed", "modelVersion": self.checkpoint.checksum,
                "verdict": prediction["verdict"][0], "error": prediction["errors"][0],
                "threshold": prediction["threshold"]}

    def finalize_result(self, row, window, result):
        """Optional result enrichment inside the result-write transaction."""
        return result

    def tick(self):
        with self.processing:
            with self.lock:
                row = self.db.execute("SELECT * FROM vibration_windows WHERE status='queued' ORDER BY ordinal LIMIT 1").fetchone()
            if row is None:
                return False
            window = json.loads(row["body"])
            result = self.result_context()
            if window["quality"] != "valid":
                result.update(status="unavailable", reason=window["quality"])
            else:
                try:
                    values = self.input_values(window)
                    result["modelInput"] = values
                    result.update(self.infer_window(values))
                except Exception:
                    LOGGER.exception("Window unavailable; not classified as normal")
                    result.update(status="unavailable", reason="MODEL_INPUT_OR_INFERENCE_FAILED")
            with self.lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                result = self.finalize_result(row, window, result)
                encoded = json.dumps(result, allow_nan=False)
                self.db.execute("UPDATE vibration_windows SET status=?,result=? WHERE ordinal=?",
                                (result["status"], encoded, row["ordinal"]))
            return True

    @device_lifecycle.serialized
    def list_device(self, user, device_id):
        data.require_permission(user, "device:read")
        data.require_permission(user, "telemetry:read")
        data.require_permission(user, "model:read")
        with data.STORE_LOCK:
            device = data.copy_payload(data.get_device(device_id))
            data.require_site_access(user, device["siteId"])
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM vibration_windows WHERE device=? AND site=? AND asset=? ORDER BY ordinal DESC LIMIT ?",
                (device_id, device["siteId"], device["assetId"], self.list_limit)).fetchall()
        return {"deviceId": device_id, "siteId": device["siteId"], "assetId": device["assetId"],
                "profileId": self.profile_id, "featureNames": self.input_names(),
                "retentionHours": RETENTION // 3600, "limit": self.list_limit,
                "items": [{"window": json.loads(row["body"]), "missingWindowsBefore": row["gap"],
                           "analysis": json.loads(row["result"]) if row["result"] else
                           {"status": "queued", "verdict": None, "affectsAlerts": False}} for row in rows]}

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join()
        device_lifecycle.unregister_history(self)
        with self.lock:
            self.db.close()
