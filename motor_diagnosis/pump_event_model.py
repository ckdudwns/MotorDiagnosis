"""Pinned JSON-only implementation of PR45 be6ec32's event verifier.

No training, pickle, dynamic module loading, network calls or invented features.
Score is max(robust/reference, PCA/reference), NOT a fault probability.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .pump_summary import FEATURES, INPUT_CONTRACT, PROFILE_ID
from .snapshot_model import SnapshotModelAdapter

MODEL_TYPE = "pump-event-verifier-v1"
CONTRACT_ID = "history-event-verifier-v1"
PREPROCESSING_VERSION = "pump-event-vector45-robust-pca-v1"
MAX_ARTIFACT_BYTES = 1024 * 1024
ENV_KEYS = ("PUMP_EVENT_VERIFIER_ARTIFACT", "PUMP_EVENT_VERIFIER_CHECKSUM",
            "PUMP_EVENT_VERIFIER_STREAM", "PUMP_EVENT_VERIFIER_DEVICE_ID",
            "PUMP_EVENT_VERIFIER_SITE_ID", "PUMP_EVENT_VERIFIER_ASSET_ID", "PUMP_EVENT_VERIFIER_SENSOR_ID")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate model field")
        result[key] = value
    return result


def _numbers(values, count):
    return (isinstance(values, list) and len(values) == count
            and all(type(v) in (int, float) and abs(v) <= 1e12 and math.isfinite(v) for v in values))


def read_artifact(path, checksum):
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Explicit artifact SHA-256 required")
    with Path(path).open("rb") as source:
        raw = source.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES or hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("Event verifier artifact size/checksum mismatch")
    try:
        artifact = json.loads(raw, object_pairs_hook=_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        required = {"schemaVersion", "modelType", "mode", "features", "historyRows", "intervalSec",
                    "currentEventMaxDelaySec", "domainValidated", "streams", "sourceSha256"}
        if (not isinstance(artifact, dict) or set(artifact) != required
                or type(artifact["schemaVersion"]) is not int or artifact["schemaVersion"] != 1
                or artifact["modelType"] != MODEL_TYPE or artifact["mode"] != "unlabeled_novelty_verifier"
                or artifact["features"] != list(FEATURES) or artifact["domainValidated"] is not False
                or any(type(artifact[k]) is not int or artifact[k] != v for k, v in
                       (("historyRows", 24), ("intervalSec", 25), ("currentEventMaxDelaySec", 50)))
                or not isinstance(artifact["sourceSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", artifact["sourceSha256"])
                or not isinstance(artifact["streams"], dict) or not 1 <= len(artifact["streams"]) <= 16):
            raise ValueError("Unsupported verifier contract")
        for name, stream in artifact["streams"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name) or not isinstance(stream, dict) or set(stream) != {
                    "center", "scale", "active", "pcaMean", "pcaAxes", "thresholds"}:
                raise ValueError("Invalid model stream")
            active = stream["active"]
            if (not _numbers(stream["center"], 45) or not _numbers(stream["scale"], 45)
                    or not isinstance(active, list) or not 1 <= len(active) <= 45
                    or any(type(i) is not int or not 0 <= i < 45 for i in active)
                    or active != sorted(set(active))
                    or any(v < 0 for v in stream["scale"])
                    or any(stream["scale"][i] <= 0 for i in active)
                    or not _numbers(stream["pcaMean"], len(active))
                    or not _numbers(stream["thresholds"], 2) or min(stream["thresholds"]) <= 0
                    or not isinstance(stream["pcaAxes"], list)
                    or not 1 <= len(stream["pcaAxes"]) <= len(active)
                    or any(not _numbers(axis, len(active)) for axis in stream["pcaAxes"])):
                raise ValueError("Invalid verifier dimensions/scales")
            for i, axis in enumerate(stream["pcaAxes"]):
                for j, other in enumerate(stream["pcaAxes"][:i+1]):
                    if abs(math.fsum(a*b for a, b in zip(axis, other)) - (1 if i == j else 0)) > 1e-6:
                        raise ValueError("Invalid PCA basis")
    except (KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid verifier artifact") from exc
    return artifact


def score_vector(stream, history, current):
    # Same population std, block order, active MAD scales, clipping and PCA as
    # verifier_vector()/predict() in ai/ai2 at be6ec32; no online fitting.
    means = [math.fsum(row[j] for row in history) / 24 for j in range(9)]
    std = [math.sqrt(math.fsum((row[j]-means[j])**2 for row in history) / 24) for j in range(9)]
    vector = list(current) + means + std + [current[j]-history[-1][j] for j in range(9)] + [current[j]-means[j] for j in range(9)]
    z = [(vector[i]-stream["center"][i])/stream["scale"][i] for i in stream["active"]]
    robust = max(abs(v) for v in z)
    centered = [min(20, max(-20, v))-mean for v, mean in zip(z, stream["pcaMean"])]
    weights = [math.fsum(v*a for v, a in zip(centered, axis)) for axis in stream["pcaAxes"]]
    residual = [v-math.fsum(w*axis[j] for w, axis in zip(weights, stream["pcaAxes"])) for j, v in enumerate(centered)]
    pca = math.fsum(v*v for v in residual)/len(residual)
    score = max(robust/stream["thresholds"][0], pca/stream["thresholds"][1])
    if not all(math.isfinite(v) for v in (robust, pca, score)):
        raise ValueError("Nonfinite model score")
    return score, robust, pca


class PumpEventModel(SnapshotModelAdapter):
    requires_history = True

    def __init__(self, artifact, checksum, stream, scope):
        model = read_artifact(artifact, checksum)
        if stream not in model["streams"]:
            raise ValueError("Explicit known model stream required")
        self._stream = copy.deepcopy(model["streams"][stream])
        metadata = {"contractId": CONTRACT_ID, "modelId": MODEL_TYPE, "modelVersion": "sha256:" + checksum,
                    "preprocessingVersion": PREPROCESSING_VERSION + ":" + stream,
                    "inputContract": INPUT_CONTRACT, "scope": scope,
                    "scoreType": "novelty_reference_ratio", "threshold": 1.0, "comparison": ">"}
        super().__init__(metadata, lambda *_: {"unavailableReason": "VERIFIER_HISTORY_REQUIRED"})

    def matches(self, window):
        return super().matches(window) and window.get("profileId") == PROFILE_ID

    def evaluate_history(self, prepared, context, history):
        from . import data
        if ({k: prepared.get(k) for k in INPUT_CONTRACT} != INPUT_CONTRACT
                or not _numbers(prepared.get("values"), 9)
                or any(context.get(k) != v for k, v in self.metadata()["scope"].items())):
            raise ValueError("Verifier input/sensor binding mismatch")
        def missing(reason):
            return {"status": "unavailable", "reason": reason, "score": None, "verdict": None,
                    "evidence": {"decision": "insufficient_history", "historicalRecordsUsed": len(history),
                                 "groundTruthAvailable": False}}
        if len(history) != 24:
            return missing("VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        previous = None
        current_at = data.parse_rfc3339("timestamp", context["timestamp"]).timestamp()
        for record in history:
            w = record["window"]
            if (w["quality"] != "valid" or w["bootId"] != context["bootId"]
                    or not self.matches(w)):
                return missing("VERIFIER_HISTORY_QUALITY_OR_SCOPE")
            if previous is not None:
                elapsed = record["captured"]-previous["captured"]
                if abs(elapsed-25) > 1e-6 or w["historySequence"] != previous["window"]["historySequence"]+1:
                    return missing("VERIFIER_HISTORY_DISCONTINUITY")
                if abs((w["startUptimeUs"]-previous["window"]["startUptimeUs"])/1e6-25) > 1e-6:
                    return missing("VERIFIER_HISTORY_UPTIME_DISCONTINUITY")
            previous = record
        delay = current_at-history[-1]["captured"]
        if context.get("historySequence") is not None and (
                context["historySequence"] != history[-1]["window"]["historySequence"]+1 or abs(delay-25) > 1e-6):
            return missing("VERIFIER_HISTORY_DISCONTINUITY")
        uptime_delay = (context["startUptimeUs"]-history[-1]["window"]["startUptimeUs"])/1e6
        if not 0 < delay <= 50 or not 0 < uptime_delay <= 50 or abs(delay-uptime_delay) > 1:
            return missing("VERIFIER_EVENT_HISTORY_TOO_OLD")
        score, robust, pca = score_vector(self._stream, [r["values"] for r in history], prepared["values"])
        verdict = score > 1
        return {"status": "completed", "reason": None, "score": score, "verdict": verdict,
                "evidence": {"decision": "possible_anomaly" if verdict else "not_confirmed_by_baseline",
                    "historicalRecordsUsed": 24, "historySpanSec": history[-1]["captured"]-history[0]["captured"],
                    "robustDeviation": robust, "pcaResidual": pca, "referenceThresholds": self._stream["thresholds"],
                    "historyDigests": [r["digest"] for r in history], "historyOrdinals": [r["ordinal"] for r in history],
                    "groundTruthAvailable": False, "domainValidated": False}}


def configured_model(env=None):
    env = os.environ if env is None else env
    values = [env.get(key) for key in ENV_KEYS]
    if not any(values):
        return None
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("All PUMP_EVENT_VERIFIER settings are required; no implicit stream/device binding")
    artifact, checksum, stream, device, site, asset, sensor = values
    return PumpEventModel(artifact, checksum, stream,
                          {"deviceId": device, "siteId": site, "assetId": asset, "sensorId": sensor})
