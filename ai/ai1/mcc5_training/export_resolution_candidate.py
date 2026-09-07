"""Export only a gate-passing research candidate; no operational activation.

Existing validation NORMAL records calibrate the final threshold, not selection.
"""

import argparse
import io
import json
from pathlib import Path

import joblib
import numpy as np
import sklearn
from threadpoolctl import threadpool_limits

from .compare import balanced_run_weights, candidate_models, probabilities
from .download import sha256
from .field_collection import dump
from .raw_input import spectral_input, counts_to_g, NUMERIC_POLICY
from .sensor_robustness import augment_fit, eligible
from .spectral import load_spectral, CONTRACT


def export(parent, spectral, cache, experiment, output):
    parent, spectral, cache, experiment, output = map(
        Path, (parent, spectral, cache, experiment, output)
    )
    if output.exists():
        raise FileExistsError("Use a new research candidate directory")
    report = json.loads((experiment / "report.json").read_text(encoding="utf-8"))
    meta = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    if (
        report["selected"] != "half_quantized_fit"
        or not eligible(report["results"])
        or report["protocol"]["cacheManifestSha256"] != sha256(cache / "manifest.json")
        or meta["cacheSha256"] != sha256(cache / "features.npz")
        or meta["parentManifestSha256"] != sha256(parent / "manifest.json")
    ):
        raise ValueError("Candidate gate or provenance mismatch")
    manifest, all_x, all_y, all_runs, split = load_spectral(parent, spectral)
    train, normal_calibration = split == "train", (split == "validation") & ~all_y
    with np.load(cache / "features.npz", allow_pickle=False) as data:
        clean, quant, runs, labels, starts, reasons = (
            data[key]
            for key in ("clean", "quantized", "runs", "labels", "starts", "reasons")
        )
    np.testing.assert_array_equal(clean, all_x[train])
    np.testing.assert_array_equal(runs, all_runs[train])
    np.testing.assert_array_equal(labels, all_y[train])
    with np.load(parent / "sequences.npz", allow_pickle=False) as base:
        np.testing.assert_array_equal(starts, base["start"][base["split"] == "train"])
    if (
        np.any(reasons != "")
        or not np.isfinite(quant).all()
        or quant.shape != clean.shape
    ):
        raise ValueError("Invalid stress features")
    if normal_calibration.sum() == 0:
        raise ValueError("Missing existing validation normals")
    model = candidate_models()["random_forest"]
    if model.get_params() != report["protocol"]["modelParameters"]:
        raise ValueError("Model parameters differ from selected comparison")
    with threadpool_limits(limits=2):
        model.fit(
            augment_fit(clean, quant, starts),
            labels,
            sample_weight=balanced_run_weights(labels, runs),
        )
        scores = probabilities(model, all_x[normal_calibration])
    threshold = float(np.quantile(scores, 0.99, method="higher"))
    payload = {
        "model": model,
        "modelKind": "random_forest",
        "contract": CONTRACT,
        "fitPolicy": "half_quantized_fit",
        "threshold": threshold,
        "numericPolicy": NUMERIC_POLICY,
        "modelInputShape": [1, 66],
        "sequenceLength": 1,
        "sklearnVersion": sklearn.__version__,
        "fieldValidated": False,
        "affectsAlerts": False,
        "comparisonReportSha256": sha256(experiment / "report.json"),
    }
    output.mkdir(parents=True)
    artifact = output / "candidate.joblib"
    joblib.dump(payload, artifact, compress=3)
    digest = sha256(artifact)
    content = artifact.read_bytes()
    if sha256(artifact) != digest:
        raise ValueError("Candidate changed during export")
    # Only bytes produced here are deserialized; no arbitrary input pickle loader.
    restored = joblib.load(io.BytesIO(content))
    np.testing.assert_allclose(
        probabilities(restored["model"], all_x[normal_calibration]),
        scores,
        rtol=0,
        atol=1e-14,
    )
    golden = json.loads((cache / "golden-input.json").read_text(encoding="utf-8"))
    clean_golden = spectral_input(golden["rawWindowG"])
    quant_golden = spectral_input(counts_to_g(golden["stressCounts"]))
    np.testing.assert_allclose(
        clean_golden, golden["expectedClean66"], rtol=1e-12, atol=1e-12
    )
    np.testing.assert_array_equal(quant_golden, golden["expectedQuantized66"])
    golden_scores = probabilities(
        restored["model"], np.stack([clean_golden, quant_golden])
    ).tolist()
    dump(
        output / "golden.json",
        {
            **golden,
            "modelSha256": digest,
            "expectedScoresCleanQuantized": golden_scores,
            "expectedAnomalyCleanQuantized": [s > threshold for s in golden_scores],
            "threshold": threshold,
            "featureNames": CONTRACT["featureNames"],
        },
    )
    spec = {key: value for key, value in payload.items() if key != "model"}
    dump(
        output / "input-contract.json",
        {
            **spec,
            "rawShape": [512, 3],
            "axes": ["X", "Y", "Z"],
            "unit": "g",
            "sampleRateHz": 800,
            "windowHopSamples": 512,
            "approvalStatus": "research only; pending field and numerical acceptance",
        },
    )
    summary = {
        "modelSha256": digest,
        "bytes": artifact.stat().st_size,
        "threshold": threshold,
        "fitWindows": len(labels),
        "normalCalibrationWindows": int(normal_calibration.sum()),
        "calibration": "Existing validation NORMAL q99 higher, strict >; reused, not independent accuracy evaluation",
        "testScored": False,
        "validationFaultsScored": False,
        "fieldValidated": False,
        "activated": False,
        "sourceDOI": manifest["sourceDOI"],
        "sourceLicense": manifest["sourceLicense"],
        "authors": manifest["authors"],
        "inputAgreementSha256": report["protocol"]["inputAgreementSha256"],
        "comparisonReportSha256": sha256(experiment / "report.json"),
        "codeSha256": {p.name: sha256(p) for p in Path(__file__).parent.glob("*.py")},
        "limits": [
            "Research joblib is NOT compatible with the current Dense/PyTorch live loader",
            "Never load an untrusted joblib; verify source/hash and a supported loader",
            "Resolution simulation does not establish sensor/mounting/field accuracy",
            "No deployed model or alert policy changed; raw XYZ transport and persistence still required",
        ],
    }
    dump(output / "report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parent", "spectral", "cache", "experiment", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    export(args.parent, args.spectral, args.cache, args.experiment, args.output)
