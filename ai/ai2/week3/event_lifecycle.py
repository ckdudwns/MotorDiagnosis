"""Score-stream lifecycle logic for AI-2 week 3.

The module turns already-calculated anomaly scores into asset-anomaly events.
It deliberately does not calculate features or a score: AI-1's baseline rule
and AI-2 week 2 score path remain the source of those values.
"""

from __future__ import annotations

import math
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class EventLifecycleConfig:
    """Threshold, persistence, and merge controls for one score stream."""

    score_enter: float = 75.0
    score_exit: float = 50.0
    min_consecutive_enter: int = 2
    min_consecutive_exit: int = 2
    merge_gap_sec: int = 30
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
            raise ValueError("merge_gap_sec must be an integer greater than or equal to zero.")
        if not self.rule_version.strip():
            raise ValueError("rule_version must not be empty.")


@dataclass
class _AssetState:
    candidate_count: int = 0
    candidate_start_at: str | None = None
    candidate_max_score: float = 0.0
    exit_count: int = 0
    exit_start_at: str | None = None
    open_event: dict[str, Any] | None = None
    latest_closed_event: dict[str, Any] | None = None


def _finite_score(point: dict[str, Any]) -> float | None:
    value = point.get("anomalyScore", point.get("score"))
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
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

    def process_point(
        self,
        point: dict[str, Any],
        *,
        sensor_fault: bool = False,
        sensor_fault_reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """Process one score point and return lifecycle updates.

        Required point fields are ``assetId`` and ``timestamp``. Missing or
        invalid scores break pending entry/exit streaks but do not manufacture
        an asset event. The caller owns chronological ordering.
        """
        asset_id = point.get("assetId")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("assetId must be a non-empty string.")
        timestamp = point.get("timestamp")
        _parse_timestamp(timestamp)
        asset_id = asset_id.strip().upper()
        state = self._states.setdefault(asset_id, _AssetState())

        if sensor_fault:
            return self._suppress_for_sensor_fault(
                state,
                asset_id,
                str(timestamp),
                sensor_fault_reason or "sensor_fault_detected",
            )

        score = _finite_score(point)
        if score is None:
            state.candidate_count = 0
            state.candidate_start_at = None
            state.candidate_max_score = 0.0
            state.exit_count = 0
            state.exit_start_at = None
            return []

        if state.open_event is None:
            return self._process_entry(state, asset_id, str(timestamp), score, point)
        return self._process_open_event(state, str(timestamp), score, point)

    def _process_entry(
        self,
        state: _AssetState,
        asset_id: str,
        timestamp: str,
        score: float,
        point: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if score < self.config.score_enter:
            state.candidate_count = 0
            state.candidate_start_at = None
            state.candidate_max_score = 0.0
            return []

        state.candidate_count += 1
        state.candidate_max_score = max(state.candidate_max_score, score)
        if state.candidate_start_at is None:
            state.candidate_start_at = timestamp
        if state.candidate_count < self.config.min_consecutive_enter:
            return []

        start_at = state.candidate_start_at
        candidate_count = state.candidate_count
        candidate_max_score = state.candidate_max_score
        state.candidate_count = 0
        state.candidate_start_at = None
        state.candidate_max_score = 0.0
        event, is_merge = self._open_or_merge_event(
            state,
            asset_id,
            start_at,
            timestamp,
            score,
            candidate_max_score,
            candidate_count,
            point,
        )
        state.open_event = event
        state.exit_count = 0
        state.exit_start_at = None
        return [
            {
                "kind": "asset_event_merged" if is_merge else "asset_event_started",
                "event": event.copy(),
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
        point: dict[str, Any],
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
                    point,
                    sample_count=candidate_count,
                    max_score=candidate_max_score,
                )
                return previous, True

        model_version = str(point.get("anomalyModel") or "unknown")
        event = {
            "id": f"AI2-EVENT-{uuid4()}",
            "assetId": asset_id,
            "startAt": start_at,
            "endAt": None,
            "status": "open",
            "endReason": None,
            "maxScore": round(candidate_max_score),
            "lastScore": round(score),
            "sampleCount": candidate_count,
            "mergeCount": 0,
            "thresholdVersion": self.config.rule_version,
            "modelVersion": model_version,
            "maxScoreModelVersion": model_version,
            "classification": "asset_anomaly",
        }
        return event, False

    def _process_open_event(
        self, state: _AssetState, timestamp: str, score: float, point: dict[str, Any]
    ) -> list[dict[str, Any]]:
        event = state.open_event
        if event is None:
            return []
        self._update_open_event(event, score, point)
        if score >= self.config.score_exit:
            state.exit_count = 0
            state.exit_start_at = None
            return [{"kind": "asset_event_updated", "event": event.copy()}]

        state.exit_count += 1
        if state.exit_start_at is None:
            state.exit_start_at = timestamp
        if state.exit_count < self.config.min_consecutive_exit:
            return [{"kind": "asset_event_updated", "event": event.copy()}]

        event["endAt"] = state.exit_start_at
        event["status"] = "closed"
        event["endReason"] = "score_recovered"
        state.latest_closed_event = event
        state.open_event = None
        state.exit_count = 0
        state.exit_start_at = None
        return [{"kind": "asset_event_closed", "event": event.copy()}]

    def _suppress_for_sensor_fault(
        self,
        state: _AssetState,
        asset_id: str,
        timestamp: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        state.candidate_count = 0
        state.candidate_start_at = None
        state.candidate_max_score = 0.0
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
                {"kind": "asset_event_closed", "event": state.open_event.copy()}
            )
            state.open_event = None
        # A sensor failure breaks the continuity of an otherwise mergeable
        # anomaly. It must not be possible to erase this audit boundary later.
        state.latest_closed_event = None
        return updates

    @staticmethod
    def _update_open_event(
        event: dict[str, Any],
        score: float,
        point: dict[str, Any],
        *,
        sample_count: int = 1,
        max_score: float | None = None,
    ) -> None:
        event["lastScore"] = round(score)
        next_max_score = round(max_score if max_score is not None else score)
        if next_max_score > int(event["maxScore"]):
            event["maxScore"] = next_max_score
            event["maxScoreModelVersion"] = str(
                point.get("anomalyModel") or "unknown"
            )
        event["sampleCount"] = int(event["sampleCount"]) + sample_count
