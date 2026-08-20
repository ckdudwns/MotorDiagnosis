"""Normalize AI-2 telemetry records to the external camelCase JSON contract."""

from __future__ import annotations

import math
from typing import Any


def get_value(record: dict[str, Any], snake_key: str, camel_key: str) -> Any:
    """Read either an internal snake_case key or an external camelCase key."""
    if camel_key in record:
        return record[camel_key]
    return record.get(snake_key)


def get_nullable_float(
    record: dict[str, Any], snake_key: str, camel_key: str
) -> float | None:
    """Convert a numeric value while preserving allowed empty values as None."""
    value = get_value(record, snake_key, camel_key)
    if value is None or value == "":
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{snake_key} must be numeric or null") from error
    if not math.isfinite(numeric_value):
        raise ValueError(f"{snake_key} must be a finite number or null")
    return numeric_value


def get_raw_only_null(record: dict[str, Any], snake_key: str, camel_key: str) -> None:
    """Reject unavailable calibrated fields in the raw-only telemetry contract."""
    value = get_value(record, snake_key, camel_key)
    if value is not None and value != "":
        raise ValueError(
            f"{snake_key} must be null for the raw-only telemetry contract"
        )


def get_boolean(record: dict[str, Any], snake_key: str, camel_key: str) -> bool:
    """Convert JSON or CSV boolean representations to a bool."""
    value = get_value(record, snake_key, camel_key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ValueError(f"{snake_key} must be true or false")


def to_external_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return one consistent external JSON payload for every replay source."""
    return {
        "timestamp": record["timestamp"],
        "sequence": int(record["sequence"]),
        "siteId": get_value(record, "site_id", "siteId"),
        "assetId": get_value(record, "asset_id", "assetId"),
        "deviceId": get_value(record, "device_id", "deviceId"),
        "rpm": get_nullable_float(record, "rpm", "rpm"),
        "vibrationRmsRaw": get_nullable_float(
            record, "vibration_rms_raw", "vibrationRmsRaw"
        ),
        "vibrationRmsMmS": get_raw_only_null(
            record, "vibration_rms_mm_s", "vibrationRmsMmS"
        ),
        "vibrationPeakHz": get_nullable_float(
            record, "vibration_peak_hz", "vibrationPeakHz"
        ),
        "acousticRmsRaw": get_nullable_float(
            record, "acoustic_rms_raw", "acousticRmsRaw"
        ),
        "acousticDb": get_raw_only_null(record, "acoustic_db", "acousticDb"),
        "acousticPeakHz": get_nullable_float(
            record, "acoustic_peak_hz", "acousticPeakHz"
        ),
        "scenarioLabel": get_value(record, "scenario_label", "scenarioLabel"),
        "knownVibrationLabel": get_value(
            record, "known_vibration_label", "knownVibrationLabel"
        )
        or record.get("known_condition"),
        "knownAcousticLabel": get_value(
            record, "known_acoustic_label", "knownAcousticLabel"
        ),
        "source": record.get("source"),
        "isSynthetic": get_boolean(record, "is_synthetic", "isSynthetic"),
        "vibrationUnitNote": get_value(
            record, "vibration_unit_note", "vibrationUnitNote"
        ),
        "acousticUnitNote": get_value(record, "acoustic_unit_note", "acousticUnitNote"),
    }
