from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor_diagnosis.server import create_server


def request_json(port: int, path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        health = request_json(port, "/api/health")
        login = request_json(port, "/api/auth/login", {"username": "admin", "password": "admin123"})
        sites = request_json(port, "/api/sites")
        assets = request_json(port, "/api/sites/SITE-01/assets")
        rollout = request_json(port, "/api/rollout-plans")
        labels = request_json(port, "/api/acoustic-labels")
        pipelines = request_json(port, "/api/data-pipelines")

        try:
            request_json(port, "/api/sites/SITE-999/assets")
        except urllib.error.HTTPError as exc:
            missing_site_status = exc.code
        else:
            raise AssertionError("unknown site should return HTTP error")

        assert health["ok"] is True
        assert login["user"]["role"] == "B"
        assert len(sites) == 65
        assert assets[0]["siteId"] == "SITE-01"
        assert len(rollout) == 65
        assert len(labels) >= 5
        assert len(pipelines) == 2
        assert missing_site_status == 404
        print(
            "HTTP smoke OK:",
            f"sites={len(sites)}",
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
