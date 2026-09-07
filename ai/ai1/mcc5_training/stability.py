"""Train-only grouped feature ablation. Test is never scored or selected on."""

import argparse
import hashlib
import io
import json
from pathlib import Path

import joblib
import numpy as np
import sklearn
from threadpoolctl import threadpool_limits

from .compare import (
    balanced_run_weights,
    candidate_models,
    grouped_metrics,
    normal_threshold,
    probabilities,
    write_json,
)
from .download import sha256
from .features import NAMES, PROFILE, extract
from .train import load_prepared, metrics

VARIANTS = ("base21", "ratios36", "log_ratios36")


def transform(matrix, variant):
    if variant not in VARIANTS:
        raise ValueError("Unknown feature variant")
    x = np.asarray(matrix, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != 21 or not np.isfinite(x).all() or np.any(x < 0):
        raise ValueError("Expected finite nonnegative base21 features")
    axes = x.reshape(-1, 3, 7)
    if np.any(axes[:, :, 0] <= 0):
        raise ValueError("Invalid zero RMS")
    if variant == "base21":
        return x.copy()
    energy = axes[:, :, 3:7]
    denominator = energy.sum(axis=2, keepdims=True)
    if np.any(denominator <= 1e-16):
        raise ValueError("Insufficient band energy for ratios")
    relative = energy / denominator
    crest = axes[:, :, 1:2] / axes[:, :, 0:1]
    base = axes.copy()
    if variant == "log_ratios36":
        # Fixed, unit-specific references; no fit on any partition.
        base[:, :, :2] = np.log1p(base[:, :, :2] / 0.01)  # 0.01 g
        base[:, :, 3:] = np.log1p(base[:, :, 3:] / 0.0001)  # 1e-4 g^2
    result = np.concatenate(
        [
            base.reshape(-1, 21),
            np.concatenate([relative, crest], axis=2).reshape(-1, 15),
        ],
        axis=1,
    )
    if not np.isfinite(result).all():
        raise ValueError("Feature transform overflow")
    return result


def contract(variant):
    if variant not in VARIANTS:
        raise ValueError("Unknown feature variant")
    names = list(NAMES)
    if variant == "log_ratios36":
        names = [
            "log1p:" + name if i % 7 != 2 else name for i, name in enumerate(names)
        ]
    if variant != "base21":
        names += [
            f"vibration{axis}.{name}"
            for axis in "XYZ"
            for name in (
                "ratio_0_50",
                "ratio_50_100",
                "ratio_100_200",
                "ratio_200_350",
                "crest",
            )
        ]
    return {
        "id": "mcc5-derived-" + variant + "-v1",
        "variant": variant,
        "inputProfile": PROFILE,
        "featureNames": names,
        "formula": "XYZ 4 band powers / their per-axis sum; peak_ac / RMS_ac; append ratios then crest per axis",
        "logReferences": {"rmsPeakG": 0.01, "bandPowerG2": 0.0001},
        "fitRequired": False,
        "fieldValidated": False,
    }


def grouped_folds(groups, labels):
    groups, labels = np.asarray(groups), np.asarray(labels)
    if groups.ndim != 1 or labels.shape != groups.shape:
        raise ValueError("Unaligned groups and labels")
    ordered = sorted(np.unique(groups))
    if len(ordered) < 4:
        raise ValueError("Need at least four operating conditions")
    if any(set(labels[groups == group]) != {0, 1} for group in ordered):
        raise ValueError("Each condition must contain healthy and faulty runs")
    folds = []
    for i, held in enumerate(ordered):
        calibration = ordered[(i + 1) % len(ordered)]
        evaluate, calibrate = groups == held, groups == calibration
        fit = ~(evaluate | calibrate)
        folds.append((held, calibration, fit, calibrate, evaluate))
    return folds


def choose(results):
    return sorted(
        results,
        key=lambda name: (
            -results[name]["meanBalancedAccuracy"],
            results[name]["worstFpr"],
            VARIANTS.index(name),
        ),
    )[0]


def run(prepared, output):
    prepared, output = Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new output directory")
    manifest, all_x, all_y, all_runs, split = load_prepared(prepared)
    selected_train = split == "train"
    x, y, runs = all_x[selected_train], all_y[selected_train], all_runs[selected_train]
    groups = np.array([manifest["runs"][i]["condition"] for i in runs])
    folds = grouped_folds(groups, y)
    output.mkdir(parents=True)
    protocol = {
        "variants": list(VARIANTS),
        "folds": [
            {
                "evaluation": str(h),
                "calibration": str(c),
                "fitConditions": sorted(np.unique(groups[f]).tolist()),
            }
            for h, c, f, _, _ in folds
        ],
        "selection": "highest mean fold balanced accuracy at fixed calibration-normal q99; worst FPR then fewer features tie break",
        "sensitivityOnly": [0.95, 0.975, 0.99],
        "testScored": False,
        "validationUsedForFeatureSelection": False,
        "modelParameters": candidate_models()["random_forest"].get_params(),
        "datasetManifestSha256": sha256(prepared / "manifest.json"),
    }
    write_json(output / "protocol.json", protocol)
    results = {}
    with threadpool_limits(limits=2):
        for variant in VARIANTS:
            features = transform(x, variant)
            fold_results = []
            oof_score, oof_threshold = np.zeros(len(y)), np.zeros(len(y))
            for held, calibration, fit, calibrate, evaluate in folds:
                model = candidate_models()["random_forest"]
                model.fit(
                    features[fit],
                    y[fit],
                    sample_weight=balanced_run_weights(y[fit], runs[fit]),
                )
                calibration_scores = probabilities(model, features[calibrate])
                threshold = normal_threshold(y[calibrate], calibration_scores)
                scores = probabilities(model, features[evaluate])
                detail = grouped_metrics(
                    y[evaluate], scores, runs[evaluate], manifest, threshold
                )
                sensitivities = []
                for q in protocol["sensitivityOnly"]:
                    t = float(
                        np.quantile(
                            calibration_scores[~y[calibrate]], q, method="higher"
                        )
                    )
                    sensitivities.append(
                        {"q": q, "threshold": t, **metrics(y[evaluate], scores, t)}
                    )
                fold_results.append(
                    {
                        "evaluation": str(held),
                        "calibration": str(calibration),
                        "threshold": threshold,
                        "calibrationNormalCount": int((~y[calibrate]).sum()),
                        "metrics": detail,
                        "sensitivity": sensitivities,
                    }
                )
                oof_score[evaluate], oof_threshold[evaluate] = scores, threshold
                print(
                    f"{variant} {held}: recall={detail['overall']['recall']:.4f}, FPR={detail['overall']['falsePositiveRate']:.4f}",
                    flush=True,
                )
            stats = [f["metrics"]["overall"] for f in fold_results]
            results[variant] = {
                "folds": fold_results,
                "meanBalancedAccuracy": float(
                    np.mean([m["balancedAccuracy"] for m in stats])
                ),
                "meanRecall": float(np.mean([m["recall"] for m in stats])),
                "meanFpr": float(np.mean([m["falsePositiveRate"] for m in stats])),
                "worstFpr": float(max(m["falsePositiveRate"] for m in stats)),
                "worstRecall": float(min(m["recall"] for m in stats)),
                "thresholdRange": [
                    min(f["threshold"] for f in fold_results),
                    max(f["threshold"] for f in fold_results),
                ],
            }
            np.savez_compressed(
                output / (variant + "-oof.npz"),
                scores=oof_score,
                thresholds=oof_threshold,
                labels=y,
                runs=runs,
            )
        selected = choose(results)
        write_json(output / "selection.json", {"selected": selected, **protocol})
        # Final fit only after train-only feature selection. Old validation is
        # reused for calibration/diagnostics, NOT an independent evaluation.
        model = candidate_models()["random_forest"]
        model.fit(
            transform(x, selected), y, sample_weight=balanced_run_weights(y, runs)
        )
        validation = split == "validation"
        val_x = transform(all_x[validation], selected)
        val_score = probabilities(model, val_x)
        threshold = normal_threshold(all_y[validation], val_score)
        payload = {
            "model": model,
            "contract": contract(selected),
            "threshold": threshold,
            "fieldValidated": False,
            "affectsAlerts": False,
            "sklearnVersion": sklearn.__version__,
        }
        artifact = output / "candidate.joblib"
        joblib.dump(payload, artifact, compress=3)
        digest = sha256(artifact)
        content = artifact.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Artifact changed")
        # Reload only the trusted artifact produced by this invocation.
        restored = joblib.load(io.BytesIO(content))
        np.testing.assert_allclose(
            probabilities(restored["model"], val_x), val_score, rtol=0, atol=1e-14
        )
        golden = json.loads(
            (prepared / "golden-window.json").read_text(encoding="utf-8")
        )
        base = extract(golden["rawWindowG"])
        np.testing.assert_allclose(
            base, golden["expectedFeatures"], rtol=1e-10, atol=1e-12
        )
        derived = transform(base[None], selected)
        score = float(probabilities(restored["model"], derived)[0])
        write_json(
            output / "golden.json",
            {
                **golden,
                "derivedFeatures": derived[0].tolist(),
                "contract": contract(selected),
                "modelSha256": digest,
                "expectedScore": score,
                "expectedAnomaly": score > threshold,
            },
        )
    report = {
        "protocol": protocol,
        "selected": selected,
        "results": results,
        "artifactSha256": digest,
        "threshold": threshold,
        "validationReusedCalibrationDiagnostic": grouped_metrics(
            all_y[validation], val_score, all_runs[validation], manifest, threshold
        ),
        "testScored": False,
        "fieldValidated": False,
        "activated": False,
        "limits": [
            "Feature selection reuses folds; not unbiased nested CV performance",
            "RF family was selected in previous experiments; not independent confirmation",
            "One calibration condition per fold; 140 normal windows from one correlated run",
            "No ADXL345 field data or unseen fault-type validation",
            "All transforms derive from existing base21; no new measured information",
            "Joblib research artifact is unsafe when untrusted and not live-runtime compatible",
        ],
        "versions": {
            "sklearn": sklearn.__version__,
            "numpy": np.__version__,
            "joblib": joblib.__version__,
        },
        "sourceCodeSha256": {
            p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
        },
    }
    write_json(output / "stability_report.json", report)
    print(
        json.dumps(
            {
                "selected": selected,
                "summary": {
                    k: {n: v for n, v in r.items() if n != "folds"}
                    for k, r in results.items()
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
    args = parser.parse_args()
    run(args.prepared, args.output)
