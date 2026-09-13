"""Independent confirmation model for an immediately reported pump anomaly.

The model compares one event-time summary with the 24 summaries already held by
the server.  It is a novelty verifier, not a supervised fault classifier: until
review labels exist, ``possible_anomaly`` means unusual relative to the learned
baseline and recent history, not a confirmed mechanical fault.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np

from ai.ai2.pump_pipeline import (
    EXPECTED_INTERVAL_SEC,
    fit_model,
    physical_feature_violations,
    predict,
    select_cadence_records,
    timestamp,
)

MODEL_TYPE = "pump-event-verifier-v1"
HISTORY_ROWS = 24
CURRENT_EVENT_MAX_DELAY_SEC = EXPECTED_INTERVAL_SEC * 2


def verifier_vector(history: list[np.ndarray], current: np.ndarray) -> np.ndarray:
    """Encode 24 prior records and one immediate event record without future data."""
    if len(history) != HISTORY_ROWS:
        raise ValueError(f"Exactly {HISTORY_ROWS} prior records are required")
    window = np.array(history)
    if window.ndim != 2 or current.shape != (window.shape[1],):
        raise ValueError("History and event values must use the same feature shape")
    return np.concatenate(
        (
            current,
            window.mean(axis=0),
            window.std(axis=0),
            current - window[-1],
            current - window.mean(axis=0),
        )
    )


def _vectors(records: list) -> tuple[list, np.ndarray]:
    retained, vectors, history = [], [], []
    previous = None
    for item in records:
        at, values, _ = item
        if (
            previous is not None
            and (at - previous).total_seconds() != EXPECTED_INTERVAL_SEC
        ):
            history.clear()
        previous = at
        numeric = np.asarray(values, dtype=float)
        if len(history) == HISTORY_ROWS:
            vectors.append(verifier_vector(history, numeric))
            retained.append(item)
        history.append(numeric)
        history = history[-HISTORY_ROWS:]
    return retained, np.array(vectors)


def train_event_verifier(
    sheets: dict[str, list[dict[str, str]]], features: list[str]
) -> tuple[dict, dict]:
    """Train a separate, time-held-out novelty verifier from unlabeled summaries."""
    if not features or len(features) != len(set(features)):
        raise ValueError("Explicit unique feature names are required")
    streams: dict[str, list] = {}
    for sensor_id, rows in sheets.items():
        if not rows or "_document_id" not in rows[0]:
            continue
        valid = []
        for row in rows:
            try:
                valid.append(
                    (
                        timestamp(row["createdAt"]),
                        [float(row[name]) for name in features],
                        row["_document_id"],
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        valid = [item for item in valid if np.isfinite(item[1]).all()]
        streams[sensor_id] = select_cadence_records(sorted(valid))
    nonempty = [records for records in streams.values() if records]
    if not nonempty:
        raise ValueError("No finite measurement rows")
    first = max(records[0][0] for records in nonempty)
    last = min(records[-1][0] for records in nonempty)
    if last <= first:
        raise ValueError("Sensors have no overlapping observation period")
    cut1, cut2 = first + (last - first) * 0.6, first + (last - first) * 0.8
    artifact: dict[str, Any] = {
        "schemaVersion": 1,
        "modelType": MODEL_TYPE,
        "mode": "unlabeled_novelty_verifier",
        "features": features,
        "historyRows": HISTORY_ROWS,
        "intervalSec": EXPECTED_INTERVAL_SEC,
        "currentEventMaxDelaySec": CURRENT_EVENT_MAX_DELAY_SEC,
        "domainValidated": False,
        "streams": {},
    }
    report: dict[str, Any] = {
        "trainBefore": cut1.isoformat(),
        "testFrom": cut2.isoformat(),
        "groundTruthMetrics": None,
        "limitation": "No review labels. Scores are novelty evidence, not confirmed-fault probabilities.",
        "streams": {},
    }
    for sensor_id, records in streams.items():
        records = [item for item in records if item[0] >= first]
        groups = [
            [item for item in records if predicate(item[0])]
            for predicate in (
                lambda at: at < cut1,
                lambda at: cut1 <= at < cut2,
                lambda at: at >= cut2,
            )
        ]
        encoded = [_vectors(group) for group in groups]
        retained = [group for group, _ in encoded]
        counts = dict(zip(("train", "calibration", "test"), map(len, retained)))
        if min(counts.values()) < 30:
            report["streams"][sensor_id] = {
                "status": "insufficient_temporal_coverage",
                "counts": counts,
            }
            continue
        matrices = [matrix for _, matrix in encoded]
        model = fit_model(matrices[0])
        calibration = predict(model, matrices[1])
        model["thresholds"] = [
            max(float(np.quantile(scores, 0.99)), 1e-12) for scores in calibration
        ]
        artifact["streams"][sensor_id] = model
        test_scores = predict(model, matrices[2])
        report["streams"][sensor_id] = {
            "status": "experimental_unlabeled",
            "counts": counts,
            "testReferenceExceedanceFraction": {
                name: float(np.mean(scores > threshold))
                for name, scores, threshold in zip(
                    ("robust", "pca"), test_scores, model["thresholds"]
                )
            },
        }
    return artifact, report


class PumpEventVerifier:
    """Verify one immediate event against 24 ordered server-history records."""

    def __init__(self, model: dict[str, Any]) -> None:
        if (
            not isinstance(model, dict)
            or model.get("modelType") != MODEL_TYPE
            or model.get("historyRows") != HISTORY_ROWS
            or model.get("intervalSec") != EXPECTED_INTERVAL_SEC
            or not isinstance(model.get("features"), list)
            or not isinstance(model.get("streams"), dict)
        ):
            raise ValueError("Unsupported event-verifier artifact")
        self.model = model

    def verify(
        self,
        sensor_id: str,
        event_at: str,
        event_values: list[float],
        history: list[dict[str, Any]],
        *,
        quality_ok: bool,
    ) -> dict[str, Any]:
        """Return novelty evidence for an immediate edge-triggered event.

        Each history item must contain ``timestamp``, ``sequence`` and ``values``.
        The caller must query only records strictly before ``event_at``.
        """
        if not quality_ok:
            return {
                "decision": "likely_sensor_issue",
                "confidence": 100,
                "reason": "event_quality_invalid",
                "historicalRecordsUsed": 0,
            }
        if sensor_id not in self.model["streams"]:
            return {
                "decision": "insufficient_history",
                "confidence": None,
                "reason": "model_not_available_for_sensor",
                "historicalRecordsUsed": 0,
            }
        if len(history) != HISTORY_ROWS:
            return {
                "decision": "insufficient_history",
                "confidence": None,
                "reason": "requires_24_prior_records",
                "historicalRecordsUsed": len(history),
            }
        try:
            event_time = timestamp(event_at)
            ordered = []
            for item in history:
                at = timestamp(str(item["timestamp"]))
                sequence = item["sequence"]
                values = np.asarray(item["values"], dtype=float)
                if isinstance(sequence, bool) or not isinstance(sequence, int):
                    raise ValueError("invalid sequence")
                if (
                    values.shape != (len(self.model["features"]),)
                    or not np.isfinite(values).all()
                ):
                    raise ValueError("invalid history values")
                ordered.append((at, sequence, values))
            current = np.asarray(event_values, dtype=float)
            if (
                current.shape != (len(self.model["features"]),)
                or not np.isfinite(current).all()
            ):
                raise ValueError("invalid event values")
        except (KeyError, TypeError, ValueError, OverflowError):
            return {
                "decision": "insufficient_history",
                "confidence": None,
                "reason": "invalid_history_or_event_values",
                "historicalRecordsUsed": 0,
            }
        current_violations = physical_feature_violations(
            self.model["features"], current
        )
        if current_violations:
            return {
                "decision": "likely_sensor_issue",
                "confidence": 100,
                "reason": "input_physical_constraint_violation",
                "invalidFeatures": current_violations,
                "historicalRecordsUsed": HISTORY_ROWS,
            }
        if any(
            physical_feature_violations(self.model["features"], row[2])
            for row in ordered
        ):
            return {
                "decision": "insufficient_history",
                "confidence": None,
                "reason": "history_physical_constraint_violation",
                "historicalRecordsUsed": 0,
            }
        for previous, following in zip(ordered, ordered[1:]):
            if (
                following[0] - previous[0]
            ).total_seconds() != EXPECTED_INTERVAL_SEC or following[1] != previous[
                1
            ] + 1:
                return {
                    "decision": "insufficient_history",
                    "confidence": None,
                    "reason": "history_timestamp_or_sequence_discontinuity",
                    "historicalRecordsUsed": 0,
                }
        delay = (event_time - ordered[-1][0]).total_seconds()
        if not 0 < delay <= self.model["currentEventMaxDelaySec"]:
            return {
                "decision": "insufficient_history",
                "confidence": None,
                "reason": "event_timestamp_not_immediate",
                "historicalRecordsUsed": HISTORY_ROWS,
            }
        stream = self.model["streams"][sensor_id]
        robust, pca = predict(
            stream, verifier_vector([row[2] for row in ordered], current).reshape(1, -1)
        )
        thresholds = stream["thresholds"]
        exceeded = robust[0] > thresholds[0] or pca[0] > thresholds[1]
        confidence = round(
            min(100.0, max(robust[0] / thresholds[0], pca[0] / thresholds[1]) * 75.0)
        )
        return {
            "decision": "possible_anomaly" if exceeded else "not_confirmed_by_baseline",
            "confidence": confidence,
            "reason": "current_event_compared_with_24_prior_records",
            "historicalRecordsUsed": HISTORY_ROWS,
            "historySpanSec": (ordered[-1][0] - ordered[0][0]).total_seconds(),
            "robustDeviation": float(robust[0]),
            "pcaResidual": float(pca[0]),
            "referenceExceeded": bool(exceeded),
            "groundTruthAvailable": False,
        }
