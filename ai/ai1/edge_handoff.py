"""Convert a downloaded capture to a checksummed, unlabeled AI1 feature artifact.

Run: python -m ai.ai1.edge_handoff capture.json output.json
No remote fetch, label promotion, dataset freeze or model deployment occurs.
"""

import argparse
import base64
import cmath
import hashlib
import json
import math
import struct
from pathlib import Path

from motor_diagnosis.edge_analysis import normalize_frame


def features(samples, block):
    count = len(samples)
    mean = sum(samples) / count
    variance = sum((value - mean) ** 2 for value in samples) / count
    kurtosis = (
        sum((value - mean) ** 4 for value in samples) / count / variance**2
        if variance
        else None
    )
    bands = [0.0] * 3
    for offset in range(0, count, block):
        part = samples[offset : offset + block]
        average = sum(part) / block
        a = [complex(value - average) for value in part]
        j = 0
        for i in range(1, block):
            bit = block >> 1
            while j & bit:
                j ^= bit
                bit >>= 1
            j ^= bit
            if i < j:
                a[i], a[j] = a[j], a[i]
        size = 2
        while size <= block:
            step = cmath.exp(-2j * math.pi / size)
            for start in range(0, block, size):
                w = 1
                for k in range(size // 2):
                    u, v = a[start + k], a[start + k + size // 2] * w
                    a[start + k], a[start + k + size // 2] = u + v, u - v
                    w *= step
            size *= 2
        for k in range(1, block // 2 + 1):
            band = 0 if k < block // 16 else 1 if k < block // 8 else 2
            bands[band] += (
                abs(a[k]) ** 2
                * (1 if k == block // 2 else 2)
                / block**2
                / (count // block)
            )
    return {
        "rms": math.sqrt(sum(value**2 for value in samples) / count),
        "peak": max(map(abs, samples)),
        "kurtosis": kurtosis,
        "bandEnergy": bands,
    }


def convert(envelope):
    original = envelope["frame"]
    digest = hashlib.sha256(
        json.dumps(
            original, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if digest != envelope.get("sha256"):
        raise ValueError("Capture checksum mismatch")
    frame, _, raw = normalize_frame(original, enforce_retention=False)
    if not raw:
        raise ValueError("A feature summary is not a raw waveform")
    columns, signals, units = {}, {}, {}
    for name, channel in frame["channels"].items():
        samples = list(
            struct.unpack(
                f"<{channel['sampleCount']}f",
                base64.b64decode(channel["samplesFloat32LE"], validate=True),
            )
        )
        extracted = features(samples, 2048 if name == "acoustic" else 512)
        for feature in ("rms", "peak", "kurtosis"):
            columns[f"{name}.{feature}"] = extracted[feature]
        for index, value in enumerate(extracted["bandEnergy"]):
            columns[f"{name}.bandEnergy{index}"] = value
        signals[name] = {
            "samples": samples,
            "samplingRateHz": channel["sampleRateHz"],
            "unit": channel["unit"],
        }
        units[name] = channel["unit"]
    artifact = {
        "schemaVersion": "ai1-edge-handoff-v1",
        "featurePipelineVersion": "edge-statistics-v1",
        "sourceRef": f"analysis:{envelope['id']}:{digest}",
        "sourceSha256": digest,
        "siteId": frame["siteId"],
        "assetId": frame["assetId"],
        "deviceId": frame["deviceId"],
        "timestamp": frame["timestamp"],
        "sequence": frame["sequence"],
        "units": units,
        "features": columns,
        "featureNames": sorted(columns),
        "signals": signals,
        "target_label": None,
        "label_status": "unlabeled",
        "training_eligible": False,
        "limitations": [
            "Checksum is not an origin signature",
            "Curate labels, splits and unit-compatible datasets before training",
            "Constant-window kurtosis is missing, not zero",
        ],
    }
    artifact["artifactSha256"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    converted = convert(json.loads(args.capture.read_text(encoding="utf-8")))
    # Explicit no-overwrite: source captures and prior handoffs are immutable.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(converted, handle, ensure_ascii=False, allow_nan=False, indent=2)


if __name__ == "__main__":
    main()
