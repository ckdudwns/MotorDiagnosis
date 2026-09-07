"""ADXL345 lossless count transport and model-independent spectral66 preparation.

No research-package imports, pickle loaders, optional ML dependencies or model
activation. All samples and units remain available for later reproducibility.
"""

import base64
import binascii
import cmath
import json
import math
import struct

from .vibration_windows import KEYS, VibrationWindowStore, reject, validate_envelope
from .window_features import FEATURE_NAMES

PROFILE_ID = "adxl345-800hz-xyz-counts-v1"
FEATURE_PROFILE = "mcc5-vibration-800hz-spectral66-v1"
ENCODING = "base64-int16le-xyz"
G_PER_COUNT = 0.0039
RAW_KEYS = (KEYS - {"features"}) | {"encoding", "gPerCount", "samples"}
FINE_BANDS = ((0, 25), (25, 50), (50, 75), (75, 100),
              (100, 150), (150, 200), (200, 275), (275, 350))
EXTRA_NAMES = [
    f"vibration{axis}.{name}" for axis in "XYZ"
    for name in ([f"ratio_{lo}_{hi}" for lo, hi in FINE_BANDS] +
                 ["entropy_normalized", "centroid_hz", "bandwidth_hz", "peak_hz",
                  "peak_power_fraction", "rolloff85_hz"])
] + ["correlation.XY", "correlation.XZ", "correlation.YZ"]
NAMES = list(FEATURE_NAMES) + EXTRA_NAMES


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


def normalize(payload):
    captured = validate_envelope(payload, RAW_KEYS, PROFILE_ID, "count")
    if (payload["encoding"] != ENCODING or type(payload["gPerCount"]) not in (int, float)
            or payload["gPerCount"] != G_PER_COUNT):
        reject("Raw encoding and fixed count-to-g conversion must match profile")
    if payload["quality"] == "valid" and payload["sampleCount"] != 512:
        reject("Valid raw windows require 512 actual XYZ samples")
    decode_samples(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), captured


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

    def __init__(self, database=":memory:"):
        super().__init__(database)
        self.variant = "spectral66"
        self.db.execute("CREATE TABLE IF NOT EXISTS raw_clock_anchors(device TEXT, boot TEXT, uptime INTEGER, captured REAL, PRIMARY KEY(device,boot))")
        self.db.commit()

    def input_names(self):
        return list(NAMES)

    def input_values(self, window):
        return extract66(decode_samples(window))

    def validate_stream_clock(self, window, captured):
        key = (window["deviceId"], window["bootId"])
        anchor = self.db.execute("SELECT uptime,captured FROM raw_clock_anchors WHERE device=? AND boot=?", key).fetchone()
        if anchor is None:
            self.db.execute("INSERT INTO raw_clock_anchors VALUES(?,?,?,?)", (*key, window["startUptimeUs"], captured))
        elif abs((captured - anchor[1]) * 1e6 - (window["startUptimeUs"] - anchor[0])) > 1000000:
            reject("Measurement UTC and uptime elapsed disagree", 409, "TIMESTAMP_UPTIME_MISMATCH")

    def list_device(self, user, device_id):
        result = super().list_device(user, device_id)
        result["featureProfileId"] = FEATURE_PROFILE
        result["numericPolicy"] = "base21 float32 promoted to float64; extra45 float64"
        result["maxRows"] = self.max_rows
        return result
