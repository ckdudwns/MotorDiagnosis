"""Fixed supervised baselines; descriptive evaluation, no deployment."""

import argparse
import hashlib
import io
import json
import platform
import time
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from threadpoolctl import threadpool_limits

from .download import sha256
from .features import NAMES, PROFILE, extract
from .train import load_prepared, metrics


def balanced_run_weights(labels, runs):
    """Equal binary-class totals and equal runs inside each class."""
    labels, runs = np.asarray(labels), np.asarray(runs)
    if labels.ndim != 1 or runs.shape != labels.shape or set(labels) != {0, 1}:
        raise ValueError("Need aligned binary labels and run IDs")
    weights = np.zeros(len(labels), dtype=float)
    for label in (0, 1):
        ids = np.unique(runs[labels == label])
        for identifier in ids:
            mask = runs == identifier
            if not np.all(labels[mask] == label):
                raise ValueError("A recording cannot have mixed labels")
            weights[mask] = len(labels) / (2 * len(ids) * mask.sum())
    return weights


def normal_threshold(labels, scores):
    labels, scores = np.asarray(labels), np.asarray(scores)
    if (
        labels.ndim != 1
        or labels.shape != scores.shape
        or set(labels) != {0, 1}
        or not np.isfinite(scores).all()
        or np.any((scores < 0) | (scores > 1))
    ):
        raise ValueError("Invalid calibration labels/probabilities")
    return float(np.quantile(scores[labels == 0], 0.99, method="higher"))


def candidate_models():
    return {
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_leaf_nodes=15,
            min_samples_leaf=20,
            l2_regularization=1.0,
            early_stopping=False,
            random_state=42,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=300,
            max_depth=12,
            min_samples_leaf=3,
            max_features="sqrt",
            n_jobs=2,
            random_state=42,
        ),
    }


def probabilities(model, matrix):
    if list(model.classes_) != [False, True] or not np.isfinite(matrix).all():
        raise ValueError("Unexpected classes or nonfinite input")
    result = model.predict_proba(matrix)[:, 1]
    if not np.isfinite(result).all() or np.any((result < 0) | (result > 1)):
        raise ValueError("Invalid model probability")
    return result


def select_candidate(validation):
    # No test scores/labels are accepted here.
    return sorted(
        validation,
        key=lambda name: (
            -validation[name]["balancedAccuracy"],
            -validation[name]["recall"],
            validation[name]["falsePositiveRate"],
            name,
        ),
    )[0]


def grouped_metrics(labels, scores, runs, manifest, threshold):
    rows = manifest["runs"]
    by_run = [
        dict(
            runId=int(i),
            member=rows[i]["member"],
            condition=rows[i]["condition"],
            label=rows[i]["label"],
            **metrics(labels[runs == i], scores[runs == i], threshold),
        )
        for i in np.unique(runs)
    ]
    by_fault = {}
    for label in sorted({r["label"] for r in by_run}):
        ids = [r["runId"] for r in by_run if r["label"] == label]
        mask = np.isin(runs, ids)
        by_fault[label] = metrics(labels[mask], scores[mask], threshold)
    recalls = [r["recall"] for label, r in by_fault.items() if label != "health"]
    return {
        "overall": metrics(labels, scores, threshold),
        "byRun": by_run,
        "byFault": by_fault,
        "macroFaultRecall": float(np.mean(recalls)),
    }


def write_json(path, content):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(content, stream, indent=2, ensure_ascii=False, allow_nan=False)


def compare(prepared, output, baseline=None):
    prepared, output = Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new comparison directory")
    manifest, x, y, runs, split = load_prepared(prepared)
    train, validation, test = (split == s for s in ("train", "validation", "test"))
    previous = None
    if baseline is not None:
        baseline = Path(baseline)
        previous = json.loads(baseline.read_text(encoding="utf-8"))
        if previous["datasetManifestSha256"] != sha256(prepared / "manifest.json"):
            raise ValueError("Baseline used a different dataset split")
        previous = {
            "reportSha256": sha256(baseline),
            "model": previous["model"],
            "validation": previous["validation"],
            "testDescriptive": previous["test"],
            "note": "normal-only training, unlike supervised candidates",
        }
    # Metadata is for weights/splits/reporting, NEVER model inputs.
    weights = balanced_run_weights(y[train], runs[train])
    models = candidate_models()
    result, val_metrics, cached = {}, {}, {}
    output.mkdir(parents=True)
    with threadpool_limits(limits=2):
        for name, model in models.items():
            started = time.perf_counter()
            model.fit(x[train], y[train], sample_weight=weights)
            fit_seconds = time.perf_counter() - started
            scores = probabilities(model, x[validation])
            threshold = normal_threshold(y[validation], scores)
            detail = grouped_metrics(
                y[validation], scores, runs[validation], manifest, threshold
            )
            val_metrics[name] = detail["overall"]
            cached[name] = scores
            result[name] = {
                "parameters": model.get_params(),
                "threshold": threshold,
                "fitSeconds": fit_seconds,
                "validation": detail,
            }
            print(f"Fitted {name}: validation {detail['overall']}", flush=True)
        selected = select_candidate(val_metrics)
        selection = {
            "selected": selected,
            "criterion": "validation balancedAccuracy, recall, lower FPR, name",
            "thresholdRule": "validation-normal 99th quantile higher; anomaly iff score > threshold",
            "testUsedForSelection": False,
            "testPreviouslyInspected": True,
        }
        write_json(output / "selection.json", selection)
        for name, model in models.items():
            threshold = result[name]["threshold"]
            payload = {
                "model": model,
                "profile": PROFILE,
                "featureNames": NAMES,
                "threshold": threshold,
                "modelType": name,
                "fieldValidated": False,
                "affectsAlerts": False,
                "sklearnVersion": sklearn.__version__,
            }
            artifact = output / (name + ".joblib")
            joblib.dump(payload, artifact, compress=3)
            digest = sha256(artifact)
            # Only unpickle our own just-generated bytes; no arbitrary-file CLI.
            content = artifact.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Artifact changed during local replay")
            restored = joblib.load(io.BytesIO(content))
            golden = json.loads(
                (prepared / "golden-window.json").read_text(encoding="utf-8")
            )
            features = extract(golden["rawWindowG"])
            np.testing.assert_allclose(
                features, golden["expectedFeatures"], rtol=1e-10, atol=1e-12
            )
            np.testing.assert_allclose(
                probabilities(model, x[validation]),
                probabilities(restored["model"], x[validation]),
                rtol=0,
                atol=1e-14,
            )
            expected = float(probabilities(restored["model"], features[None])[0])
            write_json(
                output / (name + "-golden.json"),
                {
                    **golden,
                    "profile": PROFILE,
                    "modelSha256": digest,
                    "expectedFaultScore": expected,
                    "expectedAnomaly": expected > threshold,
                    "purpose": "numeric replay only; not field validation",
                },
            )
            test_scores = probabilities(restored["model"], x[test])
            result[name].update(
                {
                    "artifact": artifact.name,
                    "sha256": digest,
                    "testDescriptive": grouped_metrics(
                        y[test], test_scores, runs[test], manifest, threshold
                    ),
                }
            )
            np.savez_compressed(
                output / (name + "-scores.npz"),
                validation=cached[name],
                test=test_scores,
                validationRun=runs[validation],
                testRun=runs[test],
                validationLabel=y[validation],
                testLabel=y[test],
            )
    report = {
        "status": "offline_comparison_not_field_validated",
        **selection,
        "profile": PROFILE,
        "datasetManifestSha256": sha256(prepared / "manifest.json"),
        "sourceDOI": manifest["sourceDOI"],
        "sourceRevision": manifest["sourceRevision"],
        "training": {
            "windows": int(train.sum()),
            "normal": int((train & ~y).sum()),
            "fault": int((train & y).sum()),
            "weighting": "binary class balanced; equal runs within class",
            "seed": 42,
            "automaticRandomEarlyStopping": False,
        },
        "candidates": result,
        "previousBaseline": previous,
        "limitations": [
            "Previously inspected test is not confirmatory evidence",
            "No independent motor/session or ADXL345 field evaluation",
            "Known-fault binary classification; unseen faults not validated",
            "Correlated windows and scarce independent healthy records",
            "Validation selected model and threshold; optimistic selection metric",
            "joblib is unsafe for untrusted files; incompatible with live PyTorch runtime",
            "21 new features not transmitted by current firmware; no audio used",
        ],
        "deployment": {"activated": False, "affectsAlerts": False},
        "versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
        "sourceCodeSha256": {
            p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
        },
    }
    write_json(output / "comparison_report.json", report)
    write_json(output / "preprocessing.json", PROFILE)
    print(
        json.dumps(
            {
                "selectedByValidation": selected,
                "testDescriptive": {
                    k: v["testDescriptive"]["overall"] for k, v in result.items()
                },
            },
            indent=2,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline-report")
    args = parser.parse_args()
    compare(args.prepared, args.output, args.baseline_report)
