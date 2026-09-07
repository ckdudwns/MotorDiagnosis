"""Explicit, checkpoint-bound raw vibration adapter for shadow comparison only.

The checkpoint defines columns, NOT sampling, units or MFCC settings. An operator
must supply those separately; a configured profile is not training/field approval.
No resampling, padding of incomplete windows, axis mixing or feature imputation.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re

MAX_SAMPLES = 65536
MAX_RAW_BYTES = 512 * 1024
MAX_STFT_CELLS = 2_000_000
COMMON_KEYS = {
    "schemaVersion",
    "modelVersion",
    "deviceId",
    "siteId",
    "assetId",
    "sourceId",
    "preprocessingId",
    "sampleRateHz",
    "windowIndex",
    "windowStartSample",
    "windowEndSample",
    "timestamp",
}
RAW_KEYS = COMMON_KEYS | {
    "signalType",
    "channel",
    "unit",
    "quality",
    "samplesFloat32LE",
}
PROFILE_KEYS = {
    "schemaVersion",
    "extractor",
    "modelVersion",
    "sampleRateHz",
    "windowSamples",
    "frameLength",
    "hopLength",
    "nMfcc",
    "bandEdgesHz",
    "signalType",
    "channel",
    "unit",
}
BANDS = [0, 500, 1000, 2000, 4000, 8000]
NAMES = tuple(
    sorted(
        [f"band_{lo}_{hi}Hz" for lo, hi in zip(BANDS, BANDS[1:])]
        + [f"mfcc_{i}" for i in range(1, 14)]
        + [
            "rms_mean",
            "rms_std",
            "zcr_mean",
            "kurtosis_mean",
            "kurtosis_std",
            "spectral_centroid",
            "spectral_bandwidth",
            "spectral_rolloff",
        ]
    )
)


class RawInputError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise RawInputError(code, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_json(path, limit):
    from motor_diagnosis.json_validation import loads_strict_json

    with Path(path).open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError("JSON file exceeds size limit")
    return loads_strict_json(content.decode("utf-8"))


class RawPreprocessor:
    def __init__(self, checkpoint, profile):
        if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
            raise ValueError("All raw preprocessing profile fields must be explicit")
        if (
            type(profile["schemaVersion"]) is not int
            or profile["schemaVersion"] != 1
            or profile["extractor"] != "ai1-week2-26-v1"
            or profile["modelVersion"] != checkpoint.checksum
            or set(checkpoint.names) != set(NAMES)
            or len(checkpoint.names) != len(NAMES)
            or profile["signalType"] != "vibration"
        ):
            raise ValueError("Profile does not match the checkpoint/extractor contract")
        for key, low, high in (
            ("sampleRateHz", 8001, 192000),
            ("windowSamples", 64, MAX_SAMPLES),
            ("frameLength", 64, 8192),
            ("hopLength", 1, 8192),
            ("nMfcc", 13, 13),
        ):
            if type(profile[key]) is not int or not low <= profile[key] <= high:
                raise ValueError("Invalid raw profile " + key)
        frame = profile["frameLength"]
        if (
            frame & (frame - 1)
            or profile["windowSamples"] < frame
            or profile["hopLength"] > frame
            or not isinstance(profile["bandEdgesHz"], list)
            or any(type(v) is not int for v in profile["bandEdgesHz"])
            or profile["bandEdgesHz"] != BANDS
        ):
            raise ValueError("Invalid frame/window/band configuration")
        if (frame // 2 + 1) * (
            1 + profile["windowSamples"] // profile["hopLength"]
        ) > MAX_STFT_CELLS:
            raise ValueError("Raw profile exceeds the bounded feature-work budget")
        # Every requested band must have measured FFT bins, not empty-band zeros.
        rate, count = profile["sampleRateHz"], profile["windowSamples"]
        for low, high in zip(BANDS, BANDS[1:]):
            if not any(low <= i * rate / count < high for i in range(count // 2 + 1)):
                raise ValueError("A requested frequency band has no measured bins")
        for key in ("channel", "unit"):
            if not isinstance(profile[key], str) or not re.fullmatch(
                r"[A-Za-z0-9_.:/^-]{1,64}", profile[key]
            ):
                raise ValueError("Invalid raw profile " + key)

        import librosa
        import numpy as np
        import scipy
        from ai.ai1.week2.ai1.feature_extraction import extract_features as extraction

        if not extraction._HAS_LIBROSA:
            raise ImportError(
                "Raw model input requires librosa; MFCC zero-fill is forbidden"
            )
        mel = librosa.filters.mel(sr=rate, n_fft=frame)
        if not np.any(mel > 0, axis=1).all():
            raise ValueError("MFCC configuration contains empty mel filters")
        self.checkpoint = checkpoint
        self._profile_json = canonical(profile)
        self._extract = extraction.extract_all_features
        self._config = extraction.FeatureConfig(
            sample_rate=rate,
            frame_length=frame,
            hop_length=profile["hopLength"],
            n_mfcc=profile["nMfcc"],
            band_edges=tuple(BANDS),
        )
        self._versions = {
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "librosa": librosa.__version__,
        }
        self._code = {
            "extractor": hashlib.sha256(
                Path(extraction.__file__).read_bytes()
            ).hexdigest(),
            "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        self.identity = (
            "sha256:"
            + hashlib.sha256(
                canonical(
                    {
                        "profile": profile,
                        "dependencies": self._versions,
                        "code": self._code,
                    }
                ).encode()
            ).hexdigest()
        )
        # Warm lazy MFCC imports once, before the HTTP service accepts raw jobs.
        self._check_features(self._extract(np.zeros(count), self._config))

    @classmethod
    def load(cls, checkpoint, path):
        return cls(checkpoint, read_json(path, 16384))

    @property
    def profile(self):
        return json.loads(self._profile_json)

    def describe(self):
        return {
            "preprocessingId": self.identity,
            "profile": self.profile,
            "dependencies": dict(self._versions),
            "codeSha256": dict(self._code),
            "trainingCompatibility": "unverified",
            "domainValidated": False,
            "mode": "shadow",
            "affectsAlerts": False,
        }

    def decode(self, payload):
        """Bounded validation/decoding only; no expensive feature work at ingest."""
        import numpy as np

        if not isinstance(payload, dict) or set(payload) != RAW_KEYS:
            fail(
                "INVALID_RAW_INPUT", "An explicit single-channel raw window is required"
            )
        for key in ("deviceId", "siteId", "assetId", "sourceId"):
            value = payload[key]
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or any(ord(c) < 32 for c in value)
            ):
                fail("INVALID_RAW_INPUT", "Invalid " + key)
        if (
            type(payload["windowIndex"]) is not int
            or not 0 <= payload["windowIndex"] <= 2**31 - 1
        ):
            fail("INVALID_RAW_INPUT", "Invalid window index")
        from motor_diagnosis import data

        stamp = payload["timestamp"]
        if not isinstance(stamp, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            stamp,
        ):
            fail("INVALID_RAW_INPUT", "A precise RFC3339 capture timestamp is required")
        try:
            data.parse_rfc3339("timestamp", stamp)
        except data.ApiError as exc:
            fail("INVALID_RAW_INPUT", exc.message)
        profile = self.profile
        if payload["preprocessingId"] != self.identity:
            fail(
                "PREPROCESSOR_CHANGED",
                "Raw input must pin the configured preprocessing ID",
            )
        if payload["modelVersion"] != self.checkpoint.checksum:
            fail("MODEL_CHANGED", "Raw input must pin the selected checkpoint")
        for key in ("signalType", "channel", "unit", "sampleRateHz"):
            if (
                type(payload[key]) is not type(profile[key])
                or payload[key] != profile[key]
            ):
                fail("RAW_PROFILE_MISMATCH", "Raw input differs from configured " + key)
        if payload["quality"] != "valid":
            fail(
                "RAW_SIGNAL_INVALID",
                "Invalid/unknown sensor quality is not an inference input",
            )
        if (
            type(payload["schemaVersion"]) is not int
            or payload["schemaVersion"] != 1
            or type(payload["windowStartSample"]) is not int
            or type(payload["windowEndSample"]) is not int
            or not 0
            <= payload["windowStartSample"]
            < payload["windowEndSample"]
            <= 2**53 - 1
            or payload["windowEndSample"] - payload["windowStartSample"]
            != profile["windowSamples"]
        ):
            fail(
                "INVALID_RAW_INPUT",
                "Raw sample boundaries must match one complete window",
            )
        text = payload["samplesFloat32LE"]
        expected = profile["windowSamples"] * 4
        if not isinstance(text, str) or len(text) != 4 * ((expected + 2) // 3):
            fail("INVALID_RAW_INPUT", "Raw payload length does not match the profile")
        try:
            content = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            fail("INVALID_RAW_INPUT", "Invalid float32 base64 encoding")
        if len(content) != expected or base64.b64encode(content).decode() != text:
            fail("INVALID_RAW_INPUT", "Non-canonical or incorrect raw encoding")
        signal = np.frombuffer(content, dtype="<f4").astype(np.float64)
        if not np.isfinite(signal).all() or np.max(np.abs(signal)) > 1e12:
            fail(
                "RAW_SIGNAL_INVALID",
                "Raw samples must be finite and numerically bounded",
            )
        return signal

    def _check_features(self, features):
        import math

        if set(features) != set(self.checkpoint.names) or any(
            not math.isfinite(v) for v in features.values()
        ):
            fail(
                "PREPROCESSING_FAILED", "Extracted features do not match the checkpoint"
            )
        return {name: float(features[name]) for name in self.checkpoint.names}

    def transform(self, payload):
        signal = self.decode(payload)
        features = self._check_features(self._extract(signal, self._config))
        return {**{key: payload[key] for key in COMMON_KEYS}, "features": features}

    def capture_window(self, envelope, *, quality):
        """Validate an existing download, never invent continuity between captures.

        A PR29 vibration capture is 800 Hz: a valid envelope still fails this
        adapter's profile check. Acoustic PCM is never relabeled as vibration.
        """
        from motor_diagnosis.edge_analysis import normalize_frame

        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("frame"), dict
        ):
            fail("INVALID_RAW_INPUT", "Expected a downloaded analysis capture")
        digest = hashlib.sha256(canonical(envelope["frame"]).encode()).hexdigest()
        if envelope.get("sha256") != digest:
            fail("CAPTURE_CHECKSUM_MISMATCH", "Capture checksum mismatch")
        frame, _, raw = normalize_frame(envelope["frame"], enforce_retention=False)
        name = self.profile["channel"]
        if not raw or name not in {"vibrationX", "vibrationY", "vibrationZ"}:
            fail("RAW_PROFILE_MISMATCH", "A raw vibration axis is required")
        channel = frame["channels"][name]
        payload = {
            "schemaVersion": 1,
            "modelVersion": self.checkpoint.checksum,
            "preprocessingId": self.identity,
            **{
                key: frame[key]
                for key in ("deviceId", "siteId", "assetId", "timestamp")
            },
            "sourceId": "capture:" + digest + ":" + name,
            "signalType": "vibration",
            "channel": name,
            "unit": channel["unit"],
            "sampleRateHz": channel["sampleRateHz"],
            "windowIndex": 0,
            "windowStartSample": 0,
            "windowEndSample": channel["sampleCount"],
            "quality": quality,
            "samplesFloat32LE": channel["samplesFloat32LE"],
        }
        self.decode(payload)
        return payload


def main():
    from ai.ai2.model_runtime import Checkpoint, contiguous_matrix
    from motor_diagnosis import data

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--candidate", default="lstm_autoencoder")
    parser.add_argument("--expected-checksum")
    parser.add_argument("--profile", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--input", help="JSON object containing raw windows in sequence order"
    )
    action.add_argument(
        "--capture",
        help="Validate/transform one existing downloaded capture; never stitch separate requests",
    )
    parser.add_argument(
        "--quality",
        choices=["valid", "invalid", "unknown"],
        default="unknown",
        help="Capture sensor quality declaration, not inferred from sample values",
    )
    parser.add_argument("--output", help="New output JSON; never overwrite")
    args = parser.parse_args()
    model = Checkpoint.load(
        args.artifact,
        candidate=args.candidate,
        expected_checksum=args.expected_checksum,
    )
    adapter = RawPreprocessor.load(model, args.profile)
    descriptor = model.describe()
    descriptor["rawAdapterAvailable"] = True
    descriptor["limitation"] = (
        "Raw conversion uses explicit unverified settings; the checkpoint contains no raw acquisition contract."
    )
    result = {"preprocessing": adapter.describe(), "checkpoint": descriptor}
    if args.capture:
        capture = read_json(args.capture, 1024 * 1024)
        window = adapter.transform(
            adapter.capture_window(capture, quality=args.quality)
        )
        result.update(
            windows=[window],
            status="warming_up" if model.sequence_length > 1 else "prepared",
        )
    elif args.input:
        document = read_json(args.input, MAX_RAW_BYTES * model.sequence_length)
        if not isinstance(document, dict) or set(document) != {"windows"}:
            raise ValueError("Expected windows object")
        raw = document["windows"]
        if not isinstance(raw, list) or len(raw) != model.sequence_length:
            raise ValueError("Provide exactly one complete model sequence")
        windows = [adapter.transform(item) for item in raw]
        matrix = contiguous_matrix(windows, model)
        times = [
            data.parse_rfc3339("timestamp", item["timestamp"]).timestamp()
            for item in windows
        ]
        period = adapter.profile["windowSamples"] / adapter.profile["sampleRateHz"]
        tolerance = max(0.002, 2 / adapter.profile["sampleRateHz"])
        if any(abs(t - times[0] - i * period) > tolerance for i, t in enumerate(times)):
            raise ValueError("Raw capture timestamps are not contiguous")
        result.update(windows=windows, result=model.predict(matrix, list(model.names)))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
