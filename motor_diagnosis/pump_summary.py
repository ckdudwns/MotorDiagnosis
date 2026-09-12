"""Explicit source-feature contract. Never infer these features from raw XYZ.

The workbook does not specify the sensor manufacturer's feature formulas. A
producer must supply the same nine source features; this is not an ADXL345
feature extractor or evidence of cross-sensor calibration.
"""
import hashlib
import math
import re
import struct
import time

from . import data
from .window_envelope import QUALITY, RETENTION, reject

PROFILE_ID = "pump-cf-sk-ku-summary-v1"
POLICY_ID = "pump-verifier-history-v1"
ADAPTER_ID = "pump-nine-source-features-v1"
FEATURES = tuple(f"{kind}_a_{axis}" for kind in ("cf", "sk", "ku") for axis in (1, 2, 3))
INPUT_CONTRACT = {"adapterId": ADAPTER_ID, "sourceProfileId": PROFILE_ID,
                  "shape": [9], "features": list(FEATURES), "unit": "source_feature_values",
                  "historyRows": 24, "historyIntervalSec": 25, "currentEventMaxDelaySec": 50}
KEYS = {"schemaVersion", "deviceId", "siteId", "assetId", "bootId", "windowIndex",
        "timestamp", "startUptimeUs", "profileId", "quality", "features",
        "historySequence", "integrity", "sensorId"}


def feature_digest(features):
    # Language-independent bytes: nine IEEE-754 float64 little-endian values.
    raw = b"" if features is None else struct.pack("<9d", *(features[k] for k in FEATURES))
    return hashlib.sha256(raw).hexdigest()


def normalize(window, *, check_time_bounds=True):
    def invalid(message):
        reject(message, code="INVALID_PUMP_SUMMARY")
    if not isinstance(window, dict) or set(window) - {"rawWindow"} != KEYS:
        invalid("Use the exact pump summary envelope")
    if type(window["schemaVersion"]) is not int or window["schemaVersion"] != 1 or window["profileId"] != PROFILE_ID:
        invalid("Unsupported summary schema/profile")
    for key in ("deviceId", "siteId", "assetId", "sensorId"):
        if not isinstance(window[key], str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,99}", window[key]):
            invalid("Invalid " + key)
    if not isinstance(window["bootId"], str) or not re.fullmatch(r"[0-9a-f]{32}", window["bootId"]):
        invalid("Invalid bootId")
    for key, maximum in (("windowIndex", 2**31-1), ("startUptimeUs", 2**53-1)):
        if type(window[key]) is not int or not 0 <= window[key] <= maximum:
            invalid("Invalid " + key)
    seq = window["historySequence"]
    if seq is not None and (type(seq) is not int or not 0 <= seq <= 2**31-1):
        invalid("Invalid historySequence")
    if not isinstance(window["quality"], str) or window["quality"] not in QUALITY:
        invalid("Unknown quality")
    values = window["features"]
    if values is None:
        if window["quality"] == "valid":
            invalid("Valid summaries require nine measured features")
    elif (not isinstance(values, dict) or set(values) != set(FEATURES)
          or any(type(v) not in (int, float) or abs(v) > 1e12 or not math.isfinite(v) for v in values.values())):
        invalid("Provide exactly nine finite source features, not strings or imputed values")
    integrity = window["integrity"]
    if (not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}
            or integrity["algorithm"] != "sha256" or integrity["digest"] != feature_digest(values)):
        invalid("Summary feature SHA-256 mismatch")
    captured = data.parse_rfc3339("timestamp", window["timestamp"]).timestamp()
    if check_time_bounds and not time.time()-RETENTION <= captured <= time.time()+300:
        invalid("Summary timestamp outside reception bounds")
    if "rawWindow" in window:
        raw = window["rawWindow"]
        required_raw = {"sampleRateHz", "sampleCount", "axes", "encoding", "gPerCount", "samples", "integrity"}
        if not isinstance(raw, dict) or set(raw) != required_raw:
            invalid("Use the complete raw evidence envelope with its own integrity digest")
        # Evidence only. This codec does not manufacture the nine source features.
        from .periodic_snapshots import normalize_window
        from .raw_samples import PROFILE_ID as RAW_PROFILE
        raw_envelope = {k: window[k] for k in ("schemaVersion", "deviceId", "siteId", "assetId",
                       "bootId", "windowIndex", "timestamp", "startUptimeUs", "quality")}
        raw_envelope.update(raw, profileId=RAW_PROFILE, unit="count")
        normalize_window(raw_envelope, check_time_bounds=check_time_bounds)
    return captured


def prepare(window):
    normalize(window, check_time_bounds=False)
    if window["quality"] != "valid":
        raise ValueError("SUMMARY_QUALITY_INVALID")
    return {**INPUT_CONTRACT, "values": [window["features"][k] for k in FEATURES]}


def validate_delivery(info, window):
    keys = {"policyId", "mode", "intervalSec", "reason", "state", "anomalyCount", "normalCount"}
    if set(info) != keys or window.get("profileId") != PROFILE_ID:
        reject("History policy requires the source-feature summary contract", code="INVALID_PERIODIC_SNAPSHOT")
    reason, state = info["reason"], info["state"]
    if state not in ("NORMAL", "ANOMALY_ACTIVE") or not isinstance(reason, str):
        reject("Invalid history report state/reason", code="INVALID_PERIODIC_SNAPSHOT")
    rules = {"history_periodic": (state, "periodic", 25),
             "anomaly_enter": ("ANOMALY_ACTIVE", "immediate", 0),
             "anomaly_periodic": ("ANOMALY_ACTIVE", "periodic", 10),
             "normal_recovered": ("NORMAL", "immediate", 0)}
    if (reason not in rules or type(info["intervalSec"]) is not int
            or (state, info["mode"], info["intervalSec"]) != rules[reason]):
        reject("Invalid history report interval/mode", code="INVALID_PERIODIC_SNAPSHOT")
    for key in ("anomalyCount", "normalCount"):
        if type(info[key]) is not int or not 0 <= info[key] <= 2**31-1:
            reject("Invalid board counter", code="INVALID_PERIODIC_SNAPSHOT")
    a, n = info["anomalyCount"], info["normalCount"]
    if (a and n or reason == "anomaly_enter" and (a, n) != (3, 0)
            or reason == "normal_recovered" and (a, n) != (0, 5)
            or state == "NORMAL" and a >= 3 or state == "ANOMALY_ACTIVE" and n >= 5):
        reject("Inconsistent board counters", code="INVALID_PERIODIC_SNAPSHOT")
    if window.get("quality") != "valid" and (info["mode"] == "immediate" or a or n):
        reject("Invalid quality cannot confirm a board transition", code="INVALID_PERIODIC_SNAPSHOT")
    sequence = window.get("historySequence")
    if (reason == "history_periodic") != (type(sequence) is int):
        reject("Only scheduled 25-second records carry historySequence", code="INVALID_PERIODIC_SNAPSHOT")
    if reason != "history_periodic" and sequence is not None:
        reject("Event records must not advance historySequence", code="INVALID_PERIODIC_SNAPSHOT")
    if reason == "anomaly_enter" and "rawWindow" not in window:
        reject("Anomaly entry requires raw evidence alongside source features", code="INVALID_PERIODIC_SNAPSHOT")
