"""Causal persistence diagnostics on frozen TRAIN-only RF66 out-of-fold scores.

No refit, validation/test prediction, threshold change or operational activation.
"""

import argparse
from collections import deque
from pathlib import Path

import numpy as np

from .field_collection import decode, digest, dump

POLICIES = {
    "single": (1, 1),
    "two_of_three": (3, 2),
    "three_of_five": (5, 3),
    "three_consecutive": (3, 3),
    "five_consecutive": (5, 5),
}
GATE = {"meanFprMax": 0.03, "worstFprMax": 0.10, "recallLossMax": 0.05}
WINDOW_SECONDS = 0.640


def decisions(hits, runs, starts, width, required, valid=None):
    """-1=pending/invalid, 0=normal, 1=anomaly; strictly causal, no sorting.

    runs must identify one device/boot/model/condition stream in a real adapter.
    This function is an offline diagnostic, not a live event lifecycle replacement.
    """
    hits, runs, starts = map(np.asarray, (hits, runs, starts))
    if (
        type(width) is not int
        or type(required) is not int
        or not 1 <= required <= width <= 5
    ):
        raise ValueError("Invalid bounded persistence policy")
    if (
        hits.ndim != 1
        or runs.shape != hits.shape
        or starts.shape != hits.shape
        or hits.dtype.kind != "b"
        or starts.dtype.kind not in "iu"
        or np.any(starts < 0)
    ):
        raise ValueError("Require aligned boolean hits and nonnegative window indices")
    valid = np.ones(len(hits), dtype=bool) if valid is None else np.asarray(valid)
    if valid.shape != hits.shape or valid.dtype.kind != "b":
        raise ValueError("Invalid quality mask")
    out = np.full(len(hits), -1, dtype=np.int8)
    history, seen, previous = deque(maxlen=width), set(), None
    for i in range(len(hits)):
        identity = (runs[i].item(), int(starts[i]))
        if identity in seen:
            raise ValueError("Duplicate run/window")
        seen.add(identity)
        if (
            previous is not None
            and identity[0] == previous[0]
            and identity[1] <= previous[1]
        ):
            raise ValueError("Out-of-order window")
        if previous is None or identity != (previous[0], previous[1] + 1):
            history.clear()
        previous = identity
        if not valid[i]:
            history.clear()
            continue
        history.append(bool(hits[i]))
        if len(history) == width:
            out[i] = int(sum(history) >= required)
    return out


def metrics(labels, predicted):
    labels, predicted = np.asarray(labels), np.asarray(predicted)
    if labels.dtype.kind != "b" or labels.shape != predicted.shape or labels.ndim != 1:
        raise ValueError("Invalid labels or predictions")
    if not len(labels) or not np.isin(predicted, [-1, 0, 1]).all():
        raise ValueError("Invalid predictions")
    positive, negative = int(labels.sum()), int((~labels).sum())
    tp, fp = int(np.sum(labels & (predicted == 1))), int(
        np.sum(~labels & (predicted == 1))
    )
    return {
        "windows": len(labels),
        "pendingWindows": int(np.sum(predicted == -1)),
        "coverage": float(np.mean(predicted != -1)),
        "tp": tp,
        "fp": fp,
        "detectedRecall": tp / positive if positive else None,
        "falsePositiveRate": fp / negative if negative else None,
        "pendingAnomalyWindows": int(np.sum(labels & (predicted == -1))),
        "pendingNormalWindows": int(np.sum(~labels & (predicted == -1))),
    }


def choose(summary):
    baseline = summary["single"]["meanRecall"]
    eligible = [
        name
        for name, value in summary.items()
        if name != "single"
        and value["meanFpr"] <= GATE["meanFprMax"]
        and value["worstFpr"] <= GATE["worstFprMax"]
        and value["meanRecall"] >= baseline - GATE["recallLossMax"]
    ]
    return (
        min(
            eligible,
            key=lambda name: (
                -summary[name]["meanRecall"],
                summary[name]["meanFpr"],
                name,
            ),
        )
        if eligible
        else None
    )


def load(parent, source, expected_oof_sha256):
    parent, source = Path(parent), Path(source)
    protocol = decode((source / "protocol.json").read_text(encoding="utf-8"))
    manifest = decode((parent / "manifest.json").read_text(encoding="utf-8"))
    report = decode((source / "report.json").read_text(encoding="utf-8"))
    if digest(source / "rf66-oof.npz") != expected_oof_sha256:
        raise ValueError("OOF checksum mismatch")
    if protocol["parentManifestSha256"] != digest(parent / "manifest.json") or manifest[
        "cacheSha256"
    ] != digest(parent / "sequences.npz"):
        raise ValueError("Parent manifest/cache checksum mismatch")
    if (
        protocol["testScored"] is not False
        or protocol["validationUsedForSelection"] is not False
    ):
        raise ValueError("Require frozen train-only spectral experiment")
    with np.load(parent / "sequences.npz", allow_pickle=False) as data:
        train = data["split"] == "train"
        runs, labels, starts = (
            data["run"][train],
            data["y"][train],
            data["start"][train],
        )
    if not np.isin(labels, [0, 1]).all() or runs.dtype.kind not in "iu":
        raise ValueError("Invalid parent labels/runs")
    labels = labels.astype(bool)
    with np.load(source / "rf66-oof.npz", allow_pickle=False) as data:
        scores, thresholds = data["scores"], data["thresholds"]
        if not np.array_equal(data["runs"], runs) or not np.array_equal(
            data["labels"], labels
        ):
            raise ValueError(
                "OOF rows must exactly match parent train order and labels"
            )
    for array in (scores, thresholds):
        if (
            array.shape != labels.shape
            or not np.isfinite(array).all()
            or np.any((array < 0) | (array > 1))
        ):
            raise ValueError("Invalid probabilities/thresholds")
    groups = []
    for run, label in zip(runs, labels):
        if not 0 <= run < len(manifest["runs"]):
            raise ValueError("Unknown run")
        row = manifest["runs"][run]
        if row["split"] != "train" or label != (row["label"] != "health"):
            raise ValueError("Run split/label mismatch")
        groups.append(row["condition"])
    groups = np.asarray(groups)
    folds = protocol["folds"]
    held = [row["evaluation"] for row in folds]
    if len(set(held)) != len(held) or set(held) != set(groups):
        raise ValueError("OOF conditions must each be held out once")
    source_details = report["results"]["rf66"]["folds"]
    for row in folds:
        fit, calibration, evaluation = (
            set(row["fit"]),
            row["calibration"],
            row["evaluation"],
        )
        if (
            calibration == evaluation
            or fit & {calibration, evaluation}
            or fit | {calibration, evaluation} != set(groups)
        ):
            raise ValueError("Leaked fit/calibration/evaluation conditions")
        details = [d for d in source_details if d["evaluation"] == evaluation]
        if (
            len(details) != 1
            or details[0]["calibration"] != calibration
            or not np.all(thresholds[groups == evaluation] == details[0]["threshold"])
        ):
            raise ValueError("OOF thresholds differ from frozen fold report")
    return labels, runs, starts, groups, scores > thresholds


def run(parent, source, output, expected_oof_sha256):
    output, source = Path(output), Path(source)
    if output.exists():
        raise FileExistsError("Use a new temporal experiment directory")
    labels, runs, starts, groups, hits = load(parent, source, expected_oof_sha256)
    output.mkdir(parents=True)
    protocol = {
        "policies": POLICIES,
        "researchGateNotOperationalSLA": GATE,
        "comparison": "same frozen RF66 train OOF scores and thresholds; causal current/past only",
        "gatePopulation": "common ready windows (all policies warmed up); no favorable pending exclusion per policy",
        "pendingAccounting": "all-window detected recall counts pending anomalies as undetected, never normal",
        "sourceOofSha256": expected_oof_sha256,
        "sourceProtocolSha256": digest(source / "protocol.json"),
        "sourceReportSha256": digest(source / "report.json"),
        "parentManifestSha256": digest(Path(parent) / "manifest.json"),
        "codeSha256": digest(__file__),
        "windowSeconds": WINDOW_SECONDS,
        "testScored": False,
        "validationScored": False,
    }
    dump(output / "protocol.json", protocol)
    predictions = {
        name: decisions(hits, runs, starts, *policy)
        for name, policy in POLICIES.items()
    }
    common = np.logical_and.reduce([p >= 0 for p in predictions.values()])
    results = {}
    for name, predicted in predictions.items():
        folds = []
        for condition in sorted(set(groups)):
            scope = groups == condition
            score = metrics(labels[scope & common], predicted[scope & common])
            if score["detectedRecall"] is None or score["falsePositiveRate"] is None:
                raise ValueError(
                    "Each held condition requires healthy and anomaly records"
                )
            folds.append(
                {
                    "condition": str(condition),
                    "commonReady": score,
                    "allWindows": metrics(labels[scope], predicted[scope]),
                }
            )
        results[name] = {
            "folds": folds,
            "meanRecall": float(
                np.mean([f["commonReady"]["detectedRecall"] for f in folds])
            ),
            "meanFpr": float(
                np.mean([f["commonReady"]["falsePositiveRate"] for f in folds])
            ),
            "worstFpr": float(
                max(f["commonReady"]["falsePositiveRate"] for f in folds)
            ),
            "allWindows": metrics(labels, predicted),
            "minimumHistorySpanSeconds": POLICIES[name][0] * WINDOW_SECONDS,
            "limits": "History span is not measured fault-onset detection latency",
        }
    selected = choose(results)
    result = {
        "protocol": protocol,
        "results": results,
        "selected": selected,
        "commonReadyWindows": int(common.sum()),
        "totalWindows": len(labels),
        "fieldValidated": False,
        "activated": False,
        "artifactExported": False,
        "limits": [
            "Repeatedly inspected train-condition OOF: selection diagnostic, not independent evaluation",
            "No fault onset ground truth; no latency or transient-fault recall claim",
            "Contiguous valid windows only; gaps/invalid data restart pending state",
            "Temporal suppression may miss brief faults; independent field/anomaly validation required",
            "No feature66 capture integration or live event policy replacement",
        ],
    }
    dump(output / "report.json", result)
    np.savez_compressed(
        output / "predictions.npz",
        runs=runs,
        starts=starts,
        labels=labels,
        commonReady=common,
        **predictions,
    )
    dump(
        output / "selection.json",
        {
            "selected": selected,
            "researchGate": GATE,
            "predictionsSha256": digest(output / "predictions.npz"),
        },
    )
    print(
        {
            "selected": selected,
            "summary": {
                n: {
                    k: v
                    for k, v in r.items()
                    if k in ("meanRecall", "meanFpr", "worstFpr")
                }
                for n, r in results.items()
            },
        }
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-oof-sha256", required=True)
    args = parser.parse_args()
    run(args.parent, args.source, args.output, args.expected_oof_sha256)
