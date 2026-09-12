"""Durable single-window inbox and model-independent input preparation.

Board-selected delivery is independent of measurement sampling. Window index gaps
are intentional selection, not missing continuous windows. Accepted originals
are never pruned here; capacity exhaustion returns retryable backpressure.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from . import data, device_lifecycle
from .raw_samples import PROFILE_ID as RAW_PROFILE, normalize as normalize_raw
from .snapshot_input import ADAPTER_ID, prepare_input
from .snapshot_inference import SnapshotInference
from .snapshot_model import SnapshotModelAdapter
from .transmission_policy import (
    EDGE_SNAPSHOT_POLICY_ID, snapshot_interval_seconds, snapshot_policy_metadata,
    validate_snapshot,
)
from .window_envelope import reject

MAX_BODY_BYTES = 64 * 1024
MAX_ROWS = 100000
MAX_STREAMS = 10000
LIST_LIMIT = 20
CLOCK_TOLERANCE_SECONDS = 1
LOGGER = logging.getLogger(__name__)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(payload):
    try:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        reject("Snapshot must contain finite JSON values", code="INVALID_PERIODIC_SNAPSHOT")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        reject("Snapshot exceeds 64 KiB", 413, "REQUEST_TOO_LARGE")
    return body


def normalize_window(window, *, check_time_bounds=True):
    """Verify wire bytes, not an RF66 feature vector or a JSON-file checksum."""
    integrity = window.get("integrity")
    if (not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}
            or integrity["algorithm"] != "sha256"):
        reject("integrity must specify algorithm=sha256 and digest", code="INVALID_PERIODIC_SNAPSHOT")
    raw = {key: value for key, value in window.items() if key != "integrity"}
    _, captured = normalize_raw(raw, check_time_bounds=check_time_bounds)
    digest = integrity["digest"]
    if raw["samples"] is None:
        if digest is not None:
            reject("Absent samples require a null integrity digest", code="INVALID_PERIODIC_SNAPSHOT")
    else:
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            reject("Sample digest must be 64 lowercase hexadecimal characters",
                   code="INVALID_PERIODIC_SNAPSHOT")
        # normalize_raw has already checked base64 canonical form, length and range.
        actual = hashlib.sha256(base64.b64decode(raw["samples"], validate=True)).hexdigest()
        if digest != actual:
            reject("Raw sample SHA-256 does not match integrity.digest", 400, "SNAPSHOT_INTEGRITY_MISMATCH")
    return captured


class PeriodicSnapshotStore:
    max_rows = MAX_ROWS
    max_streams = MAX_STREAMS

    def __init__(self, database=":memory:", *, model=None):
        if model is not None and not isinstance(model, SnapshotModelAdapter):
            raise ValueError("Use an explicit SnapshotModelAdapter; legacy model objects are not supported")
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        if str(database) != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database), check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            allowed = {"periodic_snapshot_schema", "snapshot_streams", "periodic_snapshots",
                       "snapshot_inference_jobs", "sqlite_sequence"}
            if tables and ("periodic_snapshot_schema" not in tables or not tables <= allowed):
                raise ValueError("Periodic snapshots require a separate database file")
            if "periodic_snapshot_schema" in tables:
                versions = self.db.execute("SELECT version FROM periodic_snapshot_schema").fetchall()
                if len(versions) != 1 or versions[0][0] not in (1, 2):
                    raise ValueError("Unsupported periodic snapshot schema")
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS periodic_snapshot_schema(version INTEGER PRIMARY KEY);
                INSERT INTO periodic_snapshot_schema SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM periodic_snapshot_schema);
                CREATE TABLE IF NOT EXISTS snapshot_streams(
                    device TEXT NOT NULL, boot TEXT NOT NULL, context TEXT NOT NULL,
                    anchor_uptime INTEGER NOT NULL, anchor_captured REAL NOT NULL,
                    PRIMARY KEY(device,boot));
                CREATE TABLE IF NOT EXISTS periodic_snapshots(
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    device TEXT NOT NULL, site TEXT NOT NULL, asset TEXT NOT NULL,
                    boot TEXT NOT NULL, idx INTEGER NOT NULL, uptime INTEGER NOT NULL,
                    captured REAL NOT NULL, received REAL NOT NULL,
                    digest TEXT NOT NULL, body TEXT NOT NULL, late INTEGER NOT NULL,
                    quality TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                    UNIQUE(device,boot,idx));
                CREATE INDEX IF NOT EXISTS snapshot_queue ON periodic_snapshots(status,ordinal);
                CREATE INDEX IF NOT EXISTS snapshot_latest
                    ON periodic_snapshots(device,site,asset,captured DESC,late ASC,ordinal DESC);
                CREATE TABLE IF NOT EXISTS snapshot_inference_jobs(
                    ordinal INTEGER PRIMARY KEY, binding TEXT NOT NULL, metadata TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL, created REAL NOT NULL,
                    token TEXT, lease_until REAL, started REAL);
                CREATE INDEX IF NOT EXISTS snapshot_inference_queue
                    ON snapshot_inference_jobs(binding,status,ordinal);
                UPDATE periodic_snapshot_schema SET version=2;
                COMMIT;
            """)
        except Exception:
            self.db.close()
            raise
        self.inference = SnapshotInference(self, model)
        device_lifecycle.register_history(self)

    def check_device_deletion(self, device_id):
        with self.lock:
            if self.db.execute("SELECT 1 FROM snapshot_streams WHERE device=? LIMIT 1", (device_id,)).fetchone():
                reject("Periodic snapshot history protects this device ID", 409, "DEVICE_HAS_HISTORY")

    def _check_chronology(self, window, captured):
        """Clock checks use acquisition time, never HTTP arrival intervals."""
        device, boot, index = (window[k] for k in ("deviceId", "bootId", "windowIndex"))
        uptime = window["startUptimeUs"]
        context = json.dumps([window[k] for k in ("siteId", "assetId", "profileId")])
        stream = self.db.execute(
            "SELECT * FROM snapshot_streams WHERE device=? AND boot=?", (device, boot)).fetchone()
        if stream:
            if context != stream["context"]:
                reject("Snapshot stream context changed; use a new bootId", 409, "SNAPSHOT_SEQUENCE_CONFLICT")
            elapsed = (uptime - stream["anchor_uptime"]) / 1e6
            if abs(captured - stream["anchor_captured"] - elapsed) > CLOCK_TOLERANCE_SECONDS:
                reject("Measurement timestamp and uptime disagree", 409, "TIMESTAMP_UPTIME_MISMATCH")
            # Late retries may fill history but cannot falsify either neighbor.
            for operator, order in (("<", "DESC"), (">", "ASC")):
                neighbor = self.db.execute(
                    f"SELECT idx,uptime,captured FROM periodic_snapshots WHERE device=? AND boot=? "
                    f"AND idx{operator}? ORDER BY idx {order} LIMIT 1", (device, boot, index)).fetchone()
                if neighbor:
                    if operator == "<":
                        ordered = uptime > neighbor["uptime"] and captured > neighbor["captured"]
                    else:
                        ordered = uptime < neighbor["uptime"] and captured < neighbor["captured"]
                    if not ordered:
                        reject("Snapshot index, uptime and timestamp must have the same order",
                               409, "SNAPSHOT_SEQUENCE_CONFLICT")
        else:
            if self.db.execute("SELECT count(*) FROM snapshot_streams").fetchone()[0] >= self.max_streams:
                reject("Snapshot stream capacity reached; retry later", 503, "SNAPSHOT_BACKPRESSURE")
            self.db.execute("INSERT INTO snapshot_streams VALUES(?,?,?,?,?)",
                            (device, boot, context, uptime, captured))

    @device_lifecycle.serialized
    def ingest(self, principal, device_id, payload):
        with data.STORE_LOCK:
            data.principal_can_ingest(principal, device_id)
            window = validate_snapshot(payload)
            if window.get("deviceId") != device_id:
                reject("Route/device mismatch", code="INVALID_PERIODIC_SNAPSHOT")
            for key in ("deviceId", "siteId", "assetId"):
                if window.get(key) != data.required_text(window, key).upper():
                    reject("Use canonical " + key, code="INVALID_PERIODIC_SNAPSHOT")
            data.validate_telemetry_mapping(window)
        body = canonical(payload)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            with self.lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                # Exact retry remains valid even if its original measurement has
                # aged beyond the NEW-input time bounds. No ACK timestamp rewrite.
                boot, index = window.get("bootId"), window.get("windowIndex")
                if not isinstance(boot, str) or type(index) is not int:
                    reject("Invalid snapshot identity", code="INVALID_PERIODIC_SNAPSHOT")
                if not 0 <= index <= 2**31 - 1:
                    reject("Invalid windowIndex", code="INVALID_PERIODIC_SNAPSHOT")
                row = self.db.execute(
                    "SELECT * FROM periodic_snapshots WHERE device=? AND boot=? AND idx=?",
                    (device_id, boot, index)).fetchone()
                accepted = 0
                if row:
                    if row["digest"] != digest:
                        reject("Same snapshot identity has different content", 409, "SNAPSHOT_CONFLICT")
                else:
                    captured = normalize_window(window)
                    if self.db.execute("SELECT count(*) FROM periodic_snapshots").fetchone()[0] >= self.max_rows:
                        reject("Snapshot storage capacity reached; retry later", 503, "SNAPSHOT_BACKPRESSURE")
                    self._check_chronology(window, captured)
                    latest = self.db.execute(
                        "SELECT max(captured) FROM periodic_snapshots WHERE device=? AND site=? AND asset=?",
                        (device_id, window["siteId"], window["assetId"])).fetchone()[0]
                    late = latest is not None and captured <= latest
                    quality = window["quality"]
                    status = "queued" if quality == "valid" else "unavailable"
                    result = None if quality == "valid" else json.dumps({
                        "status": "unavailable", "reason": quality,
                        "verdict": None, "affectsAlerts": False,
                    })
                    inserted = self.db.execute(
                        """INSERT INTO periodic_snapshots
                        (device,site,asset,boot,idx,uptime,captured,received,digest,body,late,quality,status,result)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (device_id, window["siteId"], window["assetId"], boot, index,
                         window["startUptimeUs"], captured, time.time(), digest, body,
                         int(late), quality, status, result))
                    row = self.db.execute("SELECT * FROM periodic_snapshots WHERE ordinal=?",
                                          (inserted.lastrowid,)).fetchone()
                    self.inference.enqueue(inserted.lastrowid, window)
                    accepted = 1
            # The context manager COMMIT above must succeed before an ACK exists.
        except sqlite3.Error as exc:
            raise data.ApiError(503, "SNAPSHOT_STORAGE_UNAVAILABLE",
                                "Snapshot not acknowledged; retry the unchanged request") from exc
        transmission = json.loads(row["body"])["transmission"]
        return {
            "deviceId": device_id, "policyId": transmission["policyId"], "accepted": accepted,
            "acknowledged": [{"bootId": row["boot"], "windowIndex": row["idx"],
                              "digest": row["digest"], "receivedAt": iso(row["received"])}],
            "duplicate": not bool(accepted), "lateArrival": bool(row["late"]),
            "processingStatus": row["status"], "processingEnabled": True,
            "inferenceEnabled": self.inference.model is not None and self.inference.model.matches(window),
        }, 202 if accepted else 200

    def start(self):
        with self.lock:
            if self.worker is not None:
                return
            self.worker = threading.Thread(target=self._run, name="snapshot-input", daemon=True)
            self.worker.start()
            self.inference.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOGGER.exception("Snapshot preparation failed; committed inbox retained for retry")
            self.stop.wait(.5)

    def tick(self):
        # One short deterministic preparation transaction. A second process
        # serializes at BEGIN IMMEDIATE; crashes cannot leave a processing lease
        # stranded, count a row twice, or overwrite a final result.
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM periodic_snapshots WHERE status='queued' ORDER BY ordinal LIMIT 1").fetchone()
            if row is None:
                return False
            result = {"status": "waiting_model", "reason": "MODEL_NOT_CONFIGURED",
                      "score": None, "threshold": None, "verdict": None, "modelVersion": None,
                      "inferenceEnabled": False, "affectsAlerts": False,
                      "inputPreparation": {"adapterId": ADAPTER_ID, "status": "ready"}}
            try:
                payload = json.loads(row["body"])
                if hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest() != row["digest"]:
                    reject("Stored snapshot digest mismatch", code="SNAPSHOT_INTEGRITY_MISMATCH")
                window = validate_snapshot(payload)
                # Already accepted input may legitimately outlive reception's 48h limit.
                normalize_window(window, check_time_bounds=False)
                result["preparedInput"] = prepare_input(window)
            except (data.ApiError, ValueError, TypeError, KeyError, OverflowError) as exc:
                result.update(status="unavailable", reason=(exc.code if isinstance(exc, data.ApiError)
                              else str(exc) if str(exc) in {"clipped", "sample_gap"}
                              else "INPUT_PREPARATION_FAILED"))
                result["inputPreparation"]["status"] = "unavailable"
            result["preparedAt"] = iso(time.time())
            job = self.db.execute("SELECT ordinal FROM snapshot_inference_jobs WHERE ordinal=?", (row["ordinal"],)).fetchone()
            if job:
                if result["status"] == "waiting_model":
                    result.update(status="queued_inference", reason=None)
                else:
                    self.db.execute("UPDATE snapshot_inference_jobs SET status='unavailable' WHERE ordinal=?", (row["ordinal"],))
            self.db.execute("UPDATE periodic_snapshots SET status=?,result=? WHERE ordinal=? AND status='queued'",
                            (result["status"], json.dumps(result, allow_nan=False), row["ordinal"]))
        return True

    @device_lifecycle.serialized
    def list_device(self, user, device_id):
        data.require_permission(user, "device:read")
        data.require_permission(user, "telemetry:read")
        with data.STORE_LOCK:
            device = data.copy_payload(data.get_device(device_id))
            data.require_site_access(user, device["siteId"])
        with self.lock:
            scope = (device_id, device["siteId"], device["assetId"])
            rows = self.db.execute(
                "SELECT * FROM periodic_snapshots WHERE device=? AND site=? AND asset=? "
                "ORDER BY captured DESC,late ASC,ordinal DESC LIMIT ?", (*scope, LIST_LIMIT)).fetchall()
            statuses = dict(self.db.execute(
                "SELECT status,count(*) FROM periodic_snapshots WHERE device=? AND site=? AND asset=? GROUP BY status",
                scope))
            jobs = {r["ordinal"]: dict(r) for r in self.db.execute("""SELECT j.* FROM snapshot_inference_jobs j
                JOIN periodic_snapshots w USING(ordinal) WHERE w.device=? AND w.site=? AND w.asset=?
                ORDER BY w.captured DESC,w.late ASC,w.ordinal DESC LIMIT ?""", (*scope, LIST_LIMIT))}
        latest_transmission = json.loads(rows[0]["body"])["transmission"] if rows else None
        model = self.inference.model
        configured = model.metadata() if model is not None and model.matches(
            {"deviceId": device_id, "siteId": device["siteId"], "assetId": device["assetId"]}) else None
        return {
            "deviceId": device_id, "siteId": device["siteId"], "assetId": device["assetId"],
            "policyId": latest_transmission["policyId"] if latest_transmission else EDGE_SNAPSHOT_POLICY_ID,
            "intervalSec": snapshot_interval_seconds(latest_transmission or {}),
            "preferredPolicyId": EDGE_SNAPSHOT_POLICY_ID,
            "transmissionPolicies": snapshot_policy_metadata(),
            "latestTransmission": latest_transmission,
            "boardStateSource": "device_report", "boardStateVerifiedByServer": False,
            "processingEnabled": True, "inferenceEnabled": configured is not None,
            "stage": "inference" if configured else "input_preparation", "inputAdapterId": ADAPTER_ID,
            "configuredModel": configured, "modelStatus": "ready" if configured else "not_configured", "limit": LIST_LIMIT,
            "affectsAlerts": False, "historyReprocessingEnabled": False,
            "statuses": statuses, "supportedProfileIds": [RAW_PROFILE],
            "items": [{**json.loads(row["body"]), "ordinal": row["ordinal"],
                       "digest": row["digest"], "receivedAt": iso(row["received"]),
                       "lateArrival": bool(row["late"]),
                       "inferenceJob": ({"status": jobs[row["ordinal"]]["status"],
                           "bindingId": jobs[row["ordinal"]]["binding"], "attempts": jobs[row["ordinal"]]["attempts"],
                           "runtimeAvailable": configured is not None and model.binding_id == jobs[row["ordinal"]]["binding"]}
                           if row["ordinal"] in jobs else None),
                       "analysis": json.loads(row["result"]) if row["result"] else {
                           "status": "queued", "reason": "INFERENCE_NOT_ENABLED",
                           "verdict": None, "affectsAlerts": False,
                       }} for row in rows],
        }

    def close(self):
        self.stop.set()
        if self.worker is not None and self.worker.ident is not None:
            self.worker.join()
        self.inference.close()
        device_lifecycle.unregister_history(self)
        with self.lock:
            self.db.close()
