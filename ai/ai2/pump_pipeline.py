"""Offline pump-summary experiments. No operational ingestion or label writes."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
LABELS = ("물 수위(L)", "슬러지 유무", "이벤트")
VERSION = "pump-summary-experiment-v1"
WINDOW_ROWS = 12
EXPECTED_INTERVAL_SEC = 5
FORECAST_HORIZON_SEC = 300
FORECAST_STEPS = FORECAST_HORIZON_SEC // EXPECTED_INTERVAL_SEC


def temporal_windows(records: list) -> tuple[list, np.ndarray]:
    """Use past-only, exactly five-second windows within one split."""
    history, retained, vectors = [], [], []
    previous = None
    for item in records:
        at, values, _ = item
        if (
            previous is not None
            and (at - previous).total_seconds() != EXPECTED_INTERVAL_SEC
        ):
            history.clear()
        previous = at
        history.append(values)
        history = history[-WINDOW_ROWS:]
        if len(history) < WINDOW_ROWS:
            continue
        window = np.array(history)
        vectors.append(
            np.concatenate(
                (
                    window[-1],
                    window.mean(axis=0),
                    window.std(axis=0),
                    window[-1] - window[0],
                )
            )
        )
        retained.append(item)
    return retained, np.array(vectors)


def window_vector(history: list[np.ndarray]) -> np.ndarray:
    """Build the serving vector using the same past-only transform as training."""
    if len(history) != WINDOW_ROWS:
        raise ValueError(f"Exactly {WINDOW_ROWS} observations are required")
    window = np.array(history)
    return np.concatenate(
        (window[-1], window.mean(axis=0), window.std(axis=0), window[-1] - window[0])
    )


@dataclass
class _RuntimeState:
    history: list[tuple[datetime, int, np.ndarray]] = field(default_factory=list)
    pending_forecasts: dict[str, dict] = field(default_factory=dict)


class FixedPumpAnalyzer:
    """Fixed-model, per-sensor sequential analysis for five-second summaries.

    This object never changes model parameters.  A missing timestamp, sequence
    discontinuity, invalid-quality measurement, or non-finite value clears only
    that sensor's recent history; it is not converted into a pump fault.
    """

    def __init__(self, model: dict) -> None:
        validate_model(model)
        self.model = model
        self.states: dict[str, _RuntimeState] = {}

    def ingest(
        self,
        sensor_id: str,
        measured_at: str | datetime,
        sequence: int,
        values: list[float] | np.ndarray,
        *,
        quality_ok: bool = True,
        source_document_id: str | None = None,
    ) -> dict:
        """Add one real measurement and optionally return a next-5-second forecast."""
        if not isinstance(sensor_id, str) or not sensor_id.strip():
            raise ValueError("sensor_id must be a non-empty string")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        at = timestamp(measured_at) if isinstance(measured_at, str) else measured_at
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("measured_at must include timezone")
        at = at.astimezone(timezone.utc)
        numeric = np.asarray(values, dtype=float)
        if (
            numeric.shape != (len(self.model["features"]),)
            or not np.isfinite(numeric).all()
        ):
            raise ValueError("values must be finite and match the model feature order")
        sensor_id = sensor_id.strip()
        state = self.states.setdefault(sensor_id, _RuntimeState())
        result = {
            "sensorId": sensor_id,
            "timestamp": at.isoformat(),
            "sequence": sequence,
            "sourceDocumentId": source_document_id,
            "modelVersion": self.model["modelType"],
            "status": "warming_up",
        }
        previous = state.history[-1] if state.history else None
        continuous = (
            previous is not None
            and (at - previous[0]).total_seconds() == EXPECTED_INTERVAL_SEC
            and sequence == previous[1] + 1
        )
        if not quality_ok or (previous is not None and not continuous):
            state.history.clear()
            state.pending_forecasts.clear()
            result["historyResetReason"] = (
                "quality_invalid"
                if not quality_ok
                else "timestamp_or_sequence_discontinuity"
            )
        if not quality_ok:
            return result
        pending = state.pending_forecasts.pop(at.isoformat(), None)
        if pending is not None:
            predicted = np.array(pending["values"])
            scale = np.array(pending["targetStd"])
            error = np.abs(predicted - numeric)
            result["previousForecast"] = {
                "predictedFor": at.isoformat(),
                "rawMae": float(np.mean(error)),
                "standardizedMae": float(np.mean(error / scale)),
            }
            state.pending_forecast = None
        state.history.append((at, sequence, numeric))
        state.history = state.history[-WINDOW_ROWS:]
        result["historyCount"] = len(state.history)
        stream = self.model["streams"].get(sensor_id)
        if stream is None:
            result["status"] = "model_not_available_for_sensor"
            return result
        if len(state.history) < WINDOW_ROWS:
            return result
        vector = window_vector([item[2] for item in state.history])
        robust, pca = predict(stream, vector.reshape(1, -1))
        forecast = stream.get("forecast")
        result.update(
            {
                "status": "analyzed",
                "robustDeviation": float(robust[0]),
                "pcaResidual": float(pca[0]),
                "referenceExceeded": bool(robust[0] > stream["thresholds"][0]),
                "anomalyScore": round(
                    min(100.0, 75.0 * robust[0] / stream["thresholds"][0])
                ),
            }
        )
        if forecast is not None:
            estimate = (vector - np.array(forecast["inputMean"])) / np.array(
                forecast["inputStd"]
            ) @ np.array(forecast["weights"]) * np.array(
                forecast["targetStd"]
            ) + np.array(
                forecast["targetMean"]
            )
            predicted_at = (at + timedelta(seconds=FORECAST_HORIZON_SEC)).isoformat()
            state.pending_forecasts[predicted_at] = {
                "predictedFor": predicted_at,
                "values": estimate.tolist(),
                "targetStd": forecast["targetStd"],
            }
            result["nextForecast"] = {
                "predictedFor": predicted_at,
                "features": estimate.tolist(),
                "horizonSec": FORECAST_HORIZON_SEC,
            }
        return result


def validate_model(model: dict) -> None:
    """Reject malformed fixed artifacts before serving any measurement."""
    if not isinstance(model, dict) or model.get("modelType") != VERSION:
        raise ValueError("Unsupported pump model artifact")
    if (
        model.get("windowRows") != WINDOW_ROWS
        or model.get("intervalSec") != EXPECTED_INTERVAL_SEC
    ):
        raise ValueError("Model does not use the required 12-row, five-second contract")
    if model.get("forecastHorizonSec") != FORECAST_HORIZON_SEC:
        raise ValueError("Model does not use the required five-minute forecast horizon")
    features, streams = model.get("features"), model.get("streams")
    if not isinstance(features, list) or not features or not isinstance(streams, dict):
        raise ValueError("Model is missing features or sensor streams")


def load_model(path: Path) -> dict:
    """Load a saved, immutable model artifact; this function never fits a model."""
    model = json.loads(path.read_text(encoding="utf-8"))
    validate_model(model)
    return model


def timestamp(value: str) -> datetime:
    """Require an explicit offset; uptime is not a wall clock."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def read_workbook(path: Path) -> dict[str, list[dict[str, str]]]:
    """Read bounded XLSX cells without executing formulas or changing originals."""
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 512 * 1024**2:
            raise ValueError("Workbook exceeds 512 MiB uncompressed limit")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(f"{NS}si")]
        relations = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            if item.attrib.get("TargetMode") != "External"
        }
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        result = {}
        for sheet in workbook.findall(f"{NS}sheets/{NS}sheet"):
            target = relations[sheet.attrib[REL + "id"]]
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(member).parts:
                raise ValueError("Invalid sheet path")
            rows, header = [], None
            with archive.open(member) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag != NS + "row":
                        continue
                    values = {}
                    for cell in element.findall(NS + "c"):
                        column = "".join(c for c in cell.attrib["r"] if c.isalpha())
                        if cell.find(NS + "f") is not None:
                            raise ValueError(
                                "Formula cells are not accepted as measurements"
                            )
                        value = cell.findtext(NS + "v", "")
                        kind = cell.attrib.get("t")
                        if kind == "s":
                            value = shared[int(value)]
                        elif kind == "inlineStr":
                            value = "".join(n.text or "" for n in cell.iter(NS + "t"))
                        values[column] = value
                    if header is None:
                        header = values
                        if len(set(header.values())) != len(header):
                            raise ValueError("Duplicate column names")
                    elif any(values.values()):
                        rows.append(
                            {name: values.get(col, "") for col, name in header.items()}
                        )
                    element.clear()
            result[sheet.attrib["name"]] = rows
        return result


def audit(sheets: dict[str, list[dict[str, str]]]) -> dict:
    """Summarize actual content; blank labels never imply normality."""
    result = {"schemaVersion": 1, "sheets": {}, "domainValidated": False}
    for name, rows in sheets.items():
        if not rows or "_document_id" not in rows[0]:
            result["sheets"][name] = {
                "measurementRows": 0,
                "descriptionRows": len(rows),
            }
            continue
        times, delays, bad_times = [], [], 0
        for row in rows:
            try:
                captured = timestamp(row["createdAt"])
                times.append(captured)
                delays.append((timestamp(row["receivedAt"]) - captured).total_seconds())
            except (KeyError, ValueError, TypeError):
                bad_times += 1
        ordered = sorted(set(times))
        intervals = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:])]
        numeric = {}
        for field in rows[0]:
            if not field.startswith(
                ("rms_", "p2p_", "cf_", "sk_", "ku_", "audio_", "imu_")
            ):
                continue
            values = []
            for row in rows:
                try:
                    value = float(row[field])
                    if np.isfinite(value):
                        values.append(value)
                except (ValueError, TypeError, OverflowError):
                    pass
            numeric[field] = {
                "finiteCount": len(values),
                "missingOrInvalidCount": len(rows) - len(values),
                "zeroCount": sum(v == 0 for v in values),
                "min": min(values, default=None),
                "max": max(values, default=None),
                "unit": "unverified",
            }
        ids = Counter(row["_document_id"] for row in rows)
        result["sheets"][name] = {
            "measurementRows": len(rows),
            "labels": {
                field: dict(Counter(row.get(field, "") for row in rows))
                for field in LABELS
            },
            "topics": dict(Counter(row.get("mqtt_topic", "") for row in rows)),
            "firstCapturedAt": ordered[0].isoformat() if ordered else None,
            "lastCapturedAt": ordered[-1].isoformat() if ordered else None,
            "invalidTimestamps": bad_times,
            "duplicateDocumentIds": sum(count - 1 for count in ids.values()),
            "duplicateCaptureTimes": len(times) - len(ordered),
            "medianIntervalSec": float(np.median(intervals)) if intervals else None,
            "gapsOver10Sec": sum(value > 10 for value in intervals),
            "maxReceiptDelaySec": max(delays, default=None),
            "features": numeric,
        }
    return result


def fit_model(matrix: np.ndarray) -> dict:
    """Fit robust marginal baseline and PCA reconstruction reference on train only."""
    if matrix.ndim != 2 or len(matrix) < 30 or not np.isfinite(matrix).all():
        raise ValueError("At least 30 finite training observations required")
    center = np.median(matrix, axis=0)
    scale = np.median(np.abs(matrix - center), axis=0) * 1.4826
    # Constant columns cannot supply a stable learned scale; drop, never fabricate it.
    active = np.flatnonzero(scale > 1e-12)
    if not len(active):
        raise ValueError("No non-constant robust features")
    z = np.clip((matrix[:, active] - center[active]) / scale[active], -20, 20)
    mean = z.mean(axis=0)
    _, singular, axes = np.linalg.svd(z - mean, full_matrices=False)
    fraction = np.cumsum(singular**2) / np.sum(singular**2)
    rank = max(1, min(int(np.searchsorted(fraction, 0.9)) + 1, max(1, len(active) - 1)))
    return {
        "center": center.tolist(),
        "scale": scale.tolist(),
        "active": active.tolist(),
        "pcaMean": mean.tolist(),
        "pcaAxes": axes[:rank].tolist(),
    }


def predict(model: dict, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shared batch/serving math; quantities are deviations, not failure probabilities."""
    center, scale = np.array(model["center"]), np.array(model["scale"])
    if (
        matrix.ndim != 2
        or matrix.shape[1] != len(center)
        or not np.isfinite(matrix).all()
    ):
        raise ValueError("Invalid feature matrix")
    active = model["active"]
    z = (matrix[:, active] - center[active]) / scale[active]
    robust = np.max(np.abs(z), axis=1)
    centered = np.clip(z, -20, 20) - np.array(model["pcaMean"])
    axes = np.array(model["pcaAxes"])
    residual = centered - (centered @ axes.T) @ axes
    return robust, np.mean(residual**2, axis=1)


def forecast_experiment(
    groups: list, matrices: list[np.ndarray]
) -> tuple[dict | None, dict]:
    """Predict the summary five minutes ahead; compare with last-value persistence."""
    pairs = []
    for group, matrix in zip(groups, matrices):
        indices = [
            i
            for i in range(len(group) - FORECAST_STEPS)
            if (group[i + FORECAST_STEPS][0] - group[i][0]).total_seconds()
            == FORECAST_HORIZON_SEC
        ]
        pairs.append(
            (
                matrix[indices],
                np.array([group[i + FORECAST_STEPS][1] for i in indices]),
                np.array([group[i][1] for i in indices]),
            )
        )
    if any(len(x) < 30 for x, _, _ in pairs):
        return None, {"status": "insufficient_five_second_pairs"}
    x, y, _ = pairs[0]
    xmean, xstd = x.mean(axis=0), x.std(axis=0)
    ymean, ystd = y.mean(axis=0), y.std(axis=0)
    xstd = np.where(xstd > 1e-12, xstd, 1.0)
    ystd = np.where(ystd > 1e-12, ystd, 1.0)
    z = (x - xmean) / xstd
    target = (y - ymean) / ystd
    candidates = []
    for alpha in (1.0, 10.0, 100.0):
        weights = np.linalg.solve(z.T @ z + alpha * np.eye(z.shape[1]), z.T @ target)
        vx, vy, _ = pairs[1]
        estimate = ((vx - xmean) / xstd) @ weights * ystd + ymean
        candidates.append(
            (float(np.mean(np.abs(estimate - vy) / ystd)), alpha, weights)
        )
    _, alpha, weights = min(candidates, key=lambda item: item[0])
    tx, ty, last = pairs[2]
    estimate = ((tx - xmean) / xstd) @ weights * ystd + ymean
    mae = float(np.mean(np.abs(estimate - ty) / ystd))
    persistence = float(np.mean(np.abs(last - ty) / ystd))
    artifact = {
        "type": "ridge",
        "horizonSec": FORECAST_HORIZON_SEC,
        "alpha": alpha,
        "inputMean": xmean.tolist(),
        "inputStd": xstd.tolist(),
        "targetMean": ymean.tolist(),
        "targetStd": ystd.tolist(),
        "weights": weights.tolist(),
        "operationallyApproved": False,
    }
    metrics = {
        "status": "experimental",
        "horizonSec": FORECAST_HORIZON_SEC,
        "testPairs": len(tx),
        "testStandardizedMAE": mae,
        "persistenceStandardizedMAE": persistence,
        "beatsPersistence": mae < persistence,
        "failurePrediction": False,
    }
    return artifact, metrics


def experiment(sheets: dict, features: list[str]) -> tuple[dict, dict, list[dict]]:
    """Time-held-out exploratory training. No healthy/faulty truth is inferred."""
    if not features or len(features) != len(set(features)):
        raise ValueError("Explicit unique feature names are required")
    if any(
        not name.startswith(("rms_", "p2p_", "cf_", "sk_", "ku_", "audio_", "imu_"))
        for name in features
    ):
        raise ValueError(
            "Only measurement columns are permitted; labels/identifiers are excluded"
        )
    streams, all_times = {}, []
    for sheet, rows in sheets.items():
        if not rows or "_document_id" not in rows[0]:
            continue
        if any(field not in rows[0] for field in features):
            raise ValueError("Feature absent from " + sheet)
        valid, identities, seen_times = [], set(), set()
        for row in rows:
            key = row["_document_id"]
            if key in identities:
                raise ValueError("Duplicate document identity; resolve before learning")
            identities.add(key)
            try:
                at = timestamp(row["createdAt"])
                values = [float(row[field]) for field in features]
                if not np.isfinite(values).all():
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            if at in seen_times:
                raise ValueError(
                    "Ambiguous duplicate capture time; resolve before learning"
                )
            seen_times.add(at)
            valid.append((at, values, key))
        streams[sheet] = sorted(valid)
        all_times.extend(item[0] for item in valid)
    if not all_times:
        raise ValueError("No finite measurement rows")
    # Shared absolute cuts prevent simultaneous sensors crossing different splits.
    nonempty = [records for records in streams.values() if records]
    first = max(records[0][0] for records in nonempty)
    last = min(records[-1][0] for records in nonempty)
    if last <= first:
        raise ValueError("Sensors have no overlapping observation period")
    cut1, cut2 = first + (last - first) * 0.6, first + (last - first) * 0.8
    artifacts = {
        "schemaVersion": 2,
        "modelType": VERSION,
        "features": features,
        "mode": "offline_experiment",
        "domainValidated": False,
        "unit": "unverified_source_values",
        "streams": {},
        "windowRows": WINDOW_ROWS,
        "intervalSec": EXPECTED_INTERVAL_SEC,
        "forecastHorizonSec": FORECAST_HORIZON_SEC,
        "derivedFeatures": [
            name + ":" + statistic
            for statistic in ("last", "mean", "std", "delta")
            for name in features
        ],
    }
    report = {
        "commonCoverageFrom": first.isoformat(),
        "commonCoverageTo": last.isoformat(),
        "trainBefore": cut1.isoformat(),
        "testFrom": cut2.isoformat(),
        "groundTruthMetrics": None,
        "streams": {},
        "limitation": "Unlabeled observations. Exceedances are not confirmed faults or false positives.",
    }
    predictions = []
    for sensor, records in streams.items():
        records = [item for item in records if item[0] >= first]
        groups = [
            [item for item in records if predicate(item[0])]
            for predicate in (
                lambda t: t < cut1,
                lambda t: cut1 <= t < cut2,
                lambda t: t >= cut2,
            )
        ]
        windows = [temporal_windows(group) for group in groups]
        groups = [group for group, _ in windows]
        counts = dict(zip(("train", "calibration", "test"), map(len, groups)))
        if min(counts.values()) < 30:
            report["streams"][sensor] = {
                "counts": counts,
                "status": "insufficient_temporal_coverage",
            }
            continue
        matrices = [matrix for _, matrix in windows]
        model = fit_model(matrices[0])
        calibration = predict(model, matrices[1])
        thresholds = [
            max(float(np.quantile(scores, 0.99)), 1e-12) for scores in calibration
        ]
        model["thresholds"] = thresholds
        artifacts["streams"][sensor] = model
        forecast, forecast_metrics = forecast_experiment(groups, matrices)
        model["forecast"] = forecast
        test_scores = predict(model, matrices[2])
        report["streams"][sensor] = {
            "counts": counts,
            "status": "experimental",
            "forecast": forecast_metrics,
            "droppedConstantFeatures": [
                name
                for i, name in enumerate(artifacts["derivedFeatures"])
                if i not in model["active"]
            ],
            "testExceedanceFraction": {
                name: float(np.mean(scores > threshold))
                for name, scores, threshold in zip(
                    ("robust", "pca"), test_scores, thresholds
                )
            },
        }
        for index, (at, _, key) in enumerate(groups[2]):
            predictions.append(
                {
                    "sensorId": sensor,
                    "sourceDocumentId": key,
                    "timestamp": at.isoformat(),
                    "mode": "offline_experiment",
                    "robustDeviation": float(test_scores[0][index]),
                    "anomalyScore": round(
                        min(100.0, 75.0 * test_scores[0][index] / thresholds[0])
                    ),
                    "pcaResidual": float(test_scores[1][index]),
                    "referenceExceeded": bool(test_scores[0][index] > thresholds[0]),
                    "groundTruthLabel": None,
                }
            )
    return (
        artifacts,
        report,
        sorted(predictions, key=lambda row: (row["timestamp"], row["sensorId"])),
    )


def replay_fixed_model(
    model: dict, sheets: dict[str, list[dict[str, str]]]
) -> tuple[list[dict], dict]:
    """Sequentially replay a workbook through an already trained artifact.

    The source workbook is never used for fitting.  Rows before ``testFrom`` are
    allowed only to warm the 12-row history; reported forecast comparisons begin
    at the fixed artifact's forward-test boundary.
    """
    validate_model(model)
    test_from = timestamp(model["evaluation"]["testFrom"])
    analyzer = FixedPumpAnalyzer(model)
    output, errors = [], []
    for sensor_id, rows in sheets.items():
        if sensor_id not in model["streams"]:
            continue
        prepared = []
        for ordinal, row in enumerate(rows):
            try:
                at = timestamp(row["createdAt"])
                values = [float(row[name]) for name in model["features"]]
                if not np.isfinite(values).all():
                    raise ValueError("non-finite values")
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                errors.append(
                    {"sensorId": sensor_id, "row": ordinal + 2, "error": str(error)}
                )
                continue
            prepared.append((at, values, row.get("_document_id")))
        # The supplied export's ``cnt`` is not a telemetry sequence (it repeats
        # across five-second observations).  Offline replay therefore assigns an
        # explicit replay-only order. Live adapters must pass device sequence.
        for replay_sequence, (at, values, document_id) in enumerate(sorted(prepared)):
            result = analyzer.ingest(
                sensor_id,
                at,
                replay_sequence,
                values,
                source_document_id=document_id,
            )
            if at >= test_from:
                result["sequenceSource"] = "replay_row_order"
                output.append(result)
    comparable = [
        row["previousForecast"] for row in output if "previousForecast" in row
    ]
    summary = {
        "schemaVersion": 1,
        "mode": "fixed_model_sequential_replay",
        "modelType": model["modelType"],
        "testFrom": test_from.isoformat(),
        "recordsReported": len(output),
        "forecastComparisons": len(comparable),
        "meanStandardizedMae": (
            float(np.mean([row["standardizedMae"] for row in comparable]))
            if comparable
            else None
        ),
        "invalidRowsSkipped": errors,
    }
    return output, summary


def replay_events(predictions: list[dict]) -> list[dict]:
    """Exercise existing lifecycle offline, keeping each sensor observation scoped.

    These are sensor observation candidates, never operational pump diagnoses.
    """
    from ai.ai2.week3.event_lifecycle import AnomalyEventLifecycle, EventLifecycleConfig

    lifecycle = AnomalyEventLifecycle(
        EventLifecycleConfig(min_duration_enter_sec=30, min_duration_exit_sec=30)
    )
    previous = {}
    events = []
    for row in predictions:
        sensor, at = row["sensorId"], timestamp(row["timestamp"])
        point = {
            "assetId": "EXPERIMENT-" + sensor,
            "timestamp": row["timestamp"],
            "anomalyScore": row["anomalyScore"],
            "anomalyModel": VERSION,
        }
        if (
            sensor in previous
            and (at - previous[sensor]).total_seconds() != EXPECTED_INTERVAL_SEC
        ):
            # Missing data breaks continuity, without manufacturing a sensor diagnosis.
            events.extend(
                lifecycle.process_point(
                    {**point, "anomalyScore": None},
                    sensor_fault=True,
                    sensor_fault_reason="observation_gap",
                )
            )
            previous[sensor] = at
            continue
        previous[sensor] = at
        events.extend(lifecycle.process_point(point))
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory; never overwrite a prior run",
    )
    parser.add_argument(
        "--exploratory-features",
        nargs="+",
        help="Explicit opt-in numeric columns with unverified units; never operational approval",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Fixed model.json to replay; does not train or update the model",
    )
    args = parser.parse_args()
    sheets = read_workbook(args.workbook)
    report = audit(sheets)
    report["sourceSha256"] = hashlib.sha256(args.workbook.read_bytes()).hexdigest()
    outputs = {"audit.json": report}
    if args.model and args.exploratory_features:
        parser.error("--model and --exploratory-features cannot be used together")
    if args.model:
        model = load_model(args.model)
        replay, replay_summary = replay_fixed_model(model, sheets)
        outputs.update(
            {
                "replay.json": replay,
                "replay-summary.json": replay_summary,
            }
        )
    elif args.exploratory_features:
        model, metrics, predictions = experiment(sheets, args.exploratory_features)
        model["sourceSha256"] = report["sourceSha256"]
        model["evaluation"] = {
            "trainBefore": metrics["trainBefore"],
            "testFrom": metrics["testFrom"],
        }
        outputs.update(
            {
                "model.json": model,
                "evaluation.json": metrics,
                "predictions.json": predictions,
            }
        )
        outputs["event-replay.json"] = replay_events(predictions)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, content in outputs.items():
        (args.output / name).write_text(
            json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {"output": str(args.output), "files": list(outputs)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
