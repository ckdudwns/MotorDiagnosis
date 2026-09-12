"""Prepare reproducible raw XYZ input without choosing a model/feature family."""
from .raw_samples import G_PER_COUNT, decode_samples

ADAPTER_ID = "adxl345-xyz-g-unmodified-v1"


def prepare_input(window):
    if window["quality"] != "valid":
        raise ValueError(window["quality"])
    rows = decode_samples(window)
    if len(rows) != 512:
        raise ValueError("sample_gap")
    if any(v in (-4096, 4095) for row in rows for v in row):
        raise ValueError("clipped")
    # Keep DC/gravity and axis order. No RF66 FFT, feature vector, scaling fit,
    # sequence accumulation, threshold or inference is selected by this adapter.
    return {
        "adapterId": ADAPTER_ID, "sourceProfileId": window["profileId"],
        "shape": [512, 3], "axes": ["X", "Y", "Z"], "unit": "g",
        "sampleRateHz": window["sampleRateHz"], "gPerCount": G_PER_COUNT,
        "meanRemoved": False, "values": [[v * G_PER_COUNT for v in row] for row in rows],
    }
