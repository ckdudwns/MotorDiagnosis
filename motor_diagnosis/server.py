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
    ApiError,
    assets_for,
    authenticate,
    copy_payload,
    create_asset,
    create_device,
    create_site,
    current_user_for_token,
    deactivate_site,
    delete_asset,
    delete_device,
    delete_site,
    devices_for,
    get_site,
    inject_anomaly,
    install_points_for,
    network_profile,
    network_profiles_for_sites,
    quarantine_unregistered_device,
    require_permission,
    require_site_access,
    review_event,
    role_policy,
    rollout_plans,
    telemetry_for,
    telemetry_units,
    update_asset,
    update_device,
    update_site,
    visible_sites_for_user,
)
from .web import render_page


LOGGER = logging.getLogger("motor_diagnosis")
MAX_JSON_BODY_BYTES = 64 * 1024


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MotorDiagnosis/0.2"

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_DELETE(self) -> None:
        self.handle_request("DELETE")

    def do_PUT(self) -> None:
        self.handle_request("PUT")

    def do_PATCH(self) -> None:
        self.handle_request("PATCH")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers("text/plain; charset=utf-8", 0)
        self.end_headers()

    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        segments = path_segments(path)
        request_started = time.monotonic()
        try:
            if method == "GET":
                self.route_get(path, segments, query)
            elif method == "POST":
                self.route_post(segments)
            elif method == "DELETE":
                self.route_delete(segments)
            else:
                raise ApiError(405, "METHOD_NOT_ALLOWED", "HTTP method is not supported for this API.")
        except ApiError as exc:
            self.send_error_json(exc)
        except json.JSONDecodeError:
            self.send_error_json(ApiError(400, "INVALID_JSON", "Request body is not valid JSON."))
        except Exception:
            LOGGER.exception("unexpected_error path=%s", path)
            self.send_error_json(ApiError(500, "INTERNAL_ERROR", "Server processing failed."))
        finally:
            elapsed_ms = round((time.monotonic() - request_started) * 1000, 2)
            LOGGER.info("%s %s %.2fms", method, path, elapsed_ms)

    def route_get(self, path: str, segments: list[str], query: dict[str, list[str]]) -> None:
        if path == "/":
            self.send_text(render_page(), "text/html; charset=utf-8")
            return
        if segments == ["api", "health"]:
            self.send_json({"ok": True, "service": "Bind Edge AI backend", "timestamp": time.time()})
            return

        user = self.require_user()

        if segments == ["api", "bootstrap"]:
            self.send_json(
                {
                    "sites": visible_sites_for_user(user),
                    "networkProfiles": copy_payload(NETWORK_PROFILES),
                    "events": authorized_events(user),
                    "parameters": copy_payload(PARAMETERS),
                    "rolePolicies": copy_payload(ROLE_POLICIES),
                    "acousticLabels": copy_payload(ACOUSTIC_LABEL_TAXONOMY),
                    "dataPipelines": copy_payload(DATA_PIPELINES),
                }
            )
            return
        if segments == ["api", "auth", "roles"]:
            self.send_json(copy_payload(ROLE_POLICIES))
            return
        if len(segments) == 4 and segments[:3] == ["api", "auth", "roles"]:
            self.send_json(role_policy(segments[3]))
            return
        if segments == ["api", "sites"]:
            self.send_json(visible_sites_for_user(user))
            return
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            site = get_site(segments[2])
            require_site_access(user, site["id"])
            require_permission(user, "site:read")
            self.send_json(copy_payload(site))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "assets":
            get_site(segments[2])
            require_site_access(user, segments[2])
            require_permission(user, "asset:read")
            self.send_json(assets_for(segments[2]))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "devices":
            get_site(segments[2])
            require_site_access(user, segments[2])
            require_permission(user, "device:read")
            self.send_json(devices_for(segments[2]))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "install-points":
            get_site(segments[2])
            require_site_access(user, segments[2])
            self.send_json(install_points_for(segments[2]))
            return
        if segments == ["api", "assets"]:
            site_id = required_query(query, "siteId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "asset:read")
            self.send_json(assets_for(site_id))
            return
        if segments == ["api", "devices"]:
            site_id = required_query(query, "siteId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "device:read")
            self.send_json(devices_for(site_id))
            return
        if segments == ["api", "rollout-plans"]:
            self.send_json(filter_site_rows(user, rollout_plans()))
            return
        if segments == ["api", "network-profiles"]:
            self.send_json(copy_payload(NETWORK_PROFILES))
            return
        if len(segments) == 3 and segments[:2] == ["api", "network-profiles"]:
            self.send_json(network_profile(segments[2]))
            return
        if segments == ["api", "site-network-profiles"]:
            self.send_json(filter_site_rows(user, network_profiles_for_sites()))
            return
        if segments == ["api", "install-points"]:
            site_id = required_query(query, "siteId")
            get_site(site_id)
            require_site_access(user, site_id)
            self.send_json(install_points_for(site_id))
            return
        if segments == ["api", "acoustic-labels"]:
            self.send_json(copy_payload(ACOUSTIC_LABEL_TAXONOMY))
            return
        if segments == ["api", "data-pipelines"]:
            self.send_json(copy_payload(DATA_PIPELINES))
            return
        if segments == ["api", "events"]:
            self.send_json(authorized_events(user))
            return
        if segments == ["api", "telemetry"]:
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "telemetry:read")
            self.send_json(
                {
                    "siteId": site_id,
                    "assetId": asset_id,
                    "units": telemetry_units(),
                    "points": telemetry_for(site_id, asset_id),
                }
            )
            return
        if segments == ["api", "export"]:
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "export:read")
            self.send_csv(site_id, asset_id)
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_post(self, segments: list[str]) -> None:
        payload = self.read_json()
        if segments == ["api", "auth", "login"]:
            self.send_json(authenticate(payload))
            return

        user = self.require_user()

        if segments == ["api", "sites"]:
            self.send_json(create_site(user, payload), status=201)
            return
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            self.send_json(update_site(user, segments[2], payload))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "deactivate":
            self.send_json(deactivate_site(user, segments[2]))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "assets":
            self.send_json(create_asset(user, segments[2], payload), status=201)
            return
        if len(segments) == 5 and segments[:2] == ["api", "sites"] and segments[3] == "assets":
            self.send_json(update_asset(user, segments[2], segments[4], payload))
            return
        if len(segments) == 4 and segments[:2] == ["api", "sites"] and segments[3] == "devices":
            self.send_json(create_device(user, segments[2], payload), status=201)
            return
        if segments == ["api", "devices", "quarantine"]:
            require_permission(user, "device:write")
            self.send_json(quarantine_unregistered_device(payload), status=201)
            return
        if len(segments) == 3 and segments[:2] == ["api", "devices"]:
            self.send_json(update_device(user, segments[2], payload))
            return
        if segments == ["api", "demo", "inject-anomaly"]:
            require_permission(user, "event:write")
            self.send_json(inject_anomaly(payload), status=201)
            return
        if len(segments) == 4 and segments[:2] == ["api", "events"] and segments[3] == "review":
            require_permission(user, "event:review")
            self.send_json(review_event(segments[2], payload))
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_delete(self, segments: list[str]) -> None:
        user = self.require_user()
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            self.send_json(delete_site(user, segments[2]))
            return
        if len(segments) == 5 and segments[:2] == ["api", "sites"] and segments[3] == "assets":
            self.send_json(delete_asset(user, segments[2], segments[4]))
            return
        if len(segments) == 3 and segments[:2] == ["api", "devices"]:
            self.send_json(delete_device(user, segments[2]))
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def require_user(self) -> dict[str, Any]:
        authorization = self.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "AUTH_REQUIRED", "A bearer session token is required.")
        return current_user_for_token(authorization.removeprefix("Bearer ").strip())

    def read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("content-length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid.") from exc
        if length < 0:
            raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid.")
        if length > MAX_JSON_BODY_BYTES:
            raise ApiError(413, "REQUEST_TOO_LARGE", "Request body must be 64KB or less.")
        if length == 0:
            return {}
        content_type = self.headers.get("content-type", "").split(";")[0].lower()
        if content_type != "application/json":
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Only application/json requests are supported.")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ApiError(400, "INVALID_JSON_BODY", "JSON body must be an object.")
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
        writer.writerow(["site_id", "asset_id", "minute", "vibration_rms_mm_s", "acoustic_db", "rpm", "anomaly_score"])
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
        self.send_header("access-control-allow-methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("content-length", str(content_length))

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("client=%s %s", self.address_string(), fmt % args)


def authorized_events(user: dict[str, Any]) -> list[dict[str, Any]]:
    require_permission(user, "event:read")
    allowed = user.get("allowedSiteIds", [])
    if "*" in allowed:
        return copy_payload(EVENTS)
    return copy_payload([event for event in EVENTS if event["siteId"] in allowed])


def filter_site_rows(user: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = user.get("allowedSiteIds", [])
    if "*" in allowed:
        return copy_payload(rows)
    return copy_payload([row for row in rows if row.get("siteId") in allowed])


def path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def required_query(query: dict[str, list[str]], key: str) -> str:
    value = query.get(key, [""])[0].strip()
    if not value:
        raise ApiError(400, "MISSING_QUERY_PARAMETER", f"{key} is required.")
    return value


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), AppHandler)
