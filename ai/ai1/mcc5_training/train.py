"""Train a NORMAL-only Dense candidate; fixed protocol, no operational activation.

Test is evaluated once after fixed training and a calibration-normal quantile.
All scores retain operating-profile, class, and source-run attribution.
"""

import argparse
import copy
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import scipy
import torch
from scipy.stats import rankdata

from ai.ai1.week4.ai1.freq_baseline.models import DenseAutoencoder, FeatureScaler
from .download import sha256
from .features import NAMES, PROFILE, extract


def metrics(labels, errors, threshold):
    labels = np.asarray(labels, dtype=bool)
    errors = np.asarray(errors, dtype=float)
    if not len(labels) or len(labels) != len(errors) or not np.isfinite(errors).all():
        raise ValueError("Invalid evaluation samples")
    predictions = errors > threshold
    tp = int(np.sum(labels & predictions))
    tn = int(np.sum(~labels & ~predictions))
    fp = int(np.sum(~labels & predictions))
    fn = int(np.sum(labels & ~predictions))
    positive, negative = tp + fn, tn + fp
    recall = tp / positive if positive else None
    specificity = tn / negative if negative else None
    auc = None
    if positive and negative:
        ranks = rankdata(errors)
        auc = float(
            (ranks[labels].sum() - positive * (positive + 1) / 2)
            / (positive * negative)
        )
    return {
        "count": len(labels),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": recall,
        "falsePositiveRate": fp / negative if negative else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        "balancedAccuracy": (
            (recall + specificity) / 2
            if recall is not None and specificity is not None
            else None
        ),
        "rocAuc": auc,
    }


def score(model, matrix, scaler):
    values = scaler.transform(matrix)
    if not np.isfinite(values).all():
        raise ValueError("Nonfinite normalization")
    model.eval()
    errors = []
    with torch.inference_mode():
        for start in range(0, len(values), 256):
            batch = torch.tensor(values[start : start + 256], dtype=torch.float32)
            errors.extend(((model(batch) - batch) ** 2).mean(dim=1).tolist())
    result = np.asarray(errors)
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite model errors")
    return result


def load_prepared(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest["profile"] != PROFILE
        or sha256(directory / "sequences.npz") != manifest["cacheSha256"]
    ):
        raise ValueError("Prepared feature contract or checksum changed")
    with np.load(directory / "sequences.npz", allow_pickle=False) as data:
        x, y, run, split = (data[k] for k in ("x", "y", "run", "split"))
    if (
        x.shape != (len(y), 1, len(NAMES))
        or not np.isfinite(x).all()
        or set(np.unique(y)) != {0, 1}
    ):
        raise ValueError("Invalid prepared shape/labels")
    if len(run) != len(y) or len(split) != len(y):
        raise ValueError("Mismatched metadata lengths")
    for index, row in enumerate(manifest["runs"]):
        selected = run == index
        if int(selected.sum()) != row["sequences"]:
            raise ValueError("Run sample counts differ")
        if set(split[selected]) != {row["split"]} or not np.all(
            y[selected] == (row["label"] != "health")
        ):
            raise ValueError("Run crossed splits or labels")
        if row["split"] != manifest["splitByCondition"][row["condition"]]:
            raise ValueError("Operating profile crossed splits")
    if set(np.unique(run)) != set(range(len(manifest["runs"]))):
        raise ValueError("Unknown run IDs")
    for name in ("train", "validation", "test"):
        if set(y[split == name]) != {0, 1}:
            raise ValueError(f"Missing healthy/fault data in {name}")
    if set(split) != {"train", "validation", "test"}:
        raise ValueError("Unknown split")
    return manifest, x[:, 0].astype(np.float64), y.astype(bool), run, split


def train(prepared, output, epochs=150, seed=42):
    if epochs < 1:
        raise ValueError("epochs must be positive")
    prepared, output = Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new model output directory")
    manifest, x, y, run, split = load_prepared(prepared)
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(seed)
    np.random.seed(seed)
    normal_train = x[(split == "train") & ~y]
    scaler = FeatureScaler().fit(normal_train)
    tensor = torch.tensor(scaler.transform(normal_train), dtype=torch.float32)
    model = DenseAutoencoder(len(NAMES))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    generator = torch.Generator().manual_seed(seed)
    losses = []
    for epoch in range(epochs):
        model.train()
        permutation = torch.randperm(len(tensor), generator=generator)
        total = 0.0
        for indices in permutation.split(64):
            batch = tensor[indices]
            optimizer.zero_grad()
            loss = ((model(batch) - batch) ** 2).mean()
            if not torch.isfinite(loss):
                raise ValueError("Training diverged")
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(batch)
        losses.append(total / len(tensor))
        if epoch == 0 or (epoch + 1) % 25 == 0:
            print(
                f"Epoch {epoch+1}/{epochs}: normal train MSE {losses[-1]:.6f}",
                flush=True,
            )
    # Fixed before seeing test labels: one percent nominal calibration-window FPR.
    calibration_errors = score(model, x[(split == "validation") & ~y], scaler)
    threshold = float(np.quantile(calibration_errors, 0.99, method="higher"))
    validation = split == "validation"
    validation_errors = score(model, x[validation], scaler)
    payload = {
        "model_type": "dense_autoencoder",
        "state_dict": copy.deepcopy(model.state_dict()),
        "input_dim": len(NAMES),
        "seq_len": None,
        "feature_names": NAMES,
        "scaler_mean": scaler.mean_.tolist(),
        "scaler_std": scaler.std_.tolist(),
        "threshold": threshold,
        "sigma": 3.0,  # legacy loader metadata only; threshold_method is authoritative
        "threshold_method": "validation-normal 0.99 quantile, higher; sigma not used",
        "preprocessing": PROFILE,
        "domain_validated": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output.mkdir(parents=True)
    artifact = output / "dense_autoencoder.pt"
    torch.save(payload, artifact)
    # Only now evaluate the frozen, selected candidate on held-out profiles.
    test = split == "test"
    test_errors = score(model, x[test], scaler)
    test_runs = run[test]
    class_results = {}
    for label in sorted({r["label"] for r in manifest["runs"]}):
        identifiers = [i for i, r in enumerate(manifest["runs"]) if r["label"] == label]
        mask = np.isin(test_runs, identifiers)
        if mask.any():
            class_results[label] = metrics(y[test][mask], test_errors[mask], threshold)
    report = {
        "status": "offline_training_completed_not_field_validated",
        "model": "dense_autoencoder",
        "modelSha256": sha256(artifact),
        "datasetManifestSha256": sha256(prepared / "manifest.json"),
        "sourceDOI": manifest["sourceDOI"],
        "sourceRevision": manifest["sourceRevision"],
        "preprocessing": PROFILE,
        "holdout": manifest["holdout"],
        "epochs": epochs,
        "seed": seed,
        "normalTrainWindows": len(normal_train),
        "threshold": threshold,
        "thresholdMethod": payload["threshold_method"],
        "losses": losses,
        "validation": metrics(y[validation], validation_errors, threshold),
        "test": metrics(y[test], test_errors, threshold),
        "testByFault": class_results,
        "testByRun": [
            {
                "member": r["member"],
                "condition": r["condition"],
                "label": r["label"],
                **metrics(
                    y[test][test_runs == i], test_errors[test_runs == i], threshold
                ),
            }
            for i, r in enumerate(manifest["runs"])
            if r["split"] == "test"
        ],
        "excludedWindows": {
            r["member"]: r["excludedWindows"]
            for r in manifest["runs"]
            if r["excludedWindows"]
        },
        "limits": [
            "Different sensor/motor/mounting; no ADXL345 noise or quantization simulation",
            "No fault-type classifier or RUL claim",
            "Single rig; operating-profile holdout only",
            "Threshold not calibrated on field normal data",
            "New XYZ features require firmware/backend integration; existing four telemetry fields are insufficient",
            "Window metrics are correlated within runs; counts are not independent experiments",
        ],
        "deployment": {
            "activated": False,
            "affectsAlerts": False,
            "fieldValidated": False,
        },
        "versions": {
            "python": platform.python_version(),
            "torch": str(torch.__version__),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "sourceCodeSha256": {
            p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
        },
    }
    example = json.loads((prepared / "golden-window.json").read_text(encoding="utf-8"))
    regenerated = extract(example["rawWindowG"])
    np.testing.assert_allclose(
        regenerated, example["expectedFeatures"], rtol=1e-10, atol=1e-12
    )
    example["expectedError"] = float(score(model, regenerated[None, :], scaler)[0])
    example["expectedAnomaly"] = example["expectedError"] > threshold
    example["modelSha256"] = sha256(artifact)
    example.update(
        {
            "profileId": PROFILE["id"],
            "sampleRateHz": 800,
            "unit": "g",
            "channels": PROFILE["channels"],
            "quality": "valid",
            "purpose": "public-dataset numeric replay, not a field accuracy test",
        }
    )
    for filename, data in (
        ("training_report.json", report),
        ("preprocessing.json", PROFILE),
        ("golden-example.json", example),
    ):
        with (output / filename).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(
        json.dumps(
            {
                "artifact": str(artifact),
                "test": report["test"],
                "fieldValidated": False,
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--epochs", type=int, default=150)
    args = parser.parse_args()
    train(args.prepared, args.output, args.epochs)
