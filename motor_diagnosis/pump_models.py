"""Pinned JSON models for a versioned, uniformly scheduled feature history.

Event novelty and numerical forecasting are separate outputs. Neither model is
trained online; forecast values never become event/alert verdicts. The supplied
workbook's feature provenance remains unverified, so ADXL use is explicit and
marked experimental rather than silently claiming source equivalence.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re

from . import data, edge_feature_snapshots as contract
from .pump_event_model import read_artifact, score_vector, _object, _numbers
from .snapshot_model import SnapshotModelAdapter
from .pump_model_settings import ENV_KEYS, INPUT_MODE

FORECAST_TYPE = "pump-summary-experiment-v3"


def read_forecast(path, checksum):
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Explicit forecast artifact SHA-256 required")
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("Forecast artifact size/checksum mismatch")
    model = json.loads(raw, object_pairs_hook=_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    keys = {"schemaVersion", "modelType", "features", "mode", "domainValidated", "unit",
            "streams", "windowRows", "intervalSec", "forecastHorizonSec", "inputHistorySec",
            "derivedFeatures", "sourceSha256", "evaluation"}
    derived = [f"{feature}:{stat}" for stat in ("last", "mean", "std", "delta") for feature in contract.FEATURES]
    if (not isinstance(model, dict) or set(model) != keys
            or any(type(model[k]) is not int or model[k] != v for k, v in
                   (("schemaVersion", 2), ("windowRows", 13), ("intervalSec", 25),
                    ("forecastHorizonSec", 300), ("inputHistorySec", 300)))
            or model["modelType"] != FORECAST_TYPE or model["features"] != list(contract.FEATURES)
            or model["derivedFeatures"] != derived or model["mode"] != "offline_experiment"
            or model["domainValidated"] is not False or model["unit"] != "unverified_source_values"
            or not isinstance(model["sourceSha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", model["sourceSha256"])
            or not isinstance(model["streams"], dict) or not 1 <= len(model["streams"]) <= 16):
        raise ValueError("Unsupported forecast model contract")
    for name, stream in model["streams"].items():
        if (not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name) or not isinstance(stream, dict)
                or set(stream) != {"center", "scale", "active", "pcaMean", "pcaAxes", "thresholds", "forecast"}):
            raise ValueError("Invalid forecast stream")
        active = stream["active"]
        if (not _numbers(stream["center"], 36) or not _numbers(stream["scale"], 36)
                or any(v < 0 for v in stream["scale"])
                or not isinstance(active, list) or not active
                or any(type(i) is not int or not 0 <= i < 36 for i in active)
                or len(set(active)) != len(active)
                or any(stream["scale"][i] <= 0 for i in active)):
            raise ValueError("Invalid forecast input guard")
        forecast = stream["forecast"]
        if (not isinstance(forecast, dict) or set(forecast) != {"type", "horizonSec", "alpha", "inputMean",
                "inputStd", "targetMean", "targetStd", "weights", "operationallyApproved",
                "targetLower", "targetUpper", "inputRobustLimit"}
                or forecast["type"] != "ridge" or type(forecast["horizonSec"]) is not int
                or forecast["horizonSec"] != 300 or forecast["operationallyApproved"] is not False
                or not _numbers([forecast["alpha"]], 1) or forecast["alpha"] < 0
                or not _numbers(forecast["inputMean"], 36) or not _numbers(forecast["inputStd"], 36)
                or min(forecast["inputStd"]) <= 0 or not _numbers(forecast["targetMean"], 9)
                or not _numbers(forecast["targetStd"], 9) or min(forecast["targetStd"]) <= 0
                or not isinstance(forecast["weights"], list) or len(forecast["weights"]) != 36
                or any(not _numbers(row, 9) for row in forecast["weights"])):
            raise ValueError("Invalid forecast dimensions/scales")
        if (not _numbers([forecast["inputRobustLimit"]], 1) or forecast["inputRobustLimit"] <= 0
                or not _numbers(forecast["targetLower"], 9) or not _numbers(forecast["targetUpper"], 9)
                or any(lo > hi for lo, hi in zip(forecast["targetLower"], forecast["targetUpper"]))):
            raise ValueError("Invalid forecast guard limits")
    return model


def forecast_vector(history):
    """Past-only last/mean/population-std/delta blocks, in artifact order."""
    if len(history) != 13 or any(not _numbers(row, 9) for row in history):
        raise ValueError("Forecast requires 13 finite nine-feature records")
    mean = [math.fsum(row[j] for row in history) / 13 for j in range(9)]
    std = [math.sqrt(math.fsum((row[j]-mean[j])**2 for row in history) / 13) for j in range(9)]
    return list(history[-1]) + mean + std + [history[-1][j]-history[0][j] for j in range(9)]


def predict_features(forecast, history):
    """Unmodified ridge equation; serving guards below never clip its output."""
    vector = forecast_vector(history)
    scaled = [(v-m)/s for v, m, s in zip(vector, forecast["inputMean"], forecast["inputStd"])]
    values = [math.fsum(scaled[i]*forecast["weights"][i][j] for i in range(36))
              * forecast["targetStd"][j] + forecast["targetMean"][j] for j in range(9)]
    if any(not math.isfinite(v) for v in values):
        raise ValueError("Nonfinite forecast")
    return values


def forecast_input_check(stream, history):
    vector = forecast_vector(history)
    # Use the forecast stream's fitted robust scales, NOT ridge inputStd or
    # the separate 24-record event verifier's scales/thresholds.
    robust = max(abs((vector[i]-stream["center"][i])/stream["scale"][i])
                 for i in stream["active"])
    if not math.isfinite(robust):
        raise ValueError("Nonfinite forecast input distance")
    return robust, stream["forecast"]["inputRobustLimit"]


def forecast_output_valid(forecast, values):
    return (_numbers(values, 9)
            and all(lo <= v <= hi for v, lo, hi in zip(values, forecast["targetLower"], forecast["targetUpper"]))
            and all(values[i] >= 1 for i in (0, 1, 2, 6, 7, 8)))


class PumpDualModels(SnapshotModelAdapter):
    requires_history = True

    def __init__(self, event_path, event_checksum, forecast_path, forecast_checksum, stream, scope, *, input_mode):
        if input_mode != INPUT_MODE:
            raise ValueError("Explicit experimental-adxl25 input mode required; source equivalence is unverified")
        event = read_artifact(event_path, event_checksum)
        forecast = read_forecast(forecast_path, forecast_checksum)
        if (stream not in event["streams"] or stream not in forecast["streams"]
                or event["sourceSha256"] != forecast["sourceSha256"]):
            raise ValueError("Both models require the same explicit training source and stream")
        self._event = copy.deepcopy(event["streams"][stream])
        self._forecast = copy.deepcopy(forecast["streams"][stream]["forecast"])
        self._forecast_stream = copy.deepcopy(forecast["streams"][stream])
        self._forecast_meta = {"modelId": FORECAST_TYPE, "modelVersion": "sha256:" + forecast_checksum,
                              "stream": stream, "windowRows": 13, "intervalSec": 25, "horizonSec": 300,
                              "affectsAlerts": False, "operationallyApproved": False}
        super().__init__({"contractId": contract.HISTORY_CONTRACT_ID,
            "modelId": "pump-event-verifier-v1", "modelVersion": "sha256:" + event_checksum,
            "preprocessingVersion": "pump-dual-adxl25-v3:" + stream + ":" + forecast_checksum,
            "inputContract": copy.deepcopy(contract.HISTORY_INPUT_CONTRACT), "scope": scope,
            "scoreType": "novelty_reference_ratio", "threshold": 1.0, "comparison": ">"},
            lambda *_: {"unavailableReason": "VERIFIER_HISTORY_REQUIRED"})
        self.binding_id = hashlib.sha256(json.dumps(self.metadata(), sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def metadata(self):
        return {**super().metadata(), "forecastModel": copy.deepcopy(self._forecast_meta),
                "inputMode": INPUT_MODE, "sourceFeatureEquivalenceVerified": False,
                "domainValidated": False}

    def _history_reason(self, history, context, count):
        if len(history) != count:
            return "HISTORY_INSUFFICIENT"
        previous = None
        for record in history:
            w = record["window"]
            if (not self.matches(w) or w["bootId"] != context["bootId"] or w["quality"] != "valid"
                    or type(w.get("historySequence")) is not int or w.get("periodicSlotEpoch") is None
                    or not _numbers(record["values"], 9)):
                return "HISTORY_QUALITY_OR_SCOPE"
            if previous and not self._adjacent(previous, w, record["captured"]):
                return "HISTORY_DISCONTINUITY"
            previous = record
        at = data.parse_rfc3339("timestamp", context["timestamp"]).timestamp()
        if context["periodicSlotEpoch"] is not None:
            if not self._adjacent(previous, context, at):
                return "HISTORY_DISCONTINUITY"
        else:
            delay = at-previous["captured"]
            uptime = (context["startUptimeUs"]-previous["window"]["startUptimeUs"])/1e6
            if not 0 < delay <= 50 or not 0 < uptime <= 50 or abs(delay-uptime) > 1:
                return "EVENT_HISTORY_TOO_OLD"
        return None

    @staticmethod
    def _adjacent(previous, current, at):
        w = previous["window"]
        elapsed = at-previous["captured"]
        uptime = (current["startUptimeUs"]-w["startUptimeUs"])/1e6
        return (current["historySequence"] == w["historySequence"]+1
                and current["periodicSlotEpoch"] == w["periodicSlotEpoch"]+25
                and abs(elapsed-25) <= 1.000001 and abs(elapsed-uptime) <= 1.000001
                and uptime > 0)

    def evaluate_history(self, prepared, context, history):
        if ({k: prepared.get(k) for k in contract.HISTORY_INPUT_CONTRACT} != contract.HISTORY_INPUT_CONTRACT
                or not _numbers(prepared.get("values"), 9)
                or any(context.get(k) != v for k, v in self.metadata()["scope"].items())):
            raise ValueError("Dual model input/sensor binding mismatch")
        reason = self._history_reason(history, context, 24)
        result = {"status": "unavailable", "reason": "VERIFIER_" + reason if reason else None,
                  "score": None, "verdict": None,
                  "evidence": {"decision": "insufficient_history", "historicalRecordsUsed": len(history),
                    "groundTruthAvailable": False, "domainValidated": False,
                    "sourceFeatureEquivalenceVerified": False, "inputMode": INPUT_MODE}}
        if reason is None:
            try:
                score, robust, pca = score_vector(self._event, [r["values"] for r in history], prepared["values"])
                if not _numbers([score, robust, pca], 3) or min(score, robust, pca) < 0:
                    raise ValueError("Invalid verifier output")
            except (ValueError, OverflowError):
                result["reason"] = "VERIFIER_OUTPUT_INVALID"
                result["evidence"]["decision"] = "unavailable"
            else:
                result.update(status="completed", score=score, verdict=score > 1)
                result["evidence"].update(decision="possible_anomaly" if score > 1 else "not_confirmed_by_baseline",
                    robustDeviation=robust, pcaResidual=pca, referenceThresholds=self._event["thresholds"],
                    historyDigests=[r["digest"] for r in history], historyOrdinals=[r["ordinal"] for r in history],
                    historySpanSec=history[-1]["window"]["periodicSlotEpoch"]-history[0]["window"]["periodicSlotEpoch"])
        forecast = {"status": "unavailable", "reason": None, "features": None, **self._forecast_meta}
        if context["periodicSlotEpoch"] is None:
            forecast.update(status="not_applicable", reason="FORECAST_REQUIRES_SCHEDULED_RECORD")
        else:
            prior = history[-12:]
            failure = self._history_reason(prior, context, 12)
            if failure:
                forecast["reason"] = "FORECAST_" + failure
            else:
                try:
                    inputs = [r["values"] for r in prior]+[prepared["values"]]
                    robust, limit = forecast_input_check(self._forecast_stream, inputs)
                    forecast["guard"] = {"policyId": "pump-forecast-reject-v3", "inputRobust": robust,
                                         "inputRobustLimit": limit, "outputClipped": False}
                    if robust > limit:
                        forecast["reason"] = "FORECAST_INPUT_OUT_OF_DISTRIBUTION"
                        result["forecast"] = forecast
                        return result
                    values = predict_features(self._forecast, inputs)
                    if not forecast_output_valid(self._forecast, values):
                        forecast["reason"] = "FORECAST_OUTPUT_OUT_OF_RANGE"
                        result["forecast"] = forecast
                        return result
                    at = data.parse_rfc3339("timestamp", context["timestamp"]).timestamp()
                    forecast.update(status="completed", features=dict(zip(contract.FEATURES, values)),
                        basedOnMeasuredAt=context["timestamp"],
                        predictedFor=datetime.fromtimestamp(at+300, timezone.utc).isoformat().replace("+00:00", "Z"),
                        targetSlotEpoch=context["periodicSlotEpoch"]+300,
                        inputOrdinals=[r["ordinal"] for r in prior]+[context["ordinal"]],
                        inputDigests=[r["digest"] for r in prior]+[context["digest"]])
                except (ValueError, OverflowError):
                    forecast["reason"] = "FORECAST_OUTPUT_INVALID"
        result["forecast"] = forecast
        return result


def configured_model(env=None):
    from .pump_event_model import configured_model as legacy, ENV_KEYS as LEGACY_KEYS
    env = os.environ if env is None else env
    values = [env.get(key) for key in ENV_KEYS]
    if not any(values):
        return legacy(env)
    if any(env.get(key) for key in LEGACY_KEYS):
        raise ValueError("Configure one model path; remove legacy PUMP_EVENT_VERIFIER settings first")
    if not all(isinstance(v, str) and v for v in values):
        raise ValueError("All PUMP_DUAL settings are required; no implicit device/sensor/stream selection")
    event, event_hash, forecast, forecast_hash, stream, device, site, asset, sensor, mode = values
    return PumpDualModels(event, event_hash, forecast, forecast_hash, stream,
        {"deviceId": device, "siteId": site, "assetId": asset, "sensorId": sensor}, input_mode=mode)


def main():
    """Read-only deployment preflight; never modifies service settings or data."""
    import argparse
    parser = argparse.ArgumentParser(description="Validate the two pinned JSON artifacts and explicit sensor binding")
    parser.add_argument("--event-artifact", required=True)
    parser.add_argument("--event-checksum", required=True)
    parser.add_argument("--forecast-artifact", required=True)
    parser.add_argument("--forecast-checksum", required=True)
    parser.add_argument("--stream", required=True)
    for name in ("device", "site", "asset", "sensor"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--input-mode", required=True, choices=[INPUT_MODE])
    args = parser.parse_args()
    model = PumpDualModels(args.event_artifact, args.event_checksum, args.forecast_artifact,
        args.forecast_checksum, args.stream,
        {"deviceId":args.device, "siteId":args.site, "assetId":args.asset, "sensorId":args.sensor},
        input_mode=args.input_mode)
    print(json.dumps({"artifactsVerified":True, "bindingId":model.binding_id,
                      "model":model.metadata(), "serviceModified":False},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
