#!/usr/bin/env python3
"""Convert CWRU .mat vibration recordings into AI2 telemetry features.

The generated records intentionally keep audio fields empty.  When the MIMII
audio data arrives, its feature records can be joined by timestamp and
scenario_label without changing the backend telemetry contract.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        "Python 3.12.x is required by the team development standard. "
        f"Current version: {sys.version.split()[0]}"
    )

import numpy as np
from scipy.io import loadmat

from telemetry_payload import to_external_payload


FILE_LABELS = {
    "97": ("normal", "normal"),
    "105": ("inner_race_fault", "vibration_anomaly"),
    "118": ("ball_fault", "vibration_anomaly"),
    "130": ("outer_race_fault", "vibration_anomaly"),
}

FEATURE_FIELDS = [
    "timestamp",
    "sequence",
    "site_id",
    "device_id",
    "asset_id",
    "source_file",
    "source",
    "sample_rate_hz",
    "window_seconds",
    "rpm",
    "known_condition",
    "scenario_label",
    "is_synthetic",
    "vibration_unit_note",
    "acoustic_unit_note",
    "vibration_rms_raw",
    "vibration_std_raw",
    "vibration_kurtosis",
    "vibration_peak_raw",
    "vibration_crest_factor",
    "vibration_peak_hz",
    "acoustic_rms_raw",
    "acoustic_peak_hz",
    "acoustic_spectral_centroid_hz",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("data/진동"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/ai2_week1"))
    parser.add_argument("--sample-rate", type=int, default=12_000)
    parser.add_argument("--window-seconds", type=float, default=2.0)
    parser.add_argument("--device-id", default="demo-motor-001")
    parser.add_argument("--asset-id", default="demo-pump-001")
    parser.add_argument("--site-id", default="SYN-SITE-01")
    parser.add_argument("--start-at", default="2026-08-24T09:00:00+09:00")
    return parser.parse_args()


def label_for(path: Path) -> tuple[str, str]:
    # The supplied filenames start with CWRU file IDs (97, 105, 118, 130).
    match = re.match(r"(97|105|118|130)(?:\D|$)", path.stem)
    if not match:
        raise ValueError(
            "Unsupported CWRU filename; add it to FILE_LABELS: " f"{path.name}"
        )
    return FILE_LABELS[match.group(1)]


def first_numeric_vector(data: dict[str, object]) -> np.ndarray:
    """Prefer the Drive End accelerometer series, then any time series."""
    keys = [key for key in data if not key.startswith("__")]
    preferred = [key for key in keys if key.endswith("_DE_time")]
    candidates = preferred or [key for key in keys if key.endswith("_time")]
    if not candidates:
        raise ValueError("No *_DE_time or *_time vibration series found")
    values = np.asarray(data[candidates[0]], dtype=float).reshape(-1)
    if not values.size:
        raise ValueError(f"Vibration series {candidates[0]} is empty")
    return values


def rpm_from(data: dict[str, object]) -> float | None:
    for key, value in data.items():
        if key.endswith("RPM"):
            numeric = np.asarray(value, dtype=float).reshape(-1)
            return float(numeric[0]) if numeric.size else None
    return None


def vibration_features(segment: np.ndarray, sample_rate: int) -> dict[str, float]:
    centered = segment - np.mean(segment)
    rms = float(np.sqrt(np.mean(np.square(centered))))
    std = float(np.std(centered))
    peak = float(np.max(np.abs(centered)))
    kurtosis = float(np.mean(np.power(centered, 4)) / (np.power(std, 4) + 1e-12))
    spectrum = np.abs(np.fft.rfft(centered))
    frequencies = np.fft.rfftfreq(centered.size, d=1 / sample_rate)
    # Ignore the DC term: it describes sensor offset, not rotating machinery.
    peak_hz = (
        float(frequencies[1 + np.argmax(spectrum[1:])]) if spectrum.size > 1 else 0.0
    )
    return {
        "vibration_rms_raw": round(rms, 8),
        "vibration_std_raw": round(std, 8),
        "vibration_kurtosis": round(kurtosis, 8),
        "vibration_peak_raw": round(peak, 8),
        "vibration_crest_factor": round(peak / (rms + 1e-12), 8),
        "vibration_peak_hz": round(peak_hz, 4),
    }


def main() -> None:
    args = parse_args()
    if args.sample_rate <= 0 or args.window_seconds <= 0:
        raise SystemExit("--sample-rate and --window-seconds must be positive")
    paths = sorted(
        args.input_dir.glob("*.mat"),
        # Keep the replay story readable: normal operation is replayed first.
        key=lambda path: (label_for(path)[1] != "normal", path.name),
    )
    if not paths:
        raise SystemExit(f"No .mat files found in {args.input_dir}")
    start_at = datetime.fromisoformat(args.start_at).astimezone(timezone.utc)
    samples_per_window = round(args.sample_rate * args.window_seconds)
    if samples_per_window < 2:
        raise SystemExit("Window is too short; it must contain at least two samples")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    manifest: list[dict[str, object]] = []
    sequence = 1
    for path in paths:
        condition, scenario = label_for(path)
        data = loadmat(path)
        signal = first_numeric_vector(data)
        rpm = rpm_from(data)
        window_count = signal.size // samples_per_window
        if not window_count:
            raise ValueError(f"{path.name} is shorter than one analysis window")
        manifest.append(
            {
                "source_file": path.name,
                "known_condition": condition,
                "scenario_label": scenario,
                "rpm": rpm,
                "sample_count": signal.size,
                "window_count": window_count,
                "discarded_samples": signal.size % samples_per_window,
            }
        )
        for index in range(window_count):
            start = index * samples_per_window
            end = (index + 1) * samples_per_window
            segment = signal[start:end]
            row: dict[str, object] = {
                "timestamp": (
                    start_at + timedelta(seconds=(sequence - 1) * args.window_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                "sequence": sequence,
                "site_id": args.site_id,
                "device_id": args.device_id,
                "asset_id": args.asset_id,
                "source_file": path.name,
                "source": "cwru_public_replay",
                "sample_rate_hz": args.sample_rate,
                "window_seconds": args.window_seconds,
                "rpm": round(rpm, 3) if rpm is not None else "",
                "known_condition": condition,
                "scenario_label": scenario,
                "is_synthetic": "true",
                "vibration_unit_note": "raw accelerometer output",
                "acoustic_unit_note": "",
                # Audio is deliberately absent until MIMII records are joined.
                "acoustic_rms_raw": "",
                "acoustic_peak_hz": "",
                "acoustic_spectral_centroid_hz": "",
            }
            row.update(vibration_features(segment, args.sample_rate))
            records.append(row)
            sequence += 1

    csv_path = args.output_dir / "vibration_features.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURE_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    jsonl_path = args.output_dir / "telemetry_replay.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            payload = to_external_payload(record)
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    manifest_path = args.output_dir / "dataset_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {"dataset": "CWRU", "records": manifest},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    label_mapping_path = args.output_dir / "label_mapping.csv"
    with label_mapping_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source_file",
                "known_condition",
                "scenario_label",
                "data_provenance",
            ],
        )
        writer.writeheader()
        writer.writerows(
            {
                "source_file": item["source_file"],
                "known_condition": item["known_condition"],
                "scenario_label": item["scenario_label"],
                "data_provenance": "cwru_public_vibration",
            }
            for item in manifest
        )
    print(f"Created {len(records)} telemetry records")
    print(f"Features: {csv_path}")
    print(f"Replay:   {jsonl_path}")
    print(f"Labels:   {label_mapping_path}")


if __name__ == "__main__":
    main()
