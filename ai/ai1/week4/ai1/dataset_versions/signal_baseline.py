"""AI1-02: normal train-only acoustic/RPM statistics and draft registration.

No physical calibration, missing-RPM imputation, approval or deployment occurs.
RPM inputs are typed, curated measurement records, not arbitrary device labels.
"""

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from dataset_version import verify_frozen_integrity
from model_version import BaselineVersionRegistry


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _conditions(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("Explicit operating conditions are required")
    json.dumps(value, allow_nan=False)
    return copy.deepcopy(value)


def _build(values, *, dataset_id, site_id, asset_id, context, sample_refs, sigma):
    for name, value in (
        ("dataset_id", dataset_id),
        ("site_id", site_id),
        ("asset_id", asset_id),
    ):
        _required_text(value, name)
    if (
        isinstance(sigma, bool)
        or not isinstance(sigma, (int, float))
        or not math.isfinite(sigma)
        or sigma <= 0
    ):
        raise ValueError("sigma must be a positive finite number")
    if len(sample_refs) < 2:
        raise ValueError("At least two eligible normal train measurements are required")
    if len(set(sample_refs)) != len(sample_refs):
        raise ValueError("Duplicate sample references would bias baseline statistics")
    features = {}
    for name, column in values.items():
        if len(column) != len(sample_refs) or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in column
        ):
            raise ValueError(f"Invalid or missing baseline feature: {name}")
        array = np.asarray(column, dtype=np.float64)
        mean, std = float(array.mean()), float(array.std())
        features[name] = {
            "mean": mean,
            "std": std,
            "min": float(array.min()),
            "max": float(array.max()),
            "normal_range": [mean - sigma * std, mean + sigma * std],
        }
    if not features:
        raise ValueError("No baseline features")
    result = {
        "datasetId": dataset_id,
        "siteId": site_id,
        "assetId": asset_id,
        "features": features,
        "signalContext": {
            **copy.deepcopy(context),
            "sampleRefs": sorted(sample_refs),
            "sampleCount": len(sample_refs),
            "split": "train",
            "sigmaMultiplier": sigma,
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(result, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    result["id"] = f"BASE-{context['modality'].upper()}-{fingerprint[:16]}"
    return result


def build_acoustic_baseline(frozen_manifest, *, site_id, asset_id, sigma=3.0):
    """Use the frozen acoustic dataset's verified NORMAL train windows only."""
    verify_frozen_integrity(frozen_manifest)
    compatibility = frozen_manifest.get("compatibility", {})
    if compatibility.get("signalType") != ["acoustic"]:
        raise ValueError("An acoustic-only frozen manifest is required")
    unit = _required_text(
        compatibility.get("units", {}).get("acoustic"), "acoustic unit"
    )
    conditions = _conditions(compatibility.get("operatingConditions"))
    names = frozen_manifest.get("featureNames")
    if not isinstance(names, list) or not names or len(set(names)) != len(names):
        raise ValueError("Explicit unique featureNames are required")
    rows = [
        row
        for row in frozen_manifest["rows"]
        if row.get("split") == "train"
        and frozen_manifest.get("labelMapping", {}).get(row.get("known_label"))
        == "NORMAL"
        and row.get("common_label") == "NORMAL"
    ]
    for row in rows:
        if (
            row.get("modality") != "acoustic"
            or row.get("signal_unit") != unit
            or row.get("sample_rate_hz") != compatibility.get("samplingRateHz")
        ):
            raise ValueError("Acoustic units/modality/sample rate cannot be mixed")
    refs = [_required_text(row.get("source_ref"), "source_ref") for row in rows]
    return _build(
        {name: [row.get(name) for row in rows] for name in names},
        dataset_id=frozen_manifest["id"],
        site_id=site_id,
        asset_id=asset_id,
        sample_refs=refs,
        sigma=sigma,
        context={
            "modality": "acoustic",
            "units": {"acoustic": unit},
            "operatingConditions": conditions,
            "samplingRateHz": compatibility.get("samplingRateHz"),
            "datasetSnapshotDigest": frozen_manifest.get("snapshotDigest"),
            "featurePipelineVersion": frozen_manifest.get("checksumInputs", {}).get(
                "featurePipelineVersion"
            ),
        },
    )


def build_rpm_baseline(
    records, *, dataset_id, site_id, asset_id, operating_conditions, sigma=3.0
):
    """Map curated export measurements to rpm statistics; null is never ratedRpm.

    Each eligible record must identify its site/asset, rpm unit, operating conditions
    and sample reference. Only explicitly verified, non-synthetic NORMAL train rows
    are consumed. Missing RPM is excluded; invalid measured values fail explicitly.
    """
    conditions = _conditions(operating_conditions)
    values, refs = [], []
    for row in records:
        if not (
            row.get("split") == "train"
            and row.get("label_status") == "verified"
            and row.get("training_eligible") is True
            and row.get("target_label") == "NORMAL"
            and row.get("is_synthetic") is False
        ):
            continue
        if row.get("site_id") != site_id or row.get("asset_id") != asset_id:
            raise ValueError(
                "Baseline measurements must belong to the selected asset/site"
            )
        if (
            row.get("rpm_unit") != "rpm"
            or row.get("operating_conditions") != conditions
        ):
            raise ValueError("RPM units/operating conditions cannot be mixed")
        value = row.get("rpm")
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("rpm must be a finite nonnegative measured number or null")
        refs.append(_required_text(row.get("source_ref"), "source_ref"))
        values.append(value)
    return _build(
        {"rpm": values},
        dataset_id=dataset_id,
        site_id=site_id,
        asset_id=asset_id,
        sample_refs=refs,
        sigma=sigma,
        context={
            "modality": "rpm",
            "units": {"rpm": "rpm"},
            "operatingConditions": conditions,
        },
    )


def register_signal_baseline(registry: BaselineVersionRegistry, baseline: dict) -> dict:
    """Register as a draft through the existing registry's ownership boundary."""
    return registry.register(
        baseline_id=baseline["id"],
        dataset_id=baseline["datasetId"],
        site_id=baseline["siteId"],
        asset_id=baseline["assetId"],
        features=baseline["features"],
        signal_context=baseline["signalContext"],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Build acoustic/RPM baseline draft; never approve or activate"
    )
    parser.add_argument("--modality", choices=("acoustic", "rpm"), required=True)
    parser.add_argument(
        "--input",
        required=True,
        help="Frozen acoustic manifest or curated RPM JSON rows",
    )
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--dataset-id", help="Required for curated RPM records")
    parser.add_argument(
        "--operating-conditions", help="Required JSON conditions for RPM"
    )
    parser.add_argument("--sigma", type=float, default=3.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with Path(args.input).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if args.modality == "acoustic":
        baseline = build_acoustic_baseline(
            payload, site_id=args.site_id, asset_id=args.asset_id, sigma=args.sigma
        )
    else:
        baseline = build_rpm_baseline(
            payload,
            dataset_id=args.dataset_id,
            site_id=args.site_id,
            asset_id=args.asset_id,
            operating_conditions=json.loads(args.operating_conditions or "null"),
            sigma=args.sigma,
        )
    draft = register_signal_baseline(BaselineVersionRegistry(), baseline)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(draft, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"{draft['id']}: draft only (not approved or active)")


if __name__ == "__main__":
    main()
