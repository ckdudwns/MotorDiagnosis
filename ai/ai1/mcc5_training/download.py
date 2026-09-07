"""Fetch pinned upstream archives linked by Mendeley DOI 10.17632/6s3dggj9mw.1.

Uses public HTTPS only, validates exact sizes and SHA256, and retains interrupted
downloads as .part files. No extraction, execution, credentials, or deployment.
"""

import argparse
import hashlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REVISION = "897bc5d2504980418249f98e733bfd7f61056186"
BASE = f"https://huggingface.co/datasets/Samlzy/MCC5-THU-Motor/resolve/{REVISION}/"
ARCHIVES = {
    "MCC5-THU Motor_speed_circulation.zip": (
        6202740516,
        "b82afc81667b4c08261b22b366020b49c3d312d92eb3256a7af08846020f4bc3",
    ),
    "MCC5-THU Motor_torque_circulation.zip": (
        6513549892,
        "cf5007378e4a28e6d7cc100bf56f144166cadb6a2607fbf38614bbfd0b8af0fb",
    ),
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_range(name, start, end, size):
    url = BASE + urllib.parse.quote(name) + f"?download=true&range={start}-{end}"
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if (
            response.status != 206
            or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}"
        ):
            raise ValueError("Server did not honor the exact requested byte range")
        if not response.url.startswith("https://"):
            raise ValueError("HTTPS required")
        result = response.read(end - start + 2)
        if len(result) != end - start + 1:
            raise ValueError("Incorrect range response length")
        return result


def download(destination, workers=4):
    if not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for name, (size, expected) in ARCHIVES.items():
        target = destination / name
        partial = target.with_suffix(".zip.part")
        if target.is_symlink() or partial.is_symlink():
            raise ValueError("Refusing symlink download target")
        if target.exists():
            if target.stat().st_size != size or sha256(target) != expected:
                raise ValueError(f"Existing archive differs: {name}")
            print(f"Verified existing {name}", flush=True)
            continue
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > size:
            raise ValueError("Partial file exceeds expected archive size")
        if offset < size:
            # Bounded batches, ordered append: at most workers * 16 MiB buffered.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                last_report = time.monotonic()
                with partial.open("ab" if offset else "xb") as output:
                    while offset < size:
                        ranges = [
                            (start, min(start + (16 << 20), size) - 1)
                            for start in range(
                                offset,
                                min(size, offset + workers * (16 << 20)),
                                16 << 20,
                            )
                        ]
                        pending = [
                            pool.submit(fetch_range, name, start, end, size)
                            for start, end in ranges
                        ]
                        for future in pending:
                            chunk = future.result()
                            output.write(chunk)
                            offset += len(chunk)
                        output.flush()
                        if time.monotonic() - last_report > 10:
                            print(
                                f"{name}: {offset / size:.1%} ({offset:,}/{size:,})",
                                flush=True,
                            )
                            last_report = time.monotonic()
        if sha256(partial) != expected:
            raise ValueError("SHA256 mismatch; .part retained, not accepted")
        partial.rename(target)
        print(f"Verified SHA256 {name}: {expected}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    download(args.destination, args.workers)
