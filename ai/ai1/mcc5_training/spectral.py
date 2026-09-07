"""Raw XYZ spectral ablation: train-condition CV only, never test scoring."""

import argparse
import hashlib
import io
import json
import zipfile
from contextlib import ExitStack
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
from .download import ARCHIVES, sha256
from .features import NAMES, PROFILE, RATE, WINDOW, extract, to_800hz
from .prepare import parse_csv
from .stability import grouped_folds
from .train import load_prepared

FINE_BANDS = (
    (0, 25),
    (25, 50),
    (50, 75),
    (75, 100),
    (100, 150),
    (150, 200),
    (200, 275),
    (275, 350),
)
EXTRA_NAMES = [
    f"vibration{axis}.{name}"
    for axis in "XYZ"
    for name in (
        [f"ratio_{lo}_{hi}" for lo, hi in FINE_BANDS]
        + [
            "entropy_normalized",
            "centroid_hz",
            "bandwidth_hz",
            "peak_hz",
            "peak_power_fraction",
            "rolloff85_hz",
        ]
    )
] + ["correlation.XY", "correlation.XZ", "correlation.YZ"]
CONTRACT = {
    "id": "mcc5-vibration-800hz-spectral66-v1",
    "baseProfile": PROFILE,
    "featureNames": NAMES + EXTRA_NAMES,
    "featureCount": 66,
    "windowHopSamples": WINDOW,
    "analyzeEveryWindow": True,
    "numericPolicy": "base21 cast float32 then promoted float64; extra45 float64",
    "spectralSupportHz": [0, 350],
    "supportBounds": "strictly positive FFT bins below 350Hz; DC excluded",
    "fineBandsHz": [list(b) for b in FINE_BANDS],
    "relativePower": "band power / total power of strictly positive bins below 350Hz",
    "entropy": "-sum(p*ln(p))/ln(number of support bins), 0*ln(0)=0",
    "centroid": "sum(p*f)",
    "bandwidth": "sqrt(sum(p*(f-centroid)^2))",
    "peak": "frequency of first maximum support bin; its fraction of support power",
    "rolloff": "first frequency with cumulative support power >= 0.85",
    "correlation": "signed Pearson correlation of mean-removed raw XYZ, no Hann",
    "fieldValidated": False,
    "affectsAlerts": False,
}
CANDIDATES = {
    "rf21": ("random_forest", 21),
    "rf66": ("random_forest", 66),
    "hgb21": ("hist_gradient_boosting", 21),
    "hgb66": ("hist_gradient_boosting", 66),
}


def extract66(window):
    """Same base21 contract; extra features use only measured in-band XYZ."""
    base = extract(window)  # shape, clipping, constant axis, finite checks
    raw = np.asarray(window, dtype=np.float64)
    centered = raw - raw.mean(axis=0)
    taper = np.hanning(WINDOW + 1)[:-1]
    spectrum = np.abs(np.fft.rfft(centered * taper[:, None], axis=0)) ** 2
    freq = np.fft.rfftfreq(WINDOW, 1 / RATE)
    mask = (freq > 0) & (freq < 350)
    freq, spectrum = freq[mask], spectrum[mask]
    energy = spectrum.sum(axis=0)
    if np.any(energy <= 1e-16):
        raise ValueError("Insufficient spectral support energy")
    fractions = spectrum / energy
    values = list(base)
    for axis in range(3):
        p = fractions[:, axis]
        values.extend(
            float(p[(freq >= lo) & (freq < hi)].sum()) for lo, hi in FINE_BANDS
        )
        positive = p > 0
        centroid = float(p @ freq)
        peak = int(np.argmax(p))
        rolloff = min(int(np.searchsorted(np.cumsum(p), 0.85)), len(freq) - 1)
        values.extend(
            [
                float(-(p[positive] @ np.log(p[positive])) / np.log(len(p))),
                centroid,
                float(np.sqrt(p @ ((freq - centroid) ** 2))),
                float(freq[peak]),
                float(p[peak]),
                float(freq[rolloff]),
            ]
        )
    norm = np.sqrt(np.sum(centered**2, axis=0))
    for a, b in ((0, 1), (0, 2), (1, 2)):
        values.append(
            float(np.clip(centered[:, a] @ centered[:, b] / (norm[a] * norm[b]), -1, 1))
        )
    result = np.asarray(values)
    if result.shape != (66,) or not np.isfinite(result).all():
        raise ValueError("Invalid spectral features")
    return result


def prepare(source, parent, output):
    """Re-read verified train/validation records, align every row to base cache."""
    source, parent, output = Path(source), Path(parent), Path(output)
    if output.exists():
        raise FileExistsError("Use a new spectral cache directory")
    manifest, base, _, runs, split = load_prepared(parent)
    with np.load(parent / "sequences.npz", allow_pickle=False) as cache:
        starts = cache["start"].copy()
    selected = np.flatnonzero(np.isin(split, ["train", "validation"]))
    if starts.shape != runs.shape or len(selected) == 0:
        raise ValueError("Missing aligned windows")
    for name, (size, digest) in ARCHIVES.items():
        path = source / name
        if path.stat().st_size != size or sha256(path) != digest:
            raise ValueError("Unverified source archive")
    output.mkdir(parents=True)
    features = np.empty((len(selected), 66), dtype=np.float64)
    with ExitStack() as stack:
        archives = {
            name: stack.enter_context(zipfile.ZipFile(source / name))
            for name in ARCHIVES
        }
        ids = np.unique(runs[selected])
        for number, run in enumerate(ids):
            row = manifest["runs"][run]
            if row["split"] not in ("train", "validation"):
                raise ValueError("Test recording forbidden")
            info = archives[row["archive"]].getinfo(row["member"])
            if (
                info.file_size != row["uncompressedBytes"]
                or info.file_size > 200_000_000
                or info.flag_bits & 1
            ):
                raise ValueError("Invalid source record")
            body = archives[row["archive"]].read(info)
            if hashlib.sha256(body).hexdigest() != row["sha256"]:
                raise ValueError("Source record hash mismatch")
            reduced = to_800hz(parse_csv(body))
            local = np.flatnonzero(runs[selected] == run)
            for i in local:
                original = selected[i]
                start = int(starts[original]) * WINDOW
                features[i] = extract66(reduced[start : start + WINDOW])
            np.testing.assert_allclose(
                features[local, :21], base[selected[local]], rtol=1e-6, atol=1e-12
            )
            print(
                f"Spectral raw {number+1}/{len(ids)}: {row['condition']} {row['label']}",
                flush=True,
            )
    np.savez_compressed(output / "features.npz", x=features, parentRow=selected)
    write_json(
        output / "manifest.json",
        {
            "contract": CONTRACT,
            "parentManifestSha256": sha256(parent / "manifest.json"),
            "cacheSha256": sha256(output / "features.npz"),
            "rows": len(features),
            "recordings": len(ids),
            "testFeaturesExtracted": False,
            "sourceCodeSha256": {
                p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")
            },
        },
    )


def load_spectral(parent, prepared):
    parent, prepared = Path(parent), Path(prepared)
    manifest, base, labels, runs, split = load_prepared(parent)
    meta = json.loads((prepared / "manifest.json").read_text(encoding="utf-8"))
    if (
        meta["contract"] != CONTRACT
        or meta["parentManifestSha256"] != sha256(parent / "manifest.json")
        or meta["cacheSha256"] != sha256(prepared / "features.npz")
    ):
        raise ValueError("Spectral cache provenance mismatch")
    with np.load(prepared / "features.npz", allow_pickle=False) as cache:
        x, index = cache["x"].copy(), cache["parentRow"].copy()
    expected = np.flatnonzero(np.isin(split, ["train", "validation"]))
    if (
        not np.issubdtype(index.dtype, np.integer)
        or not np.array_equal(index, expected)
        or x.shape != (len(expected), 66)
        or not np.isfinite(x).all()
        or len(expected) != meta["rows"]
    ):
        raise ValueError("Invalid spectral cache rows")
    np.testing.assert_allclose(x[:, :21], base[expected], rtol=1e-6, atol=1e-12)
    # Use the original float32 base for identical baseline comparisons.
    x[:, :21] = base[expected]
    return manifest, x, labels[expected], runs[expected], split[expected]


def select(results):
    return min(
        results,
        key=lambda k: (
            -results[k]["meanBalancedAccuracy"],
            results[k]["worstFpr"],
            CANDIDATES[k][1],
            k,
        ),
    )


def run(parent, prepared, output):
    parent, prepared, output = Path(parent), Path(prepared), Path(output)
    if output.exists():
        raise FileExistsError("Use a new spectral experiment directory")
    manifest, all_x, all_y, all_runs, split = load_spectral(parent, prepared)
    if not set(split) <= {"train", "validation"}:
        raise ValueError("Test rows forbidden")
    train, validation = split == "train", split == "validation"
    x, y, runs = all_x[train], all_y[train], all_runs[train]
    groups = np.array([manifest["runs"][r]["condition"] for r in runs])
    folds = grouped_folds(groups, y)
    output.mkdir(parents=True)
    protocol = {
        "candidates": CANDIDATES,
        "contract": CONTRACT,
        "selection": "highest mean condition-fold balanced accuracy; lowest worst FPR; fewer features; name",
        "calibration": "normal q99 higher, strict score > threshold; fixed before evaluation",
        "folds": [
            {
                "evaluation": str(h),
                "calibration": str(c),
                "fit": sorted(np.unique(groups[f]).tolist()),
            }
            for h, c, f, _, _ in folds
        ],
        "modelParameters": {k: v.get_params() for k, v in candidate_models().items()},
        "parentManifestSha256": sha256(parent / "manifest.json"),
        "spectralManifestSha256": sha256(prepared / "manifest.json"),
        "testScored": False,
        "validationUsedForSelection": False,
    }
    write_json(output / "protocol.json", protocol)
    results = {}
    with threadpool_limits(limits=2):
        for name, (kind, width) in CANDIDATES.items():
            details = []
            oof, thresholds = np.zeros(len(y)), np.zeros(len(y))
            for held, cal, fit, calibrate, evaluate in folds:
                model = candidate_models()[kind]
                model.fit(
                    x[fit, :width],
                    y[fit],
                    sample_weight=balanced_run_weights(y[fit], runs[fit]),
                )
                threshold = normal_threshold(
                    y[calibrate], probabilities(model, x[calibrate, :width])
                )
                score = probabilities(model, x[evaluate, :width])
                detail = grouped_metrics(
                    y[evaluate], score, runs[evaluate], manifest, threshold
                )
                details.append(
                    {
                        "evaluation": str(held),
                        "calibration": str(cal),
                        "threshold": threshold,
                        "metrics": detail,
                    }
                )
                oof[evaluate], thresholds[evaluate] = score, threshold
                m = detail["overall"]
                print(
                    f"{name} {held}: recall={m['recall']:.4f} FPR={m['falsePositiveRate']:.4f}",
                    flush=True,
                )
            stats = [d["metrics"]["overall"] for d in details]
            results[name] = {
                "folds": details,
                "meanBalancedAccuracy": float(
                    np.mean([m["balancedAccuracy"] for m in stats])
                ),
                "meanRecall": float(np.mean([m["recall"] for m in stats])),
                "meanFpr": float(np.mean([m["falsePositiveRate"] for m in stats])),
                "worstFpr": float(max(m["falsePositiveRate"] for m in stats)),
                "worstRecall": float(min(m["recall"] for m in stats)),
            }
            np.savez_compressed(
                output / f"{name}-oof.npz",
                scores=oof,
                thresholds=thresholds,
                labels=y,
                runs=runs,
            )
        selected = select(results)
        write_json(
            output / "selection.json", {"selected": selected, "protocol": protocol}
        )
        kind, width = CANDIDATES[selected]
        model = candidate_models()[kind]
        model.fit(x[:, :width], y, sample_weight=balanced_run_weights(y, runs))
        val_x = all_x[validation, :width]
        score = probabilities(model, val_x)
        threshold = normal_threshold(all_y[validation], score)
        payload = {
            "model": model,
            "contract": CONTRACT if width == 66 else PROFILE,
            "modelKind": kind,
            "threshold": threshold,
            "sklearnVersion": sklearn.__version__,
            "fieldValidated": False,
            "affectsAlerts": False,
        }
        artifact = output / "candidate.joblib"
        joblib.dump(payload, artifact, compress=3)
        digest = sha256(artifact)
        content = artifact.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Artifact changed")
        # Only reload bytes produced by this invocation. Never untrusted joblib.
        restored = joblib.load(io.BytesIO(content))
        np.testing.assert_allclose(
            probabilities(restored["model"], val_x), score, rtol=0, atol=1e-14
        )
        golden = json.loads((parent / "golden-window.json").read_text(encoding="utf-8"))
        features = extract66(golden["rawWindowG"])
        np.testing.assert_allclose(
            features[:21], golden["expectedFeatures"], rtol=1e-10, atol=1e-12
        )
        features[:21] = features[:21].astype(np.float32)
        value = float(probabilities(restored["model"], features[None, :width])[0])
        write_json(
            output / "golden.json",
            {
                **golden,
                "modelFeatures": features[:width].tolist(),
                "contract": payload["contract"],
                "modelSha256": digest,
                "expectedScore": value,
                "expectedAnomaly": value > threshold,
                "numericPolicy": "base21 cast float32 then promoted float64; extra45 float64",
            },
        )
    report = {
        "protocol": protocol,
        "selected": selected,
        "results": results,
        "artifactSha256": digest,
        "threshold": threshold,
        "validationReusedCalibrationDiagnostic": grouped_metrics(
            all_y[validation], score, all_runs[validation], manifest, threshold
        ),
        "testScored": False,
        "fieldValidated": False,
        "activated": False,
        "limits": [
            "Model selection reuses these folds: not unbiased nested CV or a new test result",
            "RF/HGB families and original validation were inspected in prior experiments",
            "Calibration uses one correlated healthy recording per fold, not independent normal samples",
            "Public resampling does not simulate ADXL345 noise, quantization, aliasing or mounting",
            "No audio, RPM, current or torque model inputs; no unseen-fault or field validation",
            "Research joblib is not live runtime compatible; new66 needs new edge extraction or raw collection",
        ],
        "versions": {
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
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
    parser.add_argument("action", choices=["prepare", "compare"])
    parser.add_argument("--parent", required=True)
    parser.add_argument("--source", help="Verified raw archive folder for prepare")
    parser.add_argument("--prepared", help="Spectral cache folder for compare")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.source:
            parser.error("prepare requires --source")
        prepare(args.source, args.parent, args.output)
    else:
        if not args.prepared:
            parser.error("compare requires --prepared")
        run(args.parent, args.prepared, args.output)
