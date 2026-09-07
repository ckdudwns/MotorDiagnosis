"""Trusted RF66 handoff loader. Never execute Python bundled in an artifact.

joblib is executable pickle: the caller must supply an independently trusted
model SHA256. A manifest alone is not a trust boundary.
"""

import hashlib
from importlib.metadata import version
import io
import json
import math
from pathlib import Path
import re
import warnings
import zipfile

from .raw_vibration import FEATURE_PROFILE, NAMES

PACKAGES = {"numpy": "2.5.2", "scipy": "1.18.1", "scikit-learn": "1.9.0",
            "joblib": "1.6.0", "threadpoolctl": "3.6.0"}
# Full preprocessing contract, including float32 rounding and FFT conventions.
# A changed contract requires numerical acceptance and a new supported digest.
CONTRACT_SHA256 = "54d50a8af512c0bbc5f43e4fb485f0a9dc1e1e37538b64926ac3c97cc7d88b19"
NUMERIC_POLICY = "base21 cast float32 then promoted float64; extra45 float64"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _contract_digest(contract):
    return _sha(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode())


def _read_package(path, expected_checksum):
    if not isinstance(expected_checksum, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_checksum):
        raise ValueError("RF66 requires an independently trusted sha256:<model digest>")
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("RF66 archive exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(entries) > 64 or len(set(names)) != len(names)
                or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES
                or any(entry.is_dir() or entry.flag_bits & 1 for entry in entries)):
            raise ValueError("Invalid or oversized RF66 archive")
        files = {name: archive.read(name) for name in names}
    manifest = json.loads(files["MANIFEST.json"])
    hashes = manifest["files"]
    if set(hashes) != set(files) - {"MANIFEST.json"}:
        raise ValueError("RF66 manifest membership mismatch")
    if any(_sha(files[name]) != digest for name, digest in hashes.items()):
        raise ValueError("RF66 manifest checksum mismatch")
    model_bytes = files["model/candidate.joblib"]
    if "sha256:" + _sha(model_bytes) != expected_checksum or manifest["modelVersion"] != expected_checksum:
        raise ValueError("RF66 trusted model checksum mismatch")
    spec = json.loads(files["input-contract.json"])
    rule = json.loads(files["decision-rule.json"])
    environment = json.loads(files["environment.json"])
    threshold = spec["threshold"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid RF66 threshold")
    required = {"modelKind": "random_forest", "modelInputShape": [1, 66],
                "sequenceLength": 1, "rawShape": [512, 3], "axes": ["X", "Y", "Z"],
                "unit": "g", "sampleRateHz": 800, "windowHopSamples": 512,
                "numericPolicy": NUMERIC_POLICY, "sklearnVersion": PACKAGES["scikit-learn"],
                "fieldValidated": False, "affectsAlerts": False}
    if any(spec.get(key) != value for key, value in required.items()):
        raise ValueError("Unsupported RF66 input contract")
    if (_contract_digest(spec["contract"]) != CONTRACT_SHA256
            or spec["contract"]["featureNames"] != list(NAMES)):
        raise ValueError("Unsupported RF66 preprocessing or feature order")
    if (rule.get("classes") != [False, True] or any(type(c) is not bool for c in rule["classes"])
            or rule.get("comparison") != ">" or rule.get("threshold") != threshold
            or rule.get("modelSequenceLength") != 1
            or environment.get("packages") != PACKAGES):
        raise ValueError("Unsupported RF66 decision rule or environment")
    return model_bytes, spec


class RF66Model:
    kind = "random_forest"

    def __init__(self, model, checksum, threshold):
        self.model, self.checksum, self.threshold = model, checksum, threshold

    @classmethod
    def load(cls, artifact, *, expected_checksum):
        model_bytes, spec = _read_package(artifact, expected_checksum)
        for package, expected in PACKAGES.items():
            if version(package) != expected:
                raise ValueError("RF66 runtime version mismatch: " + package)
        import joblib
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.exceptions import InconsistentVersionWarning

        with warnings.catch_warnings():
            warnings.simplefilter("error", InconsistentVersionWarning)
            payload = joblib.load(io.BytesIO(model_bytes))
        if not isinstance(payload, dict) or "model" not in payload:
            raise ValueError("Invalid RF66 payload")
        for key in ("modelKind", "contract", "fitPolicy", "threshold", "numericPolicy",
                    "modelInputShape", "sequenceLength", "sklearnVersion", "fieldValidated",
                    "affectsAlerts", "comparisonReportSha256"):
            if key not in payload or payload[key] != spec.get(key):
                raise ValueError("RF66 payload/contract mismatch: " + key)
        model = payload["model"]
        if (type(model) is not RandomForestClassifier or model.n_features_in_ != 66
                or model.n_outputs_ != 1 or model.classes_.dtype.kind != "b"
                or model.classes_.tolist() != [False, True]):
            raise ValueError("RF66 requires a fitted 66-feature binary RandomForestClassifier")
        model.n_jobs = 1
        return cls(model, expected_checksum, float(spec["threshold"]))

    def predict_window(self, values):
        import numpy as np

        matrix = np.asarray(values, dtype=np.float64)
        if matrix.shape != (66,) or not np.isfinite(matrix).all():
            raise ValueError("RF66 requires 66 finite features")
        probabilities = self.model.predict_proba(matrix.reshape(1, 66))
        if (probabilities.shape != (1, 2) or not np.isfinite(probabilities).all()
                or (probabilities < 0).any() or (probabilities > 1).any()
                or not np.isclose(probabilities.sum(), 1.0, rtol=0, atol=1e-12)):
            raise ValueError("Invalid RF66 probabilities")
        score = float(probabilities[0, 1])
        return {"score": score, "threshold": self.threshold,
                "verdict": score > self.threshold}

    def metadata(self):
        return {"modelType": self.kind, "modelVersion": self.checksum,
                "featureProfileId": FEATURE_PROFILE, "threshold": self.threshold,
                "comparison": ">", "scoreType": "anomaly_class_probability",
                "mode": "shadow", "affectsAlerts": False, "fieldValidated": False,
                "domainValidated": False, "confirmationApplied": False}
