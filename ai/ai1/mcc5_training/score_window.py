"""Local 800 Hz XYZ single-window scoring; never sends results to a server."""

import argparse
import io
import json
import math
from pathlib import Path

import torch

from ai.ai2.model_runtime import Checkpoint, artifact_bytes
from .features import NAMES, PROFILE, extract


def score_window(artifact, checksum, raw):
    if not isinstance(checksum, str) or not checksum.startswith("sha256:"):
        raise ValueError("Explicit sha256 checksum required")
    content = artifact_bytes(artifact, "dense_autoencoder")
    checkpoint = Checkpoint(content, expected_checksum=checksum)
    payload = torch.load(io.BytesIO(content), weights_only=True, map_location="cpu")
    if (
        payload.get("preprocessing") != PROFILE
        or checkpoint.kind != "dense_autoencoder"
    ):
        raise ValueError("Artifact does not implement this exact raw feature contract")
    if (
        raw.get("profileId") != PROFILE["id"]
        or raw.get("sampleRateHz") != 800
        or raw.get("unit") != "g"
        or raw.get("channels") != PROFILE["channels"]
    ):
        raise ValueError("Raw sampling/channel/unit contract differs")
    if raw.get("quality") != "valid":
        raise ValueError(
            "Explicit valid input quality required; unknown cannot be normal"
        )
    window = raw.get("rawWindowG")
    if not isinstance(window, list) or len(window) != 512:
        raise ValueError("Expected a single 512 x 3 window")
    for row in window:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("Expected XYZ triples")
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in row
        ):
            raise ValueError(
                "Every raw value must be finite numeric, not boolean/string"
            )
    values = extract(window)
    result = checkpoint.predict([values.tolist()], NAMES)
    return {
        **result,
        "profileId": PROFILE["id"],
        "sampleRateHz": 800,
        "inputStatus": "evaluated",
        "fieldValidated": False,
        "affectsAlerts": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    with Path(args.input).open("rb") as source:
        content = source.read(128 * 1024 + 1)
    if len(content) > 128 * 1024:
        raise ValueError("Input file too large")
    print(
        json.dumps(
            score_window(args.artifact, args.checksum, json.loads(content)),
            indent=2,
            allow_nan=False,
        )
    )
