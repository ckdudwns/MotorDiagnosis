"""Durable RF66 incidents; independent of telemetry scores and field approval.

Written in the raw-window transaction; projected into runtime state idempotently.
No network calls or runtime-state locks are taken while writing raw results.
"""
import hashlib
import json
import time

from . import data
from .rf66_timing import continuous_interval, interval_range_us

MODES = {"shadow", "events", "alerts"}
POLICY = "rf66-event-lifecycle-v1"
MAX_AGE = 30  # Old/backlogged observations cannot open or resolve live incidents.
MAX_INCIDENTS = 10000


class RF66Events:
    def __init__(self, store, mode="shadow", clock=time.time):
        if mode not in MODES:
            raise ValueError("RF66_EVENT_MODE must be shadow, events, or alerts")
        self.store, self.mode, self.clock = store, mode, clock
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS rf66_incidents(
                id TEXT PRIMARY KEY, device TEXT NOT NULL, payload TEXT NOT NULL,
                revision INTEGER NOT NULL, projected INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS rf66_event_state(device TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS rf66_incident_pending ON rf66_incidents(projected,revision);
        """)

    def metadata(self):
        return {"mode": self.mode, "policyId": POLICY, "openHits": 3,
                "recoveryHits": 3, "maxObservationAgeSeconds": MAX_AGE,
                "startIntervalRangeUs": interval_range_us(),
                "notificationsEnabled": self.mode == "alerts", "fieldValidated": False}

    def _save_incident(self, event):
        event["rf66Revision"] += 1
        self.store.db.execute("INSERT INTO rf66_incidents(id,device,payload,revision) VALUES(?,?,?,?) "
                              "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision",
                              (event["id"], event["deviceId"], json.dumps(event), event["rf66Revision"]))

    def apply(self, row, window, result):
        db = self.store.db
        record = db.execute("SELECT payload FROM rf66_event_state WHERE device=?", (row["device"],)).fetchone()
        state = json.loads(record[0]) if record else {}
        active = None
        if state.get("activeId"):
            saved = db.execute("SELECT payload FROM rf66_incidents WHERE id=?", (state["activeId"],)).fetchone()
            active = json.loads(saved[0]) if saved else None
        context = [window[k] for k in ("siteId", "assetId", "bootId", "profileId")]
        context += [result.get("modelVersion"), result.get("threshold"), self.mode]
        continuous = (state.get("context") == context
                      and state.get("index") == window["windowIndex"] - 1
                      and continuous_interval(window["startUptimeUs"], state.get("uptime", -1)))
        age = self.clock() - row["captured"]
        valid = (self.mode != "shadow" and -5 <= age <= MAX_AGE
                 and result.get("status") == "completed" and window["quality"] == "valid"
                 and type(result.get("verdict")) is bool)
        hits = state.get("hits", []) if continuous and valid else []
        hits = (hits + [result["verdict"]])[-3:] if valid else []
        opened = valid and hits == [True, True, True]
        recovered = valid and hits == [False, False, False]
        reason = None if valid else "SHADOW_MODE" if self.mode == "shadow" else "STALE_INPUT" if not -5 <= age <= MAX_AGE else result.get("reason", "INVALID_INPUT")
        if active:
            # Never use a new asset/model's normal readings to close an old incident.
            same_target = (active["siteId"] == window["siteId"] and active["assetId"] == window["assetId"]
                           and active["modelVersion"] == result.get("modelVersion")
                           and active["rf66Threshold"] == result.get("threshold"))
            observation = "observing" if valid and same_target else "unknown"
            if not same_target:
                reason = "CONTEXT_CHANGED_REQUIRES_REVIEW"
            if recovered and same_target:
                active.update(status="closed", endAt=window["timestamp"], endReason="three_normal_windows",
                              rf66Observation="recovered", rf66Transition="closed", rf66Score=result["score"],
                              title=window["assetId"] + " RF66 임계값 이내 3구간 · 이벤트 해제",
                              rf66RecoveryEvidence={"windowIndex": window["windowIndex"], "bootId": window["bootId"],
                                                    "timestamp": window["timestamp"], "confirmation": result["confirmation"]})
                self._save_incident(active)
                active = None
            elif active["rf66Observation"] != observation:
                active.update(rf66Observation=observation, rf66ObservationReason=reason)
                self._save_incident(active)
        elif opened:
            if db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0] >= MAX_INCIDENTS:
                raise RuntimeError("RF66 incident capacity reached; accepted window retained")
            key = json.dumps([window["deviceId"], context, window["windowIndex"]])
            identifier = "RF66-" + hashlib.sha256(key.encode()).hexdigest()[:32].upper()
            active = {"id": identifier, "deviceId": window["deviceId"], "siteId": window["siteId"],
                      "assetId": window["assetId"], "eventType": "rf66_anomaly_candidate", "source": "rf66",
                      "title": window["assetId"] + " RF66 3구간 이상 확인", "occurredAt": window["timestamp"],
                      "time": window["timestamp"], "status": "open", "endAt": None, "endReason": None,
                      "severity": "critical", "label": "needs_review", "reviewed": False,
                      "reviewedAt": None, "reviewedBy": None, "score": None, "maxScore": None,
                      "note": "RF66 모델 후보 판정 · 현장 성능 미검증 · 통계 점수와 별개",
                      "modelVersion": result["modelVersion"], "rf66Score": result["score"],
                      "rf66Threshold": result["threshold"], "fieldValidated": False,
                      "rf66Policy": self.metadata(), "rf66Observation": "observing",
                      "rf66Transition": "open", "rf66Revision": 0,
                      "rf66Evidence": {"windowIndex": window["windowIndex"], "bootId": window["bootId"],
                                       "timestamp": window["timestamp"], "features66": result.get("modelInput"),
                                       "confirmation": result["confirmation"]}}
            self._save_incident(active)
        new_state = {"activeId": active["id"] if active else None, "context": context,
                     "index": window["windowIndex"], "uptime": window["startUptimeUs"],
                     "hits": hits, "captured": row["captured"], "valid": valid}
        db.execute("INSERT OR REPLACE INTO rf66_event_state VALUES(?,?)", (row["device"], json.dumps(new_state)))
        return {**self.metadata(), "activeEventId": new_state["activeId"], "validWindows": len(hits),
                "anomalyHits": sum(hits), "normalHits": len(hits)-sum(hits), "reason": reason}

    def dispatch(self):
        # Missing input is an unknown observation, never a recovery transition.
        with self.store.lock, self.store.db:
            for saved in self.store.db.execute("SELECT payload FROM rf66_event_state").fetchall():
                state = json.loads(saved[0])
                if state.get("activeId") and (self.mode == "shadow" or self.clock()-state["captured"] > MAX_AGE):
                    record = self.store.db.execute("SELECT payload FROM rf66_incidents WHERE id=?", (state["activeId"],)).fetchone()
                    event = json.loads(record[0])
                    if event["rf66Observation"] != "unknown":
                        event.update(rf66Observation="unknown", rf66ObservationReason="NO_RECENT_INPUT_OR_DISABLED")
                        self._save_incident(event)
        # Copy under the raw lock; release it BEFORE entering durable runtime state.
        with self.store.lock:
            rows = self.store.db.execute("SELECT id,payload,revision FROM rf66_incidents WHERE projected<revision LIMIT 50").fetchall()
        for row in rows:
            event = json.loads(row["payload"])
            with data.STORE_LOCK:
                previous = next((e for e in data.EVENTS if e["id"] == event["id"]), None)
                if previous is None:
                    data.EVENTS.insert(0, data.copy_payload(event))
                    site = data.get_site(event["siteId"])
                    site["eventCount"] = int(site["eventCount"]) + 1
                    # RF evidence is never inferred from unrelated telemetry.
                    data.EVENT_EVIDENCE_SNAPSHOTS[event["id"]] = {"featureSnapshot": event["rf66Evidence"]}
                    data._freeze_event_evidence(event, creating=True)
                elif previous.get("rf66Revision", 0) < row["revision"]:
                    review = {k: previous.get(k) for k in ("label", "reviewed", "reviewedAt", "reviewedBy", "note")}
                    previous.update(event)
                    previous.update(review)
            # Crash between commits is safe: event ID/revision makes replay idempotent.
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE rf66_incidents SET projected=? WHERE id=? AND revision=?",
                                      (row["revision"], row["id"], row["revision"]))

    def notification_allowed(self, event):
        if event.get("rf66Transition") not in {"open", "closed"}:
            return False
        if self.mode != "alerts" or event.get("rf66Policy", {}).get("mode") != "alerts":
            return False
        with self.store.lock:
            row = self.store.db.execute("SELECT payload FROM rf66_incidents WHERE id=?", (event["id"],)).fetchone()
            record = self.store.db.execute("SELECT payload FROM rf66_event_state WHERE device=?", (event["deviceId"],)).fetchone()
        if not row or not record:
            return False
        current, state = json.loads(row[0]), json.loads(record[0])
        model = self.store.model
        transition_time = data.parse_rfc3339("transition", current.get("endAt") or current["occurredAt"]).timestamp()
        with data.STORE_LOCK:
            try:
                device = data.get_device(event["deviceId"])
            except data.ApiError:
                return False
            if (device.get("mappingStatus") != "active" or device["siteId"] != event["siteId"]
                    or device["assetId"] != event["assetId"]):
                return False
        return bool(model is not None and model.checksum == event["modelVersion"]
                    and model.threshold == event["rf66Threshold"] and state.get("valid")
                    and -5 <= self.clock()-state["captured"] <= MAX_AGE
                    and -5 <= self.clock()-transition_time <= MAX_AGE
                    and current["rf66Transition"] == event["rf66Transition"]
                    and current["rf66Observation"] != "unknown")

    def resolve(self, user, event_id, payload):
        """Explicit audited operator closure, never represented as sensor recovery."""
        data.require_permission(user, "event:review")
        reason = data.required_text(payload, "reason")
        if len(reason) > 500:
            raise data.ApiError(400, "INVALID_REASON", "Resolution reason must be at most 500 characters")
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            row = self.store.db.execute("SELECT payload FROM rf66_incidents WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise data.ApiError(404, "RF66_EVENT_NOT_FOUND", "RF66 event not found")
            event = json.loads(row[0])
            data.require_site_access(user, event["siteId"])
            if event["status"] == "closed":
                return event
            event.update(status="closed", endAt=data.now_iso(), endReason="operator_resolution",
                         rf66Transition="manual_closed", rf66Observation="operator_resolved",
                         rf66Resolution={"actorId": user["id"], "reason": reason, "at": data.now_iso()})
            self._save_incident(event)
            state_row = self.store.db.execute("SELECT payload FROM rf66_event_state WHERE device=?", (event["deviceId"],)).fetchone()
            state = json.loads(state_row[0])
            if state.get("activeId") == event_id:
                state.update(activeId=None, hits=[])
                self.store.db.execute("UPDATE rf66_event_state SET payload=? WHERE device=?", (json.dumps(state), event["deviceId"]))
        return event
