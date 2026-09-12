"""Measurement identity and wire validation, independent of any model/store."""
import re
import time

from . import data

RETENTION = 2 * 86400
COMMON_KEYS = {"schemaVersion", "deviceId", "siteId", "assetId", "bootId",
               "windowIndex", "timestamp", "startUptimeUs", "sampleRateHz",
               "sampleCount", "profileId", "axes", "unit", "quality"}
QUALITY = {"valid", "fifo_overrun", "sensor_unavailable", "sample_gap",
           "clipped", "constant_axis", "processing_overflow"}


def reject(message, status=400, code="INVALID_VIBRATION_WINDOW"):
    raise data.ApiError(status, code, message)


def validate_envelope(payload, keys, profile, unit, *, check_time_bounds=True):
    if not isinstance(payload, dict) or set(payload) != keys:
        reject("Use the exact versioned vibration-window envelope")
    for key in ("deviceId", "siteId", "assetId"):
        if payload[key] != data.required_text(payload, key).upper():
            reject("Invalid " + key)
    if not isinstance(payload["bootId"], str) or not re.fullmatch(r"[0-9a-f]{32}", payload["bootId"]):
        reject("bootId must be 32 lowercase hexadecimal characters")
    for key, low, high in (("windowIndex", 0, 2**31 - 1),
                           ("startUptimeUs", 0, 2**53 - 1), ("sampleCount", 0, 512)):
        if type(payload[key]) is not int or not low <= payload[key] <= high:
            reject("Invalid " + key)
    if (type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1
            or type(payload["sampleRateHz"]) is not int or payload["sampleRateHz"] != 800
            or payload["profileId"] != profile or payload["unit"] != unit
            or payload["axes"] != ["X", "Y", "Z"]):
        reject("The 800 Hz XYZ measurement profile must match exactly")
    if not isinstance(payload["quality"], str) or payload["quality"] not in QUALITY:
        reject("Unknown quality state")
    captured = data.parse_rfc3339("timestamp", payload["timestamp"]).timestamp()
    # The age limit applies to NEW reception, not replaying an accepted inbox.
    if check_time_bounds and not time.time() - RETENTION <= captured <= time.time() + 300:
        reject("Window timestamp outside retention/future bounds")
    return captured
