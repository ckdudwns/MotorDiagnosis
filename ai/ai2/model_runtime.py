"""Checkpoint-driven CPU inference. No report-driven defaults or raw resampling.

The artifact is the authority for architecture, feature order, scaler, sequence
length and threshold. A SHA-256 identifies bytes; it is not operational approval.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import zipfile

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
CANDIDATES = {"dense_autoencoder", "lstm_autoencoder"}


def artifact_bytes(path, candidate):
    """Read one bounded member, without extracting or executing archive content."""
    if candidate not in CANDIDATES:
        raise ValueError("Unsupported model candidate")
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > 100:
                raise ValueError("Too many archive members")
            matches = [
                entry
                for entry in archive.infolist()
                if PurePosixPath(entry.filename).name == candidate + ".pt"
            ]
            if len(matches) != 1:
                raise ValueError("Exactly one candidate checkpoint is required")
            entry = matches[0]
            if entry.file_size > MAX_ARTIFACT_BYTES or entry.flag_bits & 1:
                raise ValueError("Oversized or encrypted checkpoint")
            with archive.open(entry) as stream:
                content = stream.read(MAX_ARTIFACT_BYTES + 1)
    else:
        with path.open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
    if not content or len(content) > MAX_ARTIFACT_BYTES:
        raise ValueError("Invalid checkpoint size")
    return content


class Checkpoint:
    def __init__(self, content, *, expected_checksum=None):
        # Optional dependencies are imported only when explicitly configuring a model.
        import numpy as np
        import torch
        from ai.ai1.week4.ai1.freq_baseline import train_and_evaluate as training

        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_ARTIFACT_BYTES:
            raise ValueError("Invalid checkpoint bytes")
        self.checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        if expected_checksum is not None and expected_checksum != self.checksum:
            raise ValueError("Checkpoint checksum mismatch")
        # Hash and load precisely the same bytes; never fall back to unrestricted pickle.
        payload = torch.load(io.BytesIO(content), weights_only=True, map_location="cpu")
        if not isinstance(payload, dict):
            raise ValueError("Checkpoint must be a dictionary")
        training._validate_artifact_payload(payload)
        if payload["input_dim"] > 128 or (payload["seq_len"] or 1) > 128:
            raise ValueError("Unsupported checkpoint dimensions")
        self.kind = payload["model_type"]
        self.names = tuple(payload["feature_names"])
        self.sequence_length = payload["seq_len"] or 1
        self.threshold = float(payload["threshold"])
        self.mean = np.array(payload["scaler_mean"], dtype=np.float64)
        self.std = np.array(payload["scaler_std"], dtype=np.float64)
        self.model = training._MODEL_BUILDERS[self.kind](len(self.names))
        weights = payload.get("state_dict")
        if not isinstance(weights, dict) or set(weights) != set(
            self.model.state_dict()
        ):
            raise ValueError(
                "Checkpoint weights do not match the declared architecture"
            )
        for name, expected in self.model.state_dict().items():
            value = weights[name]
            if (
                not isinstance(value, torch.Tensor)
                or value.shape != expected.shape
                or not value.is_floating_point()
                or not torch.isfinite(value).all()
            ):
                raise ValueError("Invalid checkpoint tensor: " + name)
        self.model.load_state_dict(weights, strict=True)
        self.model.eval()

    @classmethod
    def load(cls, path, *, candidate="lstm_autoencoder", expected_checksum=None):
        result = cls(
            artifact_bytes(path, candidate), expected_checksum=expected_checksum
        )
        if result.kind != candidate:
            raise ValueError("Candidate name and checkpoint model_type disagree")
        return result

    def describe(self):
        return {
            "modelType": self.kind,
            "modelVersion": self.checksum,
            "featureNames": list(self.names),
            "featureCount": len(self.names),
            "sequenceLength": self.sequence_length,
            "threshold": self.threshold,
            "normalization": {"mean": self.mean.tolist(), "std": self.std.tolist()},
            "mode": "shadow",
            "affectsAlerts": False,
            "domainValidated": False,
            "rawAdapterAvailable": False,
            "sampleRateHz": None,
            "preprocessing": None,
            "limitation": "Checkpoint has no raw feature-extraction contract. Prepared features only; no field accuracy or RUL claim.",
        }

    def predict(self, matrix, feature_names):
        import numpy as np
        import torch

        if (
            not isinstance(feature_names, (list, tuple))
            or any(not isinstance(n, str) for n in feature_names)
            or len(feature_names) != len(set(feature_names))
            or set(feature_names) != set(self.names)
        ):
            raise ValueError("Input feature names must match the checkpoint exactly")
        # Reject bool/string/null before numeric coercion (no zero-fill or coercion).
        values = np.asarray(matrix, dtype=object)
        if values.size == 0 or values.size > 128 * 128 * 128:
            raise ValueError("Empty or oversized inference input")
        for value in values.flat:
            if (
                isinstance(value, (bool, np.bool_))
                or not isinstance(value, (int, float, np.integer, np.floating))
                or not math.isfinite(float(value))
            ):
                raise ValueError("Every input feature must be a finite number")
        arr = np.asarray(matrix, dtype=np.float64)
        rank = 3 if self.kind == "lstm_autoencoder" else 2
        if arr.ndim != rank or arr.shape[-1] != len(self.names) or arr.shape[0] > 128:
            raise ValueError("Input shape does not match the checkpoint")
        if rank == 3 and arr.shape[1] != self.sequence_length:
            raise ValueError("Sequence length does not match the checkpoint")
        arr = arr[..., [feature_names.index(name) for name in self.names]]
        with np.errstate(over="ignore", invalid="ignore"):
            normalized = (arr - self.mean) / self.std
        if not np.isfinite(normalized).all():
            raise ValueError("Normalization overflow")
        tensor = torch.tensor(normalized, dtype=torch.float32)
        if not torch.isfinite(tensor).all():
            raise ValueError("Float32 input overflow")
        with torch.inference_mode():
            errors = ((self.model(tensor) - tensor) ** 2).mean(
                dim=tuple(range(1, tensor.ndim))
            )
        if not torch.isfinite(errors).all():
            raise ValueError("Non-finite reconstruction error")
        return {
            "modelVersion": self.checksum,
            "modelType": self.kind,
            "errors": errors.tolist(),
            "verdict": (errors > self.threshold).tolist(),
            "threshold": self.threshold,
            "mode": "shadow",
            "affectsAlerts": False,
            "domainValidated": False,
        }


def contiguous_matrix(windows, checkpoint):
    """One non-overlapping sequence, with explicit source/sample boundaries.

    Claimed preprocessing identity isolates streams, but does not certify that
    those features were generated with the unknown original preprocessing code.
    """
    if len(windows) != checkpoint.sequence_length:
        raise ValueError("Incomplete sequence")
    first = windows[0]
    scope = (
        "deviceId",
        "siteId",
        "assetId",
        "sourceId",
        "preprocessingId",
        "sampleRateHz",
        "modelVersion",
    )
    width = first["windowEndSample"] - first["windowStartSample"]
    rows = []
    for index, window in enumerate(windows):
        if any(window[key] != first[key] for key in scope):
            raise ValueError("Sequence crosses a source or model boundary")
        if (
            window["windowIndex"] != first["windowIndex"] + index
            or window["windowStartSample"] != first["windowStartSample"] + index * width
            or window["windowEndSample"] - window["windowStartSample"] != width
        ):
            raise ValueError("Sequence has a missing, overlapping or irregular window")
        rows.append([window["features"][name] for name in checkpoint.names])
    return [rows] if checkpoint.kind == "lstm_autoencoder" else rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        required=True,
        help="Local .pt or ZIP; training report is not read",
    )
    parser.add_argument(
        "--candidate", choices=sorted(CANDIDATES), default="lstm_autoencoder"
    )
    parser.add_argument("--expected-checksum")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--input", help="JSON with featureNames and matrix in checkpoint shape"
    )
    action.add_argument(
        "--smoke",
        action="store_true",
        help="Synthetic scaler-mean input; not accuracy evaluation",
    )
    parser.add_argument(
        "--output", help="Create a new JSON result, without overwriting"
    )
    args = parser.parse_args()
    model = Checkpoint.load(
        args.artifact,
        candidate=args.candidate,
        expected_checksum=args.expected_checksum,
    )
    result = {"checkpoint": model.describe(), "validation": "not_reproduced"}
    if args.smoke:
        row = model.mean.tolist()
        matrix = (
            [[row for _ in range(model.sequence_length)]]
            if model.kind == "lstm_autoencoder"
            else [row]
        )
        result.update(
            purpose="synthetic_execution_smoke",
            result=model.predict(matrix, list(model.names)),
        )
    elif args.input:
        with Path(args.input).open(encoding="utf-8") as stream:
            example = json.load(stream)
        result.update(
            purpose="prepared_feature_inference",
            result=model.predict(example["matrix"], example["featureNames"]),
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
