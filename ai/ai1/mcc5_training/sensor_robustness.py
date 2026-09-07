"""TRAIN-only resolution stress and half-quantized fitting at fixed input shape.

0.0039g nearest-even quantization is a stress transform, NOT measured ADXL noise.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
from threadpoolctl import threadpool_limits

from .compare import (
    candidate_models,
    balanced_run_weights,
    normal_threshold,
    probabilities,
)
from .download import ARCHIVES, sha256
from .field_collection import dump
from .features import WINDOW, to_800hz
from .prepare import parse_csv
from .raw_input import (
    COUNT_SCALE_G,
    NUMERIC_POLICY,
    spectral_input,
    quantize_public_window,
    InputUnavailable,
)
from .spectral import load_spectral
from .stability import grouped_folds
from .temporal import decisions, metrics, GATE


def prepare(source, parent, spectral, output):
    source, parent, output = Path(source), Path(parent), Path(output)
    if output.exists():
        raise FileExistsError("Use a new sensor stress cache")
    manifest, features, labels, runs, split = load_spectral(parent, spectral)
    take = split == "train"
    clean, labels, runs = features[take], labels[take], runs[take]
    with np.load(parent / "sequences.npz", allow_pickle=False) as cache:
        starts = cache["start"][cache["split"] == "train"]
    if starts.shape != runs.shape:
        raise ValueError("Missing parent window indices")
    for name, (size, expected) in ARCHIVES.items():
        path = source / name
        if path.stat().st_size != size or sha256(path) != expected:
            raise ValueError("Unverified source archive")
    output.mkdir(parents=True)
    quantized = np.full_like(clean, np.nan)
    reasons = np.full(len(runs), "", dtype="U128")
    golden = None
    with ExitStack() as stack:
        archives = {
            name: stack.enter_context(zipfile.ZipFile(source / name))
            for name in ARCHIVES
        }
        unique = np.unique(runs)
        for number, identifier in enumerate(unique):
            row = manifest["runs"][identifier]
            if row["split"] != "train":
                raise ValueError("Only TRAIN source recordings permitted")
            info = archives[row["archive"]].getinfo(row["member"])
            if (
                info.file_size != row["uncompressedBytes"]
                or info.file_size > 200_000_000
                or info.flag_bits & 1
            ):
                raise ValueError("Invalid source member")
            body = archives[row["archive"]].read(info)
            if hashlib.sha256(body).hexdigest() != row["sha256"]:
                raise ValueError("Source member checksum mismatch")
            signal = to_800hz(
                parse_csv(body)
            )  # public-only resampling, never field conversion
            for index in np.flatnonzero(runs == identifier):
                start = int(starts[index]) * WINDOW
                raw = signal[start : start + WINDOW]
                np.testing.assert_allclose(
                    spectral_input(raw), clean[index], rtol=1e-12, atol=1e-12
                )
                try:
                    quant = quantize_public_window(raw)
                    quantized[index] = spectral_input(quant)
                except InputUnavailable as error:
                    reasons[index] = str(error)
                    continue
                if golden is None:
                    golden = {
                        "source": "public TRAIN recording, not measured device fixture",
                        "member": row["member"],
                        "runId": int(identifier),
                        "windowIndex": int(starts[index]),
                        "rawWindowG": raw.tolist(),
                        "expectedClean66": clean[index].tolist(),
                        "stressCounts": np.rint(raw / COUNT_SCALE_G)
                        .astype(int)
                        .tolist(),
                        "expectedQuantized66": quantized[index].tolist(),
                        "numericPolicy": NUMERIC_POLICY,
                    }
            print(
                f"Resolution cache {number+1}/{len(unique)}: {row['condition']} {row['label']}",
                flush=True,
            )
    np.savez_compressed(
        output / "features.npz",
        clean=clean,
        quantized=quantized,
        labels=labels,
        runs=runs,
        starts=starts,
        reasons=reasons,
    )
    dump(
        output / "manifest.json",
        {
            "parentManifestSha256": sha256(parent / "manifest.json"),
            "cacheSha256": sha256(output / "features.npz"),
            "numericPolicy": NUMERIC_POLICY,
            "rows": len(runs),
            "invalidQuantizedWindows": int(np.sum(reasons != "")),
            "testRawFeaturesExtracted": False,
            "validationRawFeaturesExtracted": False,
            "holdoutModelScored": False,
            "quantization": "nearest-even, 0.0039g/LSB; reject rails, never clip to range",
            "fieldValidated": False,
        },
    )
    if golden is not None:
        dump(output / "golden-input.json", golden)


def augment_fit(clean, quantized, starts):
    result = clean.copy()
    result[starts % 2 == 0] = quantized[starts % 2 == 0]
    return result


def summary(labels, runs, starts, groups, scores, thresholds):
    hits = scores > thresholds
    single = decisions(hits, runs, starts, 1, 1)
    confirmed = decisions(hits, runs, starts, 3, 3)
    common = confirmed >= 0
    results = {}
    for name, values in (("single", single), ("three_consecutive", confirmed)):
        folds = []
        for group in sorted(set(groups)):
            selected = (groups == group) & common
            m = metrics(labels[selected], values[selected])
            if m["detectedRecall"] is None or m["falsePositiveRate"] is None:
                raise ValueError("Missing normal or anomaly in condition")
            folds.append({"condition": str(group), **m})
        results[name] = {
            "meanRecall": float(np.mean([f["detectedRecall"] for f in folds])),
            "meanFpr": float(np.mean([f["falsePositiveRate"] for f in folds])),
            "worstFpr": float(max(f["falsePositiveRate"] for f in folds)),
            "folds": folds,
            "allWindows": metrics(labels, values),
        }
    return results


def eligible(results):
    for domain in ("clean", "quantized"):
        baseline = results["clean_fit"][domain]["three_consecutive"]
        candidate = results["half_quantized_fit"][domain]["three_consecutive"]
        if (
            candidate["meanFpr"] > GATE["meanFprMax"]
            or candidate["worstFpr"] > GATE["worstFprMax"]
            or candidate["meanRecall"] < baseline["meanRecall"] - GATE["recallLossMax"]
        ):
            return False
    baseline = results["clean_fit"]["quantized"]["three_consecutive"]
    candidate = results["half_quantized_fit"]["quantized"]["three_consecutive"]
    return (
        candidate["meanFpr"] < baseline["meanFpr"]
        and candidate["meanRecall"] >= baseline["meanRecall"]
    )


def run(parent, prepared, output, reference_oof=None):
    parent, prepared, output = Path(parent), Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new sensor experiment directory")
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    cache_meta = json.loads((prepared / "manifest.json").read_text(encoding="utf-8"))
    if (
        cache_meta["parentManifestSha256"] != sha256(parent / "manifest.json")
        or cache_meta["cacheSha256"] != sha256(prepared / "features.npz")
        or cache_meta["numericPolicy"] != NUMERIC_POLICY
    ):
        raise ValueError("Cache provenance or numeric policy mismatch")
    with np.load(prepared / "features.npz", allow_pickle=False) as data:
        clean, quant, labels, runs, starts, reasons = (
            data[key]
            for key in ("clean", "quantized", "labels", "runs", "starts", "reasons")
        )
    if (
        np.any(reasons != "")
        or not np.isfinite(clean).all()
        or not np.isfinite(quant).all()
    ):
        raise ValueError(
            "Invalid quantized windows require explicit cohort review; never silently drop/zero-fill"
        )
    if (
        clean.shape != (len(labels), 66)
        or quant.shape != clean.shape
        or starts.shape != labels.shape
        or runs.shape != labels.shape
    ):
        raise ValueError("Invalid aligned sensor cache")
    if sha256(parent / "sequences.npz") != manifest["cacheSha256"]:
        raise ValueError("Parent cache checksum mismatch")
    with np.load(parent / "sequences.npz", allow_pickle=False) as base:
        train = base["split"] == "train"
        for actual, key in ((runs, "run"), (labels, "y"), (starts, "start")):
            if not np.array_equal(actual, base[key][train]):
                raise ValueError("Training rows reordered or held-out rows included")
    groups = np.array([manifest["runs"][r]["condition"] for r in runs])
    folds = list(grouped_folds(groups, labels))
    output.mkdir(parents=True)
    protocol = {
        "inputShape": [1, 66],
        "sequenceLength": 1,
        "numericPolicy": NUMERIC_POLICY,
        "fitPolicies": ["clean_fit", "half_quantized_fit"],
        "augmentation": "Replace even window indices ONLY in fit partition; same sample/run count and class/run weights",
        "calibration": "same held calibration condition, clean normal q99 higher; strict >",
        "temporalPolicy": "fixed three_consecutive from previous research, separate from model input",
        "researchGate": GATE,
        "selection": "Both domains pass FPR/recall gates; stress FPR strictly improves without stress recall loss; otherwise no replacement",
        "cacheManifestSha256": sha256(prepared / "manifest.json"),
        "referenceOofSha256": sha256(Path(reference_oof)) if reference_oof else None,
        "modelParameters": candidate_models()["random_forest"].get_params(),
        "folds": [
            {
                "evaluation": str(held),
                "calibration": str(cal),
                "fit": sorted(set(groups[fit])),
            }
            for held, cal, fit, _, _ in folds
        ],
        "inputAgreementSha256": sha256(
            Path(__file__).resolve().parents[3] / "docs/model-input-agreement.md"
        ),
        "testScored": False,
        "validationScored": False,
        "codeSha256": {p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")},
    }
    dump(output / "protocol.json", protocol)
    results = {}
    with threadpool_limits(limits=2):
        for policy in protocol["fitPolicies"]:
            train_features = (
                clean if policy == "clean_fit" else augment_fit(clean, quant, starts)
            )
            cuts, scores_clean, scores_quant = (
                np.zeros(len(labels)),
                np.zeros(len(labels)),
                np.zeros(len(labels)),
            )
            for held, cal, fit, calibrate, evaluate in folds:
                model = candidate_models()["random_forest"]
                model.fit(
                    train_features[fit],
                    labels[fit],
                    sample_weight=balanced_run_weights(labels[fit], runs[fit]),
                )
                threshold = normal_threshold(
                    labels[calibrate], probabilities(model, clean[calibrate])
                )
                cuts[evaluate] = threshold
                scores_clean[evaluate] = probabilities(model, clean[evaluate])
                scores_quant[evaluate] = probabilities(model, quant[evaluate])
                print(f"Sensor fit {policy} held={held} calibration={cal}", flush=True)
            if policy == "clean_fit" and reference_oof:
                with np.load(reference_oof, allow_pickle=False) as reference:
                    np.testing.assert_array_equal(runs, reference["runs"])
                    np.testing.assert_array_equal(labels, reference["labels"])
                    np.testing.assert_allclose(
                        scores_clean, reference["scores"], rtol=0, atol=1e-14
                    )
                    np.testing.assert_allclose(
                        cuts, reference["thresholds"], rtol=0, atol=1e-14
                    )
            results[policy] = {
                "clean": summary(labels, runs, starts, groups, scores_clean, cuts),
                "quantized": summary(labels, runs, starts, groups, scores_quant, cuts),
            }
            np.savez_compressed(
                output / f"{policy}-oof.npz",
                cleanScores=scores_clean,
                quantizedScores=scores_quant,
                thresholds=cuts,
                labels=labels,
                runs=runs,
                starts=starts,
            )
    selected = "half_quantized_fit" if eligible(results) else None
    result = {
        "protocol": protocol,
        "results": results,
        "selected": selected,
        "fieldValidated": False,
        "artifactExported": False,
        "activated": False,
        "referenceBaselineReproduced": reference_oof is not None,
        "limits": [
            "Resolution-only simulation, not measured ADXL noise/mounting/aliasing/timing or accuracy",
            "Repeated train-condition comparisons, not independent/nested CV",
            "No fabricated fault onset or latency/brief-fault guarantee",
            "No change to raw transport, API, storage or operational model",
        ],
    }
    dump(output / "report.json", result)
    dump(output / "selection.json", {"selected": selected, "fieldValidated": False})
    compact = {
        policy: {
            domain: {
                key: value
                for key, value in scores["three_consecutive"].items()
                if key in ("meanRecall", "meanFpr", "worstFpr")
            }
            for domain, scores in domains.items()
        }
        for policy, domains in results.items()
    }
    print(json.dumps({"selected": selected, "summary": compact}, indent=2), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "compare"])
    parser.add_argument("--parent", required=True)
    parser.add_argument("--source")
    parser.add_argument("--spectral")
    parser.add_argument("--prepared")
    parser.add_argument("--reference-oof")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.source or not args.spectral:
            parser.error("prepare requires --source and --spectral")
        prepare(args.source, args.parent, args.spectral, args.output)
    else:
        if not args.prepared:
            parser.error("compare requires --prepared")
        run(args.parent, args.prepared, args.output, args.reference_oof)
