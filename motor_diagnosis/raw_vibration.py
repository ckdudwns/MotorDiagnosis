"""ADXL345 lossless count transport and model-independent spectral66 preparation.

No research-package imports, pickle loaders, optional ML dependencies or model
activation. All samples and units remain available for later reproducibility.
"""

import cmath
import json
import math
import struct
import time

from .vibration_windows import VibrationWindowStore, reject
from .raw_samples import PROFILE_ID, ENCODING, G_PER_COUNT, RAW_KEYS, decode_samples, normalize
from .window_features import FEATURE_NAMES
from .rf66_timing import continuous_interval, interval_range_us

FEATURE_PROFILE = "mcc5-vibration-800hz-spectral66-v1"
FINE_BANDS = ((0, 25), (25, 50), (50, 75), (75, 100),
              (100, 150), (150, 200), (200, 275), (275, 350))
EXTRA_NAMES = [
    f"vibration{axis}.{name}" for axis in "XYZ"
    for name in ([f"ratio_{lo}_{hi}" for lo, hi in FINE_BANDS] +
                 ["entropy_normalized", "centroid_hz", "bandwidth_hz", "peak_hz",
                  "peak_power_fraction", "rolloff85_hz"])
] + ["correlation.XY", "correlation.XZ", "correlation.YZ"]
NAMES = list(FEATURE_NAMES) + EXTRA_NAMES


def fft(values):
    """Fixed radix-2 FFT, no package installation required on the server."""
    n = len(values)
    out = [complex(v) for v in values]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            out[i], out[j] = out[j], out[i]
    width = 2
    while width <= n:
        step = cmath.exp(-2j * math.pi / width)
        for start in range(0, n, width):
            phase = 1 + 0j
            for k in range(width // 2):
                a = out[start + k]
                b = phase * out[start + k + width // 2]
                out[start + k], out[start + k + width // 2] = a + b, a - b
                phase *= step
        width *= 2
    return out


def extract66(counts):
    if len(counts) != 512 or any(len(row) != 3 for row in counts):
        raise ValueError("Expected 512 XYZ samples")
    if any(type(v) is not int or v <= -4096 or v >= 4095 for row in counts for v in row):
        raise ValueError("clipped")
    n = 512
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
    normalization = n * sum(w * w for w in hann)
    base, extra, centered = [], [], []
    frequencies = [k * 800 / n for k in range(1, 224)]  # strictly below 350 Hz
    for axis in range(3):
        raw = [row[axis] * G_PER_COUNT for row in counts]
        mean = sum(raw) / n
        ac = [v - mean for v in raw]
        centered.append(ac)
        variance = sum(v * v for v in ac) / n
        if variance <= 1e-16:
            raise ValueError("constant_axis")
        spectrum = fft([v * w for v, w in zip(ac, hann)])
        powers = [abs(v) ** 2 for v in spectrum[1:224]]
        energy = sum(powers)
        if energy <= 1e-16:
            raise ValueError("Insufficient spectral support energy")
        base.extend([math.sqrt(variance), max(abs(v) for v in ac),
                     sum(v ** 4 for v in ac) / n / variance ** 2])
        for lo, hi in ((0, 50), (50, 100), (100, 200), (200, 350)):
            base.append(sum(p * 2 / normalization for f, p in zip(frequencies, powers) if lo <= f < hi))
        fractions = [p / energy for p in powers]
        extra.extend(sum(p for f, p in zip(frequencies, fractions) if lo <= f < hi)
                     for lo, hi in FINE_BANDS)
        centroid = sum(p * f for p, f in zip(fractions, frequencies))
        peak = max(range(len(powers)), key=powers.__getitem__)
        cumulative, rolloff = 0.0, frequencies[-1]
        for f, p in zip(frequencies, fractions):
            cumulative += p
            if cumulative >= 0.85:
                rolloff = f
                break
        extra.extend([-sum(p * math.log(p) for p in fractions if p > 0) / math.log(len(powers)),
                      centroid, math.sqrt(sum(p * (f-centroid)**2 for p, f in zip(fractions, frequencies))),
                      frequencies[peak], fractions[peak], rolloff])
    for a, b in ((0, 1), (0, 2), (1, 2)):
        x, y = centered[a], centered[b]
        value = sum(u*v for u, v in zip(x, y)) / math.sqrt(sum(u*u for u in x) * sum(v*v for v in y))
        extra.append(max(-1.0, min(1.0, value)))
    # Reproduce the training cache's float32 base21, then promote to float64.
    values = [struct.unpack("<f", struct.pack("<f", v))[0] for v in base] + extra
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Nonfinite spectral features")
    return values


class RawVibrationStore(VibrationWindowStore):
    normalize = staticmethod(normalize)
    profile_id = PROFILE_ID
    storage_kind = "raw-counts"
    max_rows = 300000  # >48 hours for one device at 1.5625 windows/s; bounded globally.
    max_batch = 4
    list_limit = 20
    list_order = "CASE WHEN EXISTS(SELECT 1 FROM raw_delivery WHERE metadata IS NOT NULL) THEN captured ELSE ordinal END DESC,ordinal DESC"

    def batch_metadata(self, payload):
        from .transmission_policy import validate_batch
        return validate_batch(payload)

    def accept_reordered(self, window, stream, metadata):
        # Explicit opt-in only; legacy transport keeps the old strict contract.
        if metadata is None or metadata["mode"] == "priority":
            return False
        index, uptime = window["windowIndex"], window["startUptimeUs"]
        if not (index < stream["idx"] and uptime < stream["uptime"]):
            return False
        # A backfill may fill a real hole, not rewrite chronology or context.
        for operator, order in (("<", "DESC"), (">", "ASC")):
            neighbor = self.db.execute(
                f"SELECT body FROM vibration_windows WHERE device=? AND boot=? AND idx{operator}? ORDER BY idx {order} LIMIT 1",
                (window["deviceId"], window["bootId"], index)).fetchone()
            if neighbor:
                other = json.loads(neighbor[0])["startUptimeUs"]
                if (operator == "<" and other >= uptime) or (operator == ">" and other <= uptime):
                    return False
        return True

    def record_delivery(self, ordinal, metadata):
        self.db.execute("INSERT INTO raw_delivery VALUES(?,?,?)",
                        (ordinal, time.time(), json.dumps(metadata) if metadata else None))

    def next_queued_row(self):
        # Priority work cannot wait behind a five-minute archival backlog.
        return self.db.execute("""SELECT w.* FROM vibration_windows w
            LEFT JOIN raw_delivery d ON d.ordinal=w.ordinal WHERE w.status='queued'
            ORDER BY CASE WHEN json_extract(d.metadata,'$.mode')='priority' THEN 0 ELSE 1 END,
                     w.captured,w.ordinal LIMIT 1""").fetchone()

    def prune(self):
        if not self.processing_enabled:
            return  # Retired server runtime preserves historical evidence.
        super().prune()
        with self.lock, self.db:
            self.db.execute("DELETE FROM raw_delivery WHERE ordinal NOT IN (SELECT ordinal FROM vibration_windows)")

    def __init__(self, database=":memory:", *, model=None, event_mode="shadow", processing_enabled=True):
        from .rf66_events import RF66Events, MODES
        if event_mode not in MODES:
            raise ValueError("RF66_EVENT_MODE must be shadow, events, or alerts")
        self.processing_enabled = processing_enabled
        self.model = model if processing_enabled else None
        super().__init__(database)
        self.variant = "spectral66"
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_clock_anchors(device TEXT, boot TEXT, uptime INTEGER, captured REAL, PRIMARY KEY(device,boot))")
        self.db.execute("CREATE INDEX IF NOT EXISTS raw_device_order ON vibration_windows(device,ordinal)")
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_delivery(ordinal INTEGER PRIMARY KEY,received REAL NOT NULL,metadata TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_confirmation_heads(device TEXT,lane TEXT,ordinal INTEGER,PRIMARY KEY(device,lane))")
        self.db.commit()
        try:
            self.events = RF66Events(self, event_mode if processing_enabled else "shadow")
        except Exception:
            self.close()
            raise

    def start(self):
        if self.processing_enabled:
            super().start()

    def tick(self):
        return super().tick() if self.processing_enabled else False

    def ingest(self, principal, device_id, payload):
        ack, status = super().ingest(principal, device_id, payload)
        if not self.processing_enabled:
            ack.update(processingEnabled=False, inferenceEnabled=False, reason="LEGACY_RF66_DISABLED",
                       replacementPath=f"/api/devices/{device_id}/periodic-snapshots")
        return ack, status

    def input_names(self):
        return list(NAMES)

    def input_values(self, window):
        return extract66(decode_samples(window))

    def result_context(self):
        result = super().result_context()
        result.update(score=None, modelType="random_forest", featureProfileId=FEATURE_PROFILE,
                      fieldValidated=False, confirmationApplied=False)
        if self.model is not None:
            result.update(self.model.metadata())
        return result

    def infer_window(self, values):
        if self.model is None:
            return {"status": "waiting_model", "reason": "MODEL_NOT_CONFIGURED"}
        prediction = self.model.predict_window(values)
        score, threshold, verdict = (prediction[k] for k in ("score", "threshold", "verdict"))
        if (type(score) not in (int, float) or type(threshold) not in (int, float)
                or not math.isfinite(score) or not math.isfinite(threshold)
                or not 0 <= score <= 1 or not 0 <= threshold <= 1
                or type(verdict) is not bool or verdict != (score > threshold)
                or threshold != self.model.threshold):
            raise ValueError("Invalid RF66 window prediction")
        return {"status": "completed", "score": score, "threshold": threshold, "verdict": verdict}

    def validate_stream_clock(self, window, captured):
        key = (window["deviceId"], window["bootId"])
        anchor = self.db.execute("SELECT uptime,captured FROM raw_clock_anchors WHERE device=? AND boot=?", key).fetchone()
        if anchor is None:
            self.db.execute("INSERT INTO raw_clock_anchors VALUES(?,?,?,?)", (*key, window["startUptimeUs"], captured))
        elif abs((captured - anchor[1]) * 1e6 - (window["startUptimeUs"] - anchor[0])) > 1000000:
            reject("Measurement UTC and uptime elapsed disagree", 409, "TIMESTAMP_UPTIME_MISMATCH")

    def finalize_result(self, row, window, result):
        # Derive state only from committed results. A failed UPDATE rolls back
        # both the result and its confirmation, so retries never count twice.
        policy = "rf66-consecutive-3-v1"
        history = []
        reason = "STREAM_START"
        delivery = self.db.execute("SELECT received,metadata FROM raw_delivery WHERE ordinal=?", (row["ordinal"],)).fetchone()
        metadata = json.loads(delivery["metadata"]) if delivery and delivery["metadata"] else None
        lane = "live" if metadata and metadata["mode"] == "priority" and -5 <= self.events.clock()-row["captured"] <= 30 else "history"
        previous = self.db.execute(
            "SELECT * FROM vibration_windows WHERE device=? AND ordinal<? ORDER BY ordinal DESC LIMIT 1",
            (row["device"], row["ordinal"])).fetchone()
        if metadata is not None:
            previous = self.db.execute("""SELECT w.* FROM vibration_windows w
                JOIN raw_confirmation_heads h ON w.ordinal=h.ordinal
                WHERE h.device=? AND h.lane=?""", (row["device"], lane)).fetchone()
            # Late history cannot move a lane's current observation backwards.
            if previous is not None and previous["captured"] >= row["captured"]:
                previous = None
            else:
                self.db.execute("INSERT OR REPLACE INTO raw_confirmation_heads VALUES(?,?,?)",
                                (row["device"], lane, row["ordinal"]))
            if lane == "history":
                # Backfill can arrive behind an already analyzed later archive.
                # Build its own measured-index chain without rewinding live state.
                previous = self.db.execute("""SELECT * FROM vibration_windows
                    WHERE device=? AND boot=? AND idx=? AND result IS NOT NULL
                    AND json_extract(result,'$.transmission.lane')='history'""",
                    (row["device"], window["bootId"], window["windowIndex"]-1)).fetchone()
            result["transmission"] = {**metadata, "receivedAtEpoch": delivery["received"], "lane": lane}
        if previous is not None:
            old_window = json.loads(previous["body"])
            old_result = json.loads(previous["result"]) if previous["result"] else {}
            old = old_result.get("confirmation", {})
            if any(window[k] != old_window[k] for k in
                   ("deviceId", "siteId", "assetId", "bootId", "profileId")):
                reason = "STREAM_CHANGED"
            elif any(result.get(k) != old_result.get(k) for k in
                     ("modelVersion", "featureProfileId", "threshold")):
                reason = "MODEL_CHANGED"
            elif row["gap"] or window["windowIndex"] != old_window["windowIndex"] + 1:
                reason = "WINDOW_GAP"
            # Explicit asymmetric limits: preserve the 630ms lower bound.
            elif not continuous_interval(window["startUptimeUs"], old_window["startUptimeUs"]):
                reason = "TIME_GAP"
            elif (old_result.get("status") != "completed" or old_window["quality"] != "valid"
                  or old.get("policyId") != policy or old_result.get("confirmationApplied") is not True):
                reason = "PREVIOUS_UNAVAILABLE"
            else:
                history = old.get("verdicts", [])
                if (not isinstance(history, list) or not 1 <= len(history) <= 3
                        or any(type(v) is not bool for v in history)):
                    history, reason = [], "PREVIOUS_UNAVAILABLE"
                else:
                    reason = None
        available = (self.model is not None and result["status"] == "completed"
                     and window["quality"] == "valid" and type(result.get("verdict")) is bool)
        if available:
            history = (history + [result["verdict"]])[-3:]
            decision = int(all(history)) if len(history) == 3 else -1
            status = "confirmed_anomaly" if decision == 1 else "no_confirmed_anomaly" if decision == 0 else "warming_up"
        else:
            history, decision, status, reason = [], -1, "unavailable", result.get("reason", "UNAVAILABLE")
        result["confirmationApplied"] = self.model is not None
        result["confirmation"] = {
            "policyId": policy, "width": 3, "requiredHits": 3,
            "validWindows": len(history), "anomalyHits": sum(history),
            "verdicts": history, "decision": decision, "status": status,
            "resetReason": reason, "affectsAlerts": False,
            "startIntervalRangeUs": interval_range_us(),
        }
        if metadata is not None and lane == "history":
            result["eventLifecycle"] = {**self.events.metadata(), "reason": "HISTORICAL_DELIVERY",
                                        "activeEventId": None, "affectsAlerts": False}
        else:
            result["eventLifecycle"] = self.events.apply(row, window, result)
        return result

    def list_device(self, user, device_id):
        result = super().list_device(user, device_id)
        result["featureProfileId"] = FEATURE_PROFILE
        result["numericPolicy"] = "base21 float32 promoted to float64; extra45 float64"
        result["maxRows"] = self.max_rows
        result["configuredModel"] = self.model.metadata() if self.model is not None else None
        result["eventPolicy"] = self.events.metadata()
        result["processingEnabled"] = self.processing_enabled
        if not self.processing_enabled:
            result.update(inferenceEnabled=False, runtimeStatus="disabled", historicalOnly=True,
                          reason="LEGACY_RF66_DISABLED",
                          replacementPath=f"/api/devices/{device_id}/periodic-snapshots")
        return result
