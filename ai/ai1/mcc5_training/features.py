"""Versioned raw vibration contract shared by offline training and PC inference.

Public data resampling is NOT a simulation of ADXL345 noise, mounting, or aliasing.
No audio, MFCC, high-frequency envelope, RPM, current, or torque enters the model.
"""

import numpy as np
from scipy.signal import firwin, resample_poly

RATE = 800
WINDOW = 512
SEQUENCE = 1
BANDS = ((0, 50), (50, 100), (100, 200), (200, 350))
NAMES = [
    f"vibration{axis}.{name}"
    for axis in "XYZ"
    for name in (
        "rms_ac_g",
        "peak_ac_g",
        "kurtosis_pearson",
        "power_0_50_g2",
        "power_50_100_g2",
        "power_100_200_g2",
        "power_200_350_g2",
    )
]
PROFILE = {
    "id": "mcc5-vibration-800hz-xyz-v1",
    "sampleRateHz": RATE,
    "windowSamples": WINDOW,
    "windowHopSamples": WINDOW,
    "sequenceLength": SEQUENCE,
    "sequenceHopWindows": SEQUENCE,
    "primaryCandidate": "dense_autoencoder",
    "requiresConsecutiveWindows": False,
    "channels": ["vibrationX", "vibrationY", "vibrationZ"],
    "unit": "g",
    "featureNames": NAMES,
    "featureCount": len(NAMES),
    "dc": "subtract per-window per-axis mean before ALL features",
    "kurtosis": "Pearson central fourth moment / variance squared; constant window invalid",
    "spectrum": "periodic Hann; one-sided abs(rfft)^2 / (N*sum(hann^2)); DC excluded",
    "bandsHz": [list(band) for band in BANDS],
    "bandBounds": "lower inclusive, upper exclusive",
    "source": {
        "rateHz": 12800,
        "columns": "9 columns: time,keyphase,torque,X,Y,Z,currentA,currentB,currentC",
        "vibrationUnit": "volts",
        "voltsPerG": 0.1,
        "resampling": "resample_poly(up=1,down=16,padtype=line)",
        "fir": {"taps": 1025, "cutoffHz": 350, "window": ["kaiser", 8.6]},
        "edgeTrimTargetSamples": 32,
    },
    "fieldValidated": False,
    "adxlNoiseOrQuantizationSimulated": False,
    "affectsAlerts": False,
}


def to_800hz(volts):
    raw = np.asarray(volts, dtype=np.float64)
    if raw.ndim != 2 or raw.shape[1] != 3 or raw.shape[0] < 8192:
        raise ValueError("Expected sufficient contiguous XYZ voltage samples")
    if not np.isfinite(raw).all():
        raise ValueError("Nonfinite source samples")
    taps = firwin(1025, 350, fs=12800, window=("kaiser", 8.6))
    return resample_poly(raw / 0.1, 1, 16, axis=0, window=taps, padtype="line")[32:-32]


def extract(window):
    raw = np.asarray(window, dtype=np.float64)
    if raw.shape != (WINDOW, 3) or not np.isfinite(raw).all():
        raise ValueError("Expected 512 finite XYZ samples in g at 800 Hz")
    if np.max(np.abs(raw)) >= 16:
        raise ValueError("outside_adxl16g_range")
    signal = raw - raw.mean(axis=0)
    variance = (signal * signal).mean(axis=0)
    if np.any(variance <= 1e-16):
        raise ValueError("constant_or_near_constant_axis")
    taper = np.hanning(WINDOW + 1)[:-1]
    powers = np.abs(np.fft.rfft(signal * taper[:, None], axis=0)) ** 2
    powers /= WINDOW * np.sum(taper * taper)
    powers[1:-1] *= 2
    powers[0] = 0
    frequency = np.fft.rfftfreq(WINDOW, 1 / RATE)
    values = []
    for axis in range(3):
        values.extend(
            [
                np.sqrt(variance[axis]),
                np.max(np.abs(signal[:, axis])),
                (signal[:, axis] ** 4).mean() / variance[axis] ** 2,
            ]
        )
        for low, high in BANDS:
            values.append(powers[(frequency >= low) & (frequency < high), axis].sum())
    result = np.asarray(values, dtype=np.float64)
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite features")
    return result


def make_sequences(indexed_features):
    """Do not bridge invalid windows or recordings; caller passes one run only."""
    sequences, starts, pending = [], [], []
    previous = None
    for index, values in indexed_features:
        if previous is not None and index != previous + 1:
            pending = []
        pending.append((index, values))
        previous = index
        if len(pending) == SEQUENCE:
            starts.append(pending[0][0])
            sequences.append(np.stack([v for _, v in pending]))
            pending = []
    return sequences, starts
