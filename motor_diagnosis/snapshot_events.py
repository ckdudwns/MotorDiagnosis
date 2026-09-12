"""Durable incidents for sparse, single-snapshot server-model decisions.

Receipt pins event eligibility. Upgrades never replay historical model results.
An incident/result transaction precedes projection into the shared event view;
projection and the alert outbox are independently idempotent. No network I/O is
performed while the snapshot database or device identity gate is held.
"""
import hashlib
import json
import math
import time

from . import data, device_lifecycle
from .transmission_policy import snapshot_interval_seconds

POLICY_ID = "single-snapshot-event-v1"
MODES = {"shadow", "events", "alerts"}
MAX_TRANSITION_AGE = 60
REPORT_GRACE = 60
MAX_INCIDENTS = 10000


class SnapshotEvents:
    def __init__(self, store, mode="events", *, clock=time.time):
        self.store, self.clock = store, clock
        self.mode = mode

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        if value not in MODES:
            raise ValueError("SNAPSHOT_EVENT_MODE must be shadow, events or alerts")
        self._mode = value

    def metadata(self):
        return {"policyId": POLICY_ID, "mode": self.mode,
                "openAnomalousSnapshots": 1, "closeNormalSnapshots": 1,
                "continuousWindowsRequired": False, "boardStateUsedAsVerdict": False,
                "maxTransitionAgeSec": MAX_TRANSITION_AGE, "reportGraceSec": REPORT_GRACE,
                "historyReprocessingEnabled": False,
                "notificationsEnabled": self.mode == "alerts"}

    def enqueue(self, ordinal, window):
        # Called only for a newly accepted original, in the receipt transaction.
        model = self.store.inference.model
        binding = model.binding_id if model is not None and model.matches(window) else None
        self.store.db.execute("INSERT INTO snapshot_event_jobs VALUES(?,?,?,?,?,NULL)",
                              (ordinal, self.mode, binding, "pending", None))

    def _latest(self, device):
        model = self.store.inference.model
        if getattr(model, "requires_history", False):
            return self.store.db.execute("SELECT * FROM periodic_snapshots WHERE device=? AND sensor=? "
                "ORDER BY captured DESC,late ASC,ordinal DESC LIMIT 1",
                (device, model.metadata()["scope"]["sensorId"])).fetchone()
        return self.store.db.execute("SELECT * FROM periodic_snapshots WHERE device=? "
            "ORDER BY captured DESC,late ASC,ordinal DESC LIMIT 1", (device,)).fetchone()

    @staticmethod
    def _mapping(window):
        # Caller owns device_lifecycle gate, but not STORE_LOCK while waiting on DBs.
        with data.STORE_LOCK:
            device = next((d for d in data.DEVICES if d["id"] == window["deviceId"]), None)
            return bool(device and device.get("mappingStatus") == "active"
                        and device["siteId"] == window["siteId"]
                        and device["assetId"] == window["assetId"])

    def _reason(self, row, *, transition=False):
        """Fail closed, including malformed stored outputs; never infer from board state."""
        from .periodic_snapshots import canonical

        try:
            payload = json.loads(row["body"])
            window = payload["window"]
            result = json.loads(row["result"]) if row["result"] else {}
            model = self.store.inference.model
            if self.mode == "shadow":
                return "EVENTS_DISABLED"
            if not self._mapping(window):
                return "DEVICE_MAPPING_CHANGED"
            if model is None or not model.matches(window):
                return "MODEL_NOT_CONFIGURED_OR_SCOPE_CHANGED"
            latest = self._latest(row["device"])
            if row["late"] or not latest or latest["ordinal"] != row["ordinal"]:
                return "SUPERSEDED_OR_LATE_INPUT"
            age = self.clock() - row["captured"]
            limit = MAX_TRANSITION_AGE if transition else snapshot_interval_seconds(payload["transmission"]) + REPORT_GRACE
            if not -5 <= age <= limit:
                return "INPUT_STALE_OR_FUTURE"
            if row["quality"] != "valid" or window.get("quality") != "valid":
                return "INPUT_QUALITY"
            if row["status"] != "completed" or result.get("status") != "completed":
                return "MODEL_RESULT_UNAVAILABLE"
            meta = model.metadata()
            inference = result.get("inference") or {}
            if (inference.get("bindingId") != model.binding_id or inference.get("model") != meta
                    or inference.get("inputDigest") != row["digest"]
                    or hashlib.sha256(canonical(payload).encode()).hexdigest() != row["digest"]):
                return "MODEL_OR_INPUT_CHANGED"
            score, verdict = result.get("score"), result.get("verdict")
            if (type(score) not in (int, float) or not math.isfinite(score) or type(verdict) is not bool
                    or result.get("reason") is not None
                    or (not getattr(model, "requires_history", False) and window.get("sampleCount") != 512)
                    or any(result.get(k) != meta[k] for k in ("modelVersion", "threshold", "comparison", "scoreType"))):
                return "MODEL_RESULT_INVALID"
            threshold = meta["threshold"]
            expected = {">": score > threshold, ">=": score >= threshold,
                        "<": score < threshold, "<=": score <= threshold}[meta["comparison"]]
            if verdict != expected:
                return "MODEL_RESULT_INVALID"
        except (data.ApiError, ValueError, TypeError, KeyError, OverflowError):
            return "MODEL_RESULT_INVALID"
        return None

    @staticmethod
    def _evidence(row):
        payload, result = json.loads(row["body"]), json.loads(row["result"])
        w = payload["window"]
        return {"ordinal": row["ordinal"], "digest": row["digest"],
                **({"sensorId": w["sensorId"]} if "sensorId" in w else {}),
                "receivedAt": row["received"],
                **{k: w[k] for k in ("deviceId", "siteId", "assetId", "bootId", "windowIndex", "timestamp", "quality")},
                **{k: result[k] for k in ("score", "threshold", "comparison", "scoreType", "verdict", "modelVersion")},
                "inference": result["inference"], "transmission": payload["transmission"],
                **({"modelEvidence": result["evidence"]} if "evidence" in result else {})}

    def _save(self, event):
        event["snapshotRevision"] = event.get("snapshotRevision", 0) + 1
        self.store.db.execute("""INSERT INTO snapshot_incidents(id,device,binding,status,payload,revision)
            VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,
            payload=excluded.payload,revision=excluded.revision""",
            (event["id"], event["deviceId"], event["snapshotBindingId"], event["status"],
             json.dumps(event, allow_nan=False), event["snapshotRevision"]))

    def _apply(self, row):
        db = self.store.db
        model = self.store.inference.model
        reason = ("RECEIPT_POLICY_OR_MODEL_CHANGED" if row["event_mode"] != self.mode
                  or model is None or row["event_binding"] != model.binding_id else self._reason(row, transition=True))
        event_id = None
        if reason is None:
            result, payload = json.loads(row["result"]), json.loads(row["body"])
            w = payload["window"]
            active = db.execute("SELECT payload FROM snapshot_incidents WHERE device=? AND binding=? AND status='open'",
                                (row["device"], model.binding_id)).fetchone()
            event = json.loads(active[0]) if active else None
            evidence = self._evidence(row)
            if event is None and result["verdict"]:
                if db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0] >= MAX_INCIDENTS:
                    reason = "INCIDENT_CAPACITY"
                else:
                    event_id = "SNAPSHOT-" + hashlib.sha256((row["digest"] + model.binding_id).encode()).hexdigest()[:32]
                    event = {"id": event_id, "deviceId": row["device"], "siteId": row["site"], "assetId": row["asset"],
                        "source": "snapshot", "eventType": "snapshot_anomaly_candidate",
                        "title": row["asset"] + " · 단건 모델 이상 후보", "occurredAt": w["timestamp"],
                        "status": "open", "endAt": None, "endReason": None, "severity": "critical",
                        "score": None, "maxScore": None, "label": "needs_review", "reviewed": False,
                        "reviewedAt": None, "reviewedBy": None, "note": "서버 단건 모델의 이상 후보이며 보드 상태·통계 점수와 구분합니다.",
                        "modelVersion": result["modelVersion"], "snapshotBindingId": model.binding_id,
                        "snapshotScore": result["score"], "snapshotThreshold": result["threshold"],
                        "snapshotPolicy": self.metadata(), "snapshotTransition": "open",
                        "snapshotTransitionAt": w["timestamp"], "snapshotEvidence": evidence}
                    if getattr(model, "requires_history", False):
                        event.update(title=row["asset"] + " · 이력 검증 모델 기준 초과",
                                     sensorId=w["sensorId"], verificationLabel="unknown",
                                     note="25초 이력 24건 대비 이상 후보입니다. 점수는 고장 확률이 아니며 확정 고장 판정이 아닙니다.")
            elif event is not None and not result["verdict"]:
                event.update(status="closed", endAt=w["timestamp"],
                    endReason="novelty_reference_not_exceeded" if getattr(model, "requires_history", False) else "single_normal_snapshot",
                    snapshotTransition="closed", snapshotTransitionAt=w["timestamp"], snapshotRecoveryEvidence=evidence)
            if event is not None:
                event_id = event["id"]
                event.update(snapshotLastEvidence=evidence, snapshotLastOrdinal=row["ordinal"],
                             snapshotObservation="observing" if result["verdict"] else "recovered",
                             snapshotObservationReason=None)
                self._save(event)
        db.execute("UPDATE snapshot_event_jobs SET status=?,reason=?,event_id=? WHERE ordinal=?",
                   ("ignored" if reason else "processed", reason, event_id, row["ordinal"]))

    def _audit_active(self):
        for saved in self.store.db.execute("SELECT payload FROM snapshot_incidents WHERE status='open'").fetchall():
            event = json.loads(saved[0])
            row = self._latest(event["deviceId"])
            model = self.store.inference.model
            reason = "MODEL_CHANGED" if model is None or model.binding_id != event["snapshotBindingId"] else (
                "NO_INPUT" if row is None else self._reason(row))
            if reason is None and row["ordinal"] != event["snapshotLastOrdinal"]:
                reason = "NEW_OBSERVATION_NOT_APPLIED"
            # Only _apply may return unknown to observing, or close an incident.
            if reason and (event.get("snapshotObservation") != "unknown" or event.get("snapshotObservationReason") != reason):
                event.update(snapshotObservation="unknown", snapshotObservationReason=reason)
                self._save(event)

    @device_lifecycle.serialized
    def tick(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            rows = self.store.db.execute("""SELECT w.*,j.mode AS event_mode,j.binding AS event_binding
                FROM snapshot_event_jobs j JOIN periodic_snapshots w USING(ordinal)
                WHERE j.status='pending' AND (j.mode='shadow' OR j.binding IS NULL
                    OR j.mode!=? OR j.binding IS NOT ?
                    OR w.status IN ('completed','unavailable','waiting_model')) ORDER BY ordinal LIMIT 50""",
                (self.mode, self.store.inference.model.binding_id if self.store.inference.model else None)).fetchall()
            for row in rows:
                self._apply(row)
            self._audit_active()
        self.dispatch()
        return bool(rows)

    def dispatch(self):
        # A failed runtime-state commit must leave the revision pending.
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM snapshot_incidents WHERE projected<revision ORDER BY rowid LIMIT 50").fetchall()
        for row in rows:
            event = json.loads(row["payload"])
            with data.STORE_LOCK:
                previous = next((e for e in data.EVENTS if e["id"] == event["id"]), None)
                if previous is None:
                    data.EVENTS.insert(0, data.copy_payload(event))
                    site = data.get_site(event["siteId"])
                    site["eventCount"] = int(site["eventCount"]) + 1
                    data.EVENT_EVIDENCE_SNAPSHOTS[event["id"]] = {"featureSnapshot": event["snapshotEvidence"]}
                    data._freeze_event_evidence(event, creating=True)
                elif previous.get("snapshotRevision", 0) < row["revision"]:
                    review = {k: previous.get(k) for k in ("label", "reviewed", "reviewedAt", "reviewedBy", "note")}
                    previous.update(data.copy_payload(event))
                    previous.update(review)
                resolution = event.get("snapshotResolution")
                if resolution and not any(a["action"] == "snapshot_event_resolved" and a["targetId"] == event["id"] for a in data.AUDIT_LOGS):
                    data.append_audit_log(resolution["actor"], "snapshot_event_resolved", "event", event["id"],
                        {"status": "open"}, {"status": "closed", "resolution": resolution}, resolution["reason"], site_id=event["siteId"])
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE snapshot_incidents SET projected=? WHERE id=? AND revision=?",
                                      (row["revision"], row["id"], row["revision"]))

    @device_lifecycle.serialized
    def notification_allowed(self, event):
        if (self.mode != "alerts" or event.get("snapshotPolicy", {}).get("mode") != "alerts"
                or event.get("snapshotTransition") not in {"open", "closed"}):
            return False
        with self.store.lock:
            record = self.store.db.execute("SELECT payload FROM snapshot_incidents WHERE id=?", (event["id"],)).fetchone()
            if record is None:
                return False
            current = json.loads(record[0])
            model = self.store.inference.model
            if (model is None or model.binding_id != current["snapshotBindingId"]
                    or current["snapshotTransition"] != event["snapshotTransition"]
                    or current["snapshotObservation"] == "unknown"):
                return False
            row = self._latest(current["deviceId"])
            if row is None or row["ordinal"] != current["snapshotLastOrdinal"] or self._reason(row):
                return False
            age = self.clock() - data.parse_rfc3339("transition", current["snapshotTransitionAt"]).timestamp()
            return -5 <= age <= MAX_TRANSITION_AGE

    @device_lifecycle.serialized
    def resolve(self, user, event_id, payload):
        data.require_permission(user, "event:review")
        reason = data.required_text(payload, "reason")
        if len(reason) > 500:
            raise data.ApiError(400, "INVALID_REASON", "Resolution reason must be at most 500 characters")
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.store.db.execute("SELECT payload FROM snapshot_incidents WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise data.ApiError(404, "SNAPSHOT_EVENT_NOT_FOUND", "Snapshot event not found")
            event = json.loads(row[0])
            data.require_site_access(user, event["siteId"])
            if event["status"] != "closed":
                event.update(status="closed", endAt=data.now_iso(), endReason="operator_resolution",
                    snapshotTransition="manual_closed", snapshotObservation="operator_resolved", snapshotObservationReason=None,
                    snapshotResolution={"actor": {k: user[k] for k in ("id", "name", "role")}, "reason": reason, "at": data.now_iso()})
                self._save(event)
        self.dispatch()
        return event
