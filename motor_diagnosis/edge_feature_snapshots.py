"""Versioned feature-only ADXL345 contracts, without claiming source equivalence.

Legacy v1 uses seconds-of-minute 00/25/50; history v1 selects recent windows
on a uniform 25-second epoch grid, also during anomalies. Board state is
evidence, never a server-model verdict.
"""
import copy
import hashlib
import math
import re
import struct
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
RAW_CF_HISTORY_PROFILE_ID = "adxl345-raw-cf-centered-sk-ku-25s-v2"
RAW_CF_ADAPTER_ID = "adxl345-raw-cf-centered-moments-v2"
RAW_CF_HISTORY_CONTRACT_ID = "edge-feature-history-model-v2"
HISTORY_PROFILE_IDS = (HISTORY_PROFILE_ID, RAW_CF_HISTORY_PROFILE_ID)
PROFILE_IDS = (PROFILE_ID, *HISTORY_PROFILE_IDS)
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
# Mixed preprocessing must not claim that CF was mean-removed. These are
# declarations of board arithmetic, not transformations applied by the server.
RAW_CF_HISTORY_INPUT_CONTRACT = {
    **{k: v for k, v in HISTORY_INPUT_CONTRACT.items() if k != "meanRemoved"},
    "adapterId": RAW_CF_ADAPTER_ID, "sourceProfileId": RAW_CF_HISTORY_PROFILE_ID,
    "featureDefinitions": {
        "cf": "max(abs(raw))/sqrt(mean(raw**2))",
        "sk": "mean(centered**3)/mean(centered**2)**1.5",
        "ku": "mean(centered**4)/mean(centered**2)**2",
        "centered": "raw-mean(raw)",
    },
}


def input_contract(profile):
    return {PROFILE_ID: INPUT_CONTRACT, HISTORY_PROFILE_ID: HISTORY_INPUT_CONTRACT,
            RAW_CF_HISTORY_PROFILE_ID: RAW_CF_HISTORY_INPUT_CONTRACT}[profile]
KEYS = {"schemaVersion", "deviceId", "siteId", "assetId", "sensorId", "bootId",
        "windowIndex", "timestamp", "startUptimeUs", "sampleRateHz", "sampleCount",
        "profileId", "axes", "unit", "quality", "reason", "features", "integrity",
        "periodicSlotEpoch"}
INVALID_REASONS = {"fifo_overrun", "insufficient_samples", "timeout", "non_finite",
                   "zero_variance", "sensor_unavailable", "no_valid_window"}
RMS_AXES = ("x", "y", "z")


def rms_digest(rms):
    """SHA-256 of measured XYZ RMS as three little-endian IEEE-754 float64s."""
    return hashlib.sha256(struct.pack("<3d", *(rms[axis] for axis in RMS_AXES))).hexdigest()


def spectrum_digest(spectrum):
    """XYZ, each containing bins 1..256 as little-endian float64 power values."""
    return hashlib.sha256(struct.pack("<768d", *(value for axis in RMS_AXES
                                                 for value in spectrum[axis]))).hexdigest()


def optional_header(group, declarations, label):
    """Known, supplied metadata must match. Missing metadata marks a partial group."""
    if not isinstance(group, dict) or set(group) - (declarations.keys() | set(RMS_AXES) | {"integrity"}):
        invalid(f"Invalid {label} object or unknown field")
    complete = True
    for key, expected in declarations.items():
        value = group.get(key)
        if value is None:
            complete = False
        elif type(value) is not type(expected) or value != expected:
            invalid(f"Unexpected {label} {key}")
    return complete


def optional_integrity(group, axes_ready, digest_fn, label):
    integrity = group.get("integrity")
    if integrity is None:
        return False
    if not isinstance(integrity, dict) or set(integrity) - {"algorithm", "digest"}:
        invalid(f"Invalid {label} integrity object")
    algorithm, digest = integrity.get("algorithm"), integrity.get("digest")
    if algorithm is not None and algorithm != "sha256":
        invalid(f"Unsupported {label} digest algorithm")
    if digest is not None:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            invalid(f"Invalid {label} SHA-256")
        if axes_ready and digest != digest_fn(group):
            invalid(f"{label} SHA-256 mismatch")
    return axes_ready and algorithm == "sha256" and digest is not None


def validate_rms(rms, quality):
    if rms is None:
        return False
    header_ready = optional_header(rms, {"definition": "ac", "unit": "g"}, "RMS")
    axes_ready = True
    for axis in RMS_AXES:
        value = rms.get(axis)
        if value is None:
            axes_ready = False
        elif type(value) not in (int, float) or not 0 <= value <= 1e12:
            invalid("RMS requires finite nonnegative acceleration values")
    ready = optional_integrity(rms, axes_ready, rms_digest, "RMS") and header_ready
    if ready and quality != "valid":
        invalid("A complete RMS measurement requires valid window quality")
    return ready


def validate_spectrum(spectrum, verified_rms, quality):
    if spectrum is None:
        return False
    declarations = {"method": "fft-hann-power-v1", "unit": "g2", "binStart": 1, "binWidthHz": 1.5625}
    header_ready = optional_header(spectrum, declarations, "spectrum")
    axes_ready = True
    for axis in RMS_AXES:
        values = spectrum.get(axis)
        if values is None:
            axes_ready = False
            continue
        if not isinstance(values, list) or len(values) > 256:
            invalid("Each spectrum axis must contain at most 256 bins")
        if any(value is not None and (type(value) not in (int, float) or not 0 <= value <= 1e24)
               for value in values):
            invalid("Spectrum power must be finite and nonnegative")
        if len(values) != 256 or None in values:
            axes_ready = False
    ready = optional_integrity(spectrum, axes_ready, spectrum_digest, "Spectrum") and header_ready
    if ready:
        if quality != "valid":
            invalid("A complete spectrum requires valid window quality")
        if verified_rms is not None and any(verified_rms[a] == 0 and any(spectrum[a]) for a in RMS_AXES):
            invalid("Zero RMS requires a zero-power spectrum on the same axis")
    return ready


def measured_vibration(window):
    """Derive two display values from this stored window, independently of AI jobs."""
    result = {"vibrationRmsG": None, "dominantFrequencyHz": None,
              "rmsStatus": "unavailable", "frequencyStatus": "unavailable",
              "rmsDefinition": "vector-ac", "frequencyMethod": "fft-hann-power-sum-peak-v1"}
    if window.get("profileId") not in PROFILE_IDS or window.get("quality") != "valid":
        return result
    rms, spectrum = window.get("rms"), window.get("spectrum")
    try:
        rms_ready = validate_rms(rms, window["quality"])
    except data.ApiError:
        rms_ready = False
    if rms_ready:
        result.update(vibrationRmsG=math.hypot(*(rms[axis] for axis in RMS_AXES)), rmsStatus="available")
    try:
        spectrum_ready = validate_spectrum(spectrum, rms if rms_ready else None, window["quality"])
    except data.ApiError:
        spectrum_ready = False
    if not spectrum_ready:
        return result
    powers = [math.fsum(spectrum[axis][index] for axis in RMS_AXES) for index in range(256)]
    peak = max(range(256), key=powers.__getitem__)  # First/lower bin wins ties.
    if powers[peak] > 0:
        result.update(dominantFrequencyHz=(peak + 1) * 1.5625, frequencyStatus="available")
    else:
        result["frequencyStatus"] = "no_peak"
    return result


def invalid(message):
    reject(message, code="INVALID_EDGE_FEATURE_SNAPSHOT")


def normalize(window, *, check_time_bounds=True, allow_expired=False):
    history = isinstance(window, dict) and window.get("profileId") in HISTORY_PROFILE_IDS
    keys = KEYS | {"historySequence"} if history else KEYS
    if not isinstance(window, dict) or set(window) - {"rms", "spectrum"} != keys:
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
    rms = window.get("rms")
    rms_ready = validate_rms(rms, quality)
    validate_spectrum(window.get("spectrum"), rms if rms_ready else None, quality)
    integrity = window["integrity"]
    if (not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}
            or integrity["algorithm"] != "sha256" or integrity["digest"] != feature_digest(values)):
        invalid("Feature SHA-256 mismatch")
    captured = data.parse_rfc3339("timestamp", window["timestamp"]).timestamp()
    if check_time_bounds:
        now = time.time()
        if captured > now + 300 or (not allow_expired and captured < now - RETENTION):
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
    history = window.get("profileId") in HISTORY_PROFILE_IDS
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
