"""Matched RF66 false-positive ablation with disjoint calibration conditions."""

import argparse
import json
from pathlib import Path

import numpy as np
import sklearn
from threadpoolctl import threadpool_limits

from .compare import (
    balanced_run_weights,
    candidate_models,
    grouped_metrics,
    probabilities,
    write_json,
)
from .download import sha256
from .spectral import CONTRACT, load_spectral

POLICIES = ("single_q99", "pooled_q99", "max_condition_q99")
GATE = {"meanFprMax": 0.03, "worstFprMax": 0.10, "recallLossMax": 0.05}


def folds(groups, labels):
    groups, labels = np.asarray(groups), np.asarray(labels)
    ordered = sorted(np.unique(groups))
    if (
        groups.ndim != 1
        or groups.shape != labels.shape
        or len(ordered) < 6
        or len(ordered) % 2
    ):
        raise ValueError(
            "Need aligned labels and an even count of at least six conditions"
        )
    if any(set(labels[groups == g]) != {0, 1} for g in ordered):
        raise ValueError("Each condition needs normal and fault recordings")
    for i, held in enumerate(ordered):
        # For the eight MCC5 train conditions these two are one speed and one torque.
        first = ordered[(i + 1) % len(ordered)]
        second = ordered[(i + 1 + len(ordered) // 2) % len(ordered)]
        evaluate = groups == held
        calibrate = np.isin(groups, [first, second])
        fit = ~(evaluate | calibrate)
        yield held, (first, second), fit, calibrate, evaluate


def thresholds(normal_scores, normal_groups, ordered_conditions):
    """No fault/evaluation labels or scores accepted. Missing groups fail closed."""
    scores, groups = np.asarray(normal_scores), np.asarray(normal_groups)
    if (
        scores.ndim != 1
        or scores.shape != groups.shape
        or len(scores) == 0
        or not np.isfinite(scores).all()
        or np.any((scores < 0) | (scores > 1))
        or len(ordered_conditions) != 2
        or len(set(ordered_conditions)) != 2
        or set(groups) != set(ordered_conditions)
    ):
        raise ValueError(
            "Require finite normal scores from exactly two calibration conditions"
        )
    by_condition = [
        float(np.quantile(scores[groups == g], 0.99, method="higher"))
        for g in ordered_conditions
    ]
    return {
        "single_q99": by_condition[0],
        "pooled_q99": float(np.quantile(scores, 0.99, method="higher")),
        "max_condition_q99": max(by_condition),
    }


def choose(results):
    baseline = results["single_q99"]["meanRecall"]
    eligible = [
        p
        for p in POLICIES[1:]
        if results[p]["meanFpr"] <= GATE["meanFprMax"]
        and results[p]["worstFpr"] <= GATE["worstFprMax"]
        and results[p]["meanRecall"] >= baseline - GATE["recallLossMax"]
    ]
    if not eligible:
        return None
    return min(
        eligible, key=lambda p: (-results[p]["meanRecall"], results[p]["meanFpr"], p)
    )


def summarize(details):
    values = [d["metrics"]["overall"] for d in details]
    return {
        "folds": details,
        "meanRecall": float(np.mean([v["recall"] for v in values])),
        "meanFpr": float(np.mean([v["falsePositiveRate"] for v in values])),
        "worstRecall": float(min(v["recall"] for v in values)),
        "worstFpr": float(max(v["falsePositiveRate"] for v in values)),
        "meanBalancedAccuracy": float(np.mean([v["balancedAccuracy"] for v in values])),
    }


def run(parent, prepared, output):
    parent, prepared, output = Path(parent), Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new calibration experiment directory")
    manifest, x, y, runs, split = load_spectral(parent, prepared)
    if not set(split) <= {"train", "validation"}:
        raise ValueError("Test rows forbidden")
    train = split == "train"
    x, y, runs = x[train], y[train], runs[train]
    if x.ndim != 2 or x.shape[1] != 66 or not np.isfinite(x).all():
        raise ValueError("Invalid training features")
    groups = np.array([manifest["runs"][r]["condition"] for r in runs])
    partitions = list(folds(groups, y))
    output.mkdir(parents=True)
    protocol = {
        "contract": CONTRACT,
        "policies": POLICIES,
        "researchGateNotOperationalSLA": GATE,
        "model": candidate_models()["random_forest"].get_params(),
        "folds": [
            {
                "evaluation": str(h),
                "calibration": list(c),
                "fit": sorted(np.unique(groups[f]).tolist()),
            }
            for h, c, f, _, _ in partitions
        ],
        "selection": "pass all fixed research gates then highest recall, lowest mean FPR, name; otherwise no selection",
        "control": "single_q99 uses first calibration condition, same five-condition fitted model as both alternatives",
        "quantile": "0.99 higher, strict score > threshold; no evaluation values used to calibrate",
        "parentManifestSha256": sha256(parent / "manifest.json"),
        "spectralManifestSha256": sha256(prepared / "manifest.json"),
        "testScored": False,
        "validationScored": False,
    }
    write_json(output / "protocol.json", protocol)
    details = {p: [] for p in POLICIES}
    oof, cuts = np.zeros(len(y)), {p: np.zeros(len(y)) for p in POLICIES}
    with threadpool_limits(limits=2):
        for held, conditions, fit, calibrate, evaluate in partitions:
            model = candidate_models()["random_forest"]
            model.fit(
                x[fit], y[fit], sample_weight=balanced_run_weights(y[fit], runs[fit])
            )
            normal = calibrate & ~y
            thresholds_by_policy = thresholds(
                probabilities(model, x[normal]), groups[normal], conditions
            )
            scores = probabilities(model, x[evaluate])
            oof[evaluate] = scores
            for policy, threshold in thresholds_by_policy.items():
                metric = grouped_metrics(
                    y[evaluate], scores, runs[evaluate], manifest, threshold
                )
                details[policy].append(
                    {
                        "evaluation": str(held),
                        "calibration": list(conditions),
                        "normalCounts": {
                            str(g): int(np.sum(normal & (groups == g)))
                            for g in conditions
                        },
                        "threshold": threshold,
                        "metrics": metric,
                    }
                )
                cuts[policy][evaluate] = threshold
                m = metric["overall"]
                print(
                    f"{held} {policy}: recall={m['recall']:.4f} FPR={m['falsePositiveRate']:.4f}",
                    flush=True,
                )
    results = {p: summarize(details[p]) for p in POLICIES}
    selected = choose(results)
    write_json(output / "selection.json", {"selected": selected, "researchGate": GATE})
    np.savez_compressed(
        output / "oof.npz",
        scores=oof,
        labels=y,
        runs=runs,
        **{p: cuts[p] for p in POLICIES},
    )
    report = {
        "protocol": protocol,
        "results": results,
        "selected": selected,
        "testScored": False,
        "validationScored": False,
        "fieldValidated": False,
        "activated": False,
        "artifactExported": False,
        "limits": [
            "Same repeatedly inspected train conditions; selection diagnostic, not independent test or nested CV",
            "Five fit conditions here versus six in previous spectral experiment; use matched single_q99 control",
            "Calibration windows within each healthy recording are correlated",
            "Two calibration conditions cannot guarantee generalization to unseen normal operating conditions",
            "Every640ms window still scored independently; no temporal suppression or skipped measurements",
            "Gate is an experimental screen, not user-approved operational SLA; no model or production threshold replaced",
        ],
        "versions": {"numpy": np.__version__, "sklearn": sklearn.__version__},
        "sourceCodeSha256": {
            p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
        },
    }
    write_json(output / "report.json", report)
    print(
        json.dumps(
            {
                "selected": selected,
                "summary": {
                    p: {k: v for k, v in r.items() if k != "folds"}
                    for p, r in results.items()
                },
            },
            indent=2,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.parent, args.prepared, args.output)
