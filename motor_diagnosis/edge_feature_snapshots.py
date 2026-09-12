"""Versioned feature-only ADXL345 contracts, without claiming source equivalence.

Legacy v1 uses seconds-of-minute 00/25/50; history v1 selects recent windows
on a uniform 25-second epoch grid, also during anomalies. Board state is
evidence, never a server-model verdict.
"""
import copy
import math
import re
import time

from . import data
from .pump_summary import FEATURES, feature_digest
from .window_envelope import RETENTION, reject

PROFILE_ID = "adxl345-ac-cf-sk-ku-v1"
POLICY_ID = "edge-feature-snapshot-v1"
ADAPTER_ID = "adxl345-ac-nine-moments-v1"
CONTRACT_ID = "edge-feature-event-v1"
HISTORY_PROFILE_ID = "adxl345-ac-cf-sk-ku-25s-v1"
HISTORY_POLICY_ID = "edge-feature-history-v1"
HISTORY_CONTRACT_ID = "edge-feature-history-model-v1"
PROFILE_IDS = (PROFILE_ID, HISTORY_PROFILE_ID)
INPUT_CONTRACT = {
    "adapterId": ADAPTER_ID, "sourceProfileId": PROFILE_ID,
    "shape": [9], "features": list(FEATURES), "unit": "dimensionless",
    "axes": ["X", "Y", "Z"], "sampleRateHz": 800, "sampleCount": 512,
    "sourceUnit": "g", "meanRemoved": True, "momentConvention": "population-pearson",
}
HISTORY_INPUT_CONTRACT = {
    **INPUT_CONTRACT, "sourceProfileId": HISTORY_PROFILE_ID,
    "historyIntervalSec": 25, "historyClock": "unix_epoch_25s",
    "maxWindowEndAgeSec": 1,
}


def input_contract(profile):
    return HISTORY_INPUT_CONTRACT if profile == HISTORY_PROFILE_ID else INPUT_CONTRACT
KEYS = {"schemaVersion", "deviceId", "siteId", "assetId", "sensorId", "bootId",
        "windowIndex", "timestamp", "startUptimeUs", "sampleRateHz", "sampleCount",
        "profileId", "axes", "unit", "quality", "reason", "features", "integrity",
        "periodicSlotEpoch"}
INVALID_REASONS = {"fifo_overrun", "insufficient_samples", "timeout", "non_finite",
                   "zero_variance", "sensor_unavailable", "no_valid_window"}


def invalid(message):
    reject(message, code="INVALID_EDGE_FEATURE_SNAPSHOT")


def normalize(window, *, check_time_bounds=True):
    history = isinstance(window, dict) and window.get("profileId") == HISTORY_PROFILE_ID
    keys = KEYS | {"historySequence"} if history else KEYS
    if not isinstance(window, dict) or set(window) != keys:
        invalid("Use the exact feature-only envelope; rawWindow/samples/encoding are not accepted")
    if (type(window["schemaVersion"]) is not int or window["schemaVersion"] != (3 if history else 2)
            or window["profileId"] not in PROFILE_IDS):
        invalid("Unsupported feature schema/profile")
    for key in ("deviceId", "siteId", "assetId", "sensorId"):
        if not isinstance(window[key], str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,99}", window[key]):
            invalid("Invalid " + key)
    if not isinstance(window["bootId"], str) or not re.fullmatch(r"[0-9a-f]{32}", window["bootId"]):
        invalid("Invalid bootId")
    for key, maximum in (("windowIndex", 2**31-1), ("startUptimeUs", 2**53-1), ("sampleCount", 512)):
        if type(window[key]) is not int or not 0 <= window[key] <= maximum:
            invalid("Invalid " + key)
    if (type(window["sampleRateHz"]) is not int or window["sampleRateHz"] != 800
            or window["axes"] != ["X", "Y", "Z"] or window["unit"] != "dimensionless"):
        invalid("Expected 800Hz, XYZ and dimensionless features computed from g")
    quality, values, reason = window["quality"], window["features"], window["reason"]
    if quality == "valid":
        if window["sampleCount"] != 512 or reason is not None:
            invalid("Valid features require 512 samples and null reason")
        if (not isinstance(values, dict) or set(values) != set(FEATURES)
                or any(type(v) not in (int, float) or abs(v) > 1e12 or not math.isfinite(v) for v in values.values())):
            invalid("Expected nine finite measured features")
    elif quality == "invalid":
        if values is not None or not isinstance(reason, str) or reason not in INVALID_REASONS:
            invalid("Invalid reports require features=null and an explicit quality reason")
    else:
        invalid("quality must be valid or invalid")
    integrity = window["integrity"]
    if (not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}
            or integrity["algorithm"] != "sha256" or integrity["digest"] != feature_digest(values)):
        invalid("Feature SHA-256 mismatch")
    captured = data.parse_rfc3339("timestamp", window["timestamp"]).timestamp()
    if check_time_bounds and not time.time()-RETENTION <= captured <= time.time()+300:
        invalid("Measurement timestamp outside reception bounds")
    slot = window["periodicSlotEpoch"]
    if slot is not None:
        # An integer UTC boundary has one identity regardless of RFC3339 offset.
        if (type(slot) is not int or not 0 <= slot <= 2**53-1
                or (slot % 25 != 0 if history else slot % 60 not in (0, 25, 50))):
            invalid("Invalid periodicSlotEpoch for this versioned schedule")
        width = 1.640 if history else 10 if slot % 60 == 0 else 25
        if quality == "valid":
            if captured < slot-width or captured + .640 > slot + .000001:
                invalid("Selected window must start and finish inside the preceding UTC slot")
        elif abs(captured-slot) > .000001:
            invalid("An empty/invalid slot uses its closing boundary as timestamp")
    if history:
        seq = window["historySequence"]
        if (slot is None and seq is not None or slot is not None
                and (type(seq) is not int or not 0 <= seq <= 2**31-1)):
            invalid("Only scheduled 25-second records carry an integer historySequence")
    return captured


def validate_delivery(info, window):
    history = window.get("profileId") == HISTORY_PROFILE_ID
    keys = {"policyId", "eventType", "state", "anomalyCount", "normalCount"}
    if (set(info) != keys or window.get("profileId") not in PROFILE_IDS
            or info["policyId"] != (HISTORY_POLICY_ID if history else POLICY_ID)):
        invalid("Feature policy requires the feature-only envelope and delivery fields")
    event, state = info["eventType"], info["state"]
    states = {"periodic": info.get("state") if history else "NORMAL", "anomaly_start": "ANOMALY_ACTIVE",
              "anomaly_active": "ANOMALY_ACTIVE", "recovery": "NORMAL"}
    if (not isinstance(event, str) or event not in states or state != states[event]
            or state not in ("NORMAL", "ANOMALY_ACTIVE")):
        invalid("Invalid eventType/state for this versioned policy")
    for key in ("anomalyCount", "normalCount"):
        if type(info[key]) is not int or not 0 <= info[key] <= 2**31-1:
            invalid("Invalid consecutive board counter")
    a, n = info["anomalyCount"], info["normalCount"]
    if (a and n or event == "anomaly_start" and (a, n) != (3, 0)
            or event == "recovery" and (a, n) != (0, 5)
            or state == "NORMAL" and a >= 3 or state == "ANOMALY_ACTIVE" and n >= 5):
        invalid("Counters do not match the reported state/transition")
    if window.get("quality") != "valid" and (event in ("anomaly_start", "recovery") or a or n):
        invalid("Invalid windows reset both counters and cannot confirm a transition")
    slot = window.get("periodicSlotEpoch")
    if event == "periodic" and slot is None:
        invalid("Periodic reports require their UTC slot identity")
    if not history and event == "anomaly_active" and slot is not None:
        invalid("Active reports use uptime, not the periodic schedule")


def prepare(window):
    normalize(window, check_time_bounds=False)
    if window["quality"] != "valid":
        raise ValueError(window["reason"])
    return {**copy.deepcopy(input_contract(window["profileId"])), "values": [window["features"][k] for k in FEATURES]}


def history_policy_metadata():
    return {**policy_metadata(), "policyId": HISTORY_POLICY_ID,
            "normalSchedule": "unix_epoch_25s", "normalUtcSeconds": None,
            "normalIntervalsSec": [25], "historyIntervalSec": 25,
            "periodicDuringAnomaly": True, "maxWindowEndAgeSec": 1,
            "overlapPriority": "transition_with_history_slot",
            "immediateReportsAdvanceHistory": False}


def policy_metadata():
    return {"policyId": POLICY_ID, "normalSchedule": "utc_seconds_of_minute",
            "normalUtcSeconds": [0, 25, 50], "normalIntervalsSec": [25, 25, 10],
            "maxNormalIntervalSec": 25, "anomalyIntervalSec": 10,
            "anomalyClock": "monotonic_uptime", "periodicDuringAnomaly": False,
            "eventTypes": ["periodic", "anomaly_start", "anomaly_active", "recovery"],
            "enterConsecutiveWindows": 3, "recoveryConsecutiveWindows": 5,
            "invalidResetsCounters": True, "overlapPriority": "anomaly_start",
            "rawAccepted": False, "stateSource": "device_report", "serverVerified": False}
