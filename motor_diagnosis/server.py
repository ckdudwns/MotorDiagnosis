from __future__ import annotations

import csv
import io
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ai.ai2.week2.anomaly_score import (
    annotate_telemetry_points,
    latest_asset_statuses,
)

from .data import (
    ACOUSTIC_LABEL_TAXONOMY,
    DATA_PIPELINES,
    EVENTS,
    NETWORK_PROFILES,
    PARAMETERS,
    ROLE_POLICIES,
    TELEMETRY_RECORDS,
    ApiError,
    assets_for,
    authenticate,
    copy_payload,
    create_asset,
    create_connectivity_test,
    create_device,
    create_install_point,
    create_site,
    current_user_for_token,
    dashboard_sites_summary,
    deactivate_site,
    delete_asset,
    delete_device,
    delete_site,
    devices_for,
    device_health_for,
    get_asset_by_id,
    get_device,
    get_site,
    has_permission,
    hardware_profiles,
    ingest_telemetry,
    inject_anomaly,
    install_points_for,
    install_points_for_asset,
    logout,
    network_profile,
    network_profiles_for_sites,
    parse_rfc3339,
    quarantine_mqtt_message,
    quarantine_unregistered_device,
    report_service_dependency,
    require_permission,
    require_site_access,
    review_event,
    role_policy,
    rollout_plans,
    rollout_plan_for,
    site_network_profile,
    service_health_dependencies,
    telemetry_for,
    telemetry_principal_for_token,
    telemetry_units,
    update_asset,
    update_device,
    update_device_hardware_profile,
    update_install_point,
    update_rollout_plan,
    update_site,
    update_site_network_profile,
    visible_sites_for_user,
    connectivity_tests_for_device,
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
            elif method == "PUT":
                self.route_put(segments)
            elif method == "PATCH":
                self.route_patch(segments)
            elif method == "DELETE":
                self.route_delete(segments)
            else:
                raise ApiError(
                    405,
                    "METHOD_NOT_ALLOWED",
                    "HTTP method is not supported for this API.",
                )
        except ApiError as exc:
            self.send_error_json(exc)
        except json.JSONDecodeError:
            self.send_error_json(
                ApiError(400, "INVALID_JSON", "Request body is not valid JSON.")
            )
        except Exception:
            LOGGER.exception("unexpected_error path=%s", path)
            self.send_error_json(
                ApiError(500, "INTERNAL_ERROR", "Server processing failed.")
            )
        finally:
            elapsed_ms = round((time.monotonic() - request_started) * 1000, 2)
            LOGGER.info("%s %s %.2fms", method, path, elapsed_ms)

    def route_get(
        self, path: str, segments: list[str], query: dict[str, list[str]]
    ) -> None:
        if path == "/":
            self.send_text(render_page(), "text/html; charset=utf-8")
            return
        if segments == ["api", "health"]:
            self.send_json(
                {
                    "ok": True,
                    "service": "Bind Edge AI backend",
                    "timestamp": time.time(),
                }
            )
            return

        user = self.require_user()

        if segments == ["api", "bootstrap"]:
            response: dict[str, Any] = {"sites": visible_sites_for_user(user)}
            if has_permission(user, "network-profile:read"):
                response["networkProfiles"] = copy_payload(NETWORK_PROFILES)
            if has_permission(user, "rollout:read"):
                response["rolloutPlans"] = filter_site_rows(user, rollout_plans())
            if has_permission(user, "event:read"):
                response["events"] = authorized_events(user)
            if has_permission(user, "configuration:read"):
                response["parameters"] = copy_payload(PARAMETERS)
                response["acousticLabels"] = copy_payload(ACOUSTIC_LABEL_TAXONOMY)
                response["dataPipelines"] = copy_payload(DATA_PIPELINES)
            if has_permission(user, "role:read"):
                response["rolePolicies"] = copy_payload(ROLE_POLICIES)
            self.send_json(response)
            return
        if segments == ["api", "me"]:
            self.send_json(
                {"user": copy_payload(user), "rolePolicy": role_policy(user["role"])}
            )
            return
        if segments == ["api", "auth", "roles"]:
            require_permission(user, "role:read")
            self.send_json(copy_payload(ROLE_POLICIES))
            return
        if len(segments) == 4 and segments[:3] == ["api", "auth", "roles"]:
            require_permission(user, "role:read")
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
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "assets"
        ):
            get_site(segments[2])
            require_site_access(user, segments[2])
            require_permission(user, "asset:read")
            self.send_json(assets_for(segments[2]))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "devices"
        ):
            get_site(segments[2])
            require_site_access(user, segments[2])
            require_permission(user, "device:read")
            self.send_json(devices_for(segments[2]))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "install-points"
        ):
            get_site(segments[2])
            require_site_access(user, segments[2])
            require_permission(user, "install-point:read")
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
            require_permission(user, "device:read")
            self.send_json(paginated_devices(user, query))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "health"
        ):
            device = get_device(segments[2])
            require_site_access(user, device["siteId"])
            require_permission(user, "device:read")
            self.send_json(device_health_for(device["id"]))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "connectivity-tests"
        ):
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(
                connectivity_tests_for_device(
                    user,
                    segments[2],
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                    phase=query.get("phase", [""])[0],
                    page=page,
                    size=size,
                )
            )
            return
        if segments == ["api", "rollout-plans"]:
            require_permission(user, "rollout:read")
            self.send_json(filter_site_rows(user, rollout_plans()))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "rollout-plan"
        ):
            require_site_access(user, segments[2])
            require_permission(user, "rollout:read")
            self.send_json(rollout_plan_for(segments[2]))
            return
        if segments == ["api", "network-profiles"]:
            require_permission(user, "network-profile:read")
            self.send_json(copy_payload(NETWORK_PROFILES))
            return
        if len(segments) == 3 and segments[:2] == ["api", "network-profiles"]:
            require_permission(user, "network-profile:read")
            self.send_json(network_profile(segments[2]))
            return
        if segments == ["api", "site-network-profiles"]:
            require_permission(user, "network-profile:read")
            self.send_json(filter_site_rows(user, network_profiles_for_sites()))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "network-profile"
        ):
            require_site_access(user, segments[2])
            require_permission(user, "network-profile:read")
            self.send_json(site_network_profile(segments[2]))
            return
        if segments == ["api", "install-points"]:
            site_id = required_query(query, "siteId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "install-point:read")
            self.send_json(install_points_for(site_id))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "assets"]
            and segments[3] == "install-points"
        ):
            asset = get_asset_by_id(segments[2])
            require_site_access(user, asset["siteId"])
            require_permission(user, "install-point:read")
            active = optional_boolean_query(query, "active")
            self.send_json(install_points_for_asset(asset["id"], active=active))
            return
        if segments == ["api", "acoustic-labels"]:
            require_permission(user, "configuration:read")
            self.send_json(copy_payload(ACOUSTIC_LABEL_TAXONOMY))
            return
        if segments == ["api", "data-pipelines"]:
            require_permission(user, "configuration:read")
            self.send_json(copy_payload(DATA_PIPELINES))
            return
        if segments == ["api", "dashboard", "sites-summary"]:
            self.send_json(
                dashboard_sites_summary(
                    user,
                    region=query.get("region", [""])[0],
                    status=query.get("status", [""])[0],
                    live_asset_statuses=latest_asset_statuses(TELEMETRY_RECORDS),
                )
            )
            return
        if segments == ["api", "health", "dependencies"]:
            require_permission(user, "service-health:read")
            self.send_json(service_health_dependencies())
            return
        if segments == ["api", "device-hardware-profiles"]:
            require_permission(user, "hardware-profile:read")
            self.send_json(
                hardware_profiles(
                    active=optional_boolean_query(query, "active"),
                    board_type=query.get("boardType", [""])[0],
                    connectivity_type=query.get("connectivityType", [""])[0],
                )
            )
            return
        if segments == ["api", "events"]:
            self.send_json(
                authorized_events(
                    user,
                    site_id=query.get("siteId", [""])[0],
                    asset_id=query.get("assetId", [""])[0],
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                )
            )
            return
        if segments == ["api", "telemetry"]:
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            get_site(site_id)
            require_site_access(user, site_id)
            require_permission(user, "telemetry:read")
            points = annotate_telemetry_points(
                telemetry_for(
                    site_id,
                    asset_id,
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                )
            )
            self.send_json(
                {
                    "siteId": site_id,
                    "assetId": asset_id,
                    "units": telemetry_units(site_id, asset_id),
                    "points": points,
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
        if segments == ["api", "auth", "logout"]:
            token = self.bearer_token()
            current_user_for_token(token)
            self.send_json(logout(token))
            return
        if segments == ["api", "telemetry", "ingest"]:
            result, status = ingest_telemetry(
                telemetry_principal_for_token(self.bearer_token()), payload
            )
            self.send_json(result, status=status)
            return
        if segments == ["api", "telemetry", "quarantine"]:
            self.send_json(
                quarantine_mqtt_message(
                    telemetry_principal_for_token(self.bearer_token()), payload
                ),
                status=201,
            )
            return
        if segments == ["api", "health", "dependencies", "mqtt"]:
            self.send_json(
                report_service_dependency(
                    telemetry_principal_for_token(self.bearer_token()),
                    "mqtt",
                    payload,
                )
            )
            return

        user = self.require_user()

        if segments == ["api", "sites"]:
            self.send_json(create_site(user, payload), status=201)
            return
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            self.send_json(update_site(user, segments[2], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "deactivate"
        ):
            self.send_json(deactivate_site(user, segments[2]))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "assets"
        ):
            self.send_json(create_asset(user, segments[2], payload), status=201)
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "sites"]
            and segments[3] == "assets"
        ):
            self.send_json(update_asset(user, segments[2], segments[4], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "devices"
        ):
            self.send_json(create_device(user, segments[2], payload), status=201)
            return
        if segments == ["api", "devices"]:
            site_id = str(payload.get("siteId") or "").strip().upper()
            if not site_id:
                raise ApiError(400, "MISSING_FIELD", "siteId is required.")
            self.send_json(create_device(user, site_id, payload), status=201)
            return
        if segments == ["api", "devices", "quarantine"]:
            require_permission(user, "device:write")
            self.send_json(quarantine_unregistered_device(payload), status=201)
            return
        if len(segments) == 3 and segments[:2] == ["api", "devices"]:
            self.send_json(update_device(user, segments[2], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "connectivity-tests"
        ):
            self.send_json(
                create_connectivity_test(user, segments[2], payload), status=201
            )
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "assets"]
            and segments[3] == "install-points"
        ):
            self.send_json(create_install_point(user, segments[2], payload), status=201)
            return
        if segments == ["api", "demo", "inject-anomaly"]:
            require_permission(user, "event:write")
            self.send_json(inject_anomaly(payload), status=201)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "events"]
            and segments[3] == "review"
        ):
            require_permission(user, "event:review")
            self.send_json(review_event(segments[2], payload))
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_put(self, segments: list[str]) -> None:
        payload = self.read_json()
        user = self.require_user()
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "rollout-plan"
        ):
            self.send_json(update_rollout_plan(user, segments[2], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "sites"]
            and segments[3] == "network-profile"
        ):
            self.send_json(update_site_network_profile(user, segments[2], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "hardware-profile"
        ):
            self.send_json(update_device_hardware_profile(user, segments[2], payload))
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_patch(self, segments: list[str]) -> None:
        payload = self.read_json()
        user = self.require_user()
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            self.send_json(update_site(user, segments[2], payload))
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "sites"]
            and segments[3] == "assets"
        ):
            self.send_json(update_asset(user, segments[2], segments[4], payload))
            return
        if len(segments) == 3 and segments[:2] == ["api", "devices"]:
            self.send_json(update_device(user, segments[2], payload))
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "assets"]
            and segments[3] == "install-points"
        ):
            self.send_json(
                update_install_point(user, segments[2], segments[4], payload)
            )
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_delete(self, segments: list[str]) -> None:
        user = self.require_user()
        if len(segments) == 3 and segments[:2] == ["api", "sites"]:
            self.send_json(delete_site(user, segments[2]))
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "sites"]
            and segments[3] == "assets"
        ):
            self.send_json(delete_asset(user, segments[2], segments[4]))
            return
        if len(segments) == 3 and segments[:2] == ["api", "devices"]:
            self.send_json(delete_device(user, segments[2]))
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def require_user(self) -> dict[str, Any]:
        return current_user_for_token(self.bearer_token())

    def bearer_token(self) -> str:
        authorization = self.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "AUTH_REQUIRED", "A bearer session token is required.")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise ApiError(401, "AUTH_REQUIRED", "A bearer session token is required.")
        return token

    def read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("content-length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ApiError(
                400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid."
            ) from exc
        if length < 0:
            raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid.")
        if length > MAX_JSON_BODY_BYTES:
            raise ApiError(
                413, "REQUEST_TOO_LARGE", "Request body must be 64KB or less."
            )
        if length == 0:
            return {}
        content_type = self.headers.get("content-type", "").split(";")[0].lower()
        if content_type != "application/json":
            raise ApiError(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                "Only application/json requests are supported.",
            )
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
        self.send_json(
            {"error": {"code": exc.code, "message": exc.message}}, status=exc.status
        )

    def send_text(
        self,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        status: int = 200,
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_common_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, site_id: str, asset_id: str) -> None:
        points = annotate_telemetry_points(telemetry_for(site_id, asset_id))
        output = io.StringIO()
        writer = csv.writer(output)
        raw_telemetry = any("vibrationRmsRaw" in point for point in points)
        if raw_telemetry:
            writer.writerow(
                [
                    "timestamp",
                    "sequence",
                    "site_id",
                    "asset_id",
                    "device_id",
                    "vibration_rms_raw",
                    "vibration_peak_hz",
                    "acoustic_rms_raw",
                    "acoustic_peak_hz",
                    "rpm",
                    "anomaly_score",
                    "anomaly_status",
                ]
            )
            for point in points:
                writer.writerow(
                    [
                        point.get("timestamp"),
                        point.get("sequence"),
                        point.get("siteId"),
                        point.get("assetId"),
                        point.get("deviceId"),
                        point.get("vibrationRmsRaw"),
                        point.get("vibrationPeakHz"),
                        point.get("acousticRmsRaw"),
                        point.get("acousticPeakHz"),
                        point.get("rpm"),
                        point.get("anomalyScore"),
                        point.get("anomalyStatus"),
                    ]
                )
        else:
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
                        point.get("minute"),
                        point.get("vibrationRmsMmS"),
                        point.get("acousticDb"),
                        point.get("rpm"),
                        point.get("anomalyScore"),
                    ]
                )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        filename = f"{site_id}_{asset_id}.csv"
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", f'attachment; filename="{filename}"')
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header(
            "access-control-allow-methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header(
            "access-control-allow-methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("content-length", str(content_length))

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("client=%s %s", self.address_string(), fmt % args)


def authorized_events(
    user: dict[str, Any],
    site_id: str = "",
    asset_id: str = "",
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    require_permission(user, "event:read")
    normalized_site_id = site_id.strip().upper()
    normalized_asset_id = asset_id.strip().upper()
    if normalized_site_id:
        get_site(normalized_site_id)
        require_site_access(user, normalized_site_id)
    if normalized_asset_id:
        asset = get_asset_by_id(normalized_asset_id)
        if normalized_site_id and asset["siteId"] != normalized_site_id:
            raise ApiError(
                400, "ASSET_SITE_MISMATCH", "assetId does not belong to siteId."
            )
        require_site_access(user, asset["siteId"])
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    allowed = user.get("allowedSiteIds", [])
    rows = [event for event in EVENTS if "*" in allowed or event["siteId"] in allowed]
    if normalized_site_id:
        rows = [event for event in rows if event["siteId"] == normalized_site_id]
    if normalized_asset_id:
        rows = [event for event in rows if event["assetId"] == normalized_asset_id]
    filtered = []
    for event in rows:
        occurred_at = event.get("occurredAt")
        try:
            occurred_value = parse_rfc3339("event.occurredAt", occurred_at)
        except ApiError:
            continue
        if from_value and occurred_value < from_value:
            continue
        if to_value and occurred_value > to_value:
            continue
        filtered.append(event)
    return copy_payload(
        sorted(
            filtered,
            key=lambda event: str(event.get("occurredAt") or ""),
            reverse=True,
        )
    )


def filter_site_rows(
    user: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    allowed = user.get("allowedSiteIds", [])
    if "*" in allowed:
        return copy_payload(rows)
    return copy_payload([row for row in rows if row.get("siteId") in allowed])


def paginated_devices(
    user: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    site_id = query.get("siteId", [""])[0].strip().upper()
    if site_id:
        get_site(site_id)
        require_site_access(user, site_id)
        rows = devices_for(site_id)
    else:
        rows = []
        for site in visible_sites_for_user(user):
            rows.extend(devices_for(site["id"]))

    asset_id = query.get("assetId", [""])[0].strip().upper()
    status = query.get("status", [""])[0].strip().lower()
    if asset_id:
        rows = [row for row in rows if row["assetId"] == asset_id]
    if status:
        rows = [
            row
            for row in rows
            if status
            in {
                str(row.get("health", "")).lower(),
                str(row.get("mappingStatus", "")).lower(),
            }
        ]

    page = positive_query_int(query, "page", 1)
    size = positive_query_int(query, "size", 50, maximum=200)
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": copy_payload(rows[start : start + size]),
        "page": page,
        "size": size,
        "total": total,
    }


def path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def required_query(query: dict[str, list[str]], key: str) -> str:
    value = query.get(key, [""])[0].strip()
    if not value:
        raise ApiError(400, "MISSING_QUERY_PARAMETER", f"{key} is required.")
    return value


def positive_query_int(
    query: dict[str, list[str]], key: str, default: int, *, maximum: int | None = None
) -> int:
    value = query.get(key, [str(default)])[0].strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(
            400, "INVALID_QUERY_PARAMETER", f"{key} must be a positive integer."
        ) from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        suffix = f" up to {maximum}" if maximum is not None else ""
        raise ApiError(
            400, "INVALID_QUERY_PARAMETER", f"{key} must be a positive integer{suffix}."
        )
    return parsed


def optional_boolean_query(query: dict[str, list[str]], key: str) -> bool | None:
    if key not in query:
        return None
    value = query[key][0].strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ApiError(400, "INVALID_QUERY_PARAMETER", f"{key} must be true or false.")


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), AppHandler)
