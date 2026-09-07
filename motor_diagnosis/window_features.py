"""Frozen base21 transport contract; independent of optional training packages.

Matches the current MCC5 800 Hz extractor, not the old CWRU 26-feature model.
Changing the extractor requires a new profile ID and new parity fixtures.
"""

import math

PROFILE_ID = "mcc5-vibration-800hz-xyz-v1"
FEATURE_NAMES = tuple(
    f"vibration{axis}.{name}"
    for axis in "XYZ"
    for name in (
        "rms_ac_g", "peak_ac_g", "kurtosis_pearson", "power_0_50_g2",
        "power_50_100_g2", "power_100_200_g2", "power_200_350_g2",
    )
)
VARIANTS = ("base21", "ratios36", "log_ratios36")


def feature_names(variant):
    if variant not in VARIANTS:
        raise ValueError("Unknown window feature variant")
    names = list(FEATURE_NAMES)
    if variant == "log_ratios36":
        names = ["log1p:" + n if i % 7 != 2 else n for i, n in enumerate(names)]
    if variant != "base21":
        names += [
            f"vibration{axis}.{name}" for axis in "XYZ"
            for name in ("ratio_0_50", "ratio_50_100", "ratio_100_200",
                         "ratio_200_350", "crest")
        ]
    return names


def derive(values, variant="base21"):
    """No coercion, zero filling, fitted transforms or cross-window averaging."""
    feature_names(variant)
    if not isinstance(values, list) or len(values) != 21:
        raise ValueError("Expected exactly 21 ordered features")
    if any(type(v) not in (int, float) or not math.isfinite(v) or
           not 0 <= v <= 1e12 for v in values):
        raise ValueError("Features must be finite nonnegative numbers")
    base, extra = list(values), []
    for offset in (0, 7, 14):
        rms, peak, kurtosis = values[offset:offset + 3]
        if rms <= 1e-8 or peak < rms * (1 - 1e-9) or kurtosis < 1 - 1e-9:
            raise ValueError("Invalid RMS, peak or Pearson kurtosis")
        if variant == "base21":
            continue
        energy = values[offset + 3:offset + 7]
        total = sum(energy)
        if total <= 1e-16:
            raise ValueError("Insufficient band energy for ratios")
        extra += [v / total for v in energy] + [peak / rms]
        if variant == "log_ratios36":
            for i in (0, 1):
                base[offset + i] = math.log1p(values[offset + i] / 0.01)
            for i in (3, 4, 5, 6):
                base[offset + i] = math.log1p(values[offset + i] / 0.0001)
    return base + extra
