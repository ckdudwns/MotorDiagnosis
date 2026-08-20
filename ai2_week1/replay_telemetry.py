#!/usr/bin/env python3
"""Replay AI2 telemetry JSONL to the backend ingest endpoint or to the console."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telemetry_payload import to_external_payload

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        "Python 3.12.x is required by the team development standard. "
        f"Current version: {sys.version.split()[0]}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("output/ai2_week1/telemetry_replay.jsonl"),
    )
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:8787/api/telemetry/ingest"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TELEMETRY_INGEST_TOKEN", "demo-telemetry-ingest-token"),
        help="Bearer token with telemetry:ingest permission.",
    )
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually POST records. Default is safe console preview.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="0 means replay all records"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval_seconds < 0:
        raise SystemExit("--interval-seconds must be zero or greater")
    if not args.input.exists():
        raise SystemExit(f"Replay input does not exist: {args.input}")
    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        rows = rows[: args.limit]
    for index, row in enumerate(rows, start=1):
        payload = to_external_payload(row)
        if args.send:
            body = json.dumps(payload).encode("utf-8")
            request = Request(
                args.endpoint,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {args.token}",
                },
            )
            try:
                with urlopen(request, timeout=10) as response:
                    print(
                        f"[{index}/{len(rows)}] {response.status} "
                        f"seq={payload['sequence']} {payload['scenarioLabel']}"
                    )
            except (HTTPError, URLError) as exc:
                raise SystemExit(
                    f"POST failed at sequence {payload['sequence']}: {exc}"
                ) from exc
        else:
            print(json.dumps(payload, ensure_ascii=False))
        if index < len(rows) and args.interval_seconds:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
