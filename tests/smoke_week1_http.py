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

        me = request_json(port, "/api/me", token=admin_token)
        operator_bootstrap = request_json(port, "/api/bootstrap", token=operator_token)
        sites = request_json(port, "/api/sites", token=admin_token)
        operator_sites = request_json(port, "/api/sites?role=B", token=operator_token)
        assets = request_json(port, "/api/sites/SITE-01/assets", token=admin_token)
        devices_page = request_json(port, "/api/devices?siteId=SITE-01&page=1&size=10", token=admin_token)
        rollout = request_json(port, "/api/rollout-plans", token=admin_token)
        network_profiles = request_json(port, "/api/network-profiles", token=admin_token)
        store_forward_profile = request_json(
            port, "/api/network-profiles/NET-STORE-FWD", token=admin_token
        )
        labels = request_json(port, "/api/acoustic-labels", token=admin_token)
        pipelines = request_json(port, "/api/data-pipelines", token=admin_token)
        telemetry = request_json(port, "/api/telemetry?siteId=SITE-01&assetId=SITE-01-MOT-02", token=admin_token)
        expect_error(port, "/api/auth/roles", 403, token=operator_token)
        expect_error(port, "/api/acoustic-labels", 403, token=operator_token)
        expect_error(
            port,
            "/api/sites/SITE-01/network-profile",
            403,
            payload={
                "networkProfileId": "NET-STORE-FWD",
                "grade": "A",
                "directSend": True,
                "gateway": False,
                "offlineSync": False,
                "reason": "operator must not update configuration",
            },
            token=operator_token,
            method="PUT",
        )

        created_site = request_json(
            port,
            "/api/sites",
            {
                "id": "SITE-SMOKE",
                "code": "SMOKE",
                "name": "Smoke Test Plant",
                "networkType": "D",
                "location": "Smoke Test Bay",
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
                "installLocation": "Smoke Bay 1",
                "baselineStatus": "ready",
                "baselineCapturedAt": "2026-08-20T10:00:00+09:00",
                "baselineVibrationRmsMmS": 1.2,
                "baselineAcousticDb": 51.0,
                "baselineSampleCount": 120,
            },
            token=admin_token,
        )
        rollout_saved = request_json(
            port,
            "/api/sites/SITE-SMOKE/rollout-plan",
            {
                "networkProfileId": "NET-STORE-FWD",
                "targetAssetIds": [created_asset["id"]],
                "installPriority": "high",
                "configurationType": "store-and-forward",
                "gatewayRequired": True,
                "note": "HTTP smoke rollout",
            },
            token=admin_token,
            method="PUT",
        )
        network_saved = request_json(
            port,
            "/api/sites/SITE-SMOKE/network-profile",
            {
                "networkProfileId": "B",
                "grade": "A",
                "directSend": True,
                "gateway": False,
                "offlineSync": True,
                "reason": "HTTP smoke field survey",
            },
            token=admin_token,
            method="PUT",
        )
        network_rollout = request_json(
            port,
            "/api/sites/SITE-SMOKE/rollout-plan",
            token=admin_token,
        )
        expect_error(
            port,
            "/api/devices",
            409,
            payload={
                "id": "DEV-SMOKE-REVOKED",
                "siteId": "SITE-SMOKE",
                "assetId": created_asset["id"],
                "certificateId": "CERT-SMOKE-REVOKED",
                "certificateFingerprint": "fingerprint-smoke-revoked",
                "certificateStatus": "revoked",
            },
            token=admin_token,
        )
        created_device = request_json(
            port,
            "/api/devices",
            {
                "id": "DEV-SMOKE",
                "siteId": "SITE-SMOKE",
                "assetId": created_asset["id"],
                "certificateId": "CERT-SMOKE",
                "certificateFingerprint": "fingerprint-smoke",
            },
            token=admin_token,
        )
        patched_site = request_json(
            port,
            "/api/sites/SITE-SMOKE",
            {"name": "Smoke Test Plant Updated"},
            token=admin_token,
            method="PATCH",
        )
        patched_asset = request_json(
            port,
            f"/api/sites/SITE-SMOKE/assets/{created_asset['id']}",
            {"installLocation": "Smoke Bay 2"},
            token=admin_token,
            method="PATCH",
        )
        patched_device = request_json(
            port,
            "/api/devices/DEV-SMOKE",
            {"firmwareVersion": "edge-0.2.0", "reason": "HTTP smoke firmware update"},
            token=admin_token,
            method="PATCH",
        )
        install_point = request_json(
            port,
            f"/api/assets/{created_asset['id']}/install-points",
            {
                "position": "drive-end bearing housing",
                "orientation": "horizontal X",
                "mountingMethod": "bolt fixed bracket",
                "acousticDirection": "cooling fan",
                "ambientNoiseSources": ["adjacent pump"],
                "photoRefs": ["survey://smoke/front.jpg"],
            },
            token=admin_token,
        )
        patched_install_point = request_json(
            port,
            f"/api/assets/{created_asset['id']}/install-points/{install_point['id']}",
            {"orientation": "vertical Z", "reason": "HTTP smoke axis correction"},
            token=admin_token,
            method="PATCH",
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
        expect_error(port, "/api/sites/SITE-01", 404, payload={"name": "bad route"}, token=admin_token, method="PUT")
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
            "/api/sites",
            400,
            payload={
                "id": "SITE-INFINITE-SIGNAL",
                "code": "INFINITE-SIGNAL",
                "name": "Infinite Signal Plant",
                "signalQuality": float("inf"),
            },
            token=admin_token,
        )
        expect_error(
            port,
            "/api/sites",
            400,
            payload={
                "id": "SITE-NON-FINITE",
                "code": "NON-FINITE",
                "name": "Non-finite Coordinate Plant",
                "latitude": float("nan"),
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
        assert me["user"]["username"] == "admin"
        assert "rolePolicies" not in operator_bootstrap
        assert "parameters" not in operator_bootstrap
        assert len(sites) == 65
        assert len(operator_sites) == 4
        assert all(site["id"] != "SITE-05" for site in operator_sites)
        assert assets[0]["siteId"] == "SITE-01"
        assert devices_page["total"] >= 1
        assert len(rollout) == 65
        assert all(
            {"id", "connectivityType", "offlineSync", "recommendedTopology"} <= profile.keys()
            for profile in network_profiles
        )
        assert store_forward_profile["id"] == "NET-STORE-FWD"
        assert store_forward_profile["type"] == "C"
        assert store_forward_profile["connectivityType"] == "wifi/ethernet/lte/gateway"
        assert store_forward_profile["offlineSync"] is True
        assert store_forward_profile["recommendedTopology"] == "gateway"
        assert len(labels) >= 5
        assert len(pipelines) == 2
        assert telemetry["units"]["vibrationRmsMmS"] == "mm/s RMS"
        assert telemetry["points"] == []
        assert created_site["id"] == "SITE-SMOKE"
        assert created_asset["baseline"]["sampleCount"] == 120
        assert rollout_saved["targetAssetIds"] == [created_asset["id"]]
        assert rollout_saved["networkProfileId"] == "NET-STORE-FWD"
        assert rollout_saved["configurationType"] == "store-and-forward"
        assert rollout_saved["gatewayRequired"] is True
        assert network_saved["grade"] == "B"
        assert network_saved["directSend"] is False
        assert network_saved["gateway"] is True
        assert network_saved["offlineSync"] is False
        assert network_rollout["configurationType"] == "gateway"
        assert network_rollout["gatewayRequired"] is True
        assert created_device["assetId"] == created_asset["id"]
        assert patched_site["name"] == "Smoke Test Plant Updated"
        assert patched_asset["installLocation"] == "Smoke Bay 2"
        assert patched_device["replacementHistory"][0]["after"]["firmwareVersion"] == "edge-0.2.0"
        assert patched_install_point["changeHistory"][0]["after"]["orientation"] == "vertical Z"
        assert quarantined["deviceId"] == "DEV-SMOKE-UNKNOWN"
        request_json(port, "/api/auth/logout", {}, token=admin_token)
        expect_error(port, "/api/me", 401, token=admin_token)
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
