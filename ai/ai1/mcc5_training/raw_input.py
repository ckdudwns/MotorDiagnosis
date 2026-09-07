"""Explicit per-window input adapter; not an HTTP protocol or live model loader.

Consumes already sampled 800Hz XYZ; NEVER resamples field input or fills features.
"""

import numbers

import numpy as np

from .features import RATE, WINDOW, NAMES, extract

BASE_PROFILE = "mcc5-vibration-800hz-xyz-v1"
SPECTRAL_PROFILE = "mcc5-vibration-800hz-spectral66-v1"
COUNT_SCALE_G = 0.0039
NUMERIC_POLICY = "base21 cast float32 then promoted float64; extra45 float64"


class InputUnavailable(ValueError):
    """Measurement quality prevents inference; NEVER a normal verdict."""


def matrix(values, integer=False):
    original = np.asarray(values, dtype=object)
    if original.shape != (WINDOW, 3):
        raise ValueError("Require exactly [512,3] same-window XYZ")
    kind = numbers.Integral if integer else numbers.Real
    if any(
        isinstance(v, (bool, np.bool_)) or not isinstance(v, kind)
        for v in original.flat
    ):
        raise ValueError(
            "Invalid raw sample type; no booleans/strings or implicit coercion"
        )
    raw = np.asarray(values, dtype=np.float64)
    if not np.isfinite(raw).all():
        raise ValueError("Nonfinite raw samples")
    return raw


def counts_to_g(values):
    """Match current C++ conservative count rails before conversion or DC removal."""
    raw = matrix(values, integer=True)
    if np.any(raw <= -4096) or np.any(raw >= 4095):
        raise InputUnavailable("clipped")
    return raw * COUNT_SCALE_G


def quantize_public_window(values):
    """Resolution-only stress simulation: nearest-even counts, no rail clamping.

    Not the measured ADC transfer, noise, aliasing, timing or mounting response.
    """
    raw = matrix(values)
    counts = np.rint(raw / COUNT_SCALE_G)
    if np.any(counts <= -4096) or np.any(counts >= 4095):
        raise InputUnavailable("clipped")
    return counts_to_g(counts.astype(np.int16))


def model_input(
    values, *, profile_id, sample_rate_hz, sample_count, axes, unit, quality
):
    if profile_id not in (BASE_PROFILE, SPECTRAL_PROFILE):
        raise ValueError("Unsupported model input profile; never pad or relabel")
    if (
        type(sample_rate_hz) is not int
        or sample_rate_hz != RATE
        or axes != ["X", "Y", "Z"]
        or unit != "g"
    ):
        raise ValueError("Require exact 800Hz XYZ g metadata")
    if quality not in (
        "valid",
        "fifo_overrun",
        "sensor_unavailable",
        "sample_gap",
        "clipped",
        "constant_axis",
        "processing_overflow",
    ):
        raise ValueError("Unknown quality state")
    if quality != "valid":
        raise InputUnavailable(quality)
    if type(sample_count) is not int or sample_count != WINDOW:
        raise InputUnavailable("sample_gap")
    raw = matrix(values)
    try:
        if profile_id == BASE_PROFILE:
            features, names = extract(raw), NAMES
        else:
            # Deferred import keeps the base21 path independent of research loaders.
            from .spectral import extract66, CONTRACT

            features, names = extract66(raw), CONTRACT["featureNames"]
    except ValueError as error:
        raise InputUnavailable(str(error)) from error
    features[:21] = features[:21].astype(np.float32).astype(np.float64)
    if not np.isfinite(features).all():
        raise InputUnavailable("nonfinite_model_features")
    return {
        "profileId": profile_id,
        "featureNames": list(names),
        "values": features,
        "inputShape": [1, len(features)],
        "dtype": "float64",
        "numericPolicy": NUMERIC_POLICY,
        "normalization": "none in adapter; artifact-specific only",
        "sequenceLength": 1,
        "fieldValidated": False,
        "affectsAlerts": False,
    }


def spectral_input(values):
    return model_input(
        values,
        profile_id=SPECTRAL_PROFILE,
        sample_rate_hz=800,
        sample_count=512,
        axes=["X", "Y", "Z"],
        unit="g",
        quality="valid",
    )["values"]
