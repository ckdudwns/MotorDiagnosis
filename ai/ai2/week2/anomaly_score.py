"""AI-2 week 2 baseline-based anomaly-score draft.

The score is an operational signal, not a fault classifier.  It evaluates only
the raw vibration RMS field against AI-1's normal baseline; calibrated mm/s
and dB values are deliberately not inferred from raw public data.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


BASELINE_PATH = (
    Path(__file__).resolve().parents[2]
    / "ai1"
    / "week2"
    / "ai1"
    / "dataset"
    / "baseline.json"
)
MODEL_ID = "ai1_week2_vibration_rms_baseline_v1"
FEATURE_NAME = "rms_mean"
INPUT_FIELD = "vibrationRmsRaw"


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    """Load the committed AI-1 normal baseline used by this score draft."""
    with path.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    if FEATURE_NAME not in baseline.get("features", {}):
        raise ValueError(f"Baseline does not contain {FEATURE_NAME}")
    return baseline


def finite_number(value: object) -> float | None:
    """Return a finite numeric value, leaving absent or invalid input unavailable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if math.isfinite(numeric_value) else None


def score_telemetry_point(
    point: dict[str, Any], baseline: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a 0-100 score and evidence without mutating the stored telemetry.

    A score remains zero within AI-1's configured normal range (mean ± 3σ).
    Outside that range it rises linearly and reaches 100 at a 6σ deviation.
    """
    if baseline is None:
        baseline = load_baseline()
    value = finite_number(point.get(INPUT_FIELD))
    if value is None:
        existing_score = finite_number(point.get("anomalyScore"))
        if existing_score is not None:
            return {
                "anomalyScore": round(min(100.0, max(0.0, existing_score))),
                "anomalyStatus": "demo",
                "anomalyModel": "backend_demo_signal",
                "anomalyEvidence": [],
            }
        return {
            "anomalyScore": None,
            "anomalyStatus": "unavailable",
            "anomalyModel": MODEL_ID,
            "anomalyEvidence": [],
        }
    if value < 0:
        return {
            "anomalyScore": None,
            "anomalyStatus": "unavailable",
            "anomalyModel": MODEL_ID,
            "anomalyEvidence": [],
        }

    stats = baseline["features"][FEATURE_NAME]
    mean = finite_number(stats.get("mean"))
    std = finite_number(stats.get("std"))
    sigma_multiplier = finite_number(baseline.get("meta", {}).get("sigma_multiplier"))
    if mean is None or std is None or std <= 0 or sigma_multiplier is None:
        raise ValueError("Baseline RMS statistics are invalid")

    deviation_sigma = abs(value - mean) / std
    excess_sigma = max(0.0, deviation_sigma - sigma_multiplier)
    score = min(100.0, excess_sigma / sigma_multiplier * 100.0)
    rounded_score = round(score)
    if rounded_score >= 75:
        status = "critical"
    elif rounded_score >= 50:
        status = "warning"
    else:
        status = "normal"

    return {
        "anomalyScore": rounded_score,
        "anomalyStatus": status,
        "anomalyModel": MODEL_ID,
        "anomalyEvidence": [
            {
                "field": INPUT_FIELD,
                "feature": FEATURE_NAME,
                "value": value,
                "mean": mean,
                "std": std,
                "deviationSigma": round(deviation_sigma, 3),
                "normalRange": stats.get("normal_range"),
            }
        ],
    }


def annotate_telemetry_points(
    points: list[dict[str, Any]], baseline: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Attach derived score fields to query results while preserving raw records."""
    if baseline is None:
        baseline = load_baseline()
    return [{**point, **score_telemetry_point(point, baseline)} for point in points]


def latest_asset_statuses(
    points: list[dict[str, Any]], baseline: dict[str, Any] | None = None
) -> dict[str, str]:
    """Return the latest score-derived status for each stored asset."""
    if baseline is None:
        baseline = load_baseline()
    latest_by_asset: dict[str, dict[str, Any]] = {}
    for point in points:
        asset_id = point.get("assetId")
        timestamp = point.get("timestamp")
        if not isinstance(asset_id, str) or not isinstance(timestamp, str):
            continue
        existing = latest_by_asset.get(asset_id)
        received_at = point.get("receivedAt")
        point_key = (
            timestamp,
            received_at if isinstance(received_at, str) else timestamp,
            point.get("sequence") if isinstance(point.get("sequence"), int) else -1,
        )
        existing_received_at = existing.get("receivedAt") if existing else None
        existing_key = (
            str(existing.get("timestamp")) if existing else "",
            (
                existing_received_at
                if isinstance(existing_received_at, str)
                else str(existing.get("timestamp")) if existing else ""
            ),
            (
                existing.get("sequence")
                if existing and isinstance(existing.get("sequence"), int)
                else -1
            ),
        )
        if existing is None or point_key > existing_key:
            latest_by_asset[asset_id] = point
    return {
        asset_id: score_telemetry_point(point, baseline)["anomalyStatus"]
        for asset_id, point in latest_by_asset.items()
    }
