"""Normal-error cost ablation; fixed RF66 and max-of-two-normal-q99 calibration."""

import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .calibration import GATE, folds, summarize, thresholds
from .compare import (
    balanced_run_weights,
    candidate_models,
    grouped_metrics,
    probabilities,
    write_json,
)
from .download import sha256
from .spectral import CONTRACT, load_spectral

COSTS = (1, 2, 4)


def weights(labels, runs, normal_cost):
    if normal_cost not in COSTS:
        raise ValueError("Unplanned normal error cost")
    labels = np.asarray(labels)
    values = balanced_run_weights(labels, runs)
    values[labels == 0] *= normal_cost
    return values


def choose(results, minimum_recall):
    eligible = [
        name
        for name, r in results.items()
        if r["meanFpr"] <= GATE["meanFprMax"]
        and r["worstFpr"] <= GATE["worstFprMax"]
        and r["meanRecall"] >= minimum_recall
    ]
    return (
        min(
            eligible,
            key=lambda k: (-results[k]["meanRecall"], results[k]["meanFpr"], k),
        )
        if eligible
        else None
    )


def run(parent, prepared, baseline, output):
    parent, prepared, baseline, output = map(Path, (parent, prepared, baseline, output))
    if output.exists():
        raise FileExistsError("Use a new cost experiment directory")
    previous = json.loads(baseline.read_text(encoding="utf-8"))
    manifest, x, y, runs, split = load_spectral(parent, prepared)
    if not set(split) <= {"train", "validation"}:
        raise ValueError("Test rows forbidden")
    train = split == "train"
    x, y, runs = x[train], y[train], runs[train]
    if x.ndim != 2 or x.shape[1] != 66 or not np.isfinite(x).all():
        raise ValueError("Invalid training features")
    groups = np.array([manifest["runs"][i]["condition"] for i in runs])
    partitions = list(folds(groups, y))
    spec = [
        {
            "evaluation": str(h),
            "calibration": list(c),
            "fit": sorted(np.unique(groups[f]).tolist()),
        }
        for h, c, f, _, _ in partitions
    ]
    params = candidate_models()["random_forest"].get_params()
    prior = previous["protocol"]
    if (
        prior["contract"] != CONTRACT
        or prior["folds"] != spec
        or prior["model"] != params
        or prior["parentManifestSha256"] != sha256(parent / "manifest.json")
        or prior["spectralManifestSha256"] != sha256(prepared / "manifest.json")
    ):
        raise ValueError("Reference experiment does not match data/model/folds")
    # Do not ratchet the allowable recall down after a prior failed experiment.
    floor = previous["results"]["single_q99"]["meanRecall"] - GATE["recallLossMax"]
    if not np.isfinite(floor) or not 0 < floor <= 1:
        raise ValueError("Invalid protected recall floor")
    output.mkdir(parents=True)
    protocol = {
        "normalCosts": COSTS,
        "model": params,
        "contract": CONTRACT,
        "folds": spec,
        "thresholdPolicy": "max_condition_q99",
        "researchGate": {
            "meanFprMax": GATE["meanFprMax"],
            "worstFprMax": GATE["worstFprMax"],
            "minimumMeanRecall": floor,
        },
        "protectedReference": "original matched single_q99 mean recall minus 5pp, NOT degraded max-policy recall",
        "referenceReportSha256": sha256(baseline),
        "parentManifestSha256": sha256(parent / "manifest.json"),
        "spectralManifestSha256": sha256(prepared / "manifest.json"),
        "selection": "all gates then highest mean recall, lowest mean FPR, name; otherwise null",
        "testScored": False,
        "validationScored": False,
    }
    write_json(output / "protocol.json", protocol)
    results = {}
    with threadpool_limits(limits=2):
        for cost in COSTS:
            details = []
            oof, cuts = np.zeros(len(y)), np.zeros(len(y))
            for held, conditions, fit, cal, evaluate in partitions:
                model = candidate_models()["random_forest"]
                model.fit(
                    x[fit], y[fit], sample_weight=weights(y[fit], runs[fit], cost)
                )
                normal = cal & ~y
                threshold = thresholds(
                    probabilities(model, x[normal]), groups[normal], conditions
                )["max_condition_q99"]
                scores = probabilities(model, x[evaluate])
                metric = grouped_metrics(
                    y[evaluate], scores, runs[evaluate], manifest, threshold
                )
                details.append(
                    {
                        "evaluation": str(held),
                        "calibration": list(conditions),
                        "threshold": threshold,
                        "metrics": metric,
                    }
                )
                oof[evaluate], cuts[evaluate] = scores, threshold
                m = metric["overall"]
                print(
                    f"normal_cost{cost} {held}: recall={m['recall']:.4f} FPR={m['falsePositiveRate']:.4f}",
                    flush=True,
                )
            name = f"normal_cost{cost}"
            results[name] = summarize(details)
            np.savez_compressed(
                output / f"{name}-oof.npz",
                scores=oof,
                thresholds=cuts,
                labels=y,
                runs=runs,
            )
            if cost == 1:
                # Exact matched control must reproduce before evaluating alternatives.
                with np.load(
                    baseline.parent / "oof.npz", allow_pickle=False
                ) as reference:
                    for current, key in [
                        (oof, "scores"),
                        (cuts, "max_condition_q99"),
                        (y, "labels"),
                        (runs, "runs"),
                    ]:
                        np.testing.assert_allclose(
                            current, reference[key], rtol=0, atol=1e-14
                        )
    selected = choose(results, floor)
    report = {
        "protocol": protocol,
        "results": results,
        "selected": selected,
        "testScored": False,
        "validationScored": False,
        "activated": False,
        "fieldValidated": False,
        "controlReproduced": True,
        "artifactExported": False,
        "limits": [
            "Repeated train-condition selection diagnostics, not independent validation or nested CV",
            "Normal cost changes learned tree boundaries; q99 calibration may offset score-scale changes",
            "Healthy windows correlated within recordings; no new independent normal data",
            "Every640ms window remains scored; no temporal filtering or alarm suppression",
            "Input unchanged66, no firmware/server/model replacement",
        ],
        "sourceCodeSha256": {
            p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
        },
    }
    write_json(output / "report.json", report)
    write_json(
        output / "selection.json",
        {"selected": selected, "researchGate": protocol["researchGate"]},
    )
    print(
        json.dumps(
            {
                "selected": selected,
                "minimumRecall": floor,
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
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.parent, args.prepared, args.baseline, args.output)
