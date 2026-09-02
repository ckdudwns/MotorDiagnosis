"""Score-stream lifecycle logic for AI-2 week 3.

The module turns already-calculated anomaly scores into asset-anomaly events.
It deliberately does not calculate features or a score: AI-1's baseline rule
and AI-2 week 2 score path remain the source of those values.
"""

from __future__ import annotations

import copy
import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
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
    processed_telemetry: OrderedDict[tuple[str, int], tuple[object, ...]] = field(
        default_factory=OrderedDict
    )
    sequence_watermarks: OrderedDict[str, tuple[datetime, int]] = field(
        default_factory=OrderedDict
    )


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
    return timestamp.isoformat(), _canonical_json(raw_payload)


def _normalize_api_telemetry_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the API v1.2 normalization used before its payload hash."""
    normalized = dict(payload)
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


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    """Return an external event without lifecycle-only precision fields."""
    return {key: value for key, value in event.items() if not key.startswith("_")}


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
        self._states: dict[str, _AssetState] = {}

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable checkpoint for durable lifecycle storage."""
        return self._snapshot_for_states(self._states)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        config: EventLifecycleConfig | None = None,
    ) -> AnomalyEventLifecycle:
        """Restore a lifecycle after restart from :meth:`snapshot` output."""
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
            raise ValueError("snapshot schemaVersion must be 1.")
        snapshot_config = snapshot.get("config")
        if config is None:
            if not isinstance(snapshot_config, dict):
                raise ValueError("snapshot config is required.")
            config = EventLifecycleConfig(**snapshot_config)
        lifecycle = cls(config)
        assets = snapshot.get("assets")
        if not isinstance(assets, dict):
            raise ValueError("snapshot assets must be an object.")
        for asset_id, stored_state in assets.items():
            if not isinstance(asset_id, str) or not isinstance(stored_state, dict):
                raise ValueError("snapshot asset state is invalid.")
            restored_state = lifecycle._state_from_snapshot(stored_state)
            lifecycle._trim_history(restored_state)
            lifecycle._states[asset_id] = restored_state
        return lifecycle

    def _snapshot_for_states(self, states: dict[str, _AssetState]) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "config": {
                "score_enter": self.config.score_enter,
                "score_exit": self.config.score_exit,
                "min_consecutive_enter": self.config.min_consecutive_enter,
                "min_consecutive_exit": self.config.min_consecutive_exit,
                "merge_gap_sec": self.config.merge_gap_sec,
                "idempotency_cache_size": self.config.idempotency_cache_size,
                "rule_version": self.config.rule_version,
            },
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
                    "processedTelemetry": [
                        {
                            "deviceId": device_id,
                            "sequence": sequence,
                            "identity": list(identity),
                        }
                        for (
                            device_id,
                            sequence,
                        ), identity in state.processed_telemetry.items()
                    ],
                    "sequenceWatermarks": [
                        {
                            "deviceId": device_id,
                            "timestamp": timestamp.isoformat(),
                            "sequence": sequence,
                        }
                        for device_id, (
                            timestamp,
                            sequence,
                        ) in state.sequence_watermarks.items()
                    ],
                }
                for asset_id, state in states.items()
            },
        }

    @staticmethod
    def _state_from_snapshot(stored: dict[str, Any]) -> _AssetState:
        processed = OrderedDict()
        for item in stored.get("processedTelemetry", []):
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
            ):
                raise ValueError("snapshot processedTelemetry entry is invalid.")
            processed[(device_id, sequence)] = tuple(identity)
        sequence_watermarks = OrderedDict()
        stored_sequences = stored.get("sequenceWatermarks", [])
        if not isinstance(stored_sequences, list):
            raise ValueError("snapshot sequenceWatermarks is invalid.")
        for item in stored_sequences:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("deviceId"), str)
                or isinstance(item.get("sequence"), bool)
                or not isinstance(item.get("sequence"), int)
            ):
                raise ValueError("snapshot sequenceWatermarks entry is invalid.")
            sequence_watermarks[item["deviceId"]] = (
                _parse_timestamp(item.get("timestamp")),
                item["sequence"],
            )
        last_processed_at = stored.get("lastProcessedAt")
        return _AssetState(
            candidate_count=int(stored.get("candidateCount", 0)),
            candidate_start_at=stored.get("candidateStartAt"),
            candidate_start_model_version=stored.get("candidateStartModelVersion"),
            candidate_max_score=float(stored.get("candidateMaxScore", 0.0)),
            candidate_max_score_model_version=stored.get(
                "candidateMaxScoreModelVersion"
            ),
            exit_count=int(stored.get("exitCount", 0)),
            exit_start_at=stored.get("exitStartAt"),
            open_event=copy.deepcopy(stored.get("openEvent")),
            latest_closed_event=copy.deepcopy(stored.get("latestClosedEvent")),
            last_processed_at=(
                _parse_timestamp(last_processed_at)
                if last_processed_at is not None
                else None
            ),
            last_point_identity=(
                tuple(stored["lastPointIdentity"])
                if isinstance(stored.get("lastPointIdentity"), list)
                else None
            ),
            processed_telemetry=processed,
            sequence_watermarks=sequence_watermarks,
        )

    def _remember_telemetry(
        self,
        state: _AssetState,
        telemetry_key: tuple[str, int],
        point_identity: tuple[object, ...],
    ) -> None:
        state.processed_telemetry[telemetry_key] = point_identity
        state.processed_telemetry.move_to_end(telemetry_key)
        self._trim_history(state)

    def _remember_sequence_watermark(
        self,
        state: _AssetState,
        device_id: str,
        timestamp: datetime,
        sequence: int,
    ) -> None:
        state.sequence_watermarks[device_id] = (timestamp, sequence)
        state.sequence_watermarks.move_to_end(device_id)
        self._trim_history(state)

    def _trim_history(self, state: _AssetState) -> None:
        while len(state.sequence_watermarks) > self.config.idempotency_cache_size:
            state.sequence_watermarks.popitem(last=False)
        while len(state.processed_telemetry) > self.config.idempotency_cache_size:
            state.processed_telemetry.popitem(last=False)

    def process_point(
        self,
        point: dict[str, Any],
        *,
        sensor_fault: bool = False,
        sensor_fault_reason: str | None = None,
        telemetry_payload: dict[str, Any] | None = None,
        persist_transaction: PersistTransaction | None = None,
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
        asset_id = point.get("assetId")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("assetId must be a non-empty string.")
        timestamp = point.get("timestamp")
        parsed_timestamp = _parse_timestamp(timestamp)
        score = _finite_score(point)
        asset_id = asset_id.strip().upper()
        state = copy.deepcopy(self._states.get(asset_id, _AssetState()))
        idempotency_payload = telemetry_payload or point
        point_identity = _point_identity(
            idempotency_payload, point, parsed_timestamp, score
        )
        telemetry_key = _telemetry_key(idempotency_payload)
        if telemetry_key is not None:
            previous_identity = state.processed_telemetry.get(telemetry_key)
            if previous_identity is not None:
                if previous_identity == point_identity:
                    return []
                raise ValueError(
                    "deviceId and sequence were already processed with a different payload."
                )
            previous_sequence = state.sequence_watermarks.get(telemetry_key[0])
            if (
                previous_sequence is not None
                and previous_sequence[0] == parsed_timestamp
                and telemetry_key[1] < previous_sequence[1]
            ):
                raise ValueError(
                    "sequence must not decrease for the same device and timestamp."
                )
        if (
            state.last_processed_at is not None
            and parsed_timestamp < state.last_processed_at
        ):
            raise ValueError(
                "timestamp must not be earlier than the last point for assetId."
            )
        if (
            parsed_timestamp == state.last_processed_at
            and point_identity == state.last_point_identity
        ):
            return []
        if sensor_fault:
            updates = self._suppress_for_sensor_fault(
                state,
                asset_id,
                str(timestamp),
                sensor_fault_reason or "sensor_fault_detected",
            )
        elif score is None:
            self._reset_entry_candidate(state)
            state.exit_count = 0
            state.exit_start_at = None
            updates = []
        elif state.open_event is None:
            updates = self._process_entry(state, asset_id, str(timestamp), score, point)
        else:
            updates = self._process_open_event(state, str(timestamp), score, point)

        state.last_processed_at = parsed_timestamp
        state.last_point_identity = point_identity
        if telemetry_key is not None:
            self._remember_telemetry(state, telemetry_key, point_identity)
            self._remember_sequence_watermark(
                state, telemetry_key[0], parsed_timestamp, telemetry_key[1]
            )
        candidate_states = {**self._states, asset_id: state}
        checkpoint = self._snapshot_for_states(candidate_states)
        if persist_transaction is not None:
            persist_transaction(checkpoint, copy.deepcopy(updates))
        self._states[asset_id] = state
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
