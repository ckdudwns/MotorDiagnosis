from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor_diagnosis.server import create_server


def request_json(
    port: int,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
    method: str | None = None,
) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def expect_error(
    port: int,
    path: str,
    expected_status: int,
    payload: dict | None = None,
    token: str | None = None,
    method: str | None = None,
) -> dict:
    try:
        request_json(port, path, payload=payload, token=token, method=method)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        assert exc.code == expected_status, f"{path}: expected {expected_status}, got {exc.code}"
        assert "error" in body, f"{path}: error response should use JSON envelope"
        return body
    raise AssertionError(f"{path} should return HTTP {expected_status}")


def main() -> None:
    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        health = request_json(port, "/api/health")
        expect_error(port, "/api/sites", 401)

        admin_login = request_json(port, "/api/auth/login", {"username": "admin", "password": "admin123"})
        operator_login = request_json(port, "/api/auth/login", {"username": "operator", "password": "operator123"})
        admin_token = admin_login["session"]["token"]
        operator_token = operator_login["session"]["token"]

        sites = request_json(port, "/api/sites", token=admin_token)
        operator_sites = request_json(port, "/api/sites?role=B", token=operator_token)
        assets = request_json(port, "/api/sites/SITE-01/assets", token=admin_token)
        rollout = request_json(port, "/api/rollout-plans", token=admin_token)
        labels = request_json(port, "/api/acoustic-labels", token=admin_token)
        pipelines = request_json(port, "/api/data-pipelines", token=admin_token)
        telemetry = request_json(port, "/api/telemetry?siteId=SITE-01&assetId=SITE-01-MOT-02", token=admin_token)

        created_site = request_json(
            port,
            "/api/sites",
            {
                "id": "SITE-SMOKE",
                "code": "SMOKE",
                "name": "Smoke Test Plant",
                "networkType": "D",
            },
            token=admin_token,
        )
        created_asset = request_json(
            port,
            "/api/sites/SITE-SMOKE/assets",
            {
                "assetCode": "MOT-99",
                "name": "Smoke Motor",
                "assetType": "motor",
                "ratedRpm": 1500,
                "baselineVibrationRmsMmS": 1.2,
                "baselineAcousticDb": 51.0,
                "baselineSampleCount": 120,
            },
            token=admin_token,
        )
        created_device = request_json(
            port,
            "/api/sites/SITE-SMOKE/devices",
            {"id": "DEV-SMOKE", "assetId": created_asset["id"]},
            token=admin_token,
        )
        quarantined = request_json(
            port,
            "/api/devices/quarantine",
            {"deviceId": "DEV-SMOKE-UNKNOWN", "payload": {"rssi": -89}},
            token=admin_token,
        )

        expect_error(port, "/api/sites/SITE-999/assets", 404, token=admin_token)
        expect_error(port, "/api/sites/SITE-05", 403, token=operator_token)
        expect_error(port, "/api/sites/SITE-01/not-a-route", 404, token=admin_token)
        expect_error(port, "/api/sites/SITE-01", 405, payload={"name": "bad method"}, token=admin_token, method="PUT")
        expect_error(
            port,
            "/api/sites",
            400,
            payload={
                "id": "SITE-BAD-NUMBER",
                "code": "BAD-NUMBER",
                "name": "Bad Number Plant",
                "signalQuality": "bad",
            },
            token=admin_token,
        )
        expect_error(
            port,
            "/api/events/EV-241/review",
            413,
            payload={"label": "needs_review", "note": "x" * 70000},
            token=admin_token,
        )

        assert health["ok"] is True
        assert admin_login["user"]["role"] == "B"
        assert len(sites) == 65
        assert len(operator_sites) == 4
        assert all(site["id"] != "SITE-05" for site in operator_sites)
        assert assets[0]["siteId"] == "SITE-01"
        assert len(rollout) == 65
        assert len(labels) >= 5
        assert len(pipelines) == 2
        assert telemetry["units"]["vibrationRmsMmS"] == "mm/s RMS"
        assert len(telemetry["points"]) == 72
        assert created_site["id"] == "SITE-SMOKE"
        assert created_asset["baseline"]["sampleCount"] == 120
        assert created_device["assetId"] == created_asset["id"]
        assert quarantined["deviceId"] == "DEV-SMOKE-UNKNOWN"
        print(
            "HTTP smoke OK:",
            f"sites={len(sites)}",
            f"operatorSites={len(operator_sites)}",
            f"assets={len(assets)}",
            f"rollout={len(rollout)}",
            f"labels={len(labels)}",
            f"pipelines={len(pipelines)}",
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
