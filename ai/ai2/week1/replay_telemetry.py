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

try:
    from .telemetry_payload import to_external_payload
except ImportError:
    from telemetry_payload import to_external_payload


DEFAULT_BACKEND_SITE_ID = "SITE-01"
DEFAULT_BACKEND_ASSET_ID = "SITE-01-GEN-01"
DEFAULT_BACKEND_DEVICE_ID = "DEV-01-GEN-01"


def load_replay_payloads(path: Path) -> list[dict[str, object]]:
    return [
        to_external_payload(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def remap_payload(
    payload: dict[str, object], *, site_id: str, asset_id: str, device_id: str
) -> dict[str, object]:
    return {
        **payload,
        "siteId": site_id.strip().upper(),
        "assetId": asset_id.strip().upper(),
        "deviceId": device_id.strip().upper(),
    }


def post_payload(
    endpoint: str, token: str, payload: dict[str, object], timeout: float = 10
) -> tuple[int, dict[str, object]]:
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
        return response.status, body


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
        default=os.environ.get(
            "TELEMETRY_INGEST_TOKEN", "demo-telemetry-validation-token"
        ),
        help="Bearer token with telemetry:ingest and telemetry:label permissions.",
    )
    parser.add_argument(
        "--site-id",
        default=os.environ.get("REPLAY_SITE_ID", DEFAULT_BACKEND_SITE_ID),
        help="Registered backend site used for demo replay.",
    )
    parser.add_argument(
        "--asset-id",
        default=os.environ.get("REPLAY_ASSET_ID", DEFAULT_BACKEND_ASSET_ID),
        help="Registered backend asset used for demo replay.",
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("REPLAY_DEVICE_ID", DEFAULT_BACKEND_DEVICE_ID),
        help="Registered backend device used for demo replay.",
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
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(
            "Python 3.12.x is required by the team development standard. "
            f"Current version: {sys.version.split()[0]}"
        )
    args = parse_args()
    if args.interval_seconds < 0:
        raise SystemExit("--interval-seconds must be zero or greater")
    if not args.input.exists():
        raise SystemExit(f"Replay input does not exist: {args.input}")
    payloads = load_replay_payloads(args.input)
    if args.limit:
        payloads = payloads[: args.limit]
    for index, original_payload in enumerate(payloads, start=1):
        payload = remap_payload(
            original_payload,
            site_id=args.site_id,
            asset_id=args.asset_id,
            device_id=args.device_id,
        )
        if args.send:
            try:
                status, _response = post_payload(args.endpoint, args.token, payload)
                print(
                    f"[{index}/{len(payloads)}] {status} "
                    f"seq={payload['sequence']} {payload['scenarioLabel']}"
                )
            except (HTTPError, URLError) as exc:
                raise SystemExit(
                    f"POST failed at sequence {payload['sequence']}: {exc}"
                ) from exc
        else:
            print(json.dumps(payload, ensure_ascii=False))
        if index < len(payloads) and args.interval_seconds:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
