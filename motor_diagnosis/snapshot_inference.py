"""Durable, fenced snapshot inference jobs; computation never holds the inbox lock."""
import hashlib
import json
import logging
import threading
import time
import uuid

from . import data
from .snapshot_input import prepare_input

LOGGER = logging.getLogger(__name__)
LEASE_SECONDS = 60
MAX_ATTEMPTS = 3


class SnapshotInference:
    def __init__(self, store, model):
        self.store = store
        self.model = model
        self.worker = None
        self.gate = threading.Lock()

    def enqueue(self, ordinal, window):
        """Called inside the original receipt transaction, never on duplicate ACKs.

        Assign only newly accepted, scoped, valid windows. Existing history is
        not retroactively attached when a model is configured or replaced.
        """
        if self.model is not None and window["quality"] == "valid" and self.model.matches(window):
            self.store.db.execute("""INSERT INTO snapshot_inference_jobs
                (ordinal,binding,metadata,status,attempts,created) VALUES(?,?,?,'queued',0,?)""",
                (ordinal, self.model.binding_id, json.dumps(self.model.metadata()), time.time()))

    def start(self):
        if self.model is not None and self.worker is None:
            self.worker = threading.Thread(target=self._run, name="snapshot-inference", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.store.stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOGGER.exception("Snapshot inference storage failed; fenced job retained")
            self.store.stop.wait(.5)

    def _claim(self):
        with self.store.lock:
            if self.store.stop.is_set():
                return None
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                row = self.store.db.execute("""SELECT w.*,j.attempts,j.metadata AS model_metadata
                    FROM snapshot_inference_jobs j JOIN periodic_snapshots w USING(ordinal)
                    WHERE j.binding=? AND w.status='queued_inference'
                    AND (j.status='queued' OR (j.status='running' AND j.lease_until<=?))
                    ORDER BY j.ordinal LIMIT 1""", (self.model.binding_id, time.time())).fetchone()
                if row is None:
                    return None
                token, now = uuid.uuid4().hex, time.time()
                self.store.db.execute("""UPDATE snapshot_inference_jobs
                    SET status='running',token=?,lease_until=?,attempts=attempts+1,started=? WHERE ordinal=?""",
                    (token, now + LEASE_SECONDS, now, row["ordinal"]))
                return dict(row), token, now

    def _finish(self, row, token, started, outcome, duration_ms):
        from .periodic_snapshots import iso

        with self.store.lock:
            if self.store.stop.is_set():
                return False
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                job = self.store.db.execute("SELECT * FROM snapshot_inference_jobs WHERE ordinal=?", (row["ordinal"],)).fetchone()
                # An expired attempt cannot overwrite a newer claim or final result.
                if job["status"] != "running" or job["token"] != token or job["binding"] != self.model.binding_id:
                    return False
                if time.time() >= job["lease_until"] or duration_ms >= LEASE_SECONDS * 1000:
                    outcome = {"status": "unavailable", "reason": "INFERENCE_DEADLINE_EXCEEDED", "score": None, "verdict": None}
                metadata = self.model.metadata()
                try:
                    result = json.loads(row["result"])
                    if not isinstance(result, dict):
                        result = {}
                except (TypeError, ValueError):
                    result = {}
                result.update(outcome, modelVersion=metadata["modelVersion"], threshold=metadata["threshold"],
                              comparison=metadata["comparison"], scoreType=metadata["scoreType"],
                              inferenceEnabled=True, affectsAlerts=False)
                result["inference"] = {"bindingId": job["binding"], "model": metadata,
                    "inputDigest": row["digest"], "attempt": job["attempts"],
                    "startedAt": iso(started), "completedAt": iso(time.time()), "durationMs": duration_ms}
                self.store.db.execute("UPDATE periodic_snapshots SET status=?,result=? WHERE ordinal=? AND status='queued_inference'",
                                      (outcome["status"], json.dumps(result, allow_nan=False), row["ordinal"]))
                self.store.db.execute("UPDATE snapshot_inference_jobs SET status=?,token=NULL,lease_until=NULL WHERE ordinal=?",
                                      (outcome["status"], row["ordinal"]))
        return True

    def tick(self):
        if self.model is None or self.store.stop.is_set() or not self.gate.acquire(blocking=False):
            return False
        try:
            claimed = self._claim()
            if claimed is None:
                return False
            row, token, started = claimed
            start = time.monotonic()
            outcome = {"status": "unavailable", "reason": "MODEL_EXECUTION_FAILED", "score": None, "verdict": None}
            if row["attempts"] >= MAX_ATTEMPTS:
                outcome["reason"] = "INFERENCE_RETRY_EXHAUSTED"
            else:
                try:
                    from .periodic_snapshots import canonical, normalize_window
                    from .transmission_policy import validate_snapshot

                    payload = json.loads(row["body"])
                    if hashlib.sha256(canonical(payload).encode()).hexdigest() != row["digest"]:
                        raise ValueError("Stored original changed")
                    window = validate_snapshot(payload)
                    normalize_window(window, check_time_bounds=False)
                    prepared = prepare_input(window)
                    saved = json.loads(row["result"])
                    if (not isinstance(saved, dict) or saved.get("preparedInput") != prepared or not self.model.matches(window)
                            or json.loads(row["model_metadata"]) != self.model.metadata()):
                        raise ValueError("Prepared input or binding mismatch")
                    context = {k: window[k] for k in ("deviceId", "siteId", "assetId", "bootId",
                               "windowIndex", "timestamp", "startUptimeUs")}
                    context["historySequence"] = window.get("historySequence")
                    context["sensorId"] = window.get("sensorId")
                except (data.ApiError, ValueError, TypeError, KeyError, OverflowError):
                    outcome["reason"] = "SNAPSHOT_INPUT_INTEGRITY_FAILED"
                else:
                    try:
                        if getattr(self.model, "requires_history", False):
                            history = self._history(row)
                            outcome = self.model.evaluate_history(prepared, context, history)
                        else:
                            outcome = self.model.evaluate(prepared, context)
                    except ValueError:
                        outcome["reason"] = "MODEL_OUTPUT_INVALID"
                    except Exception:
                        # Do not expose model exceptions, filenames or credentials in API results.
                        outcome["reason"] = "MODEL_EXECUTION_FAILED"
            self._finish(row, token, started, outcome, round((time.monotonic() - start) * 1000, 3))
            return True
        finally:
            self.gate.release()

    def _history(self, current):
        """Only originals available at receipt, strictly before this measurement.

        Invalid rows are NOT filtered out: they break the history. Extra event
        reports do not occupy 25-second history slots. No arrival-order sequence
        or interpolation is manufactured. Stored input is checked again.
        """
        from .periodic_snapshots import canonical, normalize_window
        from .transmission_policy import validate_snapshot
        from .pump_summary import POLICY_ID, PROFILE_ID, FEATURES
        with self.store.lock:
            rows = self.store.db.execute("""SELECT * FROM periodic_snapshots
                WHERE device=? AND site=? AND asset=? AND boot=? AND sensor=? AND captured<? AND ordinal<?
                AND json_extract(body,'$.window.profileId')=?
                AND json_extract(body,'$.transmission.policyId')=?
                AND json_extract(body,'$.transmission.reason')='history_periodic'
                ORDER BY captured DESC,ordinal DESC LIMIT 24""",
                (current["device"], current["site"], current["asset"], current["boot"],
                 current["sensor"], current["captured"], current["ordinal"], PROFILE_ID, POLICY_ID)).fetchall()
        history = []
        for row in reversed(rows):
            payload = json.loads(row["body"])
            if hashlib.sha256(canonical(payload).encode()).hexdigest() != row["digest"]:
                raise ValueError("History digest mismatch")
            w = validate_snapshot(payload)
            captured = normalize_window(w, check_time_bounds=False)
            if (w["quality"] != row["quality"] or captured != row["captured"] or w["sensorId"] != row["sensor"]
                    or w["windowIndex"] != row["idx"] or w["startUptimeUs"] != row["uptime"]
                    or any(w[k] != row[column] for k, column in
                           (("deviceId", "device"), ("siteId", "site"), ("assetId", "asset"), ("bootId", "boot")))):
                raise ValueError("History metadata mismatch")
            history.append({"window": w, "captured": captured, "ordinal": row["ordinal"], "digest": row["digest"],
                            "values": [w["features"][k] for k in FEATURES] if w["quality"] == "valid" else None})
        return history

    def close(self):
        if self.worker is not None and self.worker.ident is not None:
            # A future adapter must implement bounded/cancellable inference. An
            # uncooperative callable cannot block shutdown or write after close.
            self.worker.join(timeout=2)
