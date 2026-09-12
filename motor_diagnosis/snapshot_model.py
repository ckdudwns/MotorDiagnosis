"""Explicit, local adapter boundary for a future single-snapshot anomaly model.

No artifact discovery, pickle loading, legacy fallback or example predictor is
provided. The deployment must supply a reviewed adapter with its own preprocessing.
"""
import copy
import hashlib
import json
import math
import operator
import re

from .raw_samples import G_PER_COUNT, PROFILE_ID
from .snapshot_input import ADAPTER_ID

CONTRACT_ID = "single-snapshot-anomaly-v1"
INPUT_CONTRACT = {
    "adapterId": ADAPTER_ID, "sourceProfileId": PROFILE_ID,
    "shape": [512, 3], "axes": ["X", "Y", "Z"], "unit": "g",
    "sampleRateHz": 800, "gPerCount": G_PER_COUNT, "meanRemoved": False,
}
COMPARISONS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class SnapshotModelAdapter:
    def __init__(self, metadata, predict):
        required = {"contractId", "modelId", "modelVersion", "preprocessingVersion",
                    "inputContract", "scope", "scoreType", "threshold", "comparison"}
        if not isinstance(metadata, dict) or set(metadata) != required:
            raise ValueError("Explicit snapshot model metadata is required")
        if metadata["contractId"] != CONTRACT_ID or metadata["inputContract"] != INPUT_CONTRACT:
            raise ValueError("Unsupported single-snapshot model input contract")
        for key in ("modelId", "modelVersion", "preprocessingVersion", "scoreType"):
            if (not isinstance(metadata[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", metadata[key])):
                raise ValueError("Invalid snapshot model " + key)
        scope = metadata["scope"]
        if (not isinstance(scope, dict) or set(scope) != {"deviceId", "siteId", "assetId"}
                or any(not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,99}", v) for v in scope.values())):
            raise ValueError("An explicit device/site/asset scope is required")
        if (not finite_number(metadata["threshold"]) or not isinstance(metadata["comparison"], str)
                or metadata["comparison"] not in COMPARISONS):
            raise ValueError("A finite threshold and explicit comparison are required")
        if not callable(predict):
            raise ValueError("A local snapshot prediction callable is required")
        # Pin an independent metadata snapshot; later caller mutations cannot
        # change queued jobs or a score's decision rule.
        self._metadata = copy.deepcopy(metadata)
        self._predict = predict
        self.binding_id = hashlib.sha256(json.dumps(
            self._metadata, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def metadata(self):
        return copy.deepcopy(self._metadata)

    def matches(self, window):
        return all(window.get(k) == v for k, v in self._metadata["scope"].items())

    def evaluate(self, prepared, context):
        if {k: prepared.get(k) for k in INPUT_CONTRACT} != INPUT_CONTRACT:
            raise ValueError("Prepared input does not match the declared adapter contract")
        output = self._predict(copy.deepcopy(prepared), copy.deepcopy(context))
        if not isinstance(output, dict):
            raise ValueError("Invalid model output")
        if set(output) == {"unavailableReason"}:
            reason = output["unavailableReason"]
            if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", reason):
                raise ValueError("Invalid model unavailability reason")
            return {"status": "unavailable", "reason": reason, "score": None, "verdict": None}
        if set(output) != {"score"} or not finite_number(output["score"]):
            raise ValueError("Model output must contain one finite score, not an inferred boolean")
        score = output["score"]
        return {"status": "completed", "reason": None, "score": score,
                "verdict": COMPARISONS[self._metadata["comparison"]](score, self._metadata["threshold"])}
