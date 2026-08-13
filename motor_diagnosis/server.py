from __future__ import annotations

import csv
import io
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .data import (
    ACOUSTIC_LABEL_TAXONOMY,
    DATA_PIPELINES,
    EVENTS,
    NETWORK_PROFILES,
    PARAMETERS,
    ROLE_POLICIES,
    SITES,
    ApiError,
    assets_for,
    authenticate,
    copy_payload,
    devices_for,
    get_site,
    inject_anomaly,
    install_points_for,
    network_profile,
    network_profiles_for_sites,
    review_event,
    rollout_plans,
    role_policy,
    telemetry_for,
    visible_sites_for_role,
)
from .web import render_page


LOGGER = logging.getLogger("motor_diagnosis")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MotorDiagnosis/0.1"

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers("text/plain; charset=utf-8", 0)
        self.end_headers()

    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        request_started = time.monotonic()
        try:
            if method == "GET":
                self.route_get(path, query)
            elif method == "POST":
                self.route_post(path)
            else:
                raise ApiError(405, "METHOD_NOT_ALLOWED", "지원하지 않는 메서드입니다.")
        except ApiError as exc:
            self.send_error_json(exc)
        except json.JSONDecodeError:
            self.send_error_json(ApiError(400, "INVALID_JSON", "JSON 형식이 올바르지 않습니다."))
        except Exception:
            LOGGER.exception("unexpected_error path=%s", path)
            self.send_error_json(ApiError(500, "INTERNAL_ERROR", "서버 처리 중 오류가 발생했습니다."))
        finally:
            elapsed_ms = round((time.monotonic() - request_started) * 1000, 2)
            LOGGER.info("%s %s %.2fms", method, path, elapsed_ms)

    def route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/":
            self.send_text(render_page(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self.send_json({"ok": True, "service": "Bind Edge AI backend", "timestamp": time.time()})
            return
        if path == "/api/bootstrap":
            self.send_json(
                {
                    "sites": copy_payload(SITES),
                    "networkProfiles": copy_payload(NETWORK_PROFILES),
                    "events": copy_payload(EVENTS),
                    "parameters": copy_payload(PARAMETERS),
                    "rolePolicies": copy_payload(ROLE_POLICIES),
                    "acousticLabels": copy_payload(ACOUSTIC_LABEL_TAXONOMY),
                    "dataPipelines": copy_payload(DATA_PIPELINES),
                }
            )
            return
        if path == "/api/auth/roles":
            self.send_json(copy_payload(ROLE_POLICIES))
            return
        if path.startswith("/api/auth/roles/"):
            role = path.split("/")[4]
            self.send_json(role_policy(role))
            return
        if path == "/api/sites":
            role = first_query(query, "role", "B")
            self.send_json(visible_sites_for_role(role))
            return
        if path.startswith("/api/sites/") and path.endswith("/assets"):
            site_id = path.split("/")[3]
            self.send_json(assets_for(site_id))
            return
        if path.startswith("/api/sites/") and path.endswith("/devices"):
            site_id = path.split("/")[3]
            self.send_json(devices_for(site_id))
            return
        if path.startswith("/api/sites/") and path.endswith("/install-points"):
            site_id = path.split("/")[3]
            self.send_json(install_points_for(site_id))
            return
        if path.startswith("/api/sites/"):
            site_id = path.split("/")[3]
            self.send_json(get_site(site_id))
            return
        if path == "/api/assets":
            site_id = required_query(query, "siteId")
            self.send_json(assets_for(site_id))
            return
        if path == "/api/devices":
            site_id = required_query(query, "siteId")
            self.send_json(devices_for(site_id))
            return
        if path == "/api/rollout-plans":
            self.send_json(rollout_plans())
            return
        if path == "/api/network-profiles":
            self.send_json(copy_payload(NETWORK_PROFILES))
            return
        if path == "/api/site-network-profiles":
            self.send_json(network_profiles_for_sites())
            return
        if path.startswith("/api/network-profiles/"):
            profile_type = path.split("/")[3]
            self.send_json(network_profile(profile_type))
            return
        if path == "/api/install-points":
            site_id = required_query(query, "siteId")
            self.send_json(install_points_for(site_id))
            return
        if path == "/api/acoustic-labels":
            self.send_json(copy_payload(ACOUSTIC_LABEL_TAXONOMY))
            return
        if path == "/api/data-pipelines":
            self.send_json(copy_payload(DATA_PIPELINES))
            return
        if path == "/api/events":
            self.send_json(copy_payload(EVENTS))
            return
        if path == "/api/telemetry":
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            self.send_json({"siteId": site_id, "assetId": asset_id, "points": telemetry_for(site_id, asset_id)})
            return
        if path == "/api/export":
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            self.send_csv(site_id, asset_id)
            return
        raise ApiError(404, "NOT_FOUND", "요청한 API를 찾을 수 없습니다.")

    def route_post(self, path: str) -> None:
        payload = self.read_json()
        if path == "/api/auth/login":
            self.send_json(authenticate(payload))
            return
        if path == "/api/demo/inject-anomaly":
            self.send_json(inject_anomaly(payload), status=201)
            return
        if path.startswith("/api/events/") and path.endswith("/review"):
            event_id = path.split("/")[3]
            self.send_json(review_event(event_id, payload))
            return
        raise ApiError(404, "NOT_FOUND", "요청한 API를 찾을 수 없습니다.")

    def read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("content-length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length가 올바르지 않습니다.") from exc
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ApiError(400, "INVALID_JSON_BODY", "JSON 본문은 객체여야 합니다.")
        return data

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc: ApiError) -> None:
        self.send_json({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)

    def send_text(self, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_common_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, site_id: str, asset_id: str) -> None:
        points = telemetry_for(site_id, asset_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "site_id",
                "asset_id",
                "minute",
                "vibration_rms_mm_s",
                "acoustic_db",
                "rpm",
                "anomaly_score",
            ]
        )
        for point in points:
            writer.writerow(
                [
                    site_id,
                    asset_id,
                    point["minute"],
                    point["vibrationRmsMmS"],
                    point["acousticDb"],
                    point["rpm"],
                    point["anomalyScore"],
                ]
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        filename = f"{site_id}_{asset_id}.csv"
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", f'attachment; filename="{filename}"')
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("content-length", str(content_length))

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("client=%s %s", self.address_string(), fmt % args)


def first_query(query: dict[str, list[str]], key: str, default: str) -> str:
    return query.get(key, [default])[0]


def required_query(query: dict[str, list[str]], key: str) -> str:
    value = query.get(key, [""])[0].strip()
    if not value:
        raise ApiError(400, "MISSING_QUERY_PARAMETER", f"{key} 값이 필요합니다.")
    return value


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), AppHandler)

