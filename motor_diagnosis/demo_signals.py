"""Deterministic raw-signal fixtures used only by the guarded Week 4 demo."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DEMO_SIGNAL_SOURCE = "ai2-week4-combined-signal-demo"
DEMO_MODEL_VERSION = "demo-rule-injection-v1"


def build_combined_anomaly_samples(
    *,
    site_id: str,
    asset_id: str,
    device_id: str,
    rated_rpm: float,
    duration_sec: int,
    first_sequence: int,
    end_at: datetime,
) -> list[dict[str, Any]]:
    """Build a labelled normal-to-combined-anomaly demo sequence.

    Values remain raw, unit-less demo amplitudes.  In particular, this function
    intentionally does not manufacture calibrated mm/s, dB, or an AI-1 model
    ``anomalyScore``.  A single normal point precedes a sustained combined
    vibration/acoustic/RPM deviation so the injected event has visible context.
    """
    if duration_sec < 1:
        raise ValueError("duration_sec must be at least 1.")
    if first_sequence < 1:
        raise ValueError("first_sequence must be at least 1.")

    samples: list[dict[str, Any]] = []
    normal_at = end_at - timedelta(seconds=duration_sec + 1)
    samples.append(
        _sample(
            timestamp=normal_at,
            sequence=first_sequence,
            site_id=site_id,
            asset_id=asset_id,
            device_id=device_id,
            rpm=rated_rpm,
            vibration_rms_raw=0.08,
            acoustic_rms_raw=0.007,
            vibration_peak_hz=29.9,
            acoustic_peak_hz=150.0,
            scenario_label="normal",
        )
    )
    for offset in range(duration_sec + 1):
        timestamp = end_at - timedelta(seconds=duration_sec - offset)
        samples.append(
            _sample(
                timestamp=timestamp,
                sequence=first_sequence + offset + 1,
                site_id=site_id,
                asset_id=asset_id,
                device_id=device_id,
                rpm=rated_rpm * 0.86,
                vibration_rms_raw=0.85 + offset * 0.015,
                acoustic_rms_raw=0.12 + offset * 0.003,
                vibration_peak_hz=118.0,
                acoustic_peak_hz=760.0,
                scenario_label="combined_anomaly",
            )
        )
    return samples


def _sample(
    *,
    timestamp: datetime,
    sequence: int,
    site_id: str,
    asset_id: str,
    device_id: str,
    rpm: float,
    vibration_rms_raw: float,
    acoustic_rms_raw: float,
    vibration_peak_hz: float,
    acoustic_peak_hz: float,
    scenario_label: str,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "sequence": sequence,
        "siteId": site_id,
        "assetId": asset_id,
        "deviceId": device_id,
        "rpm": round(rpm, 2),
        "vibrationRmsRaw": round(vibration_rms_raw, 4),
        "vibrationRmsMmS": None,
        "vibrationPeakHz": vibration_peak_hz,
        "acousticRmsRaw": round(acoustic_rms_raw, 4),
        "acousticDb": None,
        "acousticPeakHz": acoustic_peak_hz,
        "scenarioLabel": scenario_label,
        "knownVibrationLabel": None,
        "knownAcousticLabel": None,
        "source": DEMO_SIGNAL_SOURCE,
        "isSynthetic": True,
        "vibrationUnitNote": "raw demo RMS; calibration unavailable",
        "acousticUnitNote": "raw demo RMS; calibration unavailable",
    }
