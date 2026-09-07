"""Verify pinned archives and produce immutable, run-grouped 800 Hz features."""

import argparse
import collections
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np

from .download import ARCHIVES, REVISION, sha256
from .features import PROFILE, WINDOW, extract, make_sequences, to_800hz

PATTERN = re.compile(
    r"^(?P<label>.+)_(?P<mode>speed|torque)_circulation_(?P<load>\d+)Nm_(?P<speed>\d+)rpm(?:_(?P<time>\d{12})(?P<suffix>d)?)?\.csv$",
    re.I,
)
LABELS = {
    "health",
    "bearing_ball_h",
    "bearing_ball_l",
    "bearing_inner_h",
    "bearing_inner_l",
    "bearing_outer_h",
    "bearing_outer_l",
    "bearing_outer_h_and_inner_h",
    "bend",
    "broken_bar",
    "broken_bar_and_bearing_inner_h",
    "broken_bar_and_bearing_outer_h",
    "dynamic_eccentricity",
    "dynamic_eccentricity_and_bearing_inner_h",
    "dynamic_eccentricity_and_bearing_outer_h",
    "static_eccentricity_h",
    "static_eccentricity_l",
    "static_eccentricity_h_and_bearing_inner_h",
    "static_eccentricity_h_and_bearing_outer_h",
    "winding_h",
    "winding_l",
    "winding_h_and_bearing_inner_h",
    "winding_h_and_bearing_outer_h",
    "unbalance",
    "imbalance",
    "voltage_unbalance_h",
    "voltage_unbalance_l",
}


def metadata(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe archive member")
    if (
        path.name.startswith("._")
        or "__MACOSX" in path.parts
        or path.suffix.lower() != ".csv"
    ):
        return None
    match = PATTERN.fullmatch(path.name)
    if not match:
        raise ValueError(f"Unknown source filename: {name}")
    row = match.groupdict()
    row["label"] = row["label"].lower()
    if row["label"] not in LABELS:
        raise ValueError(f"Unreviewed fault label: {row['label']}")
    row["condition"] = f"{row['mode']}_{row['load']}Nm_{row['speed']}rpm"
    row["member"] = name
    return row


def split_conditions(rows, seed=42):
    """Hold out entire speed/load protocols across all labels, not random windows."""
    normal = {r["condition"] for r in rows if r["label"] == "health"}
    if {r["condition"] for r in rows} != normal:
        raise ValueError("Each operating condition requires an observed healthy run")
    mapping = {}
    for mode in ("speed", "torque"):
        conditions = sorted(c for c in normal if c.startswith(mode + "_"))
        if len(conditions) < 3:
            raise ValueError(
                "Need at least three independent operating profiles per mode"
            )
        conditions.sort(
            key=lambda c: hashlib.sha256(f"{seed}:{c}".encode()).hexdigest()
        )
        for index, condition in enumerate(conditions):
            mapping[condition] = (
                "test" if index == 0 else "validation" if index == 1 else "train"
            )
    return mapping


def parse_csv(body):
    array = np.loadtxt(io.BytesIO(body), delimiter=",", dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 9 or not np.isfinite(array).all():
        raise ValueError(
            "Require finite 9-column time+8 signal CSV; no inferred column mapping"
        )
    time = array[:, 0]
    if len(time) < 8192 or np.any(np.diff(time) <= 0):
        raise ValueError("Missing or invalid monotonic sample clock")
    # Decimal ASCII time may be rounded; check every timestamp against its grid.
    if np.max(np.abs((time - time[0]) - np.arange(len(time)) / 12800)) > 0.000051:
        raise ValueError("Sample clock does not agree with 12.8 kHz")
    return array[:, 3:6]


def prepare(source, output):
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError("Prepared output must be a new directory")
    rows, archives = [], []
    for name, (size, digest) in ARCHIVES.items():
        path = source / name
        if path.stat().st_size != size or sha256(path) != digest:
            raise ValueError(f"Unverified source archive: {name}")
        archives.append({"file": name, "bytes": size, "sha256": digest})
        with zipfile.ZipFile(path) as zipped:
            for info in zipped.infolist():
                row = metadata(info.filename)
                if row:
                    if info.file_size > 200_000_000 or info.flag_bits & 1:
                        raise ValueError("Oversized/encrypted record")
                    rows.append(
                        {
                            **row,
                            "archive": name,
                            "uncompressedBytes": info.file_size,
                            "crc32": info.CRC,
                        }
                    )
    if len({(r["archive"], r["member"]) for r in rows}) != len(rows):
        raise ValueError("Duplicate archive member")
    splits = split_conditions(rows)
    output.mkdir(parents=True)
    chunks, targets, run_ids, starts, split_ids = [], [], [], [], []
    examples, hashes = {}, set()
    opened = {name: zipfile.ZipFile(source / name) for name in ARCHIVES}
    try:
        for run_id, row in enumerate(rows):
            body = opened[row["archive"]].read(row["member"])  # verifies member CRC
            digest = hashlib.sha256(body).hexdigest()
            if digest in hashes:
                raise ValueError("Duplicate recording contents; split must be reviewed")
            hashes.add(digest)
            volts = parse_csv(body)
            reduced = to_800hz(volts)
            valid, reasons = [], collections.Counter()
            for index in range(len(reduced) // WINDOW):
                window = reduced[index * WINDOW : (index + 1) * WINDOW]
                try:
                    values = extract(window)
                except ValueError as error:
                    reasons[str(error)] += 1
                    continue
                valid.append((index, values))
                if not examples:
                    examples = {
                        "rawWindowG": window.tolist(),
                        "expectedFeatures": values.tolist(),
                        "runId": run_id,
                        "windowIndex": index,
                    }
            sequences, first_windows = make_sequences(valid)
            if not sequences:
                raise ValueError(f"No valid sequences: {row['member']}")
            count = len(sequences)
            chunks.extend(sequences)
            targets.extend([row["label"] != "health"] * count)
            run_ids.extend([run_id] * count)
            starts.extend(first_windows)
            split_ids.extend([splits[row["condition"]]] * count)
            row.update(
                {
                    "sha256": digest,
                    "rawSamples": len(volts),
                    "split": splits[row["condition"]],
                    "validWindows": len(valid),
                    "excludedWindows": dict(reasons),
                    "sequences": count,
                }
            )
            print(
                f"Prepared {run_id+1}/{len(rows)} {row['label']} {row['condition']}: {count} sequences",
                flush=True,
            )
    finally:
        for handle in opened.values():
            handle.close()
    cache = output / "sequences.npz"
    np.savez_compressed(
        cache,
        x=np.asarray(chunks, dtype=np.float32),
        y=np.asarray(targets, dtype=np.int8),
        run=np.asarray(run_ids),
        start=np.asarray(starts),
        split=np.asarray(split_ids),
    )
    manifest = {
        "sourceDOI": "10.17632/6s3dggj9mw.1",
        "sourceRevision": REVISION,
        "sourceLicense": "Mendeley CC BY 4.0; linked HF card MIT; retain attribution and both notices",
        "authors": "Shijin Chen, Zeyi Liu, Chenyang Li, Dongliang Zou, Xiao He, Donghua Zhou",
        "profile": PROFILE,
        "archives": archives,
        "runs": rows,
        "seed": 42,
        "splitByCondition": splits,
        "cacheSha256": sha256(cache),
        "holdout": "operating-profile holdout; NOT independent motor/specimen or ADXL345 evaluation",
    }
    for filename, content in (
        ("manifest.json", manifest),
        ("golden-window.json", examples),
    ):
        with (output / filename).open("x", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(
        f"Prepared {len(rows)} recordings, {len(chunks)} sequences; {output}",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
