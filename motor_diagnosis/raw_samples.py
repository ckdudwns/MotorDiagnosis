"""Lossless ADXL345 wire codec; no inference, FFT or model imports."""
import base64
import binascii
import json
import struct

from .window_envelope import COMMON_KEYS, reject, validate_envelope

PROFILE_ID = "adxl345-800hz-xyz-counts-v1"
ENCODING = "base64-int16le-xyz"
G_PER_COUNT = 0.0039
RAW_KEYS = COMMON_KEYS | {"encoding", "gPerCount", "samples"}


def decode_samples(payload):
    value = payload["samples"]
    if value is None and payload["quality"] != "valid":
        return None
    if not isinstance(value, str) or len(value) != 4 * ((payload["sampleCount"] * 6 + 2) // 3):
        reject("Raw sample length must match sampleCount × XYZ × int16")
    try:
        body = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        reject("Invalid raw base64")
    if len(body) != payload["sampleCount"] * 6 or base64.b64encode(body).decode() != value:
        reject("Use canonical base64 with exact interleaved XYZ bytes")
    rows = list(struct.iter_unpack("<hhh", body))
    if any(v < -4096 or v > 4095 for row in rows for v in row):
        reject("Counts outside full-resolution ADXL345 range")
    return rows


def normalize(payload, *, check_time_bounds=True):
    captured = validate_envelope(payload, RAW_KEYS, PROFILE_ID, "count", check_time_bounds=check_time_bounds)
    if (payload["encoding"] != ENCODING or type(payload["gPerCount"]) not in (int, float)
            or payload["gPerCount"] != G_PER_COUNT):
        reject("Raw encoding and fixed count-to-g conversion must match profile")
    if payload["quality"] == "valid" and payload["sampleCount"] != 512:
        reject("Valid raw windows require 512 actual XYZ samples")
    decode_samples(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), captured
