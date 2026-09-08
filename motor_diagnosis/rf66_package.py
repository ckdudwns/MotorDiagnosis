"""RF66 archive integrity checks shared by inference and offline maintenance.

Standard library only: this module never imports or deserializes a model. The
trusted checksum identifies model/candidate.joblib, NOT its ZIP container.
"""

import hashlib
import io
import json
from pathlib import Path
import re
import zipfile
import zlib

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def read_package_files(path, expected_checksum, *, max_bytes=MAX_ARCHIVE_BYTES):
    """Validate the archive and independently pinned model, returning raw bytes."""
    if not isinstance(expected_checksum, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_checksum
    ):
        raise ValueError("RF66 requires an independently trusted sha256:<model digest>")
    with Path(path).open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("RF66 archive exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(entries) > 64
                or len(set(names)) != len(names)
                or sum(entry.file_size for entry in entries) > max_bytes
                or any(entry.is_dir() or entry.flag_bits & 1 for entry in entries)
            ):
                raise ValueError("Invalid or oversized RF66 archive")
            files = {name: archive.read(name) for name in names}
        manifest = json.loads(files["MANIFEST.json"])
        hashes = manifest["files"]
        if not isinstance(hashes, dict) or set(hashes) != set(files) - {"MANIFEST.json"}:
            raise ValueError("RF66 manifest membership mismatch")
        if any(
            hashlib.sha256(files[name]).hexdigest() != sha
            for name, sha in hashes.items()
        ):
            raise ValueError("RF66 manifest checksum mismatch")
        model_checksum = "sha256:" + hashlib.sha256(
            files["model/candidate.joblib"]
        ).hexdigest()
        if (
            model_checksum != expected_checksum
            or manifest["modelVersion"] != expected_checksum
        ):
            raise ValueError("RF66 trusted model checksum mismatch")
    except (
        zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, TypeError,
        NotImplementedError, zlib.error,
    ) as error:
        raise ValueError("Invalid RF66 package structure") from error
    return files
