from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime
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
    acoustic_taxonomies_for,
    alert_policies_for,
    anomaly_rule_for,
    audit_logs_for,
    assets_for,
    authenticate,
    copy_payload,
    close_runtime_state,
    configure_runtime_state,
    create_asset,
    create_connectivity_test,
    create_device,
    create_dataset_version,
    create_environment_inspection,
    create_install_point,
    create_site,
    current_user_for_token,
    dataset_export_for,
    dataset_version_for,
    dataset_versions_for,
    dashboard_sites_summary,
    deactivate_site,
    delete_asset,
    delete_device,
    delete_site,
    devices_for,
    device_health_for,
    update_device_health,
    environment_inspections_for,
    event_detail_for,
    event_note_history_for,
    event_notes_for,
    event_reviews_for,
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
    maintain_runtime_retention,
    network_profile,
    network_profiles_for_sites,
    parse_rfc3339,
    quarantine_mqtt_message,
    quarantine_unregistered_device,
    report_service_dependency,
    require_permission,
    require_site_access,
    review_event,
    create_event_note,
    update_event_note,
    delete_event_note,
    role_policy,
    rollout_plans,
    rollout_plan_for,
    site_network_profile,
    service_health_dependencies,
    sensor_faults_for_device,
    telemetry_for,
    telemetry_principal_for_token,
    telemetry_units,
    update_asset,
    update_device,
    update_device_hardware_profile,
    update_acoustic_taxonomy,
    update_alert_policy,
    update_anomaly_rule,
    update_install_point,
    update_rollout_plan,
    update_parameter,
    update_site,
    update_site_network_profile,
    visible_sites_for_user,
    connectivity_tests_for_device,
    parameters_for,
    now_iso,
)
from .json_validation import loads_strict_json
from .alerts import AlertService
from .model_registry import create_baseline_version, create_model_version, versions_for
from .ai_results import submit_result, review_model
from .telemetry_bulk import ingest_telemetry_bulk
from .web import render_page
from .xlsx_export import dataset_xlsx_bytes
from . import remote_config
from .communication_quality import CommunicationQualityStore


LOGGER = logging.getLogger("motor_diagnosis")
MAX_JSON_BODY_BYTES = 64 * 1024
RETENTION_MAINTENANCE_INTERVAL_SEC = 60.0


def _safe_csv_cell(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    significant = value.lstrip(" \t\r\n")
    if significant and significant[0] in "=+-@":
        return "'" + value
    return value


def _event_occurred_at(item: dict[str, Any]) -> datetime:
    return parse_rfc3339("event.occurredAt", item.get("occurredAt"))


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
                    "timestamp": now_iso(),
                }
            )
            return

        if (
            len(segments) == 5
            and segments[:2] == ["api", "devices"]
            and segments[3:] == ["configuration", "pending"]
        ):
            self.send_json(
                remote_config.pending_configuration(self.bearer_token(), segments[2])
            )
            return

        user = self.require_user()

        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "communication-quality"
        ):
            self.send_json(
                self.server.communication_quality.query(user, segments[2], query)
            )
            return

        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "configuration"
        ):
            self.send_json(remote_config.configuration_for(user, segments[2]))
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "devices"]
            and segments[3:] == ["configuration", "history"]
        ):
            self.send_json(remote_config.configuration_history(user, segments[2]))
            return

        if segments == ["api", "bootstrap"]:
            response: dict[str, Any] = {"sites": visible_sites_for_user(user)}
            response["demoEnabled"] = self.server.demo_enabled and has_permission(
                user, "event:write"
            )
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
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "environment-inspections"
        ):
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(
                environment_inspections_for(
                    user,
                    segments[2],
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                    page=page,
                    size=size,
                )
            )
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "faults"
        ):
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(
                sensor_faults_for_device(
                    user,
                    segments[2],
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                    status=query.get("status", [""])[0],
                    fault_type=query.get("faultType", [""])[0],
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
        if segments == ["api", "label-taxonomies", "acoustic"]:
            self.send_json(acoustic_taxonomies_for(user, query.get("version", [""])[0]))
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
        if segments == ["api", "alerts", "policies"]:
            self.send_json(alert_policies_for(user))
            return
        if segments == ["api", "alerts"]:
            self.send_json(
                self.server.alerts.list_for(
                    user,
                    site_id=query.get("siteId", [""])[0],
                    channel=query.get("channel", [""])[0],
                    status=query.get("status", [""])[0],
                    page=positive_query_int(query, "page", 1),
                    size=positive_query_int(query, "size", 50, maximum=200),
                )
            )
            return
        if (
            len(segments) in {2, 3}
            and segments[:1] == ["api"]
            and segments[1] in {"model-versions", "baseline-versions"}
        ):
            self.send_json(
                versions_for(
                    user,
                    "model" if segments[1] == "model-versions" else "baseline",
                    version=segments[2] if len(segments) == 3 else "",
                    site_id=query.get("siteId", [""])[0],
                    asset_id=query.get("assetId", [""])[0],
                    status=query.get("status", [""])[0],
                    page=positive_query_int(query, "page", 1),
                    size=positive_query_int(query, "size", 50, maximum=200),
                )
            )
            return
        if segments == ["api", "parameters"]:
            self.send_json(parameters_for(user, query.get("category", [""])[0]))
            return
        if segments == ["api", "audit-logs"]:
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(
                audit_logs_for(
                    user,
                    action=query.get("action", [""])[0],
                    target_type=query.get("targetType", [""])[0],
                    actor_id=query.get("actorId", [""])[0],
                    from_timestamp=query.get("from", [None])[0],
                    to_timestamp=query.get("to", [None])[0],
                    page=page,
                    size=size,
                )
            )
            return
        if segments == ["api", "datasets"]:
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(dataset_versions_for(user, page=page, size=size))
            return
        if (
            len(segments) == 3
            and segments[:2] == ["api", "datasets"]
            and segments[2] != "export"
        ):
            self.send_json(dataset_version_for(user, segments[2]))
            return
        if len(segments) == 4 and segments[:3] == ["api", "anomaly", "rules"]:
            asset = get_asset_by_id(segments[3])
            require_site_access(user, asset["siteId"])
            require_permission(user, "anomaly-rule:read")
            self.send_json(anomaly_rule_for(asset["id"]))
            return
        if len(segments) == 4 and segments[:3] == ["api", "anomaly", "events"]:
            detail = event_detail_for(user, segments[3])
            if detail["context"]["source"] == "stored":
                detail["context"]["points"] = annotate_telemetry_points(
                    detail["context"]["points"]
                )
            self.send_json(detail)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "events"]
            and segments[3] == "reviews"
        ):
            page = positive_query_int(query, "page", 1)
            size = positive_query_int(query, "size", 50, maximum=200)
            self.send_json(event_reviews_for(user, segments[2], page=page, size=size))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "events"]
            and segments[3] == "notes"
        ):
            self.send_json(event_notes_for(user, segments[2]))
            return
        if (
            len(segments) == 6
            and segments[:2] == ["api", "events"]
            and segments[3] == "notes"
            and segments[5] == "history"
        ):
            self.send_json(event_note_history_for(user, segments[2], segments[4]))
            return
        if segments == ["api", "events"]:
            self.send_json(paginated_events(user, query))
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
                    include_demo=True,
                )
            )
            self.send_json(
                {
                    "siteId": site_id,
                    "assetId": asset_id,
                    "units": telemetry_units(site_id, asset_id, include_demo=True),
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
        if segments == ["api", "datasets", "export"]:
            export_format = query.get("format", ["csv"])[0].strip().lower()
            if export_format not in {"csv", "xlsx"}:
                raise ApiError(
                    400,
                    "INVALID_EXPORT_FORMAT",
                    "format must be csv or xlsx.",
                )
            site_id = required_query(query, "siteId")
            asset_id = required_query(query, "assetId")
            export = dataset_export_for(
                user,
                site_id,
                asset_id,
                dataset_id=query.get("datasetId", [""])[0],
                from_timestamp=query.get("from", [None])[0],
                to_timestamp=query.get("to", [None])[0],
            )
            if export_format == "xlsx":
                self.send_dataset_xlsx(export)
            else:
                self.send_dataset_csv(export)
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
        if segments == ["api", "ai1", "results"]:
            result, status = submit_result(self.bearer_token(), payload)
            self.send_json(result, status=status)
            return
        if segments == ["api", "telemetry", "bulk"]:
            result = ingest_telemetry_bulk(
                telemetry_principal_for_token(self.bearer_token()), payload
            )
            self.send_json(result, status=207 if result["rejected"] else 200)
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
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "health"
        ):
            self.send_json(
                update_device_health(
                    telemetry_principal_for_token(self.bearer_token()),
                    segments[2],
                    payload,
                )
            )
            return

        if (
            len(segments) == 5
            and segments[:2] == ["api", "devices"]
            and segments[3:] == ["configuration", "result"]
        ):
            self.send_json(
                remote_config.report_configuration(self.bearer_token(), segments[2], payload)
            )
            return

        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "communication-quality"
        ):
            response, status = self.server.communication_quality.ingest(
                self.bearer_token(), segments[2], payload
            )
            self.send_json(response, status=status)
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
            and segments[:2] == ["api", "devices"]
            and segments[3] == "environment-inspections"
        ):
            self.send_json(
                create_environment_inspection(user, segments[2], payload), status=201
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
            if not self.server.demo_enabled:
                raise ApiError(
                    403, "DEMO_DISABLED", "Demo injection is disabled on this server."
                )
            require_permission(user, "event:write")
            from .data import STORE_LOCK, append_audit_log, required_text

            with STORE_LOCK:
                site_id = required_text(payload, "siteId").upper()
                require_site_access(user, site_id)
                event = inject_anomaly({**payload, "siteId": site_id})
                append_audit_log(
                    user,
                    "demo.inject",
                    "event",
                    event["id"],
                    None,
                    event,
                    "Synthetic demo event injected",
                    site_id=site_id,
                )
            self.send_json(event, status=201)
            return
        if segments == ["api", "alerts", "send"]:
            self.send_json(self.server.alerts.send(user, payload), status=202)
            return
        if segments == ["api", "baseline-versions"]:
            self.send_json(create_baseline_version(user, payload), status=201)
            return
        if len(segments) == 4 and segments[:2] == ["api", "model-versions"] and segments[3] == "reviews":
            self.send_json(review_model(user, segments[2], payload))
            return
        if segments == ["api", "model-versions"]:
            self.send_json(create_model_version(user, payload), status=201)
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "events"]
            and segments[3] == "review"
        ):
            require_permission(user, "event:review")
            self.send_json(review_event(user, segments[2], payload))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "events"]
            and segments[3] == "notes"
        ):
            self.send_json(create_event_note(user, segments[2], payload), status=201)
            return
        if segments == ["api", "datasets"]:
            self.send_json(create_dataset_version(user, payload), status=201)
            return
        raise ApiError(404, "NOT_FOUND", "API route was not found.")

    def route_put(self, segments: list[str]) -> None:
        payload = self.read_json()
        user = self.require_user()
        if (
            len(segments) == 4
            and segments[:2] == ["api", "devices"]
            and segments[3] == "configuration"
        ):
            self.send_json(
                remote_config.request_configuration(user, segments[2], payload)
            )
            return
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
        if len(segments) == 4 and segments[:3] == ["api", "anomaly", "rules"]:
            self.send_json(update_anomaly_rule(user, segments[3], payload))
            return
        if len(segments) == 4 and segments[:3] == ["api", "alerts", "policies"]:
            self.send_json(update_alert_policy(user, segments[3], payload))
            return
        if len(segments) == 3 and segments[:2] == ["api", "parameters"]:
            self.send_json(update_parameter(user, segments[2], payload))
            return
        if segments == ["api", "label-taxonomies", "acoustic"]:
            self.send_json(update_acoustic_taxonomy(user, payload))
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
        if (
            len(segments) == 5
            and segments[:2] == ["api", "events"]
            and segments[3] == "notes"
        ):
            self.send_json(update_event_note(user, segments[2], segments[4], payload))
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
        if (
            len(segments) == 5
            and segments[:2] == ["api", "events"]
            and segments[3] == "notes"
        ):
            self.send_json(delete_event_note(user, segments[2], segments[4]))
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
        try:
            data = loads_strict_json(self.rfile.read(length).decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ApiError(
                400, "INVALID_JSON", "Request body must be valid UTF-8 JSON."
            ) from exc
        except RecursionError as exc:
            raise ApiError(
                400, "INVALID_JSON", "Request body nesting is too deep."
            ) from exc
        except ValueError as exc:
            raise ApiError(
                400, "INVALID_JSON", "Request body is not valid JSON."
            ) from exc
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
        error = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            error["details"] = exc.details
        self.send_json({"error": error}, status=exc.status)

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

    def send_dataset_csv(self, export: dict[str, Any]) -> None:
        manifest = export["manifest"]
        rows = []
        for item in export["rows"]:
            rows.append(
                {
                    **item,
                    "dataset_id": manifest.get("datasetId"),
                    "source_filters": json.dumps(
                        manifest.get("sourceFilters"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "source_record_count": manifest["sourceRecordCount"],
                    "normalized_record_count": manifest["normalizedRecordCount"],
                    "split_counts": json.dumps(
                        manifest["splitCounts"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "manifest_checksum": manifest["checksum"],
                    "source_uri": manifest["source"].get("uri"),
                    "source_license": manifest["source"].get("license"),
                    "source_checksum": manifest["source"].get("checksum"),
                    "signal_types": json.dumps(
                        manifest["compatibility"].get("signalType", []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "sampling_rate_hz": manifest["compatibility"].get("samplingRateHz"),
                    "units": json.dumps(
                        manifest["compatibility"].get("units", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "operating_conditions": json.dumps(
                        manifest["compatibility"].get("operatingConditions", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "label_mapping": json.dumps(
                        manifest.get("labelMapping", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "label_priority": json.dumps(
                        manifest.get("labelPriority", []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "label_policy_version": manifest.get("labelPolicyVersion"),
                    "snapshot_schema_version": manifest.get("snapshotSchemaVersion"),
                    "label_counts": (
                        json.dumps(
                            manifest["labelCounts"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "labelCounts" in manifest
                        else None
                    ),
                    "training_eligible_count": manifest.get("trainingEligibleCount"),
                    "training_eligible_split_counts": (
                        json.dumps(
                            manifest["trainingEligibleSplitCounts"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "trainingEligibleSplitCounts" in manifest
                        else None
                    ),
                    "split_policy": manifest["splitPolicy"],
                }
            )
        fieldnames = [
            "site_id",
            "asset_id",
            "device_id",
            "timestamp",
            "sequence",
            "vibration_rms_raw",
            "vibration_rms_mm_s",
            "vibration_peak_hz",
            "acoustic_rms_raw",
            "acoustic_db",
            "acoustic_peak_hz",
            "rpm",
            "scenario_label",
            "known_vibration_label",
            "known_acoustic_label",
            "telemetry_source",
            "is_synthetic",
            "vibration_unit_note",
            "acoustic_unit_note",
            "event_id",
            "event_label",
            "label_taxonomy_version",
            "ground_truth_label",
            "ground_truth_source",
            "target_label",
            "target_label_taxonomy_version",
            "label_status",
            "training_eligible",
            "event_reviewed",
            "dataset_split",
            "dataset_id",
            "source_filters",
            "source_record_count",
            "normalized_record_count",
            "split_counts",
            "manifest_checksum",
            "source_uri",
            "source_license",
            "source_checksum",
            "signal_types",
            "sampling_rate_hz",
            "units",
            "operating_conditions",
            "label_mapping",
            "label_priority",
            "label_policy_version",
            "snapshot_schema_version",
            "label_counts",
            "training_eligible_count",
            "training_eligible_split_counts",
            "split_policy",
        ]
        fieldnames.extend(
            key
            for key in ("rpm_status", "rpm_measured_at", "rpm_source")
            if any(key in row for row in rows)
        )
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            {key: _safe_csv_cell(value) for key, value in row.items()} for row in rows
        )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        filename = f"{manifest['siteId']}_{manifest['assetId']}_dataset.csv"
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", f'attachment; filename="{filename}"')
        self.send_header("x-dataset-checksum", manifest["checksum"])
        self.send_header("x-dataset-record-count", str(manifest["recordCount"]))
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header(
            "access-control-allow-methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        self.send_header("access-control-allow-headers", "authorization, content-type")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_dataset_xlsx(self, export: dict[str, Any]) -> None:
        manifest = export["manifest"]
        body = dataset_xlsx_bytes(export)
        filename = f"{manifest['siteId']}_{manifest['assetId']}_dataset.xlsx"
        self.send_response(200)
        self.send_header(
            "content-type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header("content-disposition", f'attachment; filename="{filename}"')
        self.send_header("x-dataset-checksum", manifest["checksum"])
        self.send_header("x-dataset-record-count", str(manifest["recordCount"]))
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
    filtered: list[tuple[Any, dict[str, Any]]] = []
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
        filtered.append((occurred_value, event))
    return copy_payload(
        [
            event
            for _, event in sorted(
                filtered,
                key=lambda row: row[0],
                reverse=True,
            )
        ]
    )


def paginated_events(
    user: dict[str, Any], query: dict[str, list[str]]
) -> dict[str, Any]:
    rows = authorized_events(
        user,
        site_id=query.get("siteId", [""])[0],
        asset_id=query.get("assetId", [""])[0],
        from_timestamp=query.get("from", [None])[0],
        to_timestamp=query.get("to", [None])[0],
    )
    severity = query.get("severity", [""])[0].strip().lower()
    label = query.get("label", [""])[0].strip().lower()
    reviewed = optional_boolean_query(query, "reviewed")
    if severity:
        if severity not in {"warning", "critical", "device"}:
            raise ApiError(400, "INVALID_SEVERITY", "severity filter is not supported.")
        rows = [
            item for item in rows if str(item.get("severity", "")).lower() == severity
        ]
    if label:
        rows = [item for item in rows if str(item.get("label", "")).lower() == label]
    if reviewed is not None:
        rows = [item for item in rows if bool(item.get("reviewed", False)) is reviewed]
    for item in rows:
        item["maxScore"] = item.get("maxScore", item.get("score"))
        item["durationSec"] = item.get("durationSec")
        item["reviewed"] = bool(item.get("reviewed", False))
    sort = query.get("sort", ["unreviewed_desc"])[0].strip()
    if sort == "unreviewed_desc":
        rows.sort(key=_event_occurred_at, reverse=True)
        rows.sort(key=lambda item: bool(item.get("reviewed", False)))
    elif sort == "occurredAt_desc":
        rows.sort(key=_event_occurred_at, reverse=True)
    elif sort == "occurredAt_asc":
        rows.sort(key=_event_occurred_at)
    elif sort == "score_desc":
        rows.sort(key=lambda item: float(item.get("maxScore") or 0), reverse=True)
    else:
        raise ApiError(400, "INVALID_SORT", "sort is not supported for event lookup.")
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


class MotorDiagnosisServer(ThreadingHTTPServer):
    # ThreadingHTTPServer defaults to daemon request threads, which are not
    # joined by server_close. A 2xx write must commit before its DB is closed.
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args, **kwargs):
        self._admission_lock = threading.Lock()
        self._closing = False
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self._admission_lock:
            if self._closing:
                self.shutdown_request(request)
                return
            # Include thread registration in the admission fence so close()
            # cannot miss an accepted request that has not started running yet.
            super().process_request(request, client_address)

    def get_request(self):
        request, address = super().get_request()
        # A client that never finishes its headers/body cannot hold shutdown
        # indefinitely now that accepted request threads are drained.
        request.settimeout(30.0)
        return request, address

    def start_alert_worker(self):
        self._alert_stop = threading.Event()
        self._alert_worker = threading.Thread(
            target=self._run_alert_worker, name="alert-outbox", daemon=True
        )
        self._alert_worker.start()

    def start_retention_worker(self):
        self._retention_stop = threading.Event()
        self._retention_worker = threading.Thread(
            target=self._run_retention_worker, name="runtime-retention", daemon=True
        )
        self._retention_worker.start()

    def _run_retention_worker(self):
        # Independent of alert delivery and of the HTTP accept loop: expiration
        # continues on an idle server, including when auto_alerts is disabled.
        while not self._retention_stop.wait(RETENTION_MAINTENANCE_INTERVAL_SEC):
            try:
                maintain_runtime_retention()
            except Exception:
                LOGGER.exception("Runtime retention failed; will retry on next tick")
            try:
                self.communication_quality.prune()
            except Exception:
                LOGGER.exception("Quality retention failed; will retry on next tick")

    def _run_alert_worker(self):
        # SQLite can wait on another writer. Never perform this work in
        # BaseServer.service_actions(), which runs in the HTTP accept loop.
        # One coordinator coalesces polling; ticks never overlap or accumulate.
        while not self._alert_stop.wait(0.5):
            if not self.auto_alerts:
                continue
            try:
                self.alerts.tick()
            except Exception:
                LOGGER.exception(
                    "Alert outbox processing failed; will retry on next tick"
                )

    def server_close(self):
        with self._admission_lock:
            self._closing = True
        if hasattr(self, "_retention_stop"):
            self._retention_stop.set()
            if self._retention_worker.ident is not None:
                self._retention_worker.join()
        if hasattr(self, "_alert_stop"):
            self._alert_stop.set()
            if self._alert_worker.ident is not None:
                self._alert_worker.join()
        super().server_close()
        if hasattr(self, "communication_quality"):
            self.communication_quality.close()
        if hasattr(self, "alerts"):
            self.alerts.close()
        close_runtime_state()


def create_server(
    host: str,
    port: int,
    *,
    demo_enabled=None,
    alert_database=":memory:",
    alert_adapters=None,
    auto_alerts=True,
    state_database=None,
    communication_database=":memory:",
) -> ThreadingHTTPServer:
    if demo_enabled is None:
        demo_enabled = os.environ.get("DEMO_ENABLED", "false").lower() == "true"
    configure_runtime_state(state_database)
    try:
        server = MotorDiagnosisServer((host, port), AppHandler)
    except Exception:
        close_runtime_state()
        raise
    server.demo_enabled = (
        bool(demo_enabled) and os.environ.get("APP_ENV", "").lower() != "production"
    )
    server.auto_alerts = auto_alerts
    try:
        server.communication_quality = CommunicationQualityStore(
            communication_database
        )
        server.alerts = AlertService(
            alert_database,
            adapters=alert_adapters,
        )
        server.start_alert_worker()
        if state_database or communication_database != ":memory:":
            server.start_retention_worker()
    except Exception:
        server.server_close()
        raise
    return server
