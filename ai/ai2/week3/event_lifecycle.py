"""Score-stream lifecycle logic for AI-2 week 3.

The module turns already-calculated anomaly scores into asset-anomaly events.
It deliberately does not calculate features or a score: AI-1's baseline rule
and AI-2 week 2 score path remain the source of those values.
"""

from __future__ import annotations

import copy
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4


RAW_TELEMETRY_FIELDS = (
    "timestamp",
    "sequence",
    "siteId",
    "assetId",
    "deviceId",
    "rpm",
    "vibrationRmsRaw",
    "vibrationRmsMmS",
    "vibrationPeakHz",
    "acousticRmsRaw",
    "acousticDb",
    "acousticPeakHz",
    "scenarioLabel",
    "knownVibrationLabel",
    "knownAcousticLabel",
    "source",
    "isSynthetic",
    "vibrationUnitNote",
    "acousticUnitNote",
)

PersistTransaction = Callable[[dict[str, Any], list[dict[str, Any]]], None]


@dataclass(frozen=True)
class EventLifecycleConfig:
    """Threshold, persistence, and merge controls for one score stream."""

    score_enter: float = 75.0
    score_exit: float = 50.0
    min_consecutive_enter: int = 2
    min_consecutive_exit: int = 2
    merge_gap_sec: int = 30
    idempotency_cache_size: int = 1024
    rule_version: str = "ai2-week3-v1"

    def __post_init__(self) -> None:
        for name in ("score_enter", "score_exit"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                raise ValueError(f"{name} must be a finite score from 0 to 100.")
        if self.score_exit > self.score_enter:
            raise ValueError("score_exit must not be greater than score_enter.")
        for name in ("min_consecutive_enter", "min_consecutive_exit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer greater than zero.")
        if (
            isinstance(self.merge_gap_sec, bool)
            or not isinstance(self.merge_gap_sec, int)
            or self.merge_gap_sec < 0
        ):
            raise ValueError(
                "merge_gap_sec must be an integer greater than or equal to zero."
            )
        if (
            isinstance(self.idempotency_cache_size, bool)
            or not isinstance(self.idempotency_cache_size, int)
            or self.idempotency_cache_size < 1
        ):
            raise ValueError(
                "idempotency_cache_size must be an integer greater than zero."
            )
        if not self.rule_version.strip():
            raise ValueError("rule_version must not be empty.")


@dataclass
class _AssetState:
    candidate_count: int = 0
    candidate_start_at: str | None = None
    candidate_start_model_version: str | None = None
    candidate_max_score: float = 0.0
    candidate_max_score_model_version: str | None = None
    exit_count: int = 0
    exit_start_at: str | None = None
    open_event: dict[str, Any] | None = None
    latest_closed_event: dict[str, Any] | None = None
    last_processed_at: datetime | None = None
    last_point_identity: tuple[object, ...] | None = None
    last_processing_key: tuple[datetime, str, int, datetime] | None = None


def _finite_score(point: dict[str, Any]) -> float | None:
    value = point.get("anomalyScore", point.get("score"))
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric_value = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value) or not 0 <= numeric_value <= 100:
        return None
    return numeric_value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty RFC3339 timestamp.")
    normalized = value.strip()
    if "T" not in normalized:
        raise ValueError("timestamp must be an RFC3339 timestamp.")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp must be an RFC3339 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _point_identity(
    telemetry_payload: dict[str, Any],
    point: dict[str, Any],
    timestamp: datetime,
    fallback_score: float | None,
    sensor_fault: bool,
    sensor_fault_reason: str | None,
) -> tuple[str, str]:
    """Build the API-contract payload identity without derived score fields."""
    raw_payload = {
        key: telemetry_payload[key]
        for key in RAW_TELEMETRY_FIELDS
        if key in telemetry_payload
    }
    if raw_payload:
        raw_payload = _normalize_api_telemetry_identity(raw_payload)
    if _telemetry_key(telemetry_payload) is None:
        raw_payload["timestamp"] = timestamp.isoformat()
        raw_payload["anomalyScore"] = fallback_score
        raw_payload["anomalyModel"] = str(point.get("anomalyModel") or "unknown")
        raw_payload["sensorFault"] = sensor_fault
        raw_payload["sensorFaultReason"] = (sensor_fault_reason or "").strip()
    return timestamp.isoformat(), _canonical_json(raw_payload)


def _normalize_api_telemetry_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the API v1.2 normalization used before its payload hash."""
    normalized = {key: payload.get(key) for key in RAW_TELEMETRY_FIELDS}
    for key in ("siteId", "assetId", "deviceId"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = value.strip().upper()
    timestamp = normalized.get("timestamp")
    if timestamp is not None:
        try:
            normalized["timestamp"] = _parse_timestamp(timestamp).isoformat()
        except ValueError:
            pass
    for key in (
        "knownVibrationLabel",
        "knownAcousticLabel",
        "source",
        "vibrationUnitNote",
        "acousticUnitNote",
    ):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = value.strip() or None
    normalized["vibrationRmsMmS"] = None
    normalized["acousticDb"] = None
    return normalized


def _canonical_json(value: object) -> str:
    """Match the API's numeric-normalized, canonical JSON payload comparison."""

    def normalize(current: object) -> object:
        if current is None or isinstance(current, (bool, str)):
            return current
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            try:
                numeric = float(current)
            except OverflowError:
                return {"invalidNumberType": type(current).__name__}
            if math.isfinite(numeric):
                return numeric
            return {"invalidNumberType": type(current).__name__}
        if isinstance(current, dict):
            return {str(key): normalize(item) for key, item in current.items()}
        if isinstance(current, (list, tuple)):
            return [normalize(item) for item in current]
        return {"unsupportedType": type(current).__name__}

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"))


def _telemetry_key(payload: dict[str, Any]) -> tuple[str, int] | None:
    """Return the device idempotency key when both contract fields exist."""
    device_id = payload.get("deviceId")
    sequence = payload.get("sequence")
    if (
        not isinstance(device_id, str)
        or not device_id.strip()
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
    ):
        return None
    return device_id.strip().upper(), sequence


def _processing_key(
    point: dict[str, Any], payload: dict[str, Any], timestamp: datetime
) -> tuple[datetime, str, int, datetime]:
    """Return the required deterministic per-asset processing order."""
    received_at = payload.get("receivedAt", point.get("receivedAt"))
    try:
        received_timestamp = _parse_timestamp(received_at)
    except ValueError:
        received_timestamp = timestamp
    device_id = payload.get("deviceId", point.get("deviceId"))
    normalized_device_id = (
        device_id.strip().upper() if isinstance(device_id, str) else ""
    )
    sequence = payload.get("sequence", point.get("sequence"))
    normalized_sequence = (
        sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else -1
    )
    return timestamp, normalized_device_id, normalized_sequence, received_timestamp


def _is_processing_key_ordered(
    previous: tuple[datetime, str, int, datetime],
    current: tuple[datetime, str, int, datetime],
) -> bool:
    """Apply one transitive order to every point in the timestamp bucket."""
    return current > previous


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    """Return an external event without lifecycle-only precision fields."""
    return {key: value for key, value in event.items() if not key.startswith("_")}


def _history_to_snapshot(
    history: OrderedDict[tuple[str, int], tuple[object, ...]],
) -> list[dict[str, Any]]:
    return [
        {"deviceId": device_id, "sequence": sequence, "identity": list(identity)}
        for (device_id, sequence), identity in history.items()
    ]


def _history_from_snapshot(
    stored: object,
) -> OrderedDict[tuple[str, int], tuple[object, ...]]:
    if not isinstance(stored, list):
        raise ValueError("snapshot processedTelemetry is invalid.")
    history: OrderedDict[tuple[str, int], tuple[object, ...]] = OrderedDict()
    for item in stored:
        if not isinstance(item, dict):
            raise ValueError("snapshot processedTelemetry entry is invalid.")
        device_id = item.get("deviceId")
        sequence = item.get("sequence")
        identity = item.get("identity")
        if (
            not isinstance(device_id, str)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not isinstance(identity, list)
            or len(identity) != 2
            or not all(isinstance(value, str) for value in identity)
        ):
            raise ValueError("snapshot processedTelemetry entry is invalid.")
        history[(device_id, sequence)] = tuple(identity)
    return history


def _watermarks_to_snapshot(
    watermarks: OrderedDict[str, tuple[datetime, int]],
) -> list[dict[str, Any]]:
    return [
        {
            "deviceId": device_id,
            "timestamp": timestamp.isoformat(),
            "sequence": sequence,
        }
        for device_id, (timestamp, sequence) in watermarks.items()
    ]


def _watermarks_from_snapshot(
    stored: object,
) -> OrderedDict[str, tuple[datetime, int]]:
    if not isinstance(stored, list):
        raise ValueError("snapshot sequenceWatermarks is invalid.")
    watermarks: OrderedDict[str, tuple[datetime, int]] = OrderedDict()
    for item in stored:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("deviceId"), str)
            or isinstance(item.get("sequence"), bool)
            or not isinstance(item.get("sequence"), int)
        ):
            raise ValueError("snapshot sequenceWatermarks entry is invalid.")
        watermarks[item["deviceId"]] = (
            _parse_timestamp(item.get("timestamp")),
            item["sequence"],
        )
    return watermarks


def _processing_key_to_snapshot(
    key: tuple[datetime, str, int, datetime] | None,
) -> list[object] | None:
    if key is None:
        return None
    return [key[0].isoformat(), key[1], key[2], key[3].isoformat()]


def _processing_key_from_snapshot(
    stored: object,
) -> tuple[datetime, str, int, datetime] | None:
    if stored is None:
        return None
    if (
        not isinstance(stored, list)
        or len(stored) != 4
        or not isinstance(stored[1], str)
        or isinstance(stored[2], bool)
        or not isinstance(stored[2], int)
    ):
        raise ValueError("snapshot lastProcessingKey is invalid.")
    return (
        _parse_timestamp(stored[0]),
        stored[1],
        stored[2],
        _parse_timestamp(stored[3]),
    )


def _snapshot_revision(snapshot: dict[str, Any]) -> int:
    revision = snapshot.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("snapshot revision must be a non-negative integer.")
    return revision


def _snapshot_count(stored: dict[str, Any], key: str) -> int:
    value = stored.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"snapshot {key} must be a non-negative integer.")
    return value


def _snapshot_score(stored: dict[str, Any], key: str) -> float:
    value = stored.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"snapshot {key} must be a finite score.")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError(f"snapshot {key} must be a finite score from 0 to 100.")
    return score


def _snapshot_optional_timestamp(stored: dict[str, Any], key: str) -> str | None:
    value = stored.get(key)
    if value is None:
        return None
    _parse_timestamp(value)
    return value


def _snapshot_event(value: object, expected_status: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("snapshot event must be an object or null.")
    required = {
        "id",
        "assetId",
        "startAt",
        "endAt",
        "status",
        "endReason",
        "maxScore",
        "_maxScoreRaw",
        "lastScore",
        "sampleCount",
        "mergeCount",
        "thresholdVersion",
        "modelVersion",
        "maxScoreModelVersion",
        "classification",
    }
    if not required.issubset(value):
        raise ValueError("snapshot event is missing required fields.")
    if value["status"] != expected_status:
        raise ValueError("snapshot event status is invalid for its state.")
    for key in (
        "id",
        "assetId",
        "startAt",
        "thresholdVersion",
        "modelVersion",
        "maxScoreModelVersion",
        "classification",
    ):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"snapshot event {key} must be a non-empty string.")
    _parse_timestamp(value["startAt"])
    _snapshot_score(value, "maxScore")
    _snapshot_score(value, "_maxScoreRaw")
    _snapshot_score(value, "lastScore")
    if round(float(value["_maxScoreRaw"])) != value["maxScore"]:
        raise ValueError("snapshot event maxScore does not match _maxScoreRaw.")
    for key in ("sampleCount", "mergeCount"):
        if (
            isinstance(value[key], bool)
            or not isinstance(value[key], int)
            or value[key] < 0
        ):
            raise ValueError(f"snapshot event {key} must be a non-negative integer.")
    if value["sampleCount"] < 1:
        raise ValueError("snapshot event sampleCount must be at least one.")
    if expected_status == "open":
        if value["endAt"] is not None or value["endReason"] is not None:
            raise ValueError("snapshot open event must not have end fields.")
    else:
        _parse_timestamp(value["endAt"])
        if not isinstance(value["endReason"], str) or not value["endReason"].strip():
            raise ValueError("snapshot closed event must have endReason.")
        if _parse_timestamp(value["endAt"]) < _parse_timestamp(value["startAt"]):
            raise ValueError("snapshot event endAt must not precede startAt.")
    return copy.deepcopy(value)


class AnomalyEventLifecycle:
    """Create, merge, and close asset events from chronological score points.

    Call :meth:`process_point` once for each point in timestamp order. Returned
    updates are suitable for a backend adapter to persist. A sensor-fault point
    never opens an asset-anomaly event; if one is currently open, it is closed
    with ``endReason=sensor_fault_detected`` so untrusted signal data cannot
    keep the asset event alive.
    """

    def __init__(self, config: EventLifecycleConfig | None = None) -> None:
        self.config = config or EventLifecycleConfig()
        self._lock = threading.RLock()
        self._revision = 0
        self._states: dict[str, _AssetState] = {}
        self._processed_telemetry: OrderedDict[tuple[str, int], tuple[object, ...]] = (
            OrderedDict()
        )
        self._sequence_watermarks: OrderedDict[str, tuple[datetime, int]] = (
            OrderedDict()
        )
        self._is_persisting = False

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable checkpoint for durable lifecycle storage."""
        with self._lock:
            return self._snapshot_for_states(
                self._states,
                self._processed_telemetry,
                self._sequence_watermarks,
                self._revision,
            )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        config: EventLifecycleConfig | None = None,
    ) -> AnomalyEventLifecycle:
        """Restore a lifecycle after restart from :meth:`snapshot` output."""
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 2:
            raise ValueError("snapshot schemaVersion must be 2.")
        required_snapshot_fields = {
            "schemaVersion",
            "revision",
            "config",
            "assets",
            "processedTelemetry",
            "sequenceWatermarks",
        }
        if not required_snapshot_fields.issubset(snapshot):
            raise ValueError("snapshot is missing required version 2 fields.")
        snapshot_config = snapshot.get("config")
        if not isinstance(snapshot_config, dict):
            raise ValueError("snapshot config is required.")
        config_keys = {
            "score_enter",
            "score_exit",
            "min_consecutive_enter",
            "min_consecutive_exit",
            "merge_gap_sec",
            "idempotency_cache_size",
            "rule_version",
        }
        if set(snapshot_config) != config_keys:
            raise ValueError(
                "snapshot config fields must exactly match schema version 2."
            )
        if config is None:
            config = EventLifecycleConfig(**snapshot_config)
        snapshot_rule = EventLifecycleConfig(**snapshot_config)
        lifecycle = cls(config)
        assets = snapshot.get("assets")
        if not isinstance(assets, dict):
            raise ValueError("snapshot assets must be an object.")
        for asset_id, stored_state in assets.items():
            if not isinstance(asset_id, str) or not isinstance(stored_state, dict):
                raise ValueError("snapshot asset state is invalid.")
            restored_state = lifecycle._state_from_snapshot(stored_state)
            for event in (
                restored_state.open_event,
                restored_state.latest_closed_event,
            ):
                if event is not None and event["assetId"] != asset_id:
                    raise ValueError("snapshot event assetId does not match its state.")
            if config != snapshot_rule:
                lifecycle._reset_entry_candidate(restored_state)
                restored_state.exit_count = 0
                restored_state.exit_start_at = None
            lifecycle._states[asset_id] = restored_state
        lifecycle._revision = _snapshot_revision(snapshot)
        lifecycle._processed_telemetry = _history_from_snapshot(
            snapshot.get("processedTelemetry", [])
        )
        lifecycle._sequence_watermarks = _watermarks_from_snapshot(
            snapshot.get("sequenceWatermarks", [])
        )
        lifecycle._trim_history()
        return lifecycle

    def _snapshot_for_states(
        self,
        states: dict[str, _AssetState],
        processed_telemetry: OrderedDict[tuple[str, int], tuple[object, ...]],
        sequence_watermarks: OrderedDict[str, tuple[datetime, int]],
        revision: int,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "revision": revision,
            "config": {
                "score_enter": self.config.score_enter,
                "score_exit": self.config.score_exit,
                "min_consecutive_enter": self.config.min_consecutive_enter,
                "min_consecutive_exit": self.config.min_consecutive_exit,
                "merge_gap_sec": self.config.merge_gap_sec,
                "idempotency_cache_size": self.config.idempotency_cache_size,
                "rule_version": self.config.rule_version,
            },
            "processedTelemetry": _history_to_snapshot(processed_telemetry),
            "sequenceWatermarks": _watermarks_to_snapshot(sequence_watermarks),
            "assets": {
                asset_id: {
                    "candidateCount": state.candidate_count,
                    "candidateStartAt": state.candidate_start_at,
                    "candidateStartModelVersion": state.candidate_start_model_version,
                    "candidateMaxScore": state.candidate_max_score,
                    "candidateMaxScoreModelVersion": state.candidate_max_score_model_version,
                    "exitCount": state.exit_count,
                    "exitStartAt": state.exit_start_at,
                    "openEvent": copy.deepcopy(state.open_event),
                    "latestClosedEvent": copy.deepcopy(state.latest_closed_event),
                    "lastProcessedAt": (
                        state.last_processed_at.isoformat()
                        if state.last_processed_at is not None
                        else None
                    ),
                    "lastPointIdentity": (
                        list(state.last_point_identity)
                        if state.last_point_identity is not None
                        else None
                    ),
                    "lastProcessingKey": _processing_key_to_snapshot(
                        state.last_processing_key
                    ),
                }
                for asset_id, state in states.items()
            },
        }

    @staticmethod
    def _state_from_snapshot(stored: dict[str, Any]) -> _AssetState:
        required_state_fields = {
            "candidateCount",
            "candidateStartAt",
            "candidateStartModelVersion",
            "candidateMaxScore",
            "candidateMaxScoreModelVersion",
            "exitCount",
            "exitStartAt",
            "openEvent",
            "latestClosedEvent",
            "lastProcessedAt",
            "lastPointIdentity",
            "lastProcessingKey",
        }
        if not required_state_fields.issubset(stored):
            raise ValueError("snapshot asset state is missing required fields.")
        candidate_count = _snapshot_count(stored, "candidateCount")
        exit_count = _snapshot_count(stored, "exitCount")
        candidate_start_at = _snapshot_optional_timestamp(stored, "candidateStartAt")
        exit_start_at = _snapshot_optional_timestamp(stored, "exitStartAt")
        candidate_start_model = stored["candidateStartModelVersion"]
        candidate_max_model = stored["candidateMaxScoreModelVersion"]
        for value in (candidate_start_model, candidate_max_model):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("snapshot candidate model version is invalid.")
        candidate_max_score = _snapshot_score(stored, "candidateMaxScore")
        if candidate_count == 0 and any(
            value is not None
            for value in (
                candidate_start_at,
                candidate_start_model,
                candidate_max_model,
            )
        ):
            raise ValueError(
                "snapshot empty candidate must not retain candidate fields."
            )
        if candidate_count > 0 and any(
            value is None
            for value in (
                candidate_start_at,
                candidate_start_model,
                candidate_max_model,
            )
        ):
            raise ValueError("snapshot pending candidate is incomplete.")
        open_event = _snapshot_event(stored["openEvent"], "open")
        latest_closed_event = _snapshot_event(stored["latestClosedEvent"], "closed")
        if exit_count > 0 and (open_event is None or exit_start_at is None):
            raise ValueError("snapshot exit streak is incomplete.")
        if exit_count == 0 and exit_start_at is not None:
            raise ValueError("snapshot empty exit streak must not have a start time.")
        last_processed_at = _snapshot_optional_timestamp(stored, "lastProcessedAt")
        identity = stored["lastPointIdentity"]
        if identity is not None and (
            not isinstance(identity, list)
            or len(identity) != 2
            or not all(isinstance(value, str) for value in identity)
        ):
            raise ValueError("snapshot lastPointIdentity is invalid.")
        last_processing_key = _processing_key_from_snapshot(stored["lastProcessingKey"])
        if (last_processed_at is None) != (last_processing_key is None):
            raise ValueError(
                "snapshot lastProcessedAt and order key must both be present."
            )
        marker_count = sum(
            value is not None
            for value in (last_processed_at, last_processing_key, identity)
        )
        if marker_count not in (0, 3):
            raise ValueError(
                "snapshot last processing markers must be all present or absent."
            )
        if last_processed_at is not None and (
            _parse_timestamp(last_processed_at) != last_processing_key[0]
        ):
            raise ValueError("snapshot lastProcessedAt and order key do not match.")
        return _AssetState(
            candidate_count=candidate_count,
            candidate_start_at=candidate_start_at,
            candidate_start_model_version=candidate_start_model,
            candidate_max_score=candidate_max_score,
            candidate_max_score_model_version=candidate_max_model,
            exit_count=exit_count,
            exit_start_at=exit_start_at,
            open_event=open_event,
            latest_closed_event=latest_closed_event,
            last_processed_at=(
                _parse_timestamp(last_processed_at)
                if last_processed_at is not None
                else None
            ),
            last_point_identity=tuple(identity) if identity is not None else None,
            last_processing_key=last_processing_key,
        )

    def _remember_telemetry(
        self,
        telemetry_key: tuple[str, int],
        point_identity: tuple[object, ...],
    ) -> None:
        self._processed_telemetry[telemetry_key] = point_identity
        self._processed_telemetry.move_to_end(telemetry_key)
        self._trim_history()

    def _remember_sequence_watermark(
        self,
        device_id: str,
        timestamp: datetime,
        sequence: int,
    ) -> None:
        self._sequence_watermarks[device_id] = (timestamp, sequence)
        self._sequence_watermarks.move_to_end(device_id)
        self._trim_history()

    def _trim_history(self) -> None:
        while len(self._sequence_watermarks) > self.config.idempotency_cache_size:
            self._sequence_watermarks.popitem(last=False)
        while len(self._processed_telemetry) > self.config.idempotency_cache_size:
            self._processed_telemetry.popitem(last=False)

    @staticmethod
    def _validate_telemetry_payload_match(
        point: dict[str, Any], payload: dict[str, Any], point_timestamp: datetime
    ) -> None:
        payload_asset_id = payload.get("assetId")
        if not isinstance(payload_asset_id, str) or not payload_asset_id.strip():
            raise ValueError("telemetry_payload assetId must be a non-empty string.")
        point_asset_id = point.get("assetId")
        if payload_asset_id.strip().upper() != str(point_asset_id).strip().upper():
            raise ValueError("point and telemetry_payload assetId must match.")
        if _parse_timestamp(payload.get("timestamp")) != point_timestamp:
            raise ValueError("point and telemetry_payload timestamp must match.")
        for key in ("siteId", "deviceId", "sequence"):
            if key not in point:
                continue
            if key not in payload:
                raise ValueError(f"telemetry_payload {key} must match point.")
            point_value = point[key]
            payload_value = payload[key]
            if key in ("siteId", "deviceId"):
                if not isinstance(point_value, str) or not isinstance(
                    payload_value, str
                ):
                    raise ValueError(f"point and telemetry_payload {key} must match.")
                if point_value.strip().upper() != payload_value.strip().upper():
                    raise ValueError(f"point and telemetry_payload {key} must match.")
            elif point_value != payload_value:
                raise ValueError("point and telemetry_payload sequence must match.")

    def process_point(
        self,
        point: dict[str, Any],
        *,
        sensor_fault: bool = False,
        sensor_fault_reason: str | None = None,
        telemetry_payload: dict[str, Any] | None = None,
        persist_transaction: PersistTransaction | None = None,
        expected_revision: int | None = None,
    ) -> list[dict[str, Any]]:
        """Process one score point and return lifecycle updates.

        Required point fields are ``assetId`` and ``timestamp``. Missing or
        invalid scores break pending entry/exit streaks but do not manufacture
        an asset event. When a persistence callback is supplied, it receives a
        proposed checkpoint and updates before in-memory state is committed;
        raising from that callback leaves this lifecycle unchanged for retry.

        ``telemetry_payload`` should be the normalized raw ingest record when
        one is available. Its canonical API payload is used for the
        ``(deviceId, sequence)`` idempotency comparison; score/model fields
        are intentionally excluded from that comparison.
        """
        if self._is_persisting:
            raise RuntimeError(
                "process_point cannot be called from persist_transaction."
            )
        asset_id = point.get("assetId")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("assetId must be a non-empty string.")
        timestamp = point.get("timestamp")
        parsed_timestamp = _parse_timestamp(timestamp)
        score = _finite_score(point)
        asset_id = asset_id.strip().upper()
        idempotency_payload = telemetry_payload or point
        if telemetry_payload is not None:
            self._validate_telemetry_payload_match(
                point, telemetry_payload, parsed_timestamp
            )
        effective_sensor_fault_reason = (
            sensor_fault_reason.strip()
            if sensor_fault
            and isinstance(sensor_fault_reason, str)
            and sensor_fault_reason.strip()
            else "sensor_fault_detected"
        )
        point_identity = _point_identity(
            idempotency_payload,
            point,
            parsed_timestamp,
            score,
            sensor_fault,
            effective_sensor_fault_reason if sensor_fault else None,
        )
        telemetry_key = _telemetry_key(idempotency_payload)
        processing_key = _processing_key(point, idempotency_payload, parsed_timestamp)

        with self._lock:
            if self._is_persisting:
                raise RuntimeError(
                    "process_point cannot be called from persist_transaction."
                )
            if expected_revision is not None and expected_revision != self._revision:
                raise ValueError(
                    "expectedRevision does not match the current checkpoint."
                )
            state = copy.deepcopy(self._states.get(asset_id, _AssetState()))
            history = copy.deepcopy(self._processed_telemetry)
            watermarks = copy.deepcopy(self._sequence_watermarks)
            if telemetry_key is not None:
                previous_identity = history.get(telemetry_key)
                if previous_identity is not None:
                    if previous_identity == point_identity:
                        return []
                    raise ValueError(
                        "deviceId and sequence were already processed with a different payload."
                    )
                previous_sequence = watermarks.get(telemetry_key[0])
                if (
                    previous_sequence is not None
                    and previous_sequence[0] == parsed_timestamp
                    and telemetry_key[1] < previous_sequence[1]
                ):
                    raise ValueError(
                        "sequence must not decrease for the same device and timestamp."
                    )
            if (
                processing_key == state.last_processing_key
                and point_identity == state.last_point_identity
            ):
                return []
            if (
                state.last_processing_key is not None
                and processing_key != state.last_processing_key
                and not _is_processing_key_ordered(
                    state.last_processing_key, processing_key
                )
            ):
                raise ValueError(
                    "point order must increase by timestamp, receivedAt, deviceId, sequence."
                )
            if sensor_fault:
                updates = self._suppress_for_sensor_fault(
                    state,
                    asset_id,
                    str(timestamp),
                    effective_sensor_fault_reason,
                )
            elif score is None:
                self._reset_entry_candidate(state)
                state.exit_count = 0
                state.exit_start_at = None
                updates = []
            elif state.open_event is None:
                updates = self._process_entry(
                    state, asset_id, str(timestamp), score, point
                )
            else:
                updates = self._process_open_event(state, str(timestamp), score, point)

            state.last_processed_at = parsed_timestamp
            state.last_processing_key = processing_key
            state.last_point_identity = point_identity
            if telemetry_key is not None:
                history[telemetry_key] = point_identity
                history.move_to_end(telemetry_key)
                watermarks[telemetry_key[0]] = (parsed_timestamp, telemetry_key[1])
                watermarks.move_to_end(telemetry_key[0])
                while len(history) > self.config.idempotency_cache_size:
                    history.popitem(last=False)
                while len(watermarks) > self.config.idempotency_cache_size:
                    watermarks.popitem(last=False)
            candidate_states = copy.deepcopy(self._states)
            candidate_states[asset_id] = state
            next_revision = self._revision + 1
            checkpoint = self._snapshot_for_states(
                candidate_states, history, watermarks, next_revision
            )
            checkpoint["expectedRevision"] = self._revision
            if persist_transaction is not None:
                self._is_persisting = True
                try:
                    persist_transaction(checkpoint, copy.deepcopy(updates))
                finally:
                    self._is_persisting = False
            self._states = candidate_states
            self._processed_telemetry = history
            self._sequence_watermarks = watermarks
            self._revision = next_revision
            return updates

    def _process_entry(
        self,
        state: _AssetState,
        asset_id: str,
        timestamp: str,
        score: float,
        point: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if score < self.config.score_enter:
            self._reset_entry_candidate(state)
            return []

        state.candidate_count += 1
        model_version = str(point.get("anomalyModel") or "unknown")
        if state.candidate_start_at is None:
            state.candidate_start_at = timestamp
            state.candidate_start_model_version = model_version
        if state.candidate_count == 1 or score > state.candidate_max_score:
            state.candidate_max_score = score
            state.candidate_max_score_model_version = model_version
        if state.candidate_count < self.config.min_consecutive_enter:
            return []

        start_at = state.candidate_start_at
        start_model_version = state.candidate_start_model_version
        candidate_count = state.candidate_count
        candidate_max_score = state.candidate_max_score
        candidate_max_score_model_version = state.candidate_max_score_model_version
        self._reset_entry_candidate(state)
        event, is_merge = self._open_or_merge_event(
            state,
            asset_id,
            start_at,
            timestamp,
            score,
            candidate_max_score,
            candidate_count,
            start_model_version or "unknown",
            candidate_max_score_model_version or "unknown",
        )
        state.open_event = event
        state.exit_count = 0
        state.exit_start_at = None
        return [
            {
                "kind": "asset_event_merged" if is_merge else "asset_event_started",
                "event": _event_view(event),
            }
        ]

    def _open_or_merge_event(
        self,
        state: _AssetState,
        asset_id: str,
        start_at: str,
        timestamp: str,
        score: float,
        candidate_max_score: float,
        candidate_count: int,
        start_model_version: str,
        candidate_max_score_model_version: str,
    ) -> tuple[dict[str, Any], bool]:
        previous = state.latest_closed_event
        if previous and previous.get("endAt"):
            gap_sec = (
                _parse_timestamp(start_at) - _parse_timestamp(previous["endAt"])
            ).total_seconds()
            if 0 <= gap_sec <= self.config.merge_gap_sec:
                previous["status"] = "open"
                previous["endAt"] = None
                previous["endReason"] = None
                previous["mergeCount"] = int(previous["mergeCount"]) + 1
                self._update_open_event(
                    previous,
                    score,
                    sample_count=candidate_count,
                    max_score=candidate_max_score,
                    max_score_model_version=candidate_max_score_model_version,
                )
                state.latest_closed_event = None
                return previous, True

        event = {
            "id": f"AI2-EVENT-{uuid4()}",
            "assetId": asset_id,
            "startAt": start_at,
            "endAt": None,
            "status": "open",
            "endReason": None,
            "maxScore": round(candidate_max_score),
            "_maxScoreRaw": candidate_max_score,
            "lastScore": round(score),
            "sampleCount": candidate_count,
            "mergeCount": 0,
            "thresholdVersion": self.config.rule_version,
            "modelVersion": start_model_version,
            "maxScoreModelVersion": candidate_max_score_model_version,
            "classification": "asset_anomaly",
        }
        return event, False

    def _process_open_event(
        self, state: _AssetState, timestamp: str, score: float, point: dict[str, Any]
    ) -> list[dict[str, Any]]:
        event = state.open_event
        if event is None:
            return []
        self._update_open_event(
            event,
            score,
            max_score_model_version=str(point.get("anomalyModel") or "unknown"),
        )
        if score >= self.config.score_exit:
            state.exit_count = 0
            state.exit_start_at = None
            return [{"kind": "asset_event_updated", "event": _event_view(event)}]

        state.exit_count += 1
        if state.exit_start_at is None:
            state.exit_start_at = timestamp
        if state.exit_count < self.config.min_consecutive_exit:
            return [{"kind": "asset_event_updated", "event": _event_view(event)}]

        event["endAt"] = timestamp
        event["status"] = "closed"
        event["endReason"] = "score_recovered"
        state.latest_closed_event = event
        state.open_event = None
        state.exit_count = 0
        state.exit_start_at = None
        return [{"kind": "asset_event_closed", "event": _event_view(event)}]

    def _suppress_for_sensor_fault(
        self,
        state: _AssetState,
        asset_id: str,
        timestamp: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        self._reset_entry_candidate(state)
        state.exit_count = 0
        state.exit_start_at = None
        updates = [
            {
                "kind": "sensor_fault_suppressed",
                "assetId": asset_id,
                "timestamp": timestamp,
                "reason": reason,
                "classification": "sensor_fault",
                "assetEventExcluded": True,
            }
        ]
        if state.open_event is not None:
            state.open_event["endAt"] = timestamp
            state.open_event["status"] = "closed"
            state.open_event["endReason"] = "sensor_fault_detected"
            state.latest_closed_event = state.open_event
            updates.append(
                {"kind": "asset_event_closed", "event": _event_view(state.open_event)}
            )
            state.open_event = None
        # A sensor failure breaks the continuity of an otherwise mergeable
        # anomaly. It must not be possible to erase this audit boundary later.
        state.latest_closed_event = None
        return updates

    @staticmethod
    def _reset_entry_candidate(state: _AssetState) -> None:
        state.candidate_count = 0
        state.candidate_start_at = None
        state.candidate_start_model_version = None
        state.candidate_max_score = 0.0
        state.candidate_max_score_model_version = None

    @staticmethod
    def _update_open_event(
        event: dict[str, Any],
        score: float,
        *,
        sample_count: int = 1,
        max_score: float | None = None,
        max_score_model_version: str,
    ) -> None:
        event["lastScore"] = round(score)
        next_max_score = max_score if max_score is not None else score
        current_max_score = float(event["_maxScoreRaw"])
        if next_max_score > current_max_score:
            event["_maxScoreRaw"] = next_max_score
            event["maxScore"] = round(next_max_score)
            event["maxScoreModelVersion"] = max_score_model_version
        event["sampleCount"] = int(event["sampleCount"]) + sample_count
