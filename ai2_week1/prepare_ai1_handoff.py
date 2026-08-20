#!/usr/bin/env python3
"""Prepare AI-1's synthetic handoff data for AI-2 analysis and replay paths."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        "Python 3.12.x is required by the team development standard. "
        f"Current version: {sys.version.split()[0]}"
    )


REQUIRED_COLUMNS = {
    "timestamp",
    "device_id",
    "asset_id",
    "rpm",
    "vibration_rms_raw",
    "vibration_peak_hz",
    "acoustic_rms_raw",
    "acoustic_peak_hz",
    "known_vibration_label",
    "known_acoustic_label",
    "scenario_label",
    "source",
    "is_synthetic",
    "vibration_unit_note",
    "acoustic_unit_note",
}
SCENARIOS = {
    "normal",
    "vibration_anomaly",
    "acoustic_anomaly",
    "combined_anomaly",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("ai1_week1/ai1/data/handoff/ai1_handoff_dataset.csv"),
        help="AI-1 handoff CSV path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/ai2_week1"),
        help="Directory for AI-2 analysis and replay outputs.",
    )
    parser.add_argument("--site-id", default="SYN-SITE-01")
    parser.add_argument("--replay-interval-sec", type=float, default=1.0)
    parser.add_argument(
        "--start-at",
        default=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        help="UTC ISO-8601 start time. Defaults to the current UTC time.",
    )
    return parser.parse_args()


def parse_float(value: str, field: str, row_number: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Row {row_number}: {field} must be numeric") from error


def read_and_validate_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"AI-1 handoff file not found: {path}")

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    if not rows:
        raise ValueError("AI-1 handoff CSV contains no data rows")

    for row_number, row in enumerate(rows, start=2):
        if row["scenario_label"] not in SCENARIOS:
            raise ValueError(
                f"Row {row_number}: unknown scenario_label "
                f"{row['scenario_label']!r}"
            )
        if row["is_synthetic"].lower() != "true":
            raise ValueError(f"Row {row_number}: is_synthetic must be True")
        for field in (
            "rpm",
            "vibration_rms_raw",
            "vibration_peak_hz",
            "acoustic_rms_raw",
            "acoustic_peak_hz",
        ):
            parse_float(row[field], field, row_number)
        if row.get("vibration_rms_mm_s") or row.get("acoustic_db"):
            raise ValueError(
                f"Row {row_number}: calibrated mm/s or dB must not be mixed "
                "into this synthetic raw-data replay"
            )
    return rows


def make_device_map(rows: list[dict[str, str]]) -> dict[str, str]:
    """Map each synthetic asset to one persistent synthetic device."""
    assets = sorted({row["asset_id"] for row in rows})
    return {
        asset_id: f"SYN-DEV-{index:02d}"
        for index, asset_id in enumerate(assets, start=1)
    }


def build_demo_order(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Create readable phases: normal → vibration → normal → acoustic → normal → combined."""
    by_scenario: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario_label"]].append(row)

    normal_rows = by_scenario["normal"]
    first_end = len(normal_rows) // 3
    second_end = first_end * 2
    return (
        normal_rows[:first_end]
        + by_scenario["vibration_anomaly"]
        + normal_rows[first_end:second_end]
        + by_scenario["acoustic_anomaly"]
        + normal_rows[second_end:]
        + by_scenario["combined_anomaly"]
    )


def build_replay_record(
    row: dict[str, str],
    sequence: int,
    timestamp: datetime,
    site_id: str,
    device_map: dict[str, str],
) -> dict[str, Any]:
    """Adapt internal snake_case CSV data to the external camelCase payload boundary."""
    return {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "siteId": site_id,
        "assetId": row["asset_id"],
        "deviceId": device_map[row["asset_id"]],
        "rpm": float(row["rpm"]),
        "vibrationRmsRaw": float(row["vibration_rms_raw"]),
        "vibrationPeakHz": float(row["vibration_peak_hz"]),
        "acousticRmsRaw": float(row["acoustic_rms_raw"]),
        "acousticPeakHz": float(row["acoustic_peak_hz"]),
        "scenarioLabel": row["scenario_label"],
        "knownVibrationLabel": row["known_vibration_label"],
        "knownAcousticLabel": row["known_acoustic_label"],
        "source": row["source"],
        "isSynthetic": True,
        "vibrationUnitNote": row["vibration_unit_note"],
        "acousticUnitNote": row["acoustic_unit_note"],
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.replay_interval_sec <= 0:
        raise SystemExit("--replay-interval-sec must be greater than zero")

    try:
        start_at = datetime.fromisoformat(args.start_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit("--start-at must be an ISO-8601 datetime") from error
    if start_at.tzinfo is None:
        raise SystemExit("--start-at must include a timezone offset or Z")
    start_at = start_at.astimezone(timezone.utc)

    rows = read_and_validate_rows(args.input)
    device_map = make_device_map(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    analysis_rows = []
    for row in rows:
        analysis_row = dict(row)
        analysis_row["ai2_device_id"] = device_map[row["asset_id"]]
        analysis_rows.append(analysis_row)
    analysis_path = args.output_dir / "ai1_analysis_dataset.csv"
    write_csv(analysis_path, analysis_rows)

    ordered_rows = build_demo_order(rows)
    replay_path = args.output_dir / "ai1_telemetry_replay.jsonl"
    with replay_path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(ordered_rows, start=1):
            timestamp = start_at + timedelta(
                seconds=(index - 1) * args.replay_interval_sec
            )
            record = build_replay_record(
                row, index, timestamp, args.site_id, device_map
            )
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "input": str(args.input),
        "site_id": args.site_id,
        "records": len(rows),
        "replay_interval_sec": args.replay_interval_sec,
        "start_at_utc": start_at.isoformat().replace("+00:00", "Z"),
        "device_map": device_map,
        "paths": {
            "analysis": str(analysis_path),
            "replay": str(replay_path),
        },
        "constraints": [
            "All records are public-data synthetic pairings.",
            "vibration_rms_mm_s and acoustic_db remain unavailable by design.",
            "Raw fields must not be displayed as mm/s RMS or dB SPL.",
        ],
    }
    manifest_path = args.output_dir / "ai1_handoff_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Validated {len(rows)} AI-1 handoff records")
    print(f"Analysis: {analysis_path}")
    print(f"Replay:   {replay_path}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
