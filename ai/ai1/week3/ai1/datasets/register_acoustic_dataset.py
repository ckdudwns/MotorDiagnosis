"""AI1-01: MIMII WAV -> machine-held-out, inline acoustic feature manifest.

This is a local trusted dataset import, not a device-label authorization path.
Public binary labels do not establish detailed fault types or field calibration.
"""

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from register_dataset import (
    DEFAULT_SPLIT_RATIOS,
    LABEL_POLICY_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    FeatureConfig,
    _import_module_from_path,
    _validate_finite_features,
    compute_feature_output_fingerprint,
    compute_peak_frequency,
    extract_all_features,
    group_split,
    sha256_of_file,
    summarize_dataset_labels,
    validate_split_ratios,
)

_AI1_ROOT = Path(__file__).resolve().parents[3]
_loader = _import_module_from_path(
    "ai1_acoustic.mimii_loader",
    str(_AI1_ROOT / "week1/ai1/scripts/load_mimii_acoustic.py"),
)
_versions = _import_module_from_path(
    "ai1_acoustic.dataset_version",
    str(_AI1_ROOT / "week4/ai1/dataset_versions/dataset_version.py"),
)
LABEL_MAPPING = {"NORMAL": "NORMAL", "PUMP_ANOMALY": "ANOMALY"}
TAXONOMY_VERSION = "MIMII-PUMP-BINARY-V1"
SIGNAL_UNIT = "normalized_pcm"
PIPELINE_VERSION = "acoustic.week2_features+peak.v1"


def build_acoustic_manifest(
    pump_dir,
    *,
    source_uri,
    license_note,
    operating_conditions,
    window_size=2048,
    hop_size=2048,
    n_mfcc=13,
    split_ratios=None,
    seed=42,
):
    """Import channel 0 with no resampling or inferred physical units.

    All windows of a machine stay in one split; insufficient machines fail rather
    than falling back to window-level leakage. Each row points to a WAV sample span.
    """
    for name, value in (("source_uri", source_uri), ("license_note", license_note)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonblank string")
    if not isinstance(operating_conditions, dict) or not operating_conditions:
        raise ValueError("Explicit operating_conditions are required")
    json.dumps(operating_conditions, allow_nan=False)
    for name, value in (("window_size", window_size), ("hop_size", hop_size)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if hop_size > window_size:
        raise ValueError("hop_size cannot exceed window_size")
    if type(n_mfcc) is not int or n_mfcc < 0:
        raise ValueError(
            "n_mfcc must be a nonnegative integer (0 explicitly omits MFCC)"
        )
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    ratios = copy.deepcopy(
        DEFAULT_SPLIT_RATIOS if split_ratios is None else split_ratios
    )
    validate_split_ratios(ratios)
    root = Path(pump_dir).resolve()
    before = {
        path.relative_to(root).as_posix(): sha256_of_file(str(path))
        for path in sorted(root.glob("id_*/*/*.wav"))
    }
    records = _loader.load_mimii_pump_dataset(str(root))
    if not records:
        raise ValueError("No MIMII pump WAV recordings found")
    records.sort(key=lambda record: record["source_file"])
    sample_rates = {record["sample_rate"] for record in records}
    if len(sample_rates) != 1:
        raise ValueError(
            "Mixed sample rates require separate dataset versions; no implicit resampling"
        )
    sample_rate = int(next(iter(sample_rates)))
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    edges = tuple(
        sorted(
            {
                0,
                *(edge for edge in (500, 1000, 2000, 4000) if edge < sample_rate / 2),
                sample_rate / 2,
            }
        )
    )
    config = FeatureConfig(
        sample_rate=sample_rate,
        frame_length=window_size,
        hop_length=hop_size,
        n_mfcc=n_mfcc,
        band_edges=edges,
    )
    split_assignments = group_split(records, ratios, seed, group_key="machine_id")
    rows, files, fingerprints = [], {}, {}
    for record, split in zip(records, split_assignments):
        name = record["source_file"]
        digest = sha256_of_file(record["source_path"])
        if before.get(name) != digest:
            raise ValueError(f"Source changed during import: {name}")
        if digest in fingerprints and fingerprints[digest] != record["machine_id"]:
            raise ValueError(
                "Duplicate recording content across machines cannot establish independent holdout"
            )
        fingerprints[digest] = record["machine_id"]
        signal = np.asarray(record["signal"], dtype=np.float64)
        if (
            signal.ndim != 1
            or not np.isfinite(signal).all()
            or len(signal) < window_size
        ):
            raise ValueError(f"Invalid or too-short acoustic recording: {name}")
        label = record["label"]
        if label not in LABEL_MAPPING:
            raise ValueError(f"Unsupported source label: {label}")
        files[name] = {"sha256": digest, "label": label}
        for index, start in enumerate(
            range(0, len(signal) - window_size + 1, hop_size)
        ):
            window = signal[start : start + window_size]
            features = extract_all_features(window, config)
            features["acoustic_peak_hz"] = compute_peak_frequency(window, sample_rate)
            _validate_finite_features(features, name)
            rows.append(
                {
                    "sample_id": f"{record['sample_id']}_{index:06d}",
                    "source_file": name,
                    "specimen_id": record["machine_id"],
                    "known_label": label,
                    "common_label": LABEL_MAPPING[label],
                    "split": split,
                    "sample_rate_hz": sample_rate,
                    "rpm": None,
                    "modality": "acoustic",
                    "signal_unit": SIGNAL_UNIT,
                    "source_ref": f"{name}#samples={start}:{start + window_size}",
                    "window_index": index,
                    "window_start_sample": start,
                    "window_end_sample": start + window_size,
                    **features,
                }
            )
    criteria_path = _AI1_ROOT / "week1/ai1/labels/acoustic_label_criteria.md"
    manifest = {
        "name": "mimii-pump-acoustic-v1",
        "status": "draft",
        "source": {
            "type": "external",
            "uri": source_uri.strip(),
            "license": license_note.strip(),
            "files": files,
        },
        "compatibility": {
            "signalType": ["acoustic"],
            "samplingRateHz": sample_rate,
            "units": {"acoustic": SIGNAL_UNIT},
            "channel": 0,
            "operatingConditions": copy.deepcopy(operating_conditions),
        },
        "labelTaxonomyVersion": TAXONOMY_VERSION,
        "labelMapping": dict(LABEL_MAPPING),
        "labelPolicyVersion": LABEL_POLICY_VERSION,
        "snapshotSchemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "labelCriteria": {
            "version": TAXONOMY_VERSION,
            "sourceLabels": dict(_loader.LABEL_MAP),
            "reference": "week1/ai1/labels/acoustic_label_criteria.md",
            "referenceChecksum": sha256_of_file(str(criteria_path)),
            "scope": "Public normal/abnormal binary labels only; detailed fault taxonomy remains unconfirmed.",
        },
        "featureNames": sorted(features),
        "featureOutputFingerprint": compute_feature_output_fingerprint(rows),
        "checksumInputs": {
            "windowSize": window_size,
            "hopSize": hop_size,
            "seed": seed,
            "featurePipelineVersion": PIPELINE_VERSION,
            "manifestContextVersion": 1,
            "featureConfig": {
                "sampleRate": sample_rate,
                "frameLength": window_size,
                "hopLength": hop_size,
                "nMfcc": n_mfcc,
                "bandEdges": list(edges),
            },
            "splitStrategyKey": "machine_group",
        },
        "split": ratios,
        "splitStrategy": "machine_group: all recordings/windows of each machine stay together",
        "holdoutType": "machine",
        "independentHoldout": True,
        "rowCount": len(rows),
        "splitCounts": {
            split: sum(row["split"] == split for row in rows) for split in ratios
        },
        "rows": rows,
        "reason": "AI1-01 acoustic candidate input; not field deployment approval",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    manifest.update(summarize_dataset_labels(rows, LABEL_MAPPING, TAXONOMY_VERSION))
    checksum = _versions.compute_source_checksum(manifest)
    manifest["source"]["checksum"] = checksum
    manifest["id"] = "DS-MIMII-ACOUSTIC-" + checksum.split(":")[1][:12]
    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Register acoustic WAV features with machine-held-out splits"
    )
    parser.add_argument("--pump-dir", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--license-note", required=True)
    parser.add_argument(
        "--operating-conditions",
        required=True,
        help="JSON object describing acquisition conditions",
    )
    parser.add_argument("--window-size", type=int, default=2048)
    parser.add_argument("--hop-size", type=int, default=2048)
    parser.add_argument(
        "--without-mfcc",
        action="store_true",
        help="Create an explicit no-MFCC feature schema",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    manifest = build_acoustic_manifest(
        args.pump_dir,
        source_uri=args.source_uri,
        license_note=args.license_note,
        operating_conditions=json.loads(args.operating_conditions),
        window_size=args.window_size,
        hop_size=args.hop_size,
        n_mfcc=0 if args.without_mfcc else 13,
    )
    if args.freeze:
        manifest = _versions.freeze_dataset_version(manifest)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(
        f"{manifest['id']}: {manifest['rowCount']} acoustic windows ({manifest['status']})"
    )


if __name__ == "__main__":
    main()
