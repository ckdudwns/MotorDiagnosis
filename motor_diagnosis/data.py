from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

from ai.ai2.week2.anomaly_score import score_telemetry_point
from ai.ai2.week3.event_lifecycle import AnomalyEventLifecycle, EventLifecycleConfig

from . import device_lifecycle
from .demo_signals import (
    DEMO_MODEL_VERSION,
    DEMO_SIGNAL_SOURCE,
    MAX_DEMO_ANOMALY_SAMPLES,
    build_combined_anomaly_samples,
)
from .runtime_store import DurableStateLock, RuntimeStateStore

MAX_LOGIN_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 60 * 60
MAX_REVIEW_NOTE_LENGTH = 2000
MAX_REASON_LENGTH = 1000
MAX_OPERATING_CONDITION_DEPTH = 8
MIN_RATED_RPM = 1
MAX_RATED_RPM = 120_000
DEVICE_CERTIFICATE_STATUSES = {"pending", "registered", "revoked", "expired"}
DEVICE_MAPPING_STATUSES = {"active", "inactive"}
TELEMETRY_SCENARIOS = {
    "normal",
    "vibration_anomaly",
    "acoustic_anomaly",
    "combined_anomaly",
}
TELEMETRY_MAX_RECORDS_PER_ASSET = 1000
TELEMETRY_MAX_FUTURE_SECONDS = 5 * 60
CONNECTIVITY_TEST_PHASES = {"before", "after"}
CONNECTIVITY_TEST_VERDICTS = {"pass", "warn", "fail"}
HARDWARE_COMPONENTS = {"sensors", "board", "connectivity", "power", "enclosure"}
EVENT_LABELS = {
    "needs_review",
    "normal_false_positive",
    "confirmed_anomaly",
    "repair_completed",
    "sensor_issue",
}
EVENT_SEVERITIES = {"normal", "warning", "critical", "device"}
EVENT_NOTE_CATEGORIES = {"root_cause", "inspection", "action"}
ALERT_CHANNELS = {"web", "email", "webhook", "stub"}
ENVIRONMENT_INSPECTION_STATUSES = {
    "not_checked",
    "ok",
    "attention",
    "critical",
}
DATASET_SPLIT_POLICY = "asset_grouped"
DATASET_LABEL_POLICY_VERSION = "LABEL-POLICY-V2"
DATASET_SNAPSHOT_SCHEMA_VERSION = "2"
DATASET_LABEL_PRIORITY_V1 = [
    "event_review",
    "known_vibration_label",
    "known_acoustic_label",
    "scenario_label",
    "event_candidate",
]
DATASET_LABEL_PRIORITY = [
    "event_review",
    "known_vibration_label",
    "known_acoustic_label",
    "scenario_label",
]
NON_ASSET_ANOMALY_EVENT_LABELS = {
    "normal_false_positive",
    "repair_completed",
    "sensor_issue",
}

NETWORK_PROFILES = [
    {
        "id": "NET-DIRECT",
        "type": "A",
        "name": "Direct upload",
        "connectivityType": "wifi/ethernet/lte",
        "offlineSync": False,
        "recommendedTopology": "direct",
        "condition": "Stable wired or Wi-Fi network is available at the site.",
        "architecture": "Sensor node -> MQTT/TLS or HTTP -> central collection API",
        "requiredEquipment": [
            "ESP32-S3 edge node",
            "device certificate",
            "stable power",
        ],
        "qualityChecks": ["RSSI", "packet loss", "display latency under 5 seconds"],
    },
    {
        "id": "NET-GATEWAY",
        "type": "B",
        "name": "Gateway relay",
        "connectivityType": "gateway",
        "offlineSync": False,
        "recommendedTopology": "gateway",
        "condition": "Equipment network is available but external access is unstable.",
        "architecture": "Sensor nodes -> local gateway -> central server",
        "requiredEquipment": [
            "field gateway",
            "LTE or wired backhaul",
            "offline buffer",
        ],
        "qualityChecks": ["gateway power", "site survey", "device heartbeat"],
    },
    {
        "id": "NET-STORE-FWD",
        "type": "C",
        "name": "Limited network",
        "connectivityType": "wifi/ethernet/lte/gateway",
        "offlineSync": True,
        "recommendedTopology": "gateway",
        "condition": "Always-on communication is hard or bandwidth is constrained.",
        "architecture": "Summary upload + store-and-forward + event priority retry",
        "requiredEquipment": [
            "local storage",
            "time sync",
            "summary feature extraction",
        ],
        "qualityChecks": [
            "offline retention hours",
            "capacity threshold",
            "duplicate removal",
        ],
    },
    {
        "id": "NET-OFFLINE",
        "type": "D",
        "name": "Offline verification",
        "connectivityType": "offline",
        "offlineSync": True,
        "recommendedTopology": "offline",
        "condition": "The site is in early verification or network type is undecided.",
        "architecture": "Local storage and manual export before central ingestion",
        "requiredEquipment": [
            "field storage media",
            "manual export tool",
            "temporary collector",
        ],
        "qualityChecks": ["missing data prevention", "time sync", "field self-check"],
    },
]

NETWORK_PROFILE_IDS = {profile["type"]: profile["id"] for profile in NETWORK_PROFILES}
NETWORK_PROFILE_TYPES = {profile["id"]: profile["type"] for profile in NETWORK_PROFILES}

PROFILE_CYCLE = ["A", "B", "C", "A", "B", "C", "D", "A", "B", "C"]
STATUS_CYCLE = ["normal", "normal", "warning", "normal", "critical", "normal", "device"]
ASSET_TEMPLATES = [
    {
        "suffix": "GEN-01",
        "name": "Generator 1",
        "assetType": "generator",
        "ratedRpm": 1800,
    },
    {
        "suffix": "MOT-02",
        "name": "Cooling pump motor",
        "assetType": "motor",
        "ratedRpm": 1450,
    },
    {
        "suffix": "FAN-03",
        "name": "Ventilation fan motor",
        "assetType": "motor",
        "ratedRpm": 1200,
    },
    {
        "suffix": "PMP-04",
        "name": "Fuel transfer pump",
        "assetType": "pump",
        "ratedRpm": 1600,
    },
]

PARAMETERS = {
    "TELEMETRY_INTERVAL_SEC": 1,
    "ANOMALY_SCORE_THRESHOLD": 75,
    "ANOMALY_HOLD_SEC": 10,
    "ANOMALY_HYSTERESIS": 5,
    "ANOMALY_ENABLED": True,
    "DEVICE_OFFLINE_SEC": 120,
    "EDGE_BUFFER_HOURS": 24,
    "RETENTION_RAW_DAYS": 30,
}

PARAMETER_DEFINITIONS = {
    "TELEMETRY_INTERVAL_SEC": {
        "category": "telemetry",
        "type": "integer",
        "minimum": 1,
        "maximum": 3600,
        "unit": "seconds",
        "mutable": False,
        "managedBy": "device_firmware",
    },
    "ANOMALY_SCORE_THRESHOLD": {
        "category": "anomaly",
        "type": "number",
        "minimum": 0,
        "maximum": 100,
        "unit": "score",
        "runtimeTarget": "anomaly_rules.scoreThreshold",
    },
    "ANOMALY_HOLD_SEC": {
        "category": "anomaly",
        "type": "integer",
        "minimum": 1,
        "maximum": 3600,
        "unit": "seconds",
        "runtimeTarget": "anomaly_rules.durationSec",
    },
    "ANOMALY_HYSTERESIS": {
        "category": "anomaly",
        "type": "number",
        "minimum": 0,
        "maximum": 50,
        "unit": "score",
        "runtimeTarget": "anomaly_rules.hysteresis",
    },
    "ANOMALY_ENABLED": {
        "category": "anomaly",
        "type": "boolean",
        "unit": "boolean",
        "runtimeTarget": "anomaly_rules.active",
    },
    "DEVICE_OFFLINE_SEC": {
        "category": "device",
        "type": "integer",
        "minimum": 5,
        "maximum": 86400,
        "unit": "seconds",
    },
    "EDGE_BUFFER_HOURS": {
        "category": "edge",
        "type": "integer",
        "minimum": 1,
        "maximum": 168,
        "unit": "hours",
        "mutable": False,
        "managedBy": "device_firmware",
    },
    "RETENTION_RAW_DAYS": {
        "category": "retention",
        "type": "integer",
        "minimum": 1,
        "maximum": 3650,
        "unit": "days",
    },
}

BASE_ALERT_POLICIES = [
    {
        "id": "ALERT-POLICY-DEFAULT",
        "severity": "critical",
        "siteIds": [],
        "assetIds": [],
        "scopeSiteIds": [],
        "workHours": {"start": "00:00", "end": "23:59"},
        "recipients": ["operations"],
        "channels": ["web"],
        "cooldownSec": 300,
        "enabled": True,
        "updatedAt": "2026-08-24T00:00:00Z",
    }
]

USERS = [
    {
        "id": "user-operator",
        "username": "operator",
        "passwordSalt": "operator-demo-salt",
        "passwordHash": "f9140e2c24cde640d33348add1b0c32036b6eb531f577a85b8784f48f7003eb9",
        "name": "Operator",
        "role": "A",
        "allowedSiteIds": ["SITE-01", "SITE-02", "SITE-03", "SITE-04"],
    },
    {
        "id": "user-admin",
        "username": "admin",
        "passwordSalt": "admin-demo-salt",
        "passwordHash": "ad5df777969ba1244e1d0ba8e21e6a8dea7e65c1ecdfc1f0d6a446002d29fd38",
        "name": "Administrator",
        "role": "B",
        "allowedSiteIds": ["*"],
    },
    {
        "id": "user-system",
        "username": "system",
        "passwordSalt": "system-demo-salt",
        "passwordHash": "03f8cd538f35400b35397647fc059433a76c6da838d9433b6eee1ea5ce652f82",
        "name": "System administrator",
        "role": "C",
        "allowedSiteIds": ["*"],
    },
]

ROLE_POLICIES = [
    {
        "role": "A",
        "name": "Operator",
        "description": "Read-only access to assigned sites, events, telemetry, and exports.",
        "permissions": [
            "site:read",
            "asset:read",
            "device:read",
            "rollout:read",
            "network-profile:read",
            "install-point:read",
            "event:read",
            "event:review",
            "telemetry:read",
            "dashboard:read",
            "connectivity-test:read",
            "hardware-profile:read",
            "export:read",
            "alert-policy:read",
            "alert:read",
            "model:read",
            "baseline:read",
            "parameter:read",
            "audit-log:read",
            "environment-inspection:read",
            "sensor-fault:read",
            "anomaly-rule:read",
            "dataset:read",
            "label-taxonomy:read",
        ],
    },
    {
        "role": "B",
        "name": "Administrator",
        "description": "Manage site, asset, and device master data.",
        "permissions": [
            "site:read",
            "site:write",
            "asset:read",
            "asset:write",
            "device:read",
            "device:write",
            "role:read",
            "rollout:read",
            "rollout:write",
            "network-profile:read",
            "network-profile:write",
            "install-point:read",
            "install-point:write",
            "configuration:read",
            "event:read",
            "event:write",
            "event:review",
            "telemetry:read",
            "dashboard:read",
            "connectivity-test:read",
            "connectivity-test:write",
            "hardware-profile:read",
            "hardware-profile:write",
            "export:read",
            "alert-policy:read",
            "alert-policy:write",
            "alert:read",
            "alert:send",
            "model:read",
            "model:review",
            "baseline:read",
            "parameter:read",
            "parameter:write",
            "audit-log:read",
            "environment-inspection:read",
            "environment-inspection:write",
            "sensor-fault:read",
            "anomaly-rule:read",
            "anomaly-rule:write",
            "dataset:read",
            "dataset:write",
            "label-taxonomy:read",
            "label-taxonomy:write",
        ],
    },
    {
        "role": "C",
        "name": "System administrator",
        "description": "Full system configuration and operational access.",
        "permissions": ["*"],
    },
    {
        "role": "AI1",
        "name": "AI1 result producer (service only)",
        "description": "Dedicated handoff principal; no user login, approval or deployment permission.",
        "permissions": ["model:write", "baseline:read", "dataset:read"],
    },
]

ACOUSTIC_LABEL_TAXONOMY = [
    {
        "code": "normal",
        "name": "Normal",
        "description": "Repeated operation within the normal baseline range.",
        "reviewRule": "Compare against at least three normal windows from the same equipment.",
    },
    {
        "code": "bearing_suspect",
        "name": "Bearing suspect",
        "description": "High frequency noise or repeated impact patterns are observed.",
        "reviewRule": "Review vibration RMS and peak trend together.",
    },
    {
        "code": "friction_noise",
        "name": "Friction noise",
        "description": "Irregular scraping or rubbing-like noise is observed.",
        "reviewRule": "Check nearby equipment and protective case contact points.",
    },
    {
        "code": "mixed_noise",
        "name": "Mixed noise",
        "description": "Multiple machine, wind, or water sounds are mixed.",
        "reviewRule": "Record install position and surrounding noise sources together.",
    },
    {
        "code": "sensor_noise",
        "name": "Sensor noise",
        "description": "Sensor, cable, or waterproofing issues are more likely than equipment abnormality.",
        "reviewRule": "Check mounting state and environment before marking equipment fault.",
    },
]

DATA_PIPELINES = [
    {
        "id": "realtime-observability",
        "name": "Realtime collection path",
        "purpose": "Expose current equipment state and abnormal alerts quickly.",
        "data": ["summary features", "device status", "events", "recent retry windows"],
        "cadence": "second-level collection and display",
        "guardrail": "Separated from AI training so model workloads do not delay live monitoring.",
    },
    {
        "id": "ai-frequency-analysis",
        "name": "AI frequency analysis path",
        "purpose": "Accumulate data for three-month frequency-based model verification.",
        "data": ["raw waveform", "FFT/spectrum", "long-term trend", "expert review"],
        "cadence": "daily or weekly batch analysis",
        "guardrail": "Separated from realtime operations so dataset generation does not block the dashboard.",
    },
]

TELEMETRY_SERVICE_TOKENS = {
    "demo-telemetry-ingest-token": {
        "id": "service-ai2-replay",
        "type": "service",
        "permissions": ["telemetry:ingest"],
        "allowedDeviceIds": ["*"],
    },
    "demo-mqtt-ingest-token": {
        "id": "service-mqtt-collector",
        "type": "service",
        "permissions": [
            "telemetry:ingest",
            "telemetry:quarantine",
            "service-health:write",
        ],
        "allowedDeviceIds": ["*"],
        "allowedDependencyIds": ["mqtt"],
    },
    "demo-telemetry-validation-token": {
        "id": "service-telemetry-validation",
        "type": "service",
        "permissions": ["telemetry:ingest", "telemetry:label"],
        "allowedDeviceIds": ["*"],
        "demoOnly": True,
    },
    "demo-device-health-token": {
        "id": "service-device-health-demo",
        "type": "service",
        "permissions": ["device-health:write"],
        "allowedDeviceIds": ["*"],
        "demoOnly": True,
    },
}

HARDWARE_PROFILES = [
    {
        "id": "HW-ESP32S3-ADXL345-INMP441-WIFI",
        "name": "ESP32-S3 ADXL345/INMP441 Wi-Fi profile",
        "boardType": "esp32-s3",
        "connectivityType": "wifi",
        "sensors": ["adxl345", "inmp441"],
        "powerModule": "5v-usb-or-regulated",
        "enclosure": "ip65",
        "firmwareFamily": "esp32-edge-node",
        "active": True,
    },
    {
        "id": "HW-PICO2W-DUAL-SENSOR-WIFI",
        "name": "Pico 2 W dual-sensor Wi-Fi profile",
        "boardType": "pico-2-w",
        "connectivityType": "wifi",
        "sensors": ["mpu-6050", "inmp441"],
        "powerModule": "5v-dc-regulated",
        "enclosure": "ip65",
        "active": True,
    },
    {
        "id": "HW-PICO2-DUAL-SENSOR-GATEWAY",
        "name": "Pico 2 dual-sensor gateway profile",
        "boardType": "pico-2",
        "connectivityType": "gateway",
        "sensors": ["mpu-6050", "inmp441"],
        "powerModule": "5v-dc-regulated",
        "enclosure": "ip65",
        "active": True,
    },
    {
        "id": "HW-PICO2W-VIBRATION-WIFI",
        "name": "Pico 2 W vibration-only Wi-Fi profile",
        "boardType": "pico-2-w",
        "connectivityType": "wifi",
        "sensors": ["mpu-6050"],
        "powerModule": "5v-dc-regulated",
        "enclosure": "ip65",
        "active": True,
    },
]

BASE_SERVICE_DEPENDENCIES = [
    {
        "id": "mqtt",
        "name": "MQTT/TLS collection",
        "status": "ready",
        "latencyMs": 0.0,
        "errorRatePct": 0.0,
        "impactScope": "MQTT device ingestion",
        "lastFailureAt": None,
        "lastRecoveryAt": None,
        "lastCheckedAt": None,
        "detail": None,
        "statusReportCount": 0,
        "failureCount": 0,
    },
    {
        "id": "ingest-api",
        "name": "Telemetry ingest API",
        "status": "healthy",
        "latencyMs": 0.0,
        "errorRatePct": 0.0,
        "impactScope": "HTTP telemetry ingestion",
        "lastFailureAt": None,
        "lastRecoveryAt": None,
    },
    {
        "id": "storage",
        "name": "In-memory telemetry storage",
        "status": "healthy",
        "latencyMs": 0.0,
        "errorRatePct": 0.0,
        "impactScope": "Telemetry query and dashboard",
        "lastFailureAt": None,
        "lastRecoveryAt": None,
    },
    {
        "id": "analysis",
        "name": "Anomaly analysis worker",
        "status": "ready",
        "latencyMs": 0.0,
        "errorRatePct": 0.0,
        "impactScope": "Anomaly score updates",
        "lastFailureAt": None,
        "lastRecoveryAt": None,
    },
    {
        "id": "alerts",
        "name": "Alert channel",
        "status": "ready",
        "latencyMs": 0.0,
        "errorRatePct": 0.0,
        "impactScope": "External alert delivery",
        "lastFailureAt": None,
        "lastRecoveryAt": None,
    },
]


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    details: Any | None = None


STORE_LOCK = DurableStateLock()
SESSIONS: dict[str, dict[str, Any]] = {}
LOGIN_STATE: dict[str, dict[str, float | int]] = {
    user["username"]: {"failures": 0, "lockedUntil": 0.0} for user in USERS
}


def copy_payload(value: Any) -> Any:
    return deepcopy(value)


def now_text() -> str:
    return time.strftime("%H:%M:%S")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_seconds_ago(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(seconds=max(0, seconds)))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()


def verify_password(password: str, user: dict[str, Any]) -> bool:
    actual = hash_password(password, str(user["passwordSalt"]))
    return hmac.compare_digest(actual, str(user["passwordHash"]))


def rollout_stage(index: int) -> str:
    if index < 4:
        return "poc_selected"
    if index < 12:
        return "phase1_candidate"
    if index < 32:
        return "site_survey"
    return "planned"


def build_sites() -> list[dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    for index in range(65):
        total_devices = 3 + (index % 5)
        offline = 1 if index % 9 == 0 else 2 if index % 17 == 0 else 0
        profile_type = PROFILE_CYCLE[index % len(PROFILE_CYCLE)]
        region = (
            "west_coast" if index < 13 else "south_coast" if index < 33 else "east_jeju"
        )
        sites.append(
            {
                "id": f"SITE-{index + 1:02d}",
                "code": f"ISLAND-{index + 1:02d}",
                "name": f"Island Plant {index + 1:02d}",
                "region": region,
                "timezone": "Asia/Seoul",
                "network": profile_type,
                "networkType": profile_type,
                "priority": (
                    "high"
                    if index % 5 == 0
                    else "medium" if index % 3 == 0 else "normal"
                ),
                "status": STATUS_CYCLE[index % len(STATUS_CYCLE)],
                "operationStatus": "active",
                "totalDevices": total_devices,
                "onlineDevices": total_devices - offline,
                "eventCount": 3 if index % 6 == 0 else 1 if index % 4 == 0 else 0,
                "assetCount": 2 + (index % 4),
                "signalQuality": max(42, 96 - ((index * 7) % 48)),
                "rolloutStage": rollout_stage(index),
                "targetAssetCount": 2 + (index % 4),
            }
        )
    return sites


def build_assets(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for site in sites:
        for index, template in enumerate(ASSET_TEMPLATES[: site["assetCount"]], 1):
            asset_id = f"{site['id']}-{template['suffix']}"
            vibration = round(1.1 + (index * 0.12), 2)
            acoustic = round(49.0 + (index * 1.3), 1)
            assets.append(
                {
                    "id": asset_id,
                    "assetCode": template["suffix"],
                    "siteId": site["id"],
                    "name": template["name"],
                    "type": template["assetType"],
                    "assetType": template["assetType"],
                    "ratedRpm": template["ratedRpm"],
                    "rpm": template["ratedRpm"],
                    "operationStatus": "active",
                    "installLocation": f"Bay {index}",
                    "baselineStatus": (
                        "draft" if site["rolloutStage"] == "site_survey" else "ready"
                    ),
                    "baseline": {
                        "status": (
                            "draft"
                            if site["rolloutStage"] == "site_survey"
                            else "ready"
                        ),
                        "capturedAt": "initial",
                        "vibrationRmsMmS": vibration,
                        "acousticDb": acoustic,
                        "sampleCount": 120,
                    },
                }
            )
    return assets


def build_devices(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for index, asset in enumerate(assets, 1):
        health = "online" if index % 4 else "warning"
        certificate = {
            "id": f"CERT-{index:04d}",
            "fingerprint": hashlib.sha256(
                f"device-certificate-{index}".encode("utf-8")
            ).hexdigest(),
            "status": "registered",
            "issuedAt": "2026-08-01T00:00:00Z",
            "expiresAt": "2027-08-01T00:00:00Z",
        }
        last_seen_sec_ago = 18 + index * 3
        devices.append(
            {
                "id": f"DEV-{asset['id'].replace('SITE-', '')}",
                "siteId": asset["siteId"],
                "assetId": asset["id"],
                "sensorChannels": ["vibration", "acoustic", "rpm"],
                "firmware": "esp32-edge-1.0.0",
                "firmwareVersion": "esp32-edge-1.0.0",
                "certificate": certificate,
                "certificateId": certificate["id"],
                "certificateFingerprint": certificate["fingerprint"],
                "certificateStatus": certificate["status"],
                "certificateIssuedAt": certificate["issuedAt"],
                "certificateExpiresAt": certificate["expiresAt"],
                "lastSeenSecAgo": last_seen_sec_ago,
                "lastReceivedAt": iso_seconds_ago(last_seen_sec_ago),
                "health": health,
                "healthHistory": [],
                "offlineSince": None,
                "lastRecoveredAt": None,
                "missingIntervals": [],
                "rssiDbm": -55 - (index % 20),
                "rebootCount": index % 3,
                "bufferUsagePct": float((index * 7) % 65),
                "hardwareProfileId": "HW-ESP32S3-ADXL345-INMP441-WIFI",
                "hardwareProfileHistory": [],
                "mappingStatus": "active",
                "mappingHistory": [
                    {"assetId": asset["id"], "mappedAt": "initial", "status": "active"}
                ],
                "replacementHistory": [],
                "certificateHistory": [],
            }
        )
    return devices


def network_profile_type(network_profile_id: str) -> str:
    normalized = str(network_profile_id).strip().upper()
    if normalized in NETWORK_PROFILE_IDS:
        return normalized
    profile_type = NETWORK_PROFILE_TYPES.get(normalized)
    if not profile_type:
        raise ApiError(
            404, "NETWORK_PROFILE_NOT_FOUND", "Network profile was not found."
        )
    return profile_type


def canonical_network_profile_id(network_profile_id: str) -> str:
    return NETWORK_PROFILE_IDS[network_profile_type(network_profile_id)]


def rollout_configuration_for_profile(network_profile_id: str) -> dict[str, Any]:
    configurations = {
        "A": {"configurationType": "direct", "gatewayRequired": False},
        "B": {"configurationType": "gateway", "gatewayRequired": True},
        "C": {"configurationType": "store-and-forward", "gatewayRequired": True},
        "D": {"configurationType": "offline", "gatewayRequired": False},
    }
    configuration = configurations[network_profile_type(network_profile_id)]
    return copy_payload(configuration)


def network_delivery_flags_for_profile(network_profile_id: str) -> dict[str, bool]:
    flags = {
        "A": {"directSend": True, "gateway": False, "offlineSync": False},
        "B": {"directSend": False, "gateway": True, "offlineSync": False},
        "C": {"directSend": False, "gateway": True, "offlineSync": True},
        "D": {"directSend": False, "gateway": False, "offlineSync": True},
    }
    profile_flags = flags[network_profile_type(network_profile_id)]
    return copy_payload(profile_flags)


def build_rollout_plan_records(
    sites: list[dict[str, Any]], assets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records = []
    for site in sites:
        target_asset_ids = [
            asset["id"] for asset in assets if asset["siteId"] == site["id"]
        ]
        configuration = rollout_configuration_for_profile(site["networkType"])
        records.append(
            {
                "siteId": site["id"],
                "networkProfileId": canonical_network_profile_id(site["networkType"]),
                "targetAssetIds": target_asset_ids,
                "installPriority": site["priority"],
                **configuration,
                "note": "Initial 65-site rollout plan",
                "updatedAt": "2026-08-11T00:00:00Z",
            }
        )
    return records


def build_site_network_profile_records(
    sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "siteId": site["id"],
            "networkProfileId": canonical_network_profile_id(site["networkType"]),
            "grade": site["networkType"],
            **network_delivery_flags_for_profile(site["networkType"]),
            "reason": "Initial site survey",
            "lastSurveyedAt": "2026-08-11",
            "updatedAt": "2026-08-11T00:00:00Z",
        }
        for site in sites
    ]


def build_install_point_records(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"IP-{asset['id']}",
            "siteId": asset["siteId"],
            "assetId": asset["id"],
            "position": "bearing housing top",
            "orientation": "horizontal X + vertical Z",
            "mountingMethod": "bolt fixed bracket",
            "acousticDirection": "1.5m from cooling side",
            "ambientNoiseSources": ["nearby rotating equipment"],
            "photoRefs": [f"survey://{asset['id']}/install-point.jpg"],
            "active": True,
            "createdAt": "2026-08-11T00:00:00Z",
            "updatedAt": "2026-08-11T00:00:00Z",
            "changeHistory": [],
        }
        for asset in assets
    ]


def build_events() -> list[dict[str, Any]]:
    return [
        {
            "id": "EV-241",
            "siteId": "SITE-01",
            "assetId": "SITE-01-MOT-02",
            "severity": "critical",
            "eventType": "asset_anomaly_candidate",
            "title": "Cooling pump motor bearing suspect",
            "occurredAt": iso_seconds_ago(18 * 60),
            "time": "19:42:12",
            "duration": "48s",
            "durationSec": 48,
            "score": 92,
            "maxScore": 92,
            "label": "needs_review",
            "note": "Vibration and acoustic features rose together in the same time window.",
            "reviewed": False,
            "status": "open",
            "thresholdVersion": "RULE-SITE-01-MOT-02-v1",
            "modelVersion": "ai1_week2_vibration_rms_baseline_v1",
        },
        {
            "id": "EV-238",
            "siteId": "SITE-02",
            "assetId": "SITE-02-GEN-01",
            "severity": "warning",
            "eventType": "asset_anomaly_candidate",
            "title": "Generator acoustic spectrum drift",
            "occurredAt": iso_seconds_ago(29 * 60),
            "time": "19:31:05",
            "duration": "31s",
            "durationSec": 31,
            "score": 78,
            "maxScore": 78,
            "label": "needs_review",
            "note": "High-frequency acoustic band increased compared with the normal baseline.",
            "reviewed": False,
            "status": "open",
            "thresholdVersion": "RULE-SITE-02-GEN-01-v1",
            "modelVersion": "ai1_week2_vibration_rms_baseline_v1",
        },
        {
            "id": "EV-233",
            "siteId": "SITE-05",
            "assetId": "SITE-05-FAN-03",
            "severity": "device",
            "eventType": "sensor_fault_candidate",
            "title": "Acoustic sensor noise floor fault",
            "occurredAt": iso_seconds_ago(40 * 60),
            "time": "19:20:44",
            "duration": "8m",
            "durationSec": 480,
            "score": 64,
            "maxScore": 64,
            "label": "sensor_issue",
            "note": "Classified as sensor state event rather than equipment abnormality.",
            "reviewed": True,
            "status": "open",
            "thresholdVersion": "RULE-SITE-05-FAN-03-v1",
            "modelVersion": "sensor-fault-rules-v1",
        },
    ]


def build_anomaly_rules(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "assetId": asset["id"],
            "siteId": asset["siteId"],
            "version": f"RULE-{asset['id']}-v1",
            "scoreThreshold": float(PARAMETERS["ANOMALY_SCORE_THRESHOLD"]),
            "durationSec": int(PARAMETERS["ANOMALY_HOLD_SEC"]),
            "hysteresis": float(PARAMETERS["ANOMALY_HYSTERESIS"]),
            "mergeWindowSec": 30,
            "active": bool(PARAMETERS["ANOMALY_ENABLED"]),
            "reason": "Initial week 3 anomaly rule",
            "updatedAt": "2026-08-24T00:00:00Z",
            "history": [],
        }
        for asset in assets
    ]


def build_event_device_snapshots(
    events: list[dict[str, Any]], devices: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    snapshots = {}
    for event in events:
        event_device_id = str(event.get("deviceId") or "").strip().upper()
        device = next(
            (
                item
                for item in devices
                if (event_device_id and item["id"] == event_device_id)
                or (
                    not event_device_id
                    and item["assetId"] == event["assetId"]
                    and item.get("mappingStatus") == "active"
                )
            ),
            None,
        )
        snapshots[event["id"]] = {
            "deviceSnapshot": copy_payload(device) if device else None
        }
    return snapshots


BASE_SITES = build_sites()
BASE_ASSETS = build_assets(BASE_SITES)
BASE_DEVICES = build_devices(BASE_ASSETS)
BASE_EVENTS = build_events()
BASE_EVENT_EVIDENCE_SNAPSHOTS = build_event_device_snapshots(BASE_EVENTS, BASE_DEVICES)
BASE_ROLLOUT_PLANS = build_rollout_plan_records(BASE_SITES, BASE_ASSETS)
BASE_SITE_NETWORK_PROFILES = build_site_network_profile_records(BASE_SITES)
BASE_INSTALL_POINTS = build_install_point_records(BASE_ASSETS)
BASE_ANOMALY_RULES = build_anomaly_rules(BASE_ASSETS)
BASE_PARAMETERS = copy_payload(PARAMETERS)

SITES = copy_payload(BASE_SITES)
ASSETS = copy_payload(BASE_ASSETS)
DEVICES = copy_payload(BASE_DEVICES)
EVENTS = copy_payload(BASE_EVENTS)
ROLLOUT_PLAN_RECORDS = copy_payload(BASE_ROLLOUT_PLANS)
SITE_NETWORK_PROFILE_RECORDS = copy_payload(BASE_SITE_NETWORK_PROFILES)
INSTALL_POINTS = copy_payload(BASE_INSTALL_POINTS)
ANOMALY_RULES = copy_payload(BASE_ANOMALY_RULES)
ALERT_POLICIES = copy_payload(BASE_ALERT_POLICIES)
QUARANTINED_DEVICE_MESSAGES: list[dict[str, Any]] = []
MQTT_QUARANTINE_IDEMPOTENCY: dict[str, dict[str, Any]] = {}
TELEMETRY_RECORDS: list[dict[str, Any]] = []
DEMO_TELEMETRY_RECORDS: list[dict[str, Any]] = []
DEMO_TELEMETRY_MAX_RECORDS_PER_ASSET = 1000
TELEMETRY_IDEMPOTENCY: dict[tuple[str, int], dict[str, str]] = {}
TELEMETRY_METRICS: dict[str, int | float | str | None] = {
    "requests": 0,
    "accepted": 0,
    "duplicates": 0,
    "rejected": 0,
    "localRejected": 0,
    "conflicts": 0,
    "lastReceivedAt": None,
    "lastLatencyMs": 0.0,
}
CONNECTIVITY_TEST_RECORDS: list[dict[str, Any]] = []
SERVICE_DEPENDENCIES = copy_payload(BASE_SERVICE_DEPENDENCIES)
SERVICE_HEALTH_EVENTS: list[dict[str, Any]] = []
EVENT_REVIEW_HISTORY: list[dict[str, Any]] = []
EVENT_EVIDENCE_SNAPSHOTS: dict[str, dict[str, Any]] = copy_payload(
    BASE_EVENT_EVIDENCE_SNAPSHOTS
)
AUDIT_LOGS: list[dict[str, Any]] = []
ENVIRONMENT_INSPECTIONS: list[dict[str, Any]] = []
DATASET_VERSIONS: list[dict[str, Any]] = []
MODEL_VERSIONS: list[dict[str, Any]] = []
BASELINE_VERSIONS: list[dict[str, Any]] = []
DATASET_SNAPSHOTS: dict[str, dict[str, list[dict[str, Any]]]] = {}
EVENT_NOTES: list[dict[str, Any]] = []
EVENT_NOTE_HISTORY: list[dict[str, Any]] = []
LIFECYCLE_CHECKPOINTS: dict[str, dict[str, Any]] = {}
DEVICE_CONFIGS: dict[str, dict[str, Any]] = {}
_LIFECYCLE_RUNTIMES: dict[str, AnomalyEventLifecycle] = {}
_RUNTIME_STATE_STORE: RuntimeStateStore | None = None
ACOUSTIC_TAXONOMY_VERSIONS: list[dict[str, Any]] = [
    {
        "version": "ACOUSTIC-V1",
        "labels": copy_payload(ACOUSTIC_LABEL_TAXONOMY),
        "active": True,
        "reason": "Initial acoustic label taxonomy",
        "updatedAt": "2026-08-24T00:00:00Z",
    }
]


def _runtime_state_payload() -> dict[str, Any]:
    return {
        "sites": copy_payload(SITES),
        "assets": copy_payload(ASSETS),
        "devices": copy_payload(DEVICES),
        "events": copy_payload(EVENTS),
        "rolloutPlans": copy_payload(ROLLOUT_PLAN_RECORDS),
        "siteNetworkProfiles": copy_payload(SITE_NETWORK_PROFILE_RECORDS),
        "installPoints": copy_payload(INSTALL_POINTS),
        "anomalyRules": copy_payload(ANOMALY_RULES),
        "alertPolicies": copy_payload(ALERT_POLICIES),
        "parameters": copy_payload(PARAMETERS),
        "quarantinedMessages": copy_payload(QUARANTINED_DEVICE_MESSAGES),
        "mqttQuarantineIdempotency": copy_payload(MQTT_QUARANTINE_IDEMPOTENCY),
        "telemetryRecords": copy_payload(TELEMETRY_RECORDS),
        "telemetryIdempotency": [
            {
                "deviceId": device_id,
                "sequence": sequence,
                **copy_payload(value),
            }
            for (device_id, sequence), value in TELEMETRY_IDEMPOTENCY.items()
        ],
        "telemetryMetrics": copy_payload(TELEMETRY_METRICS),
        "connectivityTests": copy_payload(CONNECTIVITY_TEST_RECORDS),
        "serviceDependencies": copy_payload(SERVICE_DEPENDENCIES),
        "serviceHealthEvents": copy_payload(SERVICE_HEALTH_EVENTS),
        "eventReviewHistory": copy_payload(EVENT_REVIEW_HISTORY),
        "eventEvidenceSnapshots": copy_payload(EVENT_EVIDENCE_SNAPSHOTS),
        "eventNotes": copy_payload(EVENT_NOTES),
        "eventNoteHistory": copy_payload(EVENT_NOTE_HISTORY),
        "auditLogs": copy_payload(AUDIT_LOGS),
        "environmentInspections": copy_payload(ENVIRONMENT_INSPECTIONS),
        "datasetVersions": copy_payload(DATASET_VERSIONS),
        "modelVersions": copy_payload(MODEL_VERSIONS),
        "baselineVersions": copy_payload(BASELINE_VERSIONS),
        "datasetSnapshots": copy_payload(DATASET_SNAPSHOTS),
        "acousticTaxonomies": copy_payload(ACOUSTIC_TAXONOMY_VERSIONS),
        "lifecycleCheckpoints": copy_payload(LIFECYCLE_CHECKPOINTS),
        "deviceConfigs": copy_payload(DEVICE_CONFIGS),
    }


def _restore_runtime_state(payload: dict[str, Any]) -> None:
    list_fields = {
        "sites": SITES,
        "assets": ASSETS,
        "devices": DEVICES,
        "events": EVENTS,
        "rolloutPlans": ROLLOUT_PLAN_RECORDS,
        "siteNetworkProfiles": SITE_NETWORK_PROFILE_RECORDS,
        "installPoints": INSTALL_POINTS,
        "anomalyRules": ANOMALY_RULES,
        "alertPolicies": ALERT_POLICIES,
        "quarantinedMessages": QUARANTINED_DEVICE_MESSAGES,
        "telemetryRecords": TELEMETRY_RECORDS,
        "connectivityTests": CONNECTIVITY_TEST_RECORDS,
        "serviceDependencies": SERVICE_DEPENDENCIES,
        "serviceHealthEvents": SERVICE_HEALTH_EVENTS,
        "eventReviewHistory": EVENT_REVIEW_HISTORY,
        "eventNotes": EVENT_NOTES,
        "eventNoteHistory": EVENT_NOTE_HISTORY,
        "auditLogs": AUDIT_LOGS,
        "environmentInspections": ENVIRONMENT_INSPECTIONS,
        "datasetVersions": DATASET_VERSIONS,
        "modelVersions": MODEL_VERSIONS,
        "baselineVersions": BASELINE_VERSIONS,
        "acousticTaxonomies": ACOUSTIC_TAXONOMY_VERSIONS,
    }
    for key, target in list_fields.items():
        if key in payload:
            if not isinstance(payload[key], list):
                raise ValueError(f"Persisted {key} must be a list.")
            target[:] = copy_payload(payload[key])
    # Older databases predate this optional configuration channel.
    DEVICE_CONFIGS.clear()
    dict_fields = {
        "parameters": PARAMETERS,
        "mqttQuarantineIdempotency": MQTT_QUARANTINE_IDEMPOTENCY,
        "telemetryMetrics": TELEMETRY_METRICS,
        "eventEvidenceSnapshots": EVENT_EVIDENCE_SNAPSHOTS,
        "datasetSnapshots": DATASET_SNAPSHOTS,
        "lifecycleCheckpoints": LIFECYCLE_CHECKPOINTS,
        "deviceConfigs": DEVICE_CONFIGS,
    }
    for key, target in dict_fields.items():
        if key in payload:
            if not isinstance(payload[key], dict):
                raise ValueError(f"Persisted {key} must be an object.")
            target.clear()
            target.update(copy_payload(payload[key]))
    TELEMETRY_IDEMPOTENCY.clear()
    for item in payload.get("telemetryIdempotency", []):
        if not isinstance(item, dict):
            raise ValueError("Persisted telemetry idempotency entry is invalid.")
        device_id = str(item.get("deviceId") or "").strip().upper()
        sequence = item.get("sequence")
        if not device_id or isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("Persisted telemetry idempotency key is invalid.")
        TELEMETRY_IDEMPOTENCY[(device_id, sequence)] = {
            key: copy_payload(value)
            for key, value in item.items()
            if key not in {"deviceId", "sequence"}
        }
    _LIFECYCLE_RUNTIMES.clear()


def _enforce_runtime_retention() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=int(PARAMETERS["RETENTION_RAW_DAYS"])
    )
    retained = []
    for record in TELEMETRY_RECORDS:
        try:
            if parse_rfc3339("timestamp", record.get("timestamp")) >= cutoff:
                retained.append(record)
        except ApiError:
            continue
    removed = len(TELEMETRY_RECORDS) - len(retained)
    if removed:
        TELEMETRY_RECORDS[:] = retained
        retained_keys = {
            (str(item["deviceId"]), int(item["sequence"])) for item in retained
        }
        for key in list(TELEMETRY_IDEMPOTENCY):
            if key not in retained_keys:
                TELEMETRY_IDEMPOTENCY.pop(key, None)
    return removed


def maintain_runtime_retention() -> int:
    """Expire raw telemetry even if there have been no application writes.

    The outer state transaction commits only actual deletions and restores
    both records and idempotency keys if persistence fails.
    """
    with STORE_LOCK:
        return _enforce_runtime_retention()


def _persist_runtime_state() -> None:
    if _RUNTIME_STATE_STORE is None:
        return
    started = time.monotonic()
    _enforce_runtime_retention()
    proposed = _runtime_state_payload()
    dependency = next(
        (item for item in proposed["serviceDependencies"] if item["id"] == "storage"),
        None,
    )
    previous_status = str(dependency.get("status") or "ready") if dependency else ""
    checked_at = now_iso()
    if dependency is not None:
        dependency["statusReportCount"] = (
            int(dependency.get("statusReportCount", 0)) + 1
        )
        dependency["status"] = "healthy"
        dependency["lastCheckedAt"] = checked_at
        dependency["detail"] = None
        if previous_status not in {"healthy", "ready"}:
            dependency["lastRecoveryAt"] = checked_at
            proposed["serviceHealthEvents"].append(
                {
                    "dependencyId": "storage",
                    "status": "recovered",
                    "occurredAt": checked_at,
                    "impactScope": dependency["impactScope"],
                    "detail": "Runtime state checkpoint recovered.",
                }
            )
    if dependency is not None:
        dependency["errorRatePct"] = round(
            int(dependency.get("failureCount", 0))
            / int(dependency["statusReportCount"])
            * 100,
            2,
        )
    del proposed["serviceHealthEvents"][:-100]
    # Recovery belongs to this proposed checkpoint. Publish it only after the
    # SQLite transaction commits; a failed attempt never reports recovery.
    _RUNTIME_STATE_STORE.save(proposed, checked_at)
    if dependency is not None:
        dependency["latencyMs"] = round((time.monotonic() - started) * 1000, 2)
        current = next(item for item in SERVICE_DEPENDENCIES if item["id"] == "storage")
        current.update(dependency)
    SERVICE_HEALTH_EVENTS[:] = proposed["serviceHealthEvents"]


def _runtime_storage_failed(error: Exception) -> None:
    """Record the failed attempt after the rejected transaction is restored."""
    dependency = next(
        (item for item in SERVICE_DEPENDENCIES if item["id"] == "storage"), None
    )
    if dependency is None:
        return
    dependency["status"] = "degraded"
    dependency["statusReportCount"] = int(dependency.get("statusReportCount", 0)) + 1
    dependency["failureCount"] = int(dependency.get("failureCount", 0)) + 1
    dependency["lastFailureAt"] = now_iso()
    dependency["detail"] = "Runtime state checkpoint failed."
    dependency["errorRatePct"] = round(
        dependency["failureCount"] / dependency["statusReportCount"] * 100, 2
    )
    SERVICE_HEALTH_EVENTS.append(
        {
            "dependencyId": "storage",
            "status": "failure",
            "occurredAt": dependency["lastFailureAt"],
            "impactScope": dependency["impactScope"],
            "errorCode": type(error).__name__,
            "detail": dependency["detail"],
        }
    )
    del SERVICE_HEALTH_EVENTS[:-100]


def configure_runtime_state(database: str | None) -> None:
    """Attach or detach the durable general-state database.

    Passing ``None`` preserves the isolated in-memory behavior used by unit tests.
    """

    global _RUNTIME_STATE_STORE
    STORE_LOCK.set_commit_hook(None)
    STORE_LOCK.set_transaction_hooks()
    if _RUNTIME_STATE_STORE is not None:
        _RUNTIME_STATE_STORE.close()
        _RUNTIME_STATE_STORE = None
    if not database:
        return
    store = RuntimeStateStore(database)
    with STORE_LOCK:
        persisted = store.load()
        if persisted is not None:
            _restore_runtime_state(persisted)
        storage_dependency = next(
            (item for item in SERVICE_DEPENDENCIES if item["id"] == "storage"), None
        )
        if storage_dependency is not None:
            storage_dependency["name"] = "SQLite runtime state storage"
            storage_dependency["impactScope"] = (
                "Master data, telemetry, events, reviews and dataset metadata"
            )
        _RUNTIME_STATE_STORE = store
        if persisted is None:
            _persist_runtime_state()
    STORE_LOCK.set_commit_hook(_persist_runtime_state)
    STORE_LOCK.set_transaction_hooks(
        _runtime_state_payload, _restore_runtime_state, _runtime_storage_failed
    )
    try:
        # Finish expiration before create_server can accept its first request.
        maintain_runtime_retention()
    except Exception:
        close_runtime_state()
        raise


def close_runtime_state() -> None:
    global _RUNTIME_STATE_STORE
    with STORE_LOCK:
        STORE_LOCK.set_commit_hook(None)
        STORE_LOCK.set_transaction_hooks()
        if _RUNTIME_STATE_STORE is not None:
            _RUNTIME_STATE_STORE.close()
            _RUNTIME_STATE_STORE = None


def reset_runtime_state() -> None:
    with STORE_LOCK:
        SITES[:] = copy_payload(BASE_SITES)
        ASSETS[:] = copy_payload(BASE_ASSETS)
        DEVICES[:] = copy_payload(BASE_DEVICES)
        EVENTS[:] = copy_payload(BASE_EVENTS)
        ROLLOUT_PLAN_RECORDS[:] = copy_payload(BASE_ROLLOUT_PLANS)
        SITE_NETWORK_PROFILE_RECORDS[:] = copy_payload(BASE_SITE_NETWORK_PROFILES)
        INSTALL_POINTS[:] = copy_payload(BASE_INSTALL_POINTS)
        ANOMALY_RULES[:] = copy_payload(BASE_ANOMALY_RULES)
        ALERT_POLICIES[:] = copy_payload(BASE_ALERT_POLICIES)
        PARAMETERS.clear()
        PARAMETERS.update(copy_payload(BASE_PARAMETERS))
        QUARANTINED_DEVICE_MESSAGES.clear()
        MQTT_QUARANTINE_IDEMPOTENCY.clear()
        TELEMETRY_RECORDS.clear()
        DEMO_TELEMETRY_RECORDS.clear()
        TELEMETRY_IDEMPOTENCY.clear()
        TELEMETRY_METRICS.update(
            {
                "requests": 0,
                "accepted": 0,
                "duplicates": 0,
                "rejected": 0,
                "localRejected": 0,
                "conflicts": 0,
                "lastReceivedAt": None,
                "lastLatencyMs": 0.0,
            }
        )
        CONNECTIVITY_TEST_RECORDS.clear()
        SERVICE_DEPENDENCIES[:] = copy_payload(BASE_SERVICE_DEPENDENCIES)
        SERVICE_HEALTH_EVENTS.clear()
        EVENT_REVIEW_HISTORY.clear()
        EVENT_NOTES.clear()
        EVENT_NOTE_HISTORY.clear()
        EVENT_EVIDENCE_SNAPSHOTS.clear()
        EVENT_EVIDENCE_SNAPSHOTS.update(copy_payload(BASE_EVENT_EVIDENCE_SNAPSHOTS))
        AUDIT_LOGS.clear()
        ENVIRONMENT_INSPECTIONS.clear()
        DATASET_VERSIONS.clear()
        MODEL_VERSIONS.clear()
        BASELINE_VERSIONS.clear()
        DATASET_SNAPSHOTS.clear()
        LIFECYCLE_CHECKPOINTS.clear()
        DEVICE_CONFIGS.clear()
        _LIFECYCLE_RUNTIMES.clear()
        ACOUSTIC_TAXONOMY_VERSIONS[:] = [
            {
                "version": "ACOUSTIC-V1",
                "labels": copy_payload(ACOUSTIC_LABEL_TAXONOMY),
                "active": True,
                "reason": "Initial acoustic label taxonomy",
                "updatedAt": "2026-08-24T00:00:00Z",
            }
        ]
        SESSIONS.clear()
        for username in LOGIN_STATE:
            LOGIN_STATE[username] = {"failures": 0, "lockedUntil": 0.0}


def authenticate(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    user = next((item for item in USERS if item["username"] == username), None)
    if not user:
        raise ApiError(400, "INVALID_CREDENTIALS", "Check the username or password.")

    with STORE_LOCK:
        state = LOGIN_STATE[username]
        if float(state["lockedUntil"]) > time.time():
            raise ApiError(
                423,
                "ACCOUNT_LOCKED",
                "The account is locked after repeated login failures.",
            )
        if not verify_password(password, user):
            state["failures"] = int(state["failures"]) + 1
            if int(state["failures"]) >= MAX_LOGIN_FAILURES:
                state["lockedUntil"] = time.time() + LOCK_SECONDS
                raise ApiError(
                    423,
                    "ACCOUNT_LOCKED",
                    "The account is locked after repeated login failures.",
                )
            raise ApiError(
                400, "INVALID_CREDENTIALS", "Check the username or password."
            )

        state["failures"] = 0
        state["lockedUntil"] = 0.0
        token = "demo-" + secrets.token_urlsafe(24)
        SESSIONS[token] = {
            "userId": user["id"],
            "username": username,
            "expiresAt": time.time() + SESSION_SECONDS,
        }

    session = {"token": token, "issuedAt": now_text(), "expiresInSec": SESSION_SECONDS}
    return {
        "user": public_user(user),
        "session": session,
        "rolePolicy": role_policy(user["role"]),
    }


def current_user_for_token(token: str) -> dict[str, Any]:
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "A bearer session token is required.")
    with STORE_LOCK:
        session = SESSIONS.get(token)
        if not session or float(session["expiresAt"]) <= time.time():
            SESSIONS.pop(token, None)
            raise ApiError(401, "INVALID_SESSION", "The session is expired or invalid.")
        user = next((item for item in USERS if item["id"] == session["userId"]), None)
        if not user:
            raise ApiError(401, "INVALID_SESSION", "The session is expired or invalid.")
        return public_user(user)


def logout(token: str) -> dict[str, Any]:
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "A bearer session token is required.")
    with STORE_LOCK:
        if token not in SESSIONS:
            raise ApiError(401, "INVALID_SESSION", "The session is expired or invalid.")
        SESSIONS.pop(token, None)
    return {"loggedOut": True}


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "allowedSiteIds": list(user["allowedSiteIds"]),
    }


def role_policy(role: str) -> dict[str, Any]:
    policy = next((item for item in ROLE_POLICIES if item["role"] == role), None)
    if not policy:
        raise ApiError(404, "ROLE_NOT_FOUND", "Role policy was not found.")
    return copy_payload(policy)


def require_permission(user: dict[str, Any], permission: str) -> None:
    if not has_permission(user, permission):
        raise ApiError(
            403, "FORBIDDEN", "The user does not have permission for this API."
        )


def has_permission(user: dict[str, Any], permission: str) -> bool:
    permissions = set(role_policy(user["role"])["permissions"])
    return "*" in permissions or permission in permissions


def require_site_access(user: dict[str, Any], site_id: str) -> None:
    allowed = user.get("allowedSiteIds", [])
    if "*" not in allowed and site_id not in allowed:
        raise ApiError(403, "SITE_FORBIDDEN", "The user cannot access this site.")


def visible_sites_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    require_permission(user, "site:read")
    if "*" in user["allowedSiteIds"]:
        return copy_payload(SITES)
    return copy_payload(
        [site for site in SITES if site["id"] in user["allowedSiteIds"]]
    )


def get_site(site_id: str) -> dict[str, Any]:
    site = next((item for item in SITES if item["id"] == site_id), None)
    if not site:
        raise ApiError(404, "SITE_NOT_FOUND", "Site was not found.")
    return site


def get_asset(site_id: str, asset_id: str) -> dict[str, Any]:
    asset = next(
        (
            item
            for item in ASSETS
            if item["siteId"] == site_id and item["id"] == asset_id
        ),
        None,
    )
    if not asset:
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset was not found.")
    return asset


def get_asset_by_id(asset_id: str) -> dict[str, Any]:
    asset = next((item for item in ASSETS if item["id"] == asset_id), None)
    if not asset:
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset was not found.")
    return asset


def get_device(device_id: str) -> dict[str, Any]:
    device = next((item for item in DEVICES if item["id"] == device_id), None)
    if not device:
        raise ApiError(404, "DEVICE_NOT_FOUND", "Device was not found.")
    return device


def network_profile(profile_id_or_type: str) -> dict[str, Any]:
    profile_type = network_profile_type(profile_id_or_type)
    return next(item for item in NETWORK_PROFILES if item["type"] == profile_type)


def assets_for(site_id: str) -> list[dict[str, Any]]:
    get_site(site_id)
    return copy_payload([item for item in ASSETS if item["siteId"] == site_id])


def devices_for(site_id: str) -> list[dict[str, Any]]:
    get_site(site_id)
    return copy_payload([item for item in DEVICES if item["siteId"] == site_id])


def rollout_plans() -> list[dict[str, Any]]:
    return [rollout_plan_for(record["siteId"]) for record in ROLLOUT_PLAN_RECORDS]


def rollout_plan_for(site_id: str) -> dict[str, Any]:
    site = get_site(site_id)
    record = next(
        (item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None
    )
    if not record:
        raise ApiError(
            404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found."
        )
    profile = network_profile(record["networkProfileId"])
    response = copy_payload(record)
    response.update(
        {
            "siteName": site["name"],
            "region": site["region"],
            "stage": site["rolloutStage"],
            "networkName": profile["name"],
            "targetAssetCount": len(record["targetAssetIds"]),
            "recommendedArchitecture": profile["architecture"],
            "installationReady": bool(
                record["networkProfileId"] and record["targetAssetIds"]
            ),
        }
    )
    return response


def update_rollout_plan(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "rollout:write")
    require_site_access(user, site_id)
    network_profile_id = canonical_network_profile_id(
        required_text(payload, "networkProfileId")
    )
    profile_type = network_profile_type(network_profile_id)
    configuration = rollout_configuration_for_profile(network_profile_id)
    target_asset_ids = string_list(
        payload, "targetAssetIds", required=True, uppercase=True
    )

    with STORE_LOCK:
        site = get_site(site_id)
        site_asset_ids = {asset["id"] for asset in ASSETS if asset["siteId"] == site_id}
        invalid_asset_ids = [
            asset_id for asset_id in target_asset_ids if asset_id not in site_asset_ids
        ]
        if invalid_asset_ids:
            raise ApiError(
                400,
                "ROLLOUT_ASSET_INVALID",
                "All rollout target assets must belong to the site.",
            )
        record = next(
            (item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None
        )
        if not record:
            raise ApiError(
                404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found."
            )
        candidate = copy_payload(record)
        candidate.update(
            {
                "networkProfileId": network_profile_id,
                "targetAssetIds": target_asset_ids,
                "installPriority": required_text(payload, "installPriority"),
                **configuration,
                "note": str(payload.get("note") or ""),
                "updatedAt": now_iso(),
            }
        )
        record.update(candidate)
        site["network"] = profile_type
        site["networkType"] = profile_type
        site["priority"] = candidate["installPriority"]
        site["targetAssetCount"] = len(target_asset_ids)
        sync_site_network_profile(site_id, network_profile_id)
        return rollout_plan_for(site_id)


def network_profiles_for_sites() -> list[dict[str, Any]]:
    return [
        site_network_profile(record["siteId"])
        for record in SITE_NETWORK_PROFILE_RECORDS
    ]


def site_network_profile(site_id: str) -> dict[str, Any]:
    site = get_site(site_id)
    record = next(
        (item for item in SITE_NETWORK_PROFILE_RECORDS if item["siteId"] == site_id),
        None,
    )
    if not record:
        raise ApiError(
            404,
            "SITE_NETWORK_PROFILE_NOT_FOUND",
            "The site network profile was not found.",
        )
    response = copy_payload(record)
    response.update(
        {
            "siteName": site["name"],
            "networkType": record["grade"],
            "profile": network_profile(record["networkProfileId"]),
            "signalQuality": site["signalQuality"],
        }
    )
    return response


def sync_site_network_profile(site_id: str, network_profile_id: str) -> None:
    profile_type = network_profile_type(network_profile_id)
    canonical_profile_id = canonical_network_profile_id(network_profile_id)
    record = next(
        (item for item in SITE_NETWORK_PROFILE_RECORDS if item["siteId"] == site_id),
        None,
    )
    if not record:
        raise ApiError(
            404,
            "SITE_NETWORK_PROFILE_NOT_FOUND",
            "The site network profile was not found.",
        )
    record.update(
        {
            "networkProfileId": canonical_profile_id,
            "grade": profile_type,
            **network_delivery_flags_for_profile(profile_type),
            "updatedAt": now_iso(),
        }
    )
    sync_rollout_network_configuration(site_id, canonical_profile_id)


def sync_rollout_network_configuration(site_id: str, network_profile_id: str) -> None:
    canonical_profile_id = canonical_network_profile_id(network_profile_id)
    rollout = next(
        (item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None
    )
    if not rollout:
        raise ApiError(
            404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found."
        )
    rollout.update(
        {
            "networkProfileId": canonical_profile_id,
            **rollout_configuration_for_profile(canonical_profile_id),
            "updatedAt": now_iso(),
        }
    )


def update_site_network_profile(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "network-profile:write")
    require_site_access(user, site_id)
    network_profile_id = canonical_network_profile_id(
        required_text(payload, "networkProfileId")
    )
    profile_type = network_profile_type(network_profile_id)
    reason = required_text(payload, "reason")

    with STORE_LOCK:
        site = get_site(site_id)
        record = next(
            (
                item
                for item in SITE_NETWORK_PROFILE_RECORDS
                if item["siteId"] == site_id
            ),
            None,
        )
        if not record:
            raise ApiError(
                404,
                "SITE_NETWORK_PROFILE_NOT_FOUND",
                "The site network profile was not found.",
            )
        candidate = copy_payload(record)
        candidate.update(
            {
                "networkProfileId": network_profile_id,
                "grade": profile_type,
                **network_delivery_flags_for_profile(network_profile_id),
                "reason": reason,
                "updatedAt": now_iso(),
            }
        )
        record.update(candidate)
        site["network"] = profile_type
        site["networkType"] = profile_type
        sync_rollout_network_configuration(site_id, network_profile_id)
        return site_network_profile(site_id)


def install_points_for(site_id: str) -> list[dict[str, Any]]:
    get_site(site_id)
    return copy_payload([item for item in INSTALL_POINTS if item["siteId"] == site_id])


def install_points_for_asset(
    asset_id: str, active: bool | None = None
) -> list[dict[str, Any]]:
    get_asset_by_id(asset_id)
    rows = [item for item in INSTALL_POINTS if item["assetId"] == asset_id]
    if active is not None:
        rows = [item for item in rows if bool(item["active"]) is active]
    return copy_payload(rows)


def create_install_point(
    user: dict[str, Any], asset_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "install-point:write")
    asset = get_asset_by_id(asset_id)
    require_site_access(user, asset["siteId"])
    ambient_noise_sources = string_list(payload, "ambientNoiseSources")
    photo_refs = string_list(payload, "photoRefs", required=True)

    with STORE_LOCK:
        install_point_id = (
            str(
                payload.get("id")
                or f"IP-{asset_id}-{len(install_points_for_asset(asset_id)) + 1:02d}"
            )
            .strip()
            .upper()
        )
        if any(item["id"] == install_point_id for item in INSTALL_POINTS):
            raise ApiError(
                400, "INSTALL_POINT_DUPLICATED", "Install point ID is duplicated."
            )
        timestamp = now_iso()
        install_point = {
            "id": install_point_id,
            "siteId": asset["siteId"],
            "assetId": asset_id,
            "position": required_text(payload, "position"),
            "orientation": required_text(payload, "orientation"),
            "mountingMethod": required_text(payload, "mountingMethod"),
            "acousticDirection": str(payload.get("acousticDirection") or ""),
            "ambientNoiseSources": ambient_noise_sources,
            "photoRefs": photo_refs,
            "active": True,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "changeHistory": [],
        }
        INSTALL_POINTS.append(install_point)
        return copy_payload(install_point)


def update_install_point(
    user: dict[str, Any], asset_id: str, install_point_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "install-point:write")
    asset = get_asset_by_id(asset_id)
    require_site_access(user, asset["siteId"])
    reason = required_text(payload, "reason")

    with STORE_LOCK:
        install_point = next(
            (
                item
                for item in INSTALL_POINTS
                if item["assetId"] == asset_id and item["id"] == install_point_id
            ),
            None,
        )
        if not install_point:
            raise ApiError(
                404, "INSTALL_POINT_NOT_FOUND", "Install point was not found."
            )
        candidate = copy_payload(install_point)
        for key in ("position", "orientation", "mountingMethod", "acousticDirection"):
            if key in payload:
                candidate[key] = str(payload[key]).strip()
        for key in ("ambientNoiseSources", "photoRefs"):
            if key in payload:
                candidate[key] = string_list(payload, key, required=key == "photoRefs")
        if "active" in payload:
            candidate["active"] = boolean_field(payload, "active")
        before = install_point_snapshot(install_point)
        after = install_point_snapshot(candidate)
        candidate["updatedAt"] = now_iso()
        if before != after:
            candidate.setdefault("changeHistory", []).append(
                {
                    "reason": reason,
                    "changedAt": candidate["updatedAt"],
                    "before": before,
                    "after": after,
                }
            )
        install_point.update(candidate)
        return copy_payload(install_point)


def parse_int_value(field: str, value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApiError(
            400, "INVALID_NUMBER", f"{field} must be a valid number."
        ) from exc


def parse_float_value(field: str, value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApiError(
            400, "INVALID_NUMBER", f"{field} must be a valid number."
        ) from exc
    if not math.isfinite(parsed):
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be a finite number.")
    return parsed


def parse_int_field(payload: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in payload:
            return parse_int_value(key, payload[key], default)
    return default


def rated_rpm_field(payload: dict[str, Any], *, default: int = 0) -> int:
    field = next((key for key in ("ratedRpm", "rpm") if key in payload), None)
    if field is None:
        return default
    raw_value = payload[field]
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be an integer.")
    if (
        isinstance(raw_value, bool)
        or (
            isinstance(raw_value, float)
            and (not math.isfinite(raw_value) or not raw_value.is_integer())
        )
        or not isinstance(raw_value, (int, float, str))
    ):
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be an integer.")
    value = parse_int_value(field, raw_value, default)
    if not MIN_RATED_RPM <= value <= MAX_RATED_RPM:
        raise ApiError(
            400,
            "VALUE_OUT_OF_RANGE",
            f"ratedRpm must be between {MIN_RATED_RPM} and {MAX_RATED_RPM}.",
        )
    return value


def boolean_field(
    payload: dict[str, Any], key: str, default: bool | None = None
) -> bool:
    if key not in payload:
        if default is None:
            raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
        return default
    value = payload[key]
    if isinstance(value, bool):
        return value
    raise ApiError(400, "INVALID_BOOLEAN", f"{key} must be a boolean.")


def string_list(
    payload: dict[str, Any],
    key: str,
    *,
    required: bool = False,
    uppercase: bool = False,
) -> list[str]:
    value = payload.get(key)
    if value is None:
        if required:
            raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ApiError(
            400, "INVALID_LIST", f"{key} must be a list of non-empty strings."
        )
    result = [item.strip().upper() if uppercase else item.strip() for item in value]
    if required and not result:
        raise ApiError(400, "MISSING_FIELD", f"{key} must contain at least one item.")
    return list(dict.fromkeys(result))


def create_site(user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "site:write")
    site_id = required_text(payload, "id").upper()
    site_code = str(payload.get("code") or site_id).strip().upper()
    network_type = network_profile_type(str(payload.get("networkType") or "D"))
    network_profile_id = canonical_network_profile_id(network_type)

    with STORE_LOCK:
        if any(site["id"] == site_id for site in SITES):
            raise ApiError(400, "SITE_DUPLICATED", "Site ID is duplicated.")
        if any(site["code"] == site_code for site in SITES):
            raise ApiError(400, "SITE_CODE_DUPLICATED", "Site code is duplicated.")
        timestamp = now_iso()
        site = {
            "id": site_id,
            "code": site_code,
            "name": required_text(payload, "name"),
            "region": str(payload.get("region") or "undecided"),
            "location": str(payload.get("location") or payload.get("address") or ""),
            "address": str(payload.get("address") or payload.get("location") or ""),
            "latitude": parse_float_value("latitude", payload.get("latitude"), 0.0),
            "longitude": parse_float_value("longitude", payload.get("longitude"), 0.0),
            "timezone": str(payload.get("timezone") or "Asia/Seoul"),
            "network": network_type,
            "networkType": network_type,
            "priority": str(payload.get("priority") or "normal"),
            "status": str(payload.get("status") or "normal"),
            "operationStatus": str(payload.get("operationStatus") or "active"),
            "totalDevices": 0,
            "onlineDevices": 0,
            "eventCount": 0,
            "assetCount": 0,
            "signalQuality": parse_int_field(payload, "signalQuality", default=70),
            "rolloutStage": str(payload.get("rolloutStage") or "planned"),
            "targetAssetCount": parse_int_field(payload, "targetAssetCount", default=0),
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        SITES.append(site)
        ROLLOUT_PLAN_RECORDS.append(
            {
                "siteId": site_id,
                "networkProfileId": network_profile_id,
                "targetAssetIds": [],
                "installPriority": site["priority"],
                **rollout_configuration_for_profile(network_type),
                "note": "",
                "updatedAt": timestamp,
            }
        )
        SITE_NETWORK_PROFILE_RECORDS.append(
            {
                "siteId": site_id,
                "networkProfileId": network_profile_id,
                "grade": network_type,
                **network_delivery_flags_for_profile(network_type),
                "reason": "Initial registration",
                "lastSurveyedAt": "",
                "updatedAt": timestamp,
            }
        )
        return copy_payload(site)


def update_site(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "site:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        site = get_site(site_id)
        candidate = copy_payload(site)
        if "code" in payload:
            next_code = str(payload["code"]).strip().upper()
            if any(
                item["id"] != site_id and item["code"] == next_code for item in SITES
            ):
                raise ApiError(400, "SITE_CODE_DUPLICATED", "Site code is duplicated.")
            candidate["code"] = next_code
        for key in (
            "name",
            "region",
            "location",
            "address",
            "timezone",
            "priority",
            "status",
            "operationStatus",
            "rolloutStage",
        ):
            if key in payload:
                candidate[key] = str(payload[key])
        for key in ("latitude", "longitude"):
            if key in payload:
                candidate[key] = parse_float_value(key, payload[key])
        if "networkType" in payload:
            network_type = network_profile_type(str(payload["networkType"]))
            candidate["networkType"] = network_type
            candidate["network"] = network_type
        for key in ("signalQuality", "targetAssetCount"):
            if key in payload:
                candidate[key] = parse_int_value(key, payload[key])
        candidate["updatedAt"] = now_iso()
        site.update(candidate)
        if "networkType" in payload:
            sync_site_network_profile(site_id, site["networkType"])
        return copy_payload(site)


def deactivate_site(user: dict[str, Any], site_id: str) -> dict[str, Any]:
    return update_site(
        user, site_id, {"operationStatus": "inactive", "status": "inactive"}
    )


def delete_site(user: dict[str, Any], site_id: str) -> dict[str, Any]:
    require_permission(user, "site:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        get_site(site_id)
        if any(asset["siteId"] == site_id for asset in ASSETS):
            raise ApiError(
                400, "SITE_HAS_ASSETS", "A site with assets cannot be deleted."
            )
        dataset_references = []
        for dataset in DATASET_VERSIONS:
            source_filters = dataset.get("sourceFilters") or {}
            referenced_site_ids = set(source_filters.get("siteIds", []))
            if source_filters.get("siteId"):
                referenced_site_ids.add(source_filters["siteId"])
            snapshot_references_site = any(
                snapshot_key.startswith(f"{site_id}:")
                for snapshot_key in DATASET_SNAPSHOTS.get(dataset["id"], {})
            )
            if site_id in referenced_site_ids or snapshot_references_site:
                dataset_references.append(dataset["id"])
        if dataset_references:
            raise ApiError(
                409,
                "SITE_HAS_IMMUTABLE_REFERENCES",
                "A site referenced by a frozen dataset cannot be deleted.",
            )
        SITES[:] = [site for site in SITES if site["id"] != site_id]
        ROLLOUT_PLAN_RECORDS[:] = [
            record for record in ROLLOUT_PLAN_RECORDS if record["siteId"] != site_id
        ]
        SITE_NETWORK_PROFILE_RECORDS[:] = [
            record
            for record in SITE_NETWORK_PROFILE_RECORDS
            if record["siteId"] != site_id
        ]
        return {"deleted": True, "siteId": site_id}


def create_asset(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "asset:write")
    require_site_access(user, site_id)
    asset_code = required_text(payload, "assetCode").upper()
    asset_id = str(payload.get("id") or f"{site_id}-{asset_code}").strip().upper()

    with STORE_LOCK:
        get_site(site_id)
        if any(asset["id"] == asset_id for asset in ASSETS):
            raise ApiError(400, "ASSET_DUPLICATED", "Asset ID is duplicated.")
        if any(
            asset["siteId"] == site_id and asset["assetCode"] == asset_code
            for asset in ASSETS
        ):
            raise ApiError(
                400, "ASSET_CODE_DUPLICATED", "Asset code must be unique within a site."
            )
        baseline = baseline_payload(payload)
        rated_rpm = rated_rpm_field(payload)
        timestamp = now_iso()
        asset = {
            "id": asset_id,
            "assetCode": asset_code,
            "siteId": site_id,
            "name": required_text(payload, "name"),
            "type": str(payload.get("assetType") or payload.get("type") or "motor"),
            "assetType": str(
                payload.get("assetType") or payload.get("type") or "motor"
            ),
            "ratedRpm": rated_rpm,
            "rpm": rated_rpm,
            "operationStatus": str(payload.get("operationStatus") or "active"),
            "installLocation": str(payload.get("installLocation") or ""),
            "baselineStatus": baseline["status"],
            "baseline": baseline,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        ASSETS.append(asset)
        default_rule = build_anomaly_rules([asset])[0]
        default_rule["reason"] = "Initial anomaly rule for new asset"
        default_rule["updatedAt"] = timestamp
        ANOMALY_RULES.append(default_rule)
        get_site(site_id)["assetCount"] += 1
        return copy_payload(asset)


def update_asset(
    user: dict[str, Any], site_id: str, asset_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "asset:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        asset = get_asset(site_id, asset_id)
        candidate = copy_payload(asset)
        if "assetCode" in payload:
            next_code = str(payload["assetCode"]).strip().upper()
            if any(
                item["id"] != asset_id
                and item["siteId"] == site_id
                and item["assetCode"] == next_code
                for item in ASSETS
            ):
                raise ApiError(
                    400,
                    "ASSET_CODE_DUPLICATED",
                    "Asset code must be unique within a site.",
                )
            candidate["assetCode"] = next_code
        for key in ("name", "operationStatus", "installLocation", "baselineStatus"):
            if key in payload:
                candidate[key] = str(payload[key])
        if "status" in payload:
            candidate["operationStatus"] = str(payload["status"])
        if "assetType" in payload or "type" in payload:
            candidate["assetType"] = str(
                payload.get("assetType") or payload.get("type")
            )
            candidate["type"] = candidate["assetType"]
        if "ratedRpm" in payload or "rpm" in payload:
            candidate["ratedRpm"] = rated_rpm_field(
                payload,
                default=candidate["ratedRpm"],
            )
            candidate["rpm"] = candidate["ratedRpm"]
        if "baseline" in payload or any(key.startswith("baseline") for key in payload):
            candidate["baseline"] = baseline_payload(payload, candidate.get("baseline"))
            candidate["baselineStatus"] = candidate["baseline"]["status"]
        candidate["updatedAt"] = now_iso()
        asset.update(candidate)
        return copy_payload(asset)


def delete_asset(user: dict[str, Any], site_id: str, asset_id: str) -> dict[str, Any]:
    require_permission(user, "asset:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        get_asset(site_id, asset_id)
        snapshot_key = f"{site_id}:{asset_id}"
        dataset_references = []
        for dataset in DATASET_VERSIONS:
            source_filters = dataset.get("sourceFilters") or {}
            filtered_asset_ids = set(source_filters.get("assetIds", []))
            if source_filters.get("assetId"):
                filtered_asset_ids.add(source_filters["assetId"])
            if (
                snapshot_key in DATASET_SNAPSHOTS.get(dataset["id"], {})
                or asset_id in filtered_asset_ids
            ):
                dataset_references.append(dataset["id"])
        event_references = [
            event["id"] for event in EVENTS if event.get("assetId") == asset_id
        ]
        inspection_references = [
            inspection["id"]
            for inspection in ENVIRONMENT_INSPECTIONS
            if inspection.get("assetId") == asset_id
        ]
        telemetry_references = [
            record
            for record in TELEMETRY_RECORDS
            if record.get("siteId") == site_id and record.get("assetId") == asset_id
        ]
        if (
            dataset_references
            or event_references
            or inspection_references
            or telemetry_references
            or any(row["assetId"] == asset_id for row in BASELINE_VERSIONS)
        ):
            raise ApiError(
                409,
                "ASSET_HAS_IMMUTABLE_REFERENCES",
                "An asset referenced by telemetry, a frozen dataset, event, or inspection cannot be deleted.",
            )
        if any(device["assetId"] == asset_id for device in DEVICES):
            raise ApiError(
                400,
                "ASSET_HAS_DEVICE",
                "An asset referenced by a device mapping cannot be deleted.",
            )
        ASSETS[:] = [asset for asset in ASSETS if asset["id"] != asset_id]
        ANOMALY_RULES[:] = [
            rule for rule in ANOMALY_RULES if rule["assetId"] != asset_id
        ]
        INSTALL_POINTS[:] = [
            item for item in INSTALL_POINTS if item["assetId"] != asset_id
        ]
        rollout = next(
            (item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None
        )
        if rollout and asset_id in rollout["targetAssetIds"]:
            rollout["targetAssetIds"] = [
                item for item in rollout["targetAssetIds"] if item != asset_id
            ]
            rollout["updatedAt"] = now_iso()
        site = get_site(site_id)
        site["assetCount"] = max(0, int(site["assetCount"]) - 1)
        return {"deleted": True, "assetId": asset_id}


def validate_asset_device_mapping(site_id: str, asset_id: str) -> dict[str, Any]:
    asset = get_asset(site_id, asset_id)
    baseline = asset.get("baseline") or {}
    missing = []
    if not str(asset.get("name") or "").strip():
        missing.append("name")
    if not str(asset.get("assetType") or "").strip():
        missing.append("assetType")
    if int(asset.get("ratedRpm") or 0) <= 0:
        missing.append("ratedRpm")
    if not str(asset.get("installLocation") or "").strip():
        missing.append("installLocation")
    if str(baseline.get("status") or "") != "ready":
        missing.append("baseline.status")
    if not str(baseline.get("capturedAt") or "").strip():
        missing.append("baseline.capturedAt")
    if float(baseline.get("vibrationRmsMmS") or 0) <= 0:
        missing.append("baseline.vibrationRmsMmS")
    if float(baseline.get("acousticDb") or 0) <= 0:
        missing.append("baseline.acousticDb")
    if int(baseline.get("sampleCount") or 0) <= 0:
        missing.append("baseline.sampleCount")
    if missing:
        raise ApiError(
            409,
            "ASSET_NOT_READY_FOR_MAPPING",
            "Complete the asset master data and ready baseline before device mapping: "
            + ", ".join(missing),
        )

    rollout = next(
        (item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None
    )
    if (
        not rollout
        or not rollout.get("networkProfileId")
        or asset_id not in rollout.get("targetAssetIds", [])
    ):
        raise ApiError(
            409,
            "ROLLOUT_PLAN_NOT_READY",
            "Save the site network profile and include the asset in the rollout plan before device mapping.",
        )
    return asset


def certificate_payload(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    source = dict(existing or {})
    nested = payload.get("certificate")
    if nested is not None and not isinstance(nested, dict):
        raise ApiError(400, "INVALID_CERTIFICATE", "certificate must be an object.")
    if isinstance(nested, dict):
        source.update(nested)
    aliases = {
        "certificateId": "id",
        "certificateFingerprint": "fingerprint",
        "certificateStatus": "status",
        "certificateIssuedAt": "issuedAt",
        "certificateExpiresAt": "expiresAt",
    }
    for payload_key, certificate_key in aliases.items():
        if payload_key in payload:
            source[certificate_key] = payload[payload_key]
    certificate = {
        "id": str(source.get("id") or "").strip(),
        "fingerprint": str(source.get("fingerprint") or "").strip().lower(),
        "status": str(source.get("status") or "registered").strip().lower(),
        "issuedAt": str(source.get("issuedAt") or "").strip(),
        "expiresAt": str(source.get("expiresAt") or "").strip(),
    }
    if certificate["status"] not in DEVICE_CERTIFICATE_STATUSES:
        raise ApiError(
            400, "INVALID_CERTIFICATE_STATUS", "certificateStatus is not allowed."
        )
    if not certificate["id"] or not certificate["fingerprint"]:
        raise ApiError(
            400,
            "DEVICE_CERTIFICATE_REQUIRED",
            "certificateId and certificateFingerprint are required for device registration.",
        )
    return certificate


def validate_certificate_for_mapping(
    certificate: dict[str, Any], mapping_status: str
) -> None:
    if mapping_status != "active":
        return
    if certificate["status"] != "registered":
        raise ApiError(
            409,
            "CERTIFICATE_NOT_ACTIVE",
            "An active device mapping requires a registered certificate.",
        )
    expires_at_text = str(certificate.get("expiresAt") or "").strip()
    if not expires_at_text:
        return
    normalized = (
        expires_at_text[:-1] + "+00:00"
        if expires_at_text.endswith("Z")
        else expires_at_text
    )
    try:
        expires_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(
            400,
            "INVALID_CERTIFICATE_EXPIRY",
            "certificateExpiresAt must be an ISO 8601 timestamp.",
        ) from exc
    if expires_at.tzinfo is None:
        raise ApiError(
            400,
            "INVALID_CERTIFICATE_EXPIRY",
            "certificateExpiresAt must include a timezone.",
        )
    if expires_at <= datetime.now(timezone.utc):
        raise ApiError(
            409,
            "CERTIFICATE_EXPIRED",
            "An active device mapping cannot use an expired certificate.",
        )


def validate_mapping_status(mapping_status: str) -> None:
    if mapping_status not in DEVICE_MAPPING_STATUSES:
        raise ApiError(
            400, "INVALID_MAPPING_STATUS", "mappingStatus must be active or inactive."
        )


def apply_certificate_fields(
    device: dict[str, Any], certificate: dict[str, Any]
) -> None:
    device["certificate"] = copy_payload(certificate)
    device["certificateId"] = certificate["id"]
    device["certificateFingerprint"] = certificate["fingerprint"]
    device["certificateStatus"] = certificate["status"]
    device["certificateIssuedAt"] = certificate["issuedAt"]
    device["certificateExpiresAt"] = certificate["expiresAt"]


def device_replacement_snapshot(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "assetId": device.get("assetId"),
        "sensorChannels": copy_payload(device.get("sensorChannels", [])),
        "firmwareVersion": device.get("firmwareVersion") or device.get("firmware"),
        "certificate": copy_payload(device.get("certificate", {})),
    }


def install_point_snapshot(install_point: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy_payload(install_point.get(key))
        for key in (
            "position",
            "orientation",
            "mountingMethod",
            "acousticDirection",
            "ambientNoiseSources",
            "photoRefs",
            "active",
        )
    }


@device_lifecycle.serialized
def create_device(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "device:write")
    require_site_access(user, site_id)
    device_id = required_text(payload, "id").upper()
    asset_id = required_text(payload, "assetId").upper()

    with STORE_LOCK:
        if any(device["id"] == device_id for device in DEVICES):
            raise ApiError(400, "DEVICE_DUPLICATED", "Device ID is already registered.")
    # Also reject orphaned IDs deleted by a previous release. History checks
    # may wait on a separate DB, so do not hold the master-state lock here.
    device_lifecycle.check_history(device_id)
    with STORE_LOCK:
        validate_asset_device_mapping(site_id, asset_id)
        mapping_status = str(payload.get("mappingStatus") or "active").strip().lower()
        validate_mapping_status(mapping_status)
        if any(device["id"] == device_id for device in DEVICES):
            raise ApiError(400, "DEVICE_DUPLICATED", "Device ID is already registered.")
        if mapping_status == "active" and any(
            device["assetId"] == asset_id and device["mappingStatus"] == "active"
            for device in DEVICES
        ):
            raise ApiError(
                400,
                "DEVICE_ASSET_DUPLICATED",
                "The asset already has an active device mapping.",
            )
        sensor_channels = string_list(
            {
                "sensorChannels": payload.get("sensorChannels")
                or ["vibration", "acoustic", "rpm"]
            },
            "sensorChannels",
            required=True,
        )
        firmware_version = str(
            payload.get("firmwareVersion")
            or payload.get("firmware")
            or "esp32-edge-1.0.0"
        )
        certificate = certificate_payload(payload)
        validate_certificate_for_mapping(certificate, mapping_status)
        timestamp = now_iso()
        last_seen_sec_ago = parse_int_field(payload, "lastSeenSecAgo", default=0)
        if last_seen_sec_ago < 0:
            raise ApiError(
                400, "INVALID_NUMBER", "lastSeenSecAgo must be zero or greater."
            )
        reboot_count = parse_int_field(payload, "rebootCount", default=0)
        if reboot_count < 0:
            raise ApiError(
                400, "INVALID_NUMBER", "rebootCount must be zero or greater."
            )
        buffer_usage_pct = parse_float_value(
            "bufferUsagePct", payload.get("bufferUsagePct"), 0.0
        )
        if not 0 <= buffer_usage_pct <= 100:
            raise ApiError(
                400, "INVALID_NUMBER", "bufferUsagePct must be between 0 and 100."
            )
        hardware_profile_id = (
            str(payload.get("hardwareProfileId") or "HW-ESP32S3-ADXL345-INMP441-WIFI")
            .strip()
            .upper()
        )
        hardware_profile(hardware_profile_id)
        device = {
            "id": device_id,
            "siteId": site_id,
            "assetId": asset_id,
            "sensorChannels": sensor_channels,
            "firmware": firmware_version,
            "firmwareVersion": firmware_version,
            "lastSeenSecAgo": last_seen_sec_ago,
            "lastReceivedAt": iso_seconds_ago(last_seen_sec_ago),
            "health": str(payload.get("health") or "online"),
            "healthHistory": [],
            "offlineSince": None,
            "lastRecoveredAt": None,
            "missingIntervals": [],
            "rssiDbm": parse_float_value("rssiDbm", payload.get("rssiDbm"), -60.0),
            "rebootCount": reboot_count,
            "bufferUsagePct": buffer_usage_pct,
            "hardwareProfileId": hardware_profile_id,
            "hardwareProfileHistory": [],
            "mappingStatus": mapping_status,
            "mappingHistory": [
                {"assetId": asset_id, "mappedAt": now_text(), "status": mapping_status}
            ],
            "replacementHistory": [],
            "certificateHistory": [],
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        apply_certificate_fields(device, certificate)
        DEVICES.append(device)
        site = get_site(site_id)
        site["totalDevices"] = int(site["totalDevices"]) + 1
        if device["health"] == "online":
            site["onlineDevices"] = int(site["onlineDevices"]) + 1
        return copy_payload(device)


@device_lifecycle.serialized
def update_device(
    user: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "device:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        candidate = copy_payload(device)
        previous_health = str(device.get("health") or "")
        previous_asset_id = str(device["assetId"])
        next_asset_id = str(payload.get("assetId", previous_asset_id)).strip().upper()
        next_mapping_status = (
            str(payload.get("mappingStatus", device.get("mappingStatus") or "active"))
            .strip()
            .lower()
        )
        validate_mapping_status(next_mapping_status)
        get_asset(device["siteId"], next_asset_id)
        if next_asset_id != previous_asset_id or (
            next_mapping_status == "active" and device.get("mappingStatus") != "active"
        ):
            validate_asset_device_mapping(device["siteId"], next_asset_id)
        if next_mapping_status == "active" and any(
            item["id"] != device_id
            and item["assetId"] == next_asset_id
            and item["mappingStatus"] == "active"
            for item in DEVICES
        ):
            raise ApiError(
                400,
                "DEVICE_ASSET_DUPLICATED",
                "The asset already has an active device mapping.",
            )
        if next_asset_id != previous_asset_id or next_mapping_status != device.get(
            "mappingStatus"
        ):
            candidate["assetId"] = next_asset_id
            candidate.setdefault("mappingHistory", []).append(
                {
                    "fromAssetId": previous_asset_id,
                    "assetId": next_asset_id,
                    "mappedAt": now_text(),
                    "status": next_mapping_status,
                }
            )
        if "sensorChannels" in payload:
            candidate["sensorChannels"] = string_list(
                payload, "sensorChannels", required=True
            )
        if "firmware" in payload or "firmwareVersion" in payload:
            firmware_version = str(
                payload.get("firmwareVersion") or payload.get("firmware") or ""
            ).strip()
            if not firmware_version:
                raise ApiError(400, "MISSING_FIELD", "firmwareVersion is required.")
            candidate["firmware"] = firmware_version
            candidate["firmwareVersion"] = firmware_version
        certificate_keys = {
            "certificate",
            "certificateId",
            "certificateFingerprint",
            "certificateStatus",
            "certificateIssuedAt",
            "certificateExpiresAt",
        }
        certificate_changed = bool(certificate_keys.intersection(payload))
        if certificate_changed:
            previous_certificate = copy_payload(device.get("certificate", {}))
            next_certificate = certificate_payload(payload, previous_certificate)
            apply_certificate_fields(candidate, next_certificate)
        else:
            previous_certificate = copy_payload(device.get("certificate", {}))
            next_certificate = previous_certificate
        for key in ("health", "mappingStatus"):
            if key in payload:
                candidate[key] = str(payload[key]).strip().lower()
        if certificate_changed or (
            next_mapping_status == "active"
            and (
                device.get("mappingStatus") != "active"
                or next_asset_id != previous_asset_id
            )
        ):
            validate_certificate_for_mapping(next_certificate, next_mapping_status)
        if "lastSeenSecAgo" in payload:
            last_seen_sec_ago = parse_int_value(
                "lastSeenSecAgo", payload["lastSeenSecAgo"]
            )
            if last_seen_sec_ago < 0:
                raise ApiError(
                    400, "INVALID_NUMBER", "lastSeenSecAgo must be zero or greater."
                )
            candidate["lastSeenSecAgo"] = last_seen_sec_ago
            candidate["lastReceivedAt"] = iso_seconds_ago(last_seen_sec_ago)
        if "rebootCount" in payload:
            reboot_count = parse_int_value("rebootCount", payload["rebootCount"])
            if reboot_count < 0:
                raise ApiError(
                    400, "INVALID_NUMBER", "rebootCount must be zero or greater."
                )
            candidate["rebootCount"] = reboot_count
        if "bufferUsagePct" in payload:
            buffer_usage_pct = parse_float_value(
                "bufferUsagePct", payload["bufferUsagePct"]
            )
            if not 0 <= buffer_usage_pct <= 100:
                raise ApiError(
                    400, "INVALID_NUMBER", "bufferUsagePct must be between 0 and 100."
                )
            candidate["bufferUsagePct"] = buffer_usage_pct
        if "rssiDbm" in payload:
            rssi_dbm = parse_float_value("rssiDbm", payload["rssiDbm"])
            if not -120 <= rssi_dbm <= 0:
                raise ApiError(
                    400, "INVALID_NUMBER", "rssiDbm must be between -120 and 0."
                )
            candidate["rssiDbm"] = rssi_dbm
        before = device_replacement_snapshot(device)
        after = device_replacement_snapshot(candidate)
        if before != after:
            reason = str(
                payload.get("replacementReason") or payload.get("reason") or ""
            ).strip()
            if not reason:
                raise ApiError(
                    400,
                    "REPLACEMENT_REASON_REQUIRED",
                    "reason is required when device mapping, firmware, sensors, or certificate changes.",
                )
            replaced_at = now_iso()
            candidate.setdefault("replacementHistory", []).append(
                {
                    "reason": reason,
                    "replacedAt": replaced_at,
                    "before": before,
                    "after": after,
                }
            )
            if previous_certificate != next_certificate:
                candidate.setdefault("certificateHistory", []).append(
                    {
                        "reason": reason,
                        "changedAt": replaced_at,
                        "before": previous_certificate,
                        "after": next_certificate,
                    }
                )
        candidate["updatedAt"] = now_iso()
        next_health = str(candidate.get("health") or "")
        device.update(candidate)
        if previous_health != next_health:
            site = get_site(device["siteId"])
            if previous_health == "online":
                site["onlineDevices"] = max(0, int(site["onlineDevices"]) - 1)
            if next_health == "online":
                site["onlineDevices"] = int(site["onlineDevices"]) + 1
        return copy_payload(device)


@device_lifecycle.serialized
def delete_device(user: dict[str, Any], device_id: str) -> dict[str, Any]:
    require_permission(user, "device:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
    device_lifecycle.check_history(device_id)
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        if device_id in DEVICE_CONFIGS:
            raise ApiError(
                409,
                "DEVICE_HAS_CONFIG_HISTORY",
                "A device with configuration history cannot be deleted; deactivate it instead.",
            )
        has_telemetry_history = any(
            record.get("deviceId") == device_id for record in TELEMETRY_RECORDS
        )
        has_idempotency_history = any(
            key[0] == device_id for key in TELEMETRY_IDEMPOTENCY
        )
        if has_telemetry_history or has_idempotency_history:
            raise ApiError(
                409,
                "DEVICE_HAS_TELEMETRY_HISTORY",
                "A device with telemetry or idempotency history cannot be deleted.",
            )
        if any(record["deviceId"] == device_id for record in CONNECTIVITY_TEST_RECORDS):
            raise ApiError(
                409,
                "DEVICE_HAS_CONNECTIVITY_HISTORY",
                "A device with connectivity test history cannot be deleted.",
            )
        if any(
            inspection["deviceId"] == device_id
            for inspection in ENVIRONMENT_INSPECTIONS
        ):
            raise ApiError(
                409,
                "DEVICE_HAS_ENVIRONMENT_INSPECTIONS",
                "A device with environment inspection history cannot be deleted.",
            )
        DEVICES[:] = [item for item in DEVICES if item["id"] != device_id]
        site = get_site(device["siteId"])
        site["totalDevices"] = max(0, int(site["totalDevices"]) - 1)
        if device["health"] == "online":
            site["onlineDevices"] = max(0, int(site["onlineDevices"]) - 1)
        return {"deleted": True, "deviceId": device_id}


def quarantine_unregistered_device(payload: dict[str, Any]) -> dict[str, Any]:
    device_id = required_text(payload, "deviceId").upper()
    with STORE_LOCK:
        if any(device["id"] == device_id for device in DEVICES):
            raise ApiError(
                400, "DEVICE_REGISTERED", "The device is already registered."
            )
        record = {
            "id": f"Q-{len(QUARANTINED_DEVICE_MESSAGES) + 1:04d}",
            "deviceId": device_id,
            "reason": "unregistered_device",
            "receivedAt": now_text(),
            "payload": copy_payload(payload),
        }
        QUARANTINED_DEVICE_MESSAGES.append(record)
        return copy_payload(record)


def parse_rfc3339(
    field: str, value: Any, error_code: str = "INVALID_TIMESTAMP"
) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, error_code, f"{field} must be an RFC 3339 timestamp.")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(
            400, error_code, f"{field} must be an RFC 3339 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise ApiError(400, error_code, f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def format_rfc3339(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def telemetry_principal_for_token(token: str) -> dict[str, Any]:
    if not token:
        raise ApiError(
            401, "AUTH_REQUIRED", "A bearer device or service token is required."
        )
    service = TELEMETRY_SERVICE_TOKENS.get(token)
    configured_health_token = os.environ.get("DEVICE_HEALTH_TOKEN", "")
    if (
        service is None
        and configured_health_token
        and hmac.compare_digest(token, configured_health_token)
    ):
        service = {
            "id": "service-device-health",
            "type": "service",
            "permissions": ["device-health:write"],
            "allowedDeviceIds": ["*"],
        }
    if service:
        if service.get("demoOnly") and os.environ.get(
            "APP_ENV", ""
        ).strip().lower() == ("production"):
            raise ApiError(
                401,
                "AUTH_REQUIRED",
                "Demo telemetry validation tokens are disabled in production.",
            )
        return copy_payload(service)
    user = current_user_for_token(token)
    if not has_permission(user, "telemetry:ingest"):
        raise ApiError(
            403,
            "TELEMETRY_INGEST_FORBIDDEN",
            "The token does not have telemetry:ingest permission.",
        )
    return {
        **copy_payload(user),
        "type": "user",
        "permissions": copy_payload(role_policy(user["role"])["permissions"]),
        "allowedDeviceIds": ["*"],
    }


def principal_can_ingest(principal: dict[str, Any], device_id: str) -> None:
    permissions = set(principal.get("permissions", []))
    if "*" not in permissions and "telemetry:ingest" not in permissions:
        raise ApiError(
            403,
            "TELEMETRY_INGEST_FORBIDDEN",
            "The token does not have telemetry:ingest permission.",
        )
    allowed_device_ids = principal.get("allowedDeviceIds", ["*"])
    if "*" not in allowed_device_ids and device_id not in allowed_device_ids:
        raise ApiError(
            403,
            "TELEMETRY_INGEST_FORBIDDEN",
            "The token cannot ingest telemetry for this device.",
        )


def telemetry_label_fields(payload: dict[str, Any]) -> list[str]:
    fields = []
    for field in (
        "scenarioLabel",
        "knownVibrationLabel",
        "knownAcousticLabel",
    ):
        value = payload.get(field)
        # Authorization follows the same canonical values produced by the
        # single-record normalizer. Malformed values are rejected later as 400s;
        # blank optional labels normalize to null and do not require label rights.
        if not isinstance(value, str) or not value.strip():
            continue
        if field == "scenarioLabel" and value.strip() not in TELEMETRY_SCENARIOS:
            continue
        fields.append(field)
    return fields


def principal_can_submit_telemetry_labels(
    principal: dict[str, Any], payload: dict[str, Any]
) -> list[str]:
    fields = telemetry_label_fields(payload)
    permissions = set(principal.get("permissions", []))
    # Label submission is deliberately narrower than the ordinary wildcard role:
    # only a principal explicitly provisioned for controlled experiments may use it.
    if fields and "telemetry:label" not in permissions:
        raise ApiError(
            403,
            "TELEMETRY_LABEL_FORBIDDEN",
            "The token cannot submit non-null telemetry labels.",
        )
    return fields


def telemetry_number(
    payload: dict[str, Any], key: str, *, required: bool, nullable: bool
) -> float | None:
    if key not in payload:
        if required:
            raise ApiError(400, "INVALID_TELEMETRY_PAYLOAD", f"{key} is required.")
        return None
    value = payload[key]
    if value is None:
        if nullable:
            return None
        raise ApiError(
            400, "INVALID_TELEMETRY_PAYLOAD", f"{key} must be a finite number."
        )
    if isinstance(value, bool):
        raise ApiError(
            400, "INVALID_TELEMETRY_PAYLOAD", f"{key} must be a finite number."
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApiError(
            400, "INVALID_TELEMETRY_PAYLOAD", f"{key} must be a finite number."
        ) from exc
    if not math.isfinite(parsed):
        raise ApiError(
            400, "INVALID_TELEMETRY_PAYLOAD", f"{key} must be a finite number."
        )
    return parsed


def telemetry_optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(
            400, "INVALID_TELEMETRY_PAYLOAD", f"{key} must be a string or null."
        )
    return value.strip() or None


def normalize_rpm_observation(
    payload: dict[str, Any], timestamp: datetime
) -> dict[str, Any]:
    """Validate optional equipment feedback without upgrading legacy RPM to measured."""
    fields = {"rpmStatus", "rpmMeasuredAt", "rpmSource"}
    if not fields.intersection(payload):
        # Preserve legacy normalization, including its idempotency hash.
        return {"rpm": telemetry_number(payload, "rpm", required=False, nullable=True)}

    def reject(message: str) -> NoReturn:
        raise ApiError(400, "INVALID_TELEMETRY_PAYLOAD", message)

    if not (fields | {"rpm"}).issubset(payload):
        reject("rpm, rpmStatus, rpmMeasuredAt and rpmSource must be supplied together.")
    status = payload["rpmStatus"]
    if not isinstance(status, str) or status not in {
        "valid",
        "unavailable",
        "stale",
        "invalid",
    }:
        reject("rpmStatus must be valid, unavailable, stale or invalid.")
    source = payload["rpmSource"]
    if source is not None:
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source) > 160
            or not source.isprintable()
        ):
            reject(
                "rpmSource must be null or a nonblank identifier of at most 160 characters without control characters."
            )
        source = source.strip()
    measured_at = payload["rpmMeasuredAt"]
    if measured_at is not None:
        if (
            not isinstance(measured_at, str)
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
                measured_at,
            )
            is None
        ):
            reject(
                "rpmMeasuredAt must be an RFC3339 timestamp (up to six fractional digits) or null."
            )
        # Limit offset components as fromisoformat also accepts overflowing minutes.
        if measured_at[-1:] not in {"Z", "z"} and (
            int(measured_at[-5:-3]) > 23 or int(measured_at[-2:]) > 59
        ):
            reject("rpmMeasuredAt has an invalid timezone offset.")
        if measured_at.endswith("-00:00"):
            reject("rpmMeasuredAt must have a known UTC offset.")
        try:
            measured = parse_rfc3339(
                "rpmMeasuredAt", measured_at.upper(), "INVALID_TELEMETRY_PAYLOAD"
            )
        except OverflowError:
            reject("rpmMeasuredAt is outside the supported UTC date range.")
        if measured > timestamp:
            reject("rpmMeasuredAt must not be after the telemetry timestamp.")
        measured_at = format_rfc3339(measured)
    rpm = payload["rpm"]
    if status == "valid":
        if source is None or measured_at is None:
            reject("Valid RPM requires rpmSource and rpmMeasuredAt.")
        if not isinstance(rpm, (int, float)) or isinstance(rpm, bool):
            reject("Valid RPM must be a finite nonnegative JSON number.")
        rpm = telemetry_number(payload, "rpm", required=True, nullable=False)
        if rpm < 0:
            reject("Valid RPM must be nonnegative; zero represents a measured stop.")
        if payload.get("isSynthetic"):
            reject("Synthetic telemetry must not claim valid equipment RPM feedback.")
    elif rpm is not None:
        reject("Unavailable, stale or invalid RPM must be null, not a fallback value.")
    if status == "stale" and (source is None or measured_at is None):
        reject("Stale RPM requires the source and timestamp of the last observation.")
    if status == "unavailable" and measured_at is not None:
        reject("Unavailable RPM has no observation; rpmMeasuredAt must be null.")
    if measured_at is not None and source is None:
        reject("An RPM observation timestamp requires rpmSource.")
    return {
        "rpm": rpm,
        "rpmStatus": status,
        "rpmMeasuredAt": measured_at,
        "rpmSource": source,
    }


def normalize_telemetry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "timestamp",
        "sequence",
        "siteId",
        "assetId",
        "deviceId",
        "rpm",
        "rpmStatus",
        "rpmMeasuredAt",
        "rpmSource",
        "vibrationRmsRaw",
        "vibrationRmsMmS",
        "vibrationPeakHz",
        "acousticRmsRaw",
        "acousticDb",
        "acousticPeakHz",
        "scenarioLabel",
        "knownVibrationLabel",
        "knownAcousticLabel",
        "source",
        "isSynthetic",
        "vibrationUnitNote",
        "acousticUnitNote",
    }
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "Unknown telemetry fields: " + ", ".join(unknown_fields),
        )

    identifiers: dict[str, str] = {}
    for key in ("siteId", "assetId", "deviceId"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "INVALID_TELEMETRY_PAYLOAD", f"{key} is required.")
        identifiers[key] = value.strip().upper()

    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "sequence must be an integer greater than or equal to 1.",
        )

    timestamp = parse_rfc3339(
        "timestamp", payload.get("timestamp"), "INVALID_TELEMETRY_PAYLOAD"
    )
    current = datetime.now(timezone.utc)
    if timestamp > current + timedelta(seconds=TELEMETRY_MAX_FUTURE_SECONDS):
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "timestamp is too far in the future.",
        )
    if timestamp < current - timedelta(days=int(PARAMETERS["RETENTION_RAW_DAYS"])):
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "timestamp is outside the raw telemetry retention window.",
        )

    for key in ("vibrationRmsMmS", "acousticDb"):
        if key not in payload or payload[key] is not None:
            raise ApiError(
                400,
                "INVALID_RAW_ONLY_FIELD",
                f"{key} must be present and null for the raw-only contract.",
            )

    if "scenarioLabel" not in payload:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "scenarioLabel is required and may be null.",
        )
    scenario_label = payload["scenarioLabel"]
    if scenario_label is not None and (
        not isinstance(scenario_label, str)
        or not scenario_label.strip()
        or scenario_label.strip() not in TELEMETRY_SCENARIOS
    ):
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "scenarioLabel must be null or a supported scenario.",
        )
    if isinstance(scenario_label, str):
        scenario_label = scenario_label.strip()
    is_synthetic = payload.get("isSynthetic")
    if not isinstance(is_synthetic, bool):
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "isSynthetic must be a boolean.",
        )

    vibration_rms_raw = telemetry_number(
        payload, "vibrationRmsRaw", required=True, nullable=False
    )
    acoustic_rms_raw = telemetry_number(
        payload, "acousticRmsRaw", required=False, nullable=True
    )
    if vibration_rms_raw < 0:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "vibrationRmsRaw must be greater than or equal to zero.",
        )
    if acoustic_rms_raw is not None and acoustic_rms_raw < 0:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "acousticRmsRaw must be greater than or equal to zero.",
        )

    return {
        "timestamp": format_rfc3339(timestamp),
        "sequence": sequence,
        **identifiers,
        **normalize_rpm_observation(payload, timestamp),
        "vibrationRmsRaw": vibration_rms_raw,
        "vibrationRmsMmS": None,
        "vibrationPeakHz": telemetry_number(
            payload, "vibrationPeakHz", required=True, nullable=False
        ),
        "acousticRmsRaw": acoustic_rms_raw,
        "acousticDb": None,
        "acousticPeakHz": telemetry_number(
            payload, "acousticPeakHz", required=False, nullable=True
        ),
        "scenarioLabel": scenario_label,
        "knownVibrationLabel": telemetry_optional_text(payload, "knownVibrationLabel"),
        "knownAcousticLabel": telemetry_optional_text(payload, "knownAcousticLabel"),
        "source": telemetry_optional_text(payload, "source"),
        "isSynthetic": is_synthetic,
        "vibrationUnitNote": telemetry_optional_text(payload, "vibrationUnitNote"),
        "acousticUnitNote": telemetry_optional_text(payload, "acousticUnitNote"),
    }


def validate_telemetry_mapping(record: dict[str, Any]) -> dict[str, Any]:
    get_site(record["siteId"])
    get_asset(record["siteId"], record["assetId"])
    device = get_device(record["deviceId"])
    if (
        device["siteId"] != record["siteId"]
        or device["assetId"] != record["assetId"]
        or device.get("mappingStatus") != "active"
    ):
        raise ApiError(
            409,
            "DEVICE_MAPPING_MISMATCH",
            "deviceId is not actively mapped to the requested siteId and assetId.",
        )
    if device.get("certificateStatus") != "registered":
        raise ApiError(
            409,
            "DEVICE_CERTIFICATE_NOT_ACTIVE",
            "The device certificate is not active for telemetry ingestion.",
        )
    return device


def quarantine_telemetry_error(payload: dict[str, Any], error: ApiError) -> None:
    QUARANTINED_DEVICE_MESSAGES.append(
        {
            "id": f"Q-TEL-{len(QUARANTINED_DEVICE_MESSAGES) + 1:04d}",
            "deviceId": str(payload.get("deviceId") or "").strip().upper() or None,
            "reason": error.code,
            "message": error.message,
            "receivedAt": now_iso(),
            "payload": copy_payload(payload),
        }
    )


def quarantine_mqtt_message(
    principal: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    permissions = set(principal.get("permissions", []))
    if "*" not in permissions and "telemetry:quarantine" not in permissions:
        raise ApiError(
            403,
            "TELEMETRY_QUARANTINE_FORBIDDEN",
            "The token cannot quarantine MQTT messages.",
        )

    topic = required_text(payload, "topic")
    reason = required_text(payload, "reason")
    message = required_text(payload, "message")
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, str):
        raise ApiError(
            400,
            "INVALID_QUARANTINE_PAYLOAD",
            "payload must contain the original MQTT message as text.",
        )
    idempotency_key_value = payload.get("idempotencyKey")
    idempotency_key = None
    if idempotency_key_value is not None:
        if not isinstance(idempotency_key_value, str):
            raise ApiError(
                400,
                "INVALID_IDEMPOTENCY_KEY",
                "idempotencyKey must be a non-empty string.",
            )
        idempotency_key = idempotency_key_value.strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise ApiError(
                400,
                "INVALID_IDEMPOTENCY_KEY",
                "idempotencyKey must contain 1 to 200 characters.",
            )

    topic_parts = [part for part in topic.strip("/").split("/") if part]
    device_id = None
    if (
        len(topic_parts) == 3
        and topic_parts[0] == "devices"
        and topic_parts[2] == "telemetry"
    ):
        device_id = topic_parts[1].strip().upper() or None

    with STORE_LOCK:
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "topic": topic,
                    "payload": raw_payload,
                    "reason": reason,
                    "message": message,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if idempotency_key is not None:
            existing = MQTT_QUARANTINE_IDEMPOTENCY.get(idempotency_key)
            if existing is not None:
                if existing["fingerprint"] != request_fingerprint:
                    raise ApiError(
                        409,
                        "QUARANTINE_IDEMPOTENCY_CONFLICT",
                        "idempotencyKey was already used for another MQTT message.",
                    )
                return copy_payload(existing["record"])
        TELEMETRY_METRICS["requests"] = int(TELEMETRY_METRICS["requests"]) + 1
        TELEMETRY_METRICS["rejected"] = int(TELEMETRY_METRICS["rejected"]) + 1
        TELEMETRY_METRICS["localRejected"] = int(TELEMETRY_METRICS["localRejected"]) + 1
        record = {
            "id": f"Q-MQTT-{len(QUARANTINED_DEVICE_MESSAGES) + 1:04d}",
            "source": "mqtt_bridge",
            "deviceId": device_id,
            "topic": topic,
            "reason": reason,
            "message": message,
            "receivedAt": now_iso(),
            "payload": raw_payload,
            "idempotencyKey": idempotency_key,
        }
        QUARANTINED_DEVICE_MESSAGES.append(record)
        if idempotency_key is not None:
            MQTT_QUARANTINE_IDEMPOTENCY[idempotency_key] = {
                "fingerprint": request_fingerprint,
                "record": copy_payload(record),
            }
        return copy_payload(record)


def update_ingest_dependency(
    success: bool, latency_ms: float, error: ApiError | None = None
) -> None:
    dependency = next(
        item for item in SERVICE_DEPENDENCIES if item["id"] == "ingest-api"
    )
    requests = int(TELEMETRY_METRICS["requests"])
    rejected = int(TELEMETRY_METRICS["rejected"])
    dependency["latencyMs"] = round(latency_ms, 2)
    dependency["errorRatePct"] = round(
        (rejected / requests * 100) if requests else 0.0, 2
    )
    previous_status = dependency["status"]
    dependency["status"] = "healthy" if dependency["errorRatePct"] < 10 else "degraded"
    if not success:
        dependency["lastFailureAt"] = now_iso()
        SERVICE_HEALTH_EVENTS.append(
            {
                "dependencyId": dependency["id"],
                "status": "failure",
                "occurredAt": dependency["lastFailureAt"],
                "impactScope": dependency["impactScope"],
                "errorCode": error.code if error else None,
            }
        )
    elif previous_status != "healthy" and dependency["status"] == "healthy":
        dependency["lastRecoveryAt"] = now_iso()
        SERVICE_HEALTH_EVENTS.append(
            {
                "dependencyId": dependency["id"],
                "status": "recovered",
                "occurredAt": dependency["lastRecoveryAt"],
                "impactScope": dependency["impactScope"],
            }
        )
    del SERVICE_HEALTH_EVENTS[:-100]


def recover_device_from_telemetry(device: dict[str, Any], received_at: str) -> None:
    previous_health = str(device.get("health") or "")
    offline_since = device.get("offlineSince")
    recovery_event = None
    if offline_since:
        interval = {"from": offline_since, "to": received_at, "reason": "telemetry_gap"}
        device.setdefault("missingIntervals", []).append(interval)
        device["lastRecoveredAt"] = received_at
        device.setdefault("healthHistory", []).append(
            {"from": previous_health, "to": "online", "changedAt": received_at}
        )
        recovery_event = {
            "id": f"EV-DEVICE-{len(EVENTS) + 1:04d}",
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "deviceId": device["id"],
            "severity": "device",
            "eventType": "device_recovered",
            "title": f"{device['id']} telemetry recovered",
            "occurredAt": received_at,
            "time": received_at,
            "duration": "recovered",
            "score": 0,
            "label": "needs_review",
            "note": "Telemetry resumed after an offline interval.",
        }
    if previous_health == "offline":
        site = get_site(device["siteId"])
        site["onlineDevices"] = int(site["onlineDevices"]) + 1
        site["eventCount"] = int(site["eventCount"]) + 1
    device["health"] = "online"
    device["offlineSince"] = None
    device["lastReceivedAt"] = received_at
    device["lastSeenSecAgo"] = 0
    device["updatedAt"] = received_at
    if recovery_event:
        EVENTS.insert(0, recovery_event)
        _freeze_event_evidence(recovery_event, creating=True)


def _lifecycle_config_for(rule: dict[str, Any]) -> EventLifecycleConfig:
    return EventLifecycleConfig(
        score_enter=float(rule["scoreThreshold"]),
        score_exit=max(0.0, float(rule["scoreThreshold"]) - float(rule["hysteresis"])),
        min_consecutive_enter=1,
        min_consecutive_exit=1,
        min_duration_enter_sec=float(rule["durationSec"]),
        min_duration_exit_sec=float(rule["durationSec"]),
        merge_gap_sec=int(rule["mergeWindowSec"]),
        idempotency_cache_size=max(1024, TELEMETRY_MAX_RECORDS_PER_ASSET * 2),
        rule_version=str(rule["version"]),
    )


def _lifecycle_for(asset_id: str, rule: dict[str, Any]) -> AnomalyEventLifecycle:
    config = _lifecycle_config_for(rule)
    lifecycle = _LIFECYCLE_RUNTIMES.get(asset_id)
    if lifecycle is not None and lifecycle.config == config:
        return lifecycle
    checkpoint = LIFECYCLE_CHECKPOINTS.get(asset_id)
    lifecycle = (
        AnomalyEventLifecycle.from_snapshot(checkpoint, config=config)
        if checkpoint is not None
        else AnomalyEventLifecycle(config)
    )
    _LIFECYCLE_RUNTIMES[asset_id] = lifecycle
    return lifecycle


def _lifecycle_event_record(
    update: dict[str, Any],
    scored_record: dict[str, Any],
    rule: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    lifecycle_event = copy_payload(update["event"])
    event_id = str(lifecycle_event["id"])
    existing = next((item for item in EVENTS if item["id"] == event_id), None)
    created = existing is None
    max_score = int(lifecycle_event.get("maxScore") or 0)
    start_at = str(lifecycle_event["startAt"])
    end_at = lifecycle_event.get("endAt")
    duration_sec = max(
        0,
        int(
            (
                parse_rfc3339("endAt", end_at or scored_record["timestamp"])
                - parse_rfc3339("startAt", start_at)
            ).total_seconds()
        ),
    )
    record = existing or {
        "id": event_id,
        "siteId": scored_record["siteId"],
        "assetId": scored_record["assetId"],
        "deviceId": scored_record["deviceId"],
        "eventType": "asset_anomaly_candidate",
        "title": f"{scored_record['assetId']} anomaly score event",
        "occurredAt": start_at,
        "time": start_at,
        "label": "needs_review",
        "note": "Generated from accepted telemetry by the AI2 event lifecycle.",
        "reviewed": False,
        "reviewedAt": None,
        "reviewedBy": None,
        "isSynthetic": bool(scored_record.get("isSynthetic")),
        "source": scored_record.get("source") or "telemetry_ingest",
        "triggerTelemetry": copy_payload(scored_record),
        "triggerEvidence": copy_payload(scored_record.get("anomalyEvidence", [])),
    }
    record.update(
        {
            "severity": (
                "critical" if max_score >= float(rule["scoreThreshold"]) else "warning"
            ),
            "duration": f"{duration_sec}s",
            "durationSec": duration_sec,
            "score": lifecycle_event.get("lastScore"),
            "maxScore": max_score,
            "status": lifecycle_event["status"],
            "endAt": end_at,
            "endReason": lifecycle_event.get("endReason"),
            "sampleCount": lifecycle_event.get("sampleCount"),
            "mergeCount": lifecycle_event.get("mergeCount"),
            "thresholdVersion": lifecycle_event.get("thresholdVersion")
            or rule["version"],
            "modelVersion": lifecycle_event.get("modelVersion")
            or scored_record.get("anomalyModel"),
            "maxScoreModelVersion": lifecycle_event.get("maxScoreModelVersion"),
            "classification": "asset_anomaly",
        }
    )
    if created:
        EVENTS.insert(0, record)
        site = get_site(record["siteId"])
        site["eventCount"] = int(site["eventCount"]) + 1
        _freeze_event_evidence(record, creating=True)
    return record, created


def process_accepted_telemetry(
    stored_record: dict[str, Any], scored: dict[str, Any]
) -> list[dict[str, Any]]:
    rule = anomaly_rule_for(stored_record["assetId"])
    active_sensor_fault = next(
        (
            event
            for event in EVENTS
            if event.get("eventType") == "sensor_fault"
            and event.get("deviceId") == stored_record["deviceId"]
            and event.get("status") == "open"
        ),
        None,
    )
    if not rule.get("active") and active_sensor_fault is None:
        return []
    lifecycle = _lifecycle_for(stored_record["assetId"], rule)
    point = {**stored_record, **scored}
    updates_applied: list[dict[str, Any]] = []

    def persist(checkpoint: dict[str, Any], updates: list[dict[str, Any]]) -> None:
        LIFECYCLE_CHECKPOINTS[stored_record["assetId"]] = copy_payload(checkpoint)
        for update in updates:
            if "event" in update:
                event, created = _lifecycle_event_record(update, point, rule)
                updates_applied.append(
                    {"kind": update["kind"], "eventId": event["id"], "created": created}
                )
            else:
                updates_applied.append(copy_payload(update))

    lifecycle.process_point(
        point,
        sensor_fault=active_sensor_fault is not None,
        sensor_fault_reason=(
            f"device_sensor_fault:{active_sensor_fault['faultCode']}"
            if active_sensor_fault is not None
            else None
        ),
        telemetry_payload=stored_record,
        persist_transaction=persist,
    )
    return updates_applied


def ingest_telemetry(
    principal: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    try:
        return _accept_telemetry(principal, payload)
    except ApiError as error:
        # The failed ingest transaction has already unwound. Intentionally
        # persist rejection diagnostics, then return the original API error.
        with STORE_LOCK:
            TELEMETRY_METRICS["requests"] = int(TELEMETRY_METRICS["requests"]) + 1
            TELEMETRY_METRICS["rejected"] = int(TELEMETRY_METRICS["rejected"]) + 1
            if error.code == "SEQUENCE_CONFLICT":
                TELEMETRY_METRICS["conflicts"] = int(TELEMETRY_METRICS["conflicts"]) + 1
            quarantine_telemetry_error(payload, error)
            latency_ms = (time.monotonic() - started) * 1000
            TELEMETRY_METRICS["lastLatencyMs"] = round(latency_ms, 2)
            update_ingest_dependency(False, latency_ms, error)
        raise


def _accept_telemetry(
    principal: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    with STORE_LOCK:
        record = normalize_telemetry_payload(payload)
        principal_can_ingest(principal, record["deviceId"])
        label_fields = principal_can_submit_telemetry_labels(principal, record)
        device = validate_telemetry_mapping(record)
        payload_hash = hashlib.sha256(
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = (record["deviceId"], int(record["sequence"]))
        existing = TELEMETRY_IDEMPOTENCY.get(idempotency_key)
        if existing:
            if existing["payloadHash"] != payload_hash:
                raise ApiError(
                    409,
                    "SEQUENCE_CONFLICT",
                    "The same deviceId and sequence already exist with a different payload.",
                )
            TELEMETRY_METRICS["duplicates"] = int(TELEMETRY_METRICS["duplicates"]) + 1
            TELEMETRY_METRICS["requests"] = int(TELEMETRY_METRICS["requests"]) + 1
            latency_ms = (time.monotonic() - started) * 1000
            TELEMETRY_METRICS["lastLatencyMs"] = round(latency_ms, 2)
            update_ingest_dependency(True, latency_ms)
            return (
                {
                    "accepted": True,
                    "duplicate": True,
                    "deviceId": record["deviceId"],
                    "sequence": record["sequence"],
                    "receivedAt": existing["receivedAt"],
                },
                200,
            )

        received_at = format_rfc3339(datetime.now(timezone.utc))
        label_provenance = None
        if label_fields:
            label_provenance = {
                "principalId": str(principal.get("id") or "unknown"),
                "principalType": str(principal.get("type") or "unknown"),
                "submittedAt": received_at,
                "fields": sorted(label_fields),
                "verifiedByServer": True,
            }
        stored_record = {
            **record,
            "receivedAt": received_at,
            "labelProvenance": label_provenance,
        }
        rule = anomaly_rule_for(stored_record["assetId"])
        if rule.get("signalBaselines"):
            from ai.ai2.week3.signal_score import score_signals

            scored = score_signals(
                stored_record, rule["signalBaselines"], rule["version"]
            )
        else:
            scored = score_telemetry_point(stored_record)
        stored_record.update(copy_payload(scored))
        TELEMETRY_RECORDS.append(stored_record)
        TELEMETRY_IDEMPOTENCY[idempotency_key] = {
            "payloadHash": payload_hash,
            "receivedAt": received_at,
        }
        asset_records = [
            item
            for item in TELEMETRY_RECORDS
            if item["siteId"] == record["siteId"]
            and item["assetId"] == record["assetId"]
        ]
        overflow = len(asset_records) - TELEMETRY_MAX_RECORDS_PER_ASSET
        for expired_record in asset_records[: max(0, overflow)]:
            TELEMETRY_RECORDS.remove(expired_record)
            TELEMETRY_IDEMPOTENCY.pop(
                (expired_record["deviceId"], int(expired_record["sequence"])), None
            )
        recover_device_from_telemetry(device, received_at)
        analysis_started = time.monotonic()
        lifecycle_updates: list[dict[str, Any]] = []
        try:
            lifecycle_updates = process_accepted_telemetry(stored_record, scored)
        except Exception as error:
            record_runtime_dependency(
                "analysis",
                success=False,
                latency_ms=(time.monotonic() - analysis_started) * 1000,
                error_code=type(error).__name__,
                detail="Accepted telemetry was stored but analysis failed.",
            )
        else:
            record_runtime_dependency(
                "analysis",
                success=True,
                latency_ms=(time.monotonic() - analysis_started) * 1000,
            )
        TELEMETRY_METRICS["accepted"] = int(TELEMETRY_METRICS["accepted"]) + 1
        TELEMETRY_METRICS["requests"] = int(TELEMETRY_METRICS["requests"]) + 1
        TELEMETRY_METRICS["lastReceivedAt"] = received_at
        latency_ms = (time.monotonic() - started) * 1000
        TELEMETRY_METRICS["lastLatencyMs"] = round(latency_ms, 2)
        update_ingest_dependency(True, latency_ms)
        return (
            {
                "accepted": True,
                "duplicate": False,
                "deviceId": record["deviceId"],
                "sequence": record["sequence"],
                "receivedAt": received_at,
                "anomalyScore": scored.get("anomalyScore"),
                "lifecycleUpdates": lifecycle_updates,
            },
            201,
        )


def _telemetry_sort_key(record: dict[str, Any]) -> tuple[str, str, int, str]:
    sequence = record.get("sequence")
    normalized_sequence = sequence if isinstance(sequence, int) else 0
    canonical_record = {
        key: value for key, value in record.items() if key != "receivedAt"
    }
    return (
        str(record.get("timestamp") or ""),
        str(record.get("deviceId") or ""),
        normalized_sequence,
        json.dumps(canonical_record, sort_keys=True, separators=(",", ":")),
    )


def _canonical_telemetry_checksum(records: list[dict[str, Any]]) -> str:
    canonical_records = [
        {key: value for key, value in record.items() if key != "receivedAt"}
        for record in sorted(records, key=_telemetry_sort_key)
    ]
    digest = hashlib.sha256(
        json.dumps(canonical_records, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"sha256:{digest}"


def _stored_telemetry_for(
    site_id: str,
    asset_id: str,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    *,
    include_demo: bool = False,
) -> list[dict[str, Any]]:
    with STORE_LOCK:
        get_asset(site_id, asset_id)
        records = TELEMETRY_RECORDS
        if include_demo:
            records = [*TELEMETRY_RECORDS, *DEMO_TELEMETRY_RECORDS]
        stored = [
            copy_payload(record)
            for record in records
            if record["siteId"] == site_id and record["assetId"] == asset_id
        ]
        retention_cutoff = datetime.now(timezone.utc) - timedelta(
            days=int(PARAMETERS["RETENTION_RAW_DAYS"])
        )
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    filtered = []
    for record in stored:
        timestamp = parse_rfc3339("timestamp", record["timestamp"])
        # The maintenance worker may be between ticks or retrying a DB error.
        # Expired raw data must not be exposed while physical deletion waits.
        if timestamp < retention_cutoff:
            continue
        if from_value and timestamp < from_value:
            continue
        if to_value and timestamp > to_value:
            continue
        filtered.append(record)
    return sorted(filtered, key=_telemetry_sort_key)


def telemetry_for(
    site_id: str,
    asset_id: str,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    *,
    include_demo: bool = False,
) -> list[dict[str, Any]]:
    """Read stored measurements; an empty window never fabricates samples.

    Demo samples must be explicitly injected and requested with include_demo.
    """
    return _stored_telemetry_for(
        site_id,
        asset_id,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        include_demo=include_demo,
    )


def telemetry_units(
    site_id: str | None = None,
    asset_id: str | None = None,
    *,
    include_demo: bool = False,
) -> dict[str, str]:
    records = TELEMETRY_RECORDS
    if include_demo:
        records = [*TELEMETRY_RECORDS, *DEMO_TELEMETRY_RECORDS]
    if (
        site_id
        and asset_id
        and any(
            record["siteId"] == site_id and record["assetId"] == asset_id
            for record in records
        )
    ):
        return {
            "vibrationRmsRaw": "raw accelerometer RMS",
            "vibrationPeakHz": "Hz",
            "acousticRmsRaw": "raw waveform RMS",
            "acousticPeakHz": "Hz",
            "rpm": "rev/min",
            "anomalyScore": "0-100",
        }
    return {
        "vibrationRmsMmS": "mm/s RMS",
        "acousticDb": "dB",
        "rpm": "rev/min",
        "anomalyScore": "0-100",
    }


def _device_elapsed_seconds(device: dict[str, Any]) -> int:
    last_received_at = str(device.get("lastReceivedAt") or "")
    elapsed_from_timestamp = 0
    if last_received_at:
        last_received = parse_rfc3339(
            "lastReceivedAt", last_received_at, "INVALID_DEVICE_HEALTH"
        )
        elapsed_from_timestamp = max(
            0, int((datetime.now(timezone.utc) - last_received).total_seconds())
        )
    return max(elapsed_from_timestamp, int(device.get("lastSeenSecAgo") or 0))


def device_health_for(device_id: str) -> dict[str, Any]:
    with STORE_LOCK:
        device = get_device(device_id)
        last_received_at = str(device.get("lastReceivedAt") or "")
        last_received = (
            parse_rfc3339("lastReceivedAt", last_received_at, "INVALID_DEVICE_HEALTH")
            if last_received_at
            else datetime.now(timezone.utc)
        )
        elapsed = _device_elapsed_seconds(device)
        offline_threshold = int(PARAMETERS["DEVICE_OFFLINE_SEC"])
        if elapsed > offline_threshold and device.get("health") != "offline":
            previous_health = str(device.get("health") or "unknown")
            offline_since = format_rfc3339(
                last_received + timedelta(seconds=offline_threshold)
            )
            device["health"] = "offline"
            device["offlineSince"] = offline_since
            device.setdefault("healthHistory", []).append(
                {"from": previous_health, "to": "offline", "changedAt": offline_since}
            )
            if previous_health == "online":
                site = get_site(device["siteId"])
                site["onlineDevices"] = max(0, int(site["onlineDevices"]) - 1)
            event = {
                "id": f"EV-DEVICE-{len(EVENTS) + 1:04d}",
                "siteId": device["siteId"],
                "assetId": device["assetId"],
                "deviceId": device["id"],
                "severity": "device",
                "eventType": "device_offline",
                "title": f"{device['id']} telemetry offline",
                "occurredAt": offline_since,
                "time": offline_since,
                "duration": f">{offline_threshold}s",
                "score": 0,
                "label": "needs_review",
                "note": "No telemetry was received within DEVICE_OFFLINE_SEC.",
            }
            EVENTS.insert(0, event)
            _freeze_event_evidence(event, creating=True)
            get_site(device["siteId"])["eventCount"] = (
                int(get_site(device["siteId"])["eventCount"]) + 1
            )

        latest_connectivity = next(
            (
                copy_payload(record)
                for record in sorted(
                    CONNECTIVITY_TEST_RECORDS,
                    key=lambda item: item["testedAt"],
                    reverse=True,
                )
                if record["deviceId"] == device_id
            ),
            None,
        )
        return {
            "deviceId": device["id"],
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "health": device["health"],
            "lastReceivedAt": device.get("lastReceivedAt"),
            "lastSeenSecAgo": max(0, elapsed),
            "offlineThresholdSec": offline_threshold,
            "offlineSince": device.get("offlineSince"),
            "lastRecoveredAt": device.get("lastRecoveredAt"),
            "missingIntervals": copy_payload(device.get("missingIntervals", [])),
            "rssiDbm": device.get("rssiDbm"),
            "rebootCount": int(device.get("rebootCount") or 0),
            "bufferUsagePct": float(device.get("bufferUsagePct") or 0.0),
            "firmwareVersion": device.get("firmwareVersion") or device.get("firmware"),
            "certificateStatus": device["certificateStatus"],
            "mappingStatus": device["mappingStatus"],
            "latestConnectivityTest": latest_connectivity,
            "activeSensorFaults": copy_payload(device.get("activeSensorFaults", [])),
            "sensorHealth": device.get("sensorHealth", "healthy"),
        }


def _device_health_number(
    payload: dict[str, Any], key: str, minimum: float, maximum: float
) -> float | None:
    if key not in payload:
        return None
    value = payload[key]
    if value is None or isinstance(value, bool):
        raise ApiError(400, "INVALID_DEVICE_HEALTH", f"{key} must be a number.")
    parsed = parse_float_value(key, value)
    if not minimum <= parsed <= maximum:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            f"{key} must be between {minimum:g} and {maximum:g}.",
        )
    return parsed


def _device_health_int(
    payload: dict[str, Any], key: str, minimum: int, maximum: int
) -> int | None:
    if key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "INVALID_DEVICE_HEALTH", f"{key} must be an integer.")
    if not minimum <= value <= maximum:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            f"{key} must be between {minimum} and {maximum}.",
        )
    return value


def _sensor_fault_payload(value: Any, reported_at: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(
            400, "INVALID_DEVICE_HEALTH", "sensorFaults entries must be objects."
        )
    unknown = set(value) - {"code", "status", "severity", "detail", "occurredAt"}
    if unknown:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "Unknown sensor fault fields: " + ", ".join(sorted(unknown)),
        )
    code = str(value.get("code") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", code):
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "sensorFaults.code must be a stable lowercase identifier.",
        )
    status = str(value.get("status") or "active").strip().lower()
    if status not in {"active", "recovered"}:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "sensorFaults.status must be active or recovered.",
        )
    severity = str(value.get("severity") or "warning").strip().lower()
    if severity not in {"warning", "critical"}:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "sensorFaults.severity must be warning or critical.",
        )
    detail_value = value.get("detail")
    if detail_value is not None and not isinstance(detail_value, str):
        raise ApiError(
            400, "INVALID_DEVICE_HEALTH", "sensorFaults.detail must be a string."
        )
    detail = str(detail_value or "").strip()
    if len(detail) > 500:
        raise ApiError(400, "INVALID_DEVICE_HEALTH", "sensorFaults.detail is too long.")
    occurred_at = format_rfc3339(
        parse_rfc3339(
            "sensorFaults.occurredAt",
            value.get("occurredAt") or reported_at,
            "INVALID_DEVICE_HEALTH",
        )
    )
    return {
        "code": code,
        "status": status,
        "severity": severity,
        "detail": detail,
        "occurredAt": occurred_at,
    }


def _apply_sensor_health_boundary(
    device: dict[str, Any], fault: dict[str, Any]
) -> None:
    # update_device_health holds STORE_LOCK while reading this authoritative
    # mapping and applying the boundary, so replacement cannot race this check.
    # Inactive devices still retain their own health/fault history.
    if device.get("mappingStatus") != "active":
        return
    rule = anomaly_rule_for(device["assetId"])
    lifecycle = _lifecycle_for(device["assetId"], rule)
    context = {
        "siteId": device["siteId"],
        "assetId": device["assetId"],
        "deviceId": device["id"],
        "timestamp": fault["occurredAt"],
    }

    def persist(checkpoint: dict[str, Any], updates: list[dict[str, Any]]) -> None:
        LIFECYCLE_CHECKPOINTS[device["assetId"]] = copy_payload(checkpoint)
        for update in updates:
            if "event" in update:
                _lifecycle_event_record(update, context, rule)

    lifecycle.process_sensor_fault(
        device["assetId"],
        fault["occurredAt"],
        reason=f"device_sensor_fault:{fault['code']}",
        persist_transaction=persist,
    )


def update_device_health(
    principal: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    permissions = set(principal.get("permissions", []))
    if "*" not in permissions and "device-health:write" not in permissions:
        raise ApiError(
            403,
            "DEVICE_HEALTH_WRITE_FORBIDDEN",
            "The token cannot report device health.",
        )
    normalized_device_id = str(device_id).strip().upper()
    allowed_device_ids = principal.get("allowedDeviceIds", ["*"])
    if "*" not in allowed_device_ids and normalized_device_id not in allowed_device_ids:
        raise ApiError(
            403,
            "DEVICE_HEALTH_WRITE_FORBIDDEN",
            "The token cannot report health for this device.",
        )
    allowed = {
        "reportedAt",
        "rssiDbm",
        "rebootCount",
        "bufferUsagePct",
        "firmwareVersion",
        "sensorFaults",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "Unknown device health fields: " + ", ".join(sorted(unknown)),
        )
    reported_at = format_rfc3339(
        parse_rfc3339(
            "reportedAt",
            payload.get("reportedAt") or now_iso(),
            "INVALID_DEVICE_HEALTH",
        )
    )
    rssi = _device_health_number(payload, "rssiDbm", -120, 0)
    reboot_count = _device_health_int(payload, "rebootCount", 0, 2_147_483_647)
    buffer_usage = _device_health_number(payload, "bufferUsagePct", 0, 100)
    firmware = payload.get("firmwareVersion")
    if firmware is not None and (
        not isinstance(firmware, str)
        or not firmware.strip()
        or len(firmware.strip()) > 100
    ):
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "firmwareVersion must be a non-empty string up to 100 characters.",
        )
    sensor_fault_values = payload.get("sensorFaults", [])
    if not isinstance(sensor_fault_values, list) or len(sensor_fault_values) > 32:
        raise ApiError(
            400,
            "INVALID_DEVICE_HEALTH",
            "sensorFaults must be a list of at most 32 entries.",
        )
    faults = [
        _sensor_fault_payload(value, reported_at) for value in sensor_fault_values
    ]

    with STORE_LOCK:
        device = get_device(normalized_device_id)
        previous_health_report = device.get("lastHealthReportedAt")
        if previous_health_report and parse_rfc3339(
            "lastHealthReportedAt", previous_health_report, "INVALID_DEVICE_HEALTH"
        ) > parse_rfc3339("reportedAt", reported_at, "INVALID_DEVICE_HEALTH"):
            raise ApiError(
                409,
                "STALE_DEVICE_HEALTH",
                "reportedAt is older than the latest accepted device health report.",
            )
        if rssi is not None:
            device["rssiDbm"] = rssi
        if reboot_count is not None:
            device["rebootCount"] = reboot_count
        if buffer_usage is not None:
            device["bufferUsagePct"] = buffer_usage
        if firmware is not None:
            device["firmwareVersion"] = firmware.strip()
            device["firmware"] = firmware.strip()
        device["lastHealthReportedAt"] = reported_at
        device["lastReceivedAt"] = reported_at
        device["lastSeenSecAgo"] = 0
        if device.get("health") == "offline":
            recover_device_from_telemetry(device, reported_at)

        for fault in faults:
            existing = next(
                (
                    event
                    for event in EVENTS
                    if event.get("eventType") == "sensor_fault"
                    and event.get("deviceId") == device["id"]
                    and event.get("faultCode") == fault["code"]
                    and event.get("status") == "open"
                ),
                None,
            )
            if fault["status"] == "active" and existing is None:
                event = {
                    "id": f"EV-SENSOR-{secrets.token_hex(12).upper()}",
                    "siteId": device["siteId"],
                    "assetId": device["assetId"],
                    "deviceId": device["id"],
                    "severity": fault["severity"],
                    "eventType": "sensor_fault",
                    "faultCode": fault["code"],
                    "title": f"{device['id']} sensor fault: {fault['code']}",
                    "occurredAt": fault["occurredAt"],
                    "time": fault["occurredAt"],
                    "duration": "active",
                    "durationSec": 0,
                    "score": 0,
                    "maxScore": 0,
                    "label": "sensor_issue",
                    "note": fault["detail"] or "Device reported a sensor fault.",
                    "reviewed": False,
                    "status": "open",
                    "thresholdVersion": "sensor-health-v1",
                    "ruleSnapshot": {
                        "version": "sensor-health-v1",
                        "type": "device_sensor_health",
                        "faultCode": fault["code"],
                        "source": "device-self-report",
                        "assetEventExcluded": True,
                    },
                    "modelVersion": "device-self-report",
                    "classification": "sensor_fault",
                    "assetEventExcluded": True,
                }
                EVENTS.insert(0, event)
                _freeze_event_evidence(event, creating=True)
                get_site(device["siteId"])["eventCount"] += 1
            elif fault["status"] == "active" and existing is not None:
                existing["severity"] = fault["severity"]
                if fault["detail"]:
                    existing["note"] = fault["detail"]
            elif fault["status"] == "recovered" and existing is not None:
                existing["status"] = "closed"
                existing["endAt"] = fault["occurredAt"]
                existing["endReason"] = "sensor_recovered"
                started = parse_rfc3339("occurredAt", existing["occurredAt"])
                ended = parse_rfc3339("endAt", existing["endAt"])
                existing["durationSec"] = max(0, int((ended - started).total_seconds()))
                existing["duration"] = f"{existing['durationSec']}s"

            if fault["status"] == "active" or existing is not None:
                _apply_sensor_health_boundary(device, fault)

        active_faults = [
            {
                "eventId": event["id"],
                "code": event["faultCode"],
                "severity": event["severity"],
                "occurredAt": event["occurredAt"],
            }
            for event in EVENTS
            if event.get("eventType") == "sensor_fault"
            and event.get("deviceId") == device["id"]
            and event.get("status") == "open"
        ]
        device["activeSensorFaults"] = active_faults
        device["sensorHealth"] = "fault" if active_faults else "healthy"
        device["updatedAt"] = reported_at
        return device_health_for(device["id"])


def dashboard_sites_summary(
    user: dict[str, Any],
    region: str = "",
    status: str = "",
    live_asset_statuses: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    # Device offline transitions belong to one summary transaction, regardless
    # of how many devices the caller can see.
    with STORE_LOCK:
        return _dashboard_sites_summary(user, region, status, live_asset_statuses)


def _dashboard_sites_summary(
    user: dict[str, Any],
    region: str,
    status: str,
    live_asset_statuses: dict[str, str] | None,
) -> list[dict[str, Any]]:
    require_permission(user, "dashboard:read")
    rows = []
    for site in visible_sites_for_user(user):
        if region and str(site.get("region", "")).lower() != region.lower():
            continue
        site_devices = [device for device in DEVICES if device["siteId"] == site["id"]]
        health_rows = [device_health_for(device["id"]) for device in site_devices]
        online_devices = sum(
            1 for device in health_rows if device["health"] != "offline"
        )
        site_assets = [asset for asset in ASSETS if asset["siteId"] == site["id"]]
        site_events = [event for event in EVENTS if event["siteId"] == site["id"]]
        asset_statuses = {asset["id"]: "normal" for asset in site_assets}
        severity_rank = {"normal": 0, "warning": 1, "critical": 2}
        for event in site_events:
            if event.get("label") in NON_ASSET_ANOMALY_EVENT_LABELS:
                continue
            severity = str(event.get("severity") or "")
            asset_id = event.get("assetId")
            if asset_id in asset_statuses and severity in severity_rank:
                if severity_rank[severity] > severity_rank[asset_statuses[asset_id]]:
                    asset_statuses[asset_id] = severity
        for asset_id, live_status in (live_asset_statuses or {}).items():
            if asset_id in asset_statuses and live_status in severity_rank:
                if severity_rank[live_status] > severity_rank[asset_statuses[asset_id]]:
                    asset_statuses[asset_id] = live_status
        critical_asset_ids = {
            asset_id
            for asset_id, asset_status in asset_statuses.items()
            if asset_status == "critical"
        }
        warning_asset_ids = {
            asset_id
            for asset_id, asset_status in asset_statuses.items()
            if asset_status == "warning"
        }
        normal_assets = sum(
            1 for asset_status in asset_statuses.values() if asset_status == "normal"
        )
        summary_status = (
            "critical"
            if critical_asset_ids
            else (
                "warning"
                if warning_asset_ids or online_devices < len(site_devices)
                else "normal"
            )
        )
        if status and summary_status != status.lower():
            continue
        received_times = [
            str(record["receivedAt"])
            for record in TELEMETRY_RECORDS
            if record["siteId"] == site["id"]
        ] + [
            str(device.get("lastReceivedAt"))
            for device in site_devices
            if device.get("lastReceivedAt")
        ]
        rows.append(
            {
                "siteId": site["id"],
                "siteName": site["name"],
                "region": site["region"],
                "status": summary_status,
                "totalDevices": len(site_devices),
                "onlineDevices": online_devices,
                "normalAssets": normal_assets,
                "warningAssets": len(warning_asset_ids),
                "criticalAssets": len(critical_asset_ids),
                "unreviewedEvents": sum(
                    1 for event in site_events if event.get("label") == "needs_review"
                ),
                "lastReceivedAt": max(received_times) if received_times else None,
            }
        )
    return rows


def service_health_dependencies() -> dict[str, Any]:
    with STORE_LOCK:
        dependencies = copy_payload(SERVICE_DEPENDENCIES)
        degraded = any(
            item["status"] not in {"healthy", "ready"} for item in dependencies
        )
        return {
            "status": "degraded" if degraded else "healthy",
            "checkedAt": now_iso(),
            "dependencies": dependencies,
            "ingestMetrics": copy_payload(TELEMETRY_METRICS),
            "quarantinedMessageCount": len(QUARANTINED_DEVICE_MESSAGES),
            "events": copy_payload(SERVICE_HEALTH_EVENTS[-20:]),
        }


def report_service_dependency(
    principal: dict[str, Any], dependency_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    permissions = set(principal.get("permissions", []))
    if "*" not in permissions and "service-health:write" not in permissions:
        raise ApiError(
            403,
            "SERVICE_HEALTH_WRITE_FORBIDDEN",
            "The token cannot update service dependency health.",
        )
    allowed_dependency_ids = principal.get("allowedDependencyIds", ["*"])
    if (
        "*" not in allowed_dependency_ids
        and dependency_id not in allowed_dependency_ids
    ):
        raise ApiError(
            403,
            "SERVICE_HEALTH_WRITE_FORBIDDEN",
            "The token cannot update this service dependency.",
        )

    status = str(payload.get("status") or "").strip().lower()
    if status not in {"healthy", "degraded"}:
        raise ApiError(
            400,
            "INVALID_SERVICE_HEALTH",
            "status must be healthy or degraded.",
        )
    detail_value = payload.get("detail")
    if detail_value is not None and not isinstance(detail_value, str):
        raise ApiError(
            400,
            "INVALID_SERVICE_HEALTH",
            "detail must be a string or null.",
        )
    detail = str(detail_value or "").strip() or None
    if detail and len(detail) > 500:
        raise ApiError(
            400,
            "INVALID_SERVICE_HEALTH",
            "detail must be 500 characters or fewer.",
        )
    error_code_value = payload.get("errorCode")
    if error_code_value is not None and not isinstance(error_code_value, str):
        raise ApiError(
            400,
            "INVALID_SERVICE_HEALTH",
            "errorCode must be a string or null.",
        )
    error_code = str(error_code_value or "").strip() or None

    with STORE_LOCK:
        dependency = next(
            (item for item in SERVICE_DEPENDENCIES if item["id"] == dependency_id),
            None,
        )
        if dependency is None:
            raise ApiError(
                404,
                "SERVICE_DEPENDENCY_NOT_FOUND",
                "Service dependency was not found.",
            )
        previous_status = str(dependency["status"])
        checked_at = now_iso()
        dependency["statusReportCount"] = (
            int(dependency.get("statusReportCount", 0)) + 1
        )
        if status == "degraded":
            dependency["failureCount"] = int(dependency.get("failureCount", 0)) + 1
        dependency["errorRatePct"] = round(
            int(dependency.get("failureCount", 0))
            / int(dependency["statusReportCount"])
            * 100,
            2,
        )
        dependency["status"] = status
        dependency["lastCheckedAt"] = checked_at
        dependency["detail"] = detail
        if status == "degraded":
            dependency["lastFailureAt"] = checked_at
            SERVICE_HEALTH_EVENTS.append(
                {
                    "dependencyId": dependency_id,
                    "status": "failure",
                    "occurredAt": checked_at,
                    "impactScope": dependency["impactScope"],
                    "errorCode": error_code,
                    "detail": detail,
                }
            )
        elif previous_status == "degraded":
            dependency["lastRecoveryAt"] = checked_at
            SERVICE_HEALTH_EVENTS.append(
                {
                    "dependencyId": dependency_id,
                    "status": "recovered",
                    "occurredAt": checked_at,
                    "impactScope": dependency["impactScope"],
                    "detail": detail,
                }
            )
        del SERVICE_HEALTH_EVENTS[:-100]
        return copy_payload(dependency)


def record_runtime_dependency(
    dependency_id: str,
    *,
    success: bool,
    latency_ms: float,
    error_code: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Record real execution results for backend-owned dependencies."""

    with STORE_LOCK:
        dependency = next(
            (item for item in SERVICE_DEPENDENCIES if item["id"] == dependency_id),
            None,
        )
        if dependency is None:
            raise ApiError(
                404,
                "SERVICE_DEPENDENCY_NOT_FOUND",
                "Service dependency was not found.",
            )
        previous_status = str(dependency.get("status") or "ready")
        checked_at = now_iso()
        dependency["statusReportCount"] = (
            int(dependency.get("statusReportCount", 0)) + 1
        )
        if not success:
            dependency["failureCount"] = int(dependency.get("failureCount", 0)) + 1
        dependency["latencyMs"] = round(max(0.0, float(latency_ms)), 2)
        dependency["errorRatePct"] = round(
            int(dependency.get("failureCount", 0))
            / int(dependency["statusReportCount"])
            * 100,
            2,
        )
        dependency["status"] = "healthy" if success else "degraded"
        dependency["lastCheckedAt"] = checked_at
        dependency["detail"] = str(detail).strip()[:500] if detail else None
        if not success:
            dependency["lastFailureAt"] = checked_at
            SERVICE_HEALTH_EVENTS.append(
                {
                    "dependencyId": dependency_id,
                    "status": "failure",
                    "occurredAt": checked_at,
                    "impactScope": dependency["impactScope"],
                    "errorCode": error_code,
                    "detail": dependency["detail"],
                }
            )
        elif previous_status not in {"healthy", "ready"}:
            dependency["lastRecoveryAt"] = checked_at
            SERVICE_HEALTH_EVENTS.append(
                {
                    "dependencyId": dependency_id,
                    "status": "recovered",
                    "occurredAt": checked_at,
                    "impactScope": dependency["impactScope"],
                    "detail": dependency["detail"],
                }
            )
        del SERVICE_HEALTH_EVENTS[:-100]
        return copy_payload(dependency)


def connectivity_number(
    payload: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    if key not in payload or payload[key] in (None, ""):
        raise ApiError(
            400,
            "INVALID_CONNECTIVITY_TEST",
            f"{key} is required.",
        )
    value = parse_float_value(key, payload.get(key))
    if not minimum <= value <= maximum:
        raise ApiError(
            400,
            "INVALID_CONNECTIVITY_METRIC",
            f"{key} must be between {minimum:g} and {maximum:g}.",
        )
    return value


def create_connectivity_test(
    user: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "connectivity-test:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        tested_at = format_rfc3339(
            parse_rfc3339(
                "testedAt", payload.get("testedAt"), "INVALID_CONNECTIVITY_TEST"
            )
        )
        phase = str(payload.get("phase") or "").strip().lower()
        if phase not in CONNECTIVITY_TEST_PHASES:
            raise ApiError(
                400,
                "INVALID_CONNECTIVITY_TEST",
                "phase must be before or after.",
            )
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict not in CONNECTIVITY_TEST_VERDICTS:
            raise ApiError(
                400,
                "INVALID_CONNECTIVITY_TEST",
                "verdict must be pass, warn, or fail.",
            )
        record = {
            "id": f"NET-TEST-{len(CONNECTIVITY_TEST_RECORDS) + 1:05d}",
            "deviceId": device["id"],
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "testedAt": tested_at,
            "phase": phase,
            "rssiDbm": connectivity_number(payload, "rssiDbm", -120, 0),
            "packetLossPct": connectivity_number(payload, "packetLossPct", 0, 100),
            "retryRatePct": connectivity_number(payload, "retryRatePct", 0, 100),
            "latencyMs": connectivity_number(payload, "latencyMs", 0, 60000),
            "verdict": verdict,
            "note": str(payload.get("note") or "").strip(),
            "createdAt": now_iso(),
        }
        CONNECTIVITY_TEST_RECORDS.append(record)
        device["rssiDbm"] = record["rssiDbm"]
        device["connectivityVerdict"] = verdict
        device["updatedAt"] = record["createdAt"]
        return copy_payload(record)


def connectivity_tests_for_device(
    user: dict[str, Any],
    device_id: str,
    *,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    phase: str = "",
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    require_permission(user, "connectivity-test:read")
    device = get_device(device_id)
    require_site_access(user, device["siteId"])
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    normalized_phase = phase.strip().lower()
    if normalized_phase and normalized_phase not in CONNECTIVITY_TEST_PHASES:
        raise ApiError(400, "INVALID_QUERY_PARAMETER", "phase must be before or after.")
    rows = []
    for record in CONNECTIVITY_TEST_RECORDS:
        if record["deviceId"] != device_id:
            continue
        tested_at = parse_rfc3339("testedAt", record["testedAt"])
        if from_value and tested_at < from_value:
            continue
        if to_value and tested_at > to_value:
            continue
        if normalized_phase and record["phase"] != normalized_phase:
            continue
        rows.append(record)
    rows.sort(key=lambda item: item["testedAt"], reverse=True)
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": copy_payload(rows[start : start + size]),
        "page": page,
        "size": size,
        "total": total,
        "latest": copy_payload(rows[0]) if rows else None,
    }


def hardware_profile(profile_id: str) -> dict[str, Any]:
    normalized = str(profile_id).strip().upper()
    profile = next(
        (item for item in HARDWARE_PROFILES if item["id"] == normalized), None
    )
    if not profile:
        raise ApiError(
            404, "HARDWARE_PROFILE_NOT_FOUND", "Hardware profile was not found."
        )
    return profile


def hardware_profiles(
    *, active: bool | None = None, board_type: str = "", connectivity_type: str = ""
) -> list[dict[str, Any]]:
    rows = HARDWARE_PROFILES
    if active is not None:
        rows = [item for item in rows if bool(item["active"]) is active]
    if board_type:
        rows = [
            item
            for item in rows
            if item["boardType"].lower() == board_type.strip().lower()
        ]
    if connectivity_type:
        rows = [
            item
            for item in rows
            if item["connectivityType"].lower() == connectivity_type.strip().lower()
        ]
    return copy_payload(rows)


def update_device_hardware_profile(
    user: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "hardware-profile:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        profile_id = required_text(payload, "profileId").upper()
        next_profile = hardware_profile(profile_id)
        if not next_profile["active"]:
            raise ApiError(
                409, "HARDWARE_PROFILE_INACTIVE", "Hardware profile is inactive."
            )
        replaced_components = [
            component.lower()
            for component in string_list(payload, "replacedComponents", required=True)
        ]
        invalid_components = sorted(set(replaced_components) - HARDWARE_COMPONENTS)
        if invalid_components:
            raise ApiError(
                400,
                "INVALID_HARDWARE_COMPONENT",
                "Unsupported replacedComponents: " + ", ".join(invalid_components),
            )
        reason = required_text(payload, "reason")
        effective_at = format_rfc3339(
            parse_rfc3339(
                "effectiveAt",
                payload.get("effectiveAt"),
                "INVALID_HARDWARE_REPLACEMENT",
            )
        )
        previous_profile_id = str(
            device.get("hardwareProfileId") or "HW-ESP32S3-ADXL345-INMP441-WIFI"
        )
        changed_at = now_iso()
        device["hardwareProfileId"] = profile_id
        device.setdefault("hardwareProfileHistory", []).append(
            {
                "fromProfileId": previous_profile_id,
                "toProfileId": profile_id,
                "replacedComponents": list(dict.fromkeys(replaced_components)),
                "reason": reason,
                "effectiveAt": effective_at,
                "changedAt": changed_at,
                "assetId": device["assetId"],
            }
        )
        device["updatedAt"] = changed_at
        return {
            "deviceId": device["id"],
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "profileId": profile_id,
            "profile": copy_payload(next_profile),
            "hardwareProfileHistory": copy_payload(device["hardwareProfileHistory"]),
            "updatedAt": changed_at,
        }


def inject_anomaly(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("APP_ENV", "").strip().lower() == "production":
        raise ApiError(
            403, "DEMO_DISABLED", "Demo injection is disabled in production."
        )
    site_id = str(payload.get("siteId") or "SITE-01").strip().upper()
    with STORE_LOCK:
        site = get_site(site_id)
        asset_id = (
            str(payload.get("assetId") or assets_for(site["id"])[0]["id"])
            .strip()
            .upper()
        )
        asset = get_asset(site["id"], asset_id)
        rule = anomaly_rule_for(asset["id"])
        duration_sec = int(rule["durationSec"])
        demo_device_id = f"DEMO-{asset['id']}"
        existing_sequences = [
            int(record["sequence"])
            for record in DEMO_TELEMETRY_RECORDS
            if record["deviceId"] == demo_device_id
        ]
        samples = build_combined_anomaly_samples(
            site_id=site["id"],
            asset_id=asset["id"],
            device_id=demo_device_id,
            rated_rpm=float(asset["ratedRpm"]),
            duration_sec=duration_sec,
            first_sequence=max(existing_sequences, default=0) + 1,
            end_at=datetime.now(timezone.utc),
        )
        DEMO_TELEMETRY_RECORDS.extend(samples)
        demo_asset_records = [
            record
            for record in DEMO_TELEMETRY_RECORDS
            if record["siteId"] == site["id"] and record["assetId"] == asset["id"]
        ]
        overflow = len(demo_asset_records) - DEMO_TELEMETRY_MAX_RECORDS_PER_ASSET
        for expired_record in demo_asset_records[: max(0, overflow)]:
            DEMO_TELEMETRY_RECORDS.remove(expired_record)
        # Alert delivery history is durable even when the demo store restarts.
        event_id = f"EV-{secrets.token_hex(12).upper()}"
        event = {
            "id": event_id,
            "siteId": site["id"],
            "assetId": asset["id"],
            "severity": "critical",
            "eventType": "asset_anomaly_candidate",
            "title": f"{asset['name']} anomaly injection event",
            "deviceId": demo_device_id,
            "occurredAt": samples[-1]["timestamp"],
            "time": samples[-1]["timestamp"],
            "duration": f"{duration_sec}s",
            "durationSec": duration_sec,
            "score": 94,
            "maxScore": 94,
            "label": "needs_review",
            "note": "This event was generated by the demo anomaly injection API.",
            "reviewed": False,
            "status": "open",
            "thresholdVersion": rule["version"],
            "modelVersion": DEMO_MODEL_VERSION,
            "isSynthetic": True,
            "source": DEMO_SIGNAL_SOURCE,
            "telemetrySampleCount": len(samples),
            "telemetryIntervalSec": max(
                1, math.ceil(duration_sec / (MAX_DEMO_ANOMALY_SAMPLES - 1))
            ),
            "scenarioLabel": "combined_anomaly",
        }
        _freeze_event_evidence(event, creating=True)
        EVENTS.insert(0, event)
        site["status"] = "critical"
        site["eventCount"] = int(site["eventCount"]) + 1
        return copy_payload(event)


def review_event(
    user: dict[str, Any], event_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "event:review")
    with STORE_LOCK:
        event = get_event(event_id)
        require_site_access(user, event["siteId"])
        label = str(payload.get("label", event["label"]))
        if label not in EVENT_LABELS:
            raise ApiError(400, "INVALID_EVENT_LABEL", "Event label is not allowed.")
        note = str(payload.get("note", event["note"]))
        if len(note) > MAX_REVIEW_NOTE_LENGTH:
            raise ApiError(
                400, "NOTE_TOO_LONG", "Review note must be 2000 characters or less."
            )
        reason = str(payload.get("reason") or "Event review updated").strip()
        if len(reason) > MAX_REASON_LENGTH:
            raise ApiError(
                400, "REASON_TOO_LONG", "Change reason must be 1000 characters or less."
            )
        before = {"label": event.get("label"), "note": event.get("note")}
        changed_at = now_iso()
        event["label"] = label
        event["note"] = note
        review_completed = label != "needs_review"
        event["reviewed"] = review_completed
        event["reviewedAt"] = changed_at if review_completed else None
        event["reviewedBy"] = user["id"] if review_completed else None
        review = {
            "id": f"REVIEW-{len(EVENT_REVIEW_HISTORY) + 1:05d}",
            "eventId": event["id"],
            "siteId": event["siteId"],
            "assetId": event["assetId"],
            "actor": {
                "id": user["id"],
                "name": user["name"],
                "role": user["role"],
            },
            "changedAt": changed_at,
            "reason": reason,
            "before": before,
            "after": {"label": label, "note": note},
        }
        EVENT_REVIEW_HISTORY.append(review)
        append_audit_log(
            user,
            "event.review",
            "event",
            event["id"],
            before,
            review["after"],
            reason,
            site_id=event["siteId"],
        )
        return {"event": copy_payload(event), "review": copy_payload(review)}


def _event_note_category(value: Any) -> str:
    category = str(value or "").strip().lower()
    if category not in EVENT_NOTE_CATEGORIES:
        raise ApiError(
            400,
            "INVALID_EVENT_NOTE_CATEGORY",
            "category must be root_cause, inspection, or action.",
        )
    return category


def _event_note_attachments(payload: dict[str, Any]) -> list[str]:
    values = payload.get("attachmentRefs", [])
    if not isinstance(values, list) or len(values) > 20:
        raise ApiError(
            400,
            "INVALID_EVENT_NOTE_ATTACHMENT",
            "attachmentRefs must be a list of at most 20 references.",
        )
    refs = []
    for value in values:
        if not isinstance(value, str):
            raise ApiError(
                400,
                "INVALID_EVENT_NOTE_ATTACHMENT",
                "Each attachment reference must be a string.",
            )
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 1000
            or not re.fullmatch(
                r"(?:https|s3|file|survey|attachment)://[^\s]+", normalized
            )
        ):
            raise ApiError(
                400,
                "INVALID_EVENT_NOTE_ATTACHMENT",
                "Attachment references must use https, s3, file, survey, or attachment URI schemes.",
            )
        refs.append(normalized)
    return list(dict.fromkeys(refs))


def _event_note_text(payload: dict[str, Any]) -> str:
    value = payload.get("text")
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "MISSING_FIELD", "text is required.")
    text = value.strip()
    if len(text) > MAX_REVIEW_NOTE_LENGTH:
        raise ApiError(
            400, "NOTE_TOO_LONG", "Event note must be 2000 characters or less."
        )
    return text


def _event_note_history_entry(
    user: dict[str, Any], note: dict[str, Any], action: str, before: Any, after: Any
) -> dict[str, Any]:
    entry = {
        "id": f"NOTE-HISTORY-{len(EVENT_NOTE_HISTORY) + 1:06d}",
        "noteId": note["id"],
        "eventId": note["eventId"],
        "action": action,
        "version": note["version"],
        "author": {"id": user["id"], "name": user["name"], "role": user["role"]},
        "changedAt": note["updatedAt"],
        "before": copy_payload(before),
        "after": copy_payload(after),
    }
    EVENT_NOTE_HISTORY.append(entry)
    return entry


def create_event_note(
    user: dict[str, Any], event_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "event:review")
    unknown = set(payload) - {"category", "text", "attachmentRefs"}
    if unknown:
        raise ApiError(
            400,
            "INVALID_EVENT_NOTE",
            "Unknown event note fields: " + ", ".join(sorted(unknown)),
        )
    with STORE_LOCK:
        event = get_event(event_id)
        require_site_access(user, event["siteId"])
        timestamp = now_iso()
        note = {
            "id": f"NOTE-{secrets.token_hex(10).upper()}",
            "eventId": event["id"],
            "siteId": event["siteId"],
            "assetId": event["assetId"],
            "category": _event_note_category(payload.get("category")),
            "text": _event_note_text(payload),
            "attachmentRefs": _event_note_attachments(payload),
            "author": {
                "id": user["id"],
                "name": user["name"],
                "role": user["role"],
            },
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "deletedAt": None,
            "version": 1,
            "changeSequence": len(EVENT_NOTE_HISTORY) + 1,
        }
        EVENT_NOTES.append(note)
        _event_note_history_entry(user, note, "created", None, note)
        append_audit_log(
            user,
            "event-note.create",
            "eventNote",
            note["id"],
            None,
            note,
            "Event note created",
            site_id=event["siteId"],
        )
        return copy_payload(note)


def _event_note_for(event_id: str, note_id: str) -> dict[str, Any]:
    event = get_event(event_id)
    normalized = note_id.strip().upper()
    note = next(
        (
            item
            for item in EVENT_NOTES
            if item["eventId"] == event["id"] and item["id"] == normalized
        ),
        None,
    )
    if note is None:
        raise ApiError(404, "EVENT_NOTE_NOT_FOUND", "Event note was not found.")
    return note


def event_notes_for(user: dict[str, Any], event_id: str) -> dict[str, Any]:
    require_permission(user, "event:read")
    with STORE_LOCK:
        event = get_event(event_id)
        require_site_access(user, event["siteId"])
        history_order = {
            item["noteId"]: index for index, item in enumerate(EVENT_NOTE_HISTORY, 1)
        }
        rows = sorted(
            (
                copy_payload(item)
                for item in EVENT_NOTES
                if item["eventId"] == event["id"] and item.get("deletedAt") is None
            ),
            key=lambda item: (
                int(item.get("changeSequence", history_order.get(item["id"], 0))),
                item["updatedAt"],
                item["id"],
            ),
            reverse=True,
        )
        latest_by_category = {
            category: next(
                (item for item in rows if item["category"] == category), None
            )
            for category in sorted(EVENT_NOTE_CATEGORIES)
        }
        return {
            "eventId": event["id"],
            "items": rows,
            "latestByCategory": latest_by_category,
            "total": len(rows),
        }


def event_note_history_for(
    user: dict[str, Any], event_id: str, note_id: str
) -> dict[str, Any]:
    require_permission(user, "event:read")
    with STORE_LOCK:
        note = _event_note_for(event_id, note_id)
        require_site_access(user, note["siteId"])
        rows = [
            copy_payload(item)
            for item in EVENT_NOTE_HISTORY
            if item["noteId"] == note["id"]
        ]
        rows.sort(key=lambda item: int(item["version"]), reverse=True)
        return {"noteId": note["id"], "items": rows, "total": len(rows)}


def update_event_note(
    user: dict[str, Any], event_id: str, note_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "event:review")
    unknown = set(payload) - {"category", "text", "attachmentRefs", "reason"}
    if unknown:
        raise ApiError(
            400,
            "INVALID_EVENT_NOTE",
            "Unknown event note fields: " + ", ".join(sorted(unknown)),
        )
    if not {"category", "text", "attachmentRefs"}.intersection(payload):
        raise ApiError(
            400, "INVALID_EVENT_NOTE", "At least one note field must be updated."
        )
    reason = str(payload.get("reason") or "Event note updated").strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400,
            "INVALID_EVENT_NOTE",
            "reason must be between 1 and 1000 characters.",
        )
    with STORE_LOCK:
        note = _event_note_for(event_id, note_id)
        require_site_access(user, note["siteId"])
        if note.get("deletedAt") is not None:
            raise ApiError(409, "EVENT_NOTE_DELETED", "Deleted notes are immutable.")
        before = copy_payload(note)
        candidate = copy_payload(note)
        if "category" in payload:
            candidate["category"] = _event_note_category(payload["category"])
        if "text" in payload:
            candidate["text"] = _event_note_text(payload)
        if "attachmentRefs" in payload:
            candidate["attachmentRefs"] = _event_note_attachments(payload)
        note.update(candidate)
        note["updatedAt"] = now_iso()
        note["version"] = int(note["version"]) + 1
        note["changeSequence"] = len(EVENT_NOTE_HISTORY) + 1
        note["author"] = {
            "id": user["id"],
            "name": user["name"],
            "role": user["role"],
        }
        _event_note_history_entry(user, note, "updated", before, note)
        append_audit_log(
            user,
            "event-note.update",
            "eventNote",
            note["id"],
            before,
            note,
            reason,
            site_id=note["siteId"],
        )
        return copy_payload(note)


def delete_event_note(
    user: dict[str, Any], event_id: str, note_id: str
) -> dict[str, Any]:
    require_permission(user, "event:review")
    with STORE_LOCK:
        note = _event_note_for(event_id, note_id)
        require_site_access(user, note["siteId"])
        if note.get("deletedAt") is not None:
            return {"deleted": True, "noteId": note["id"], "duplicate": True}
        before = copy_payload(note)
        note["deletedAt"] = now_iso()
        note["updatedAt"] = note["deletedAt"]
        note["version"] = int(note["version"]) + 1
        note["changeSequence"] = len(EVENT_NOTE_HISTORY) + 1
        _event_note_history_entry(user, note, "deleted", before, None)
        append_audit_log(
            user,
            "event-note.delete",
            "eventNote",
            note["id"],
            before,
            None,
            "Event note deleted",
            site_id=note["siteId"],
        )
        return {"deleted": True, "noteId": note["id"], "duplicate": False}


def baseline_payload(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    source = dict(existing or {})
    nested = payload.get("baseline")
    if isinstance(nested, dict):
        source.update(nested)
    field_map = {
        "baselineStatus": "status",
        "baselineCapturedAt": "capturedAt",
        "baselineVibrationRmsMmS": "vibrationRmsMmS",
        "baselineAcousticDb": "acousticDb",
        "baselineSampleCount": "sampleCount",
    }
    for payload_key, baseline_key in field_map.items():
        if payload_key in payload:
            source[baseline_key] = payload[payload_key]
    return {
        "status": str(source.get("status") or "draft"),
        "capturedAt": str(source.get("capturedAt") or ""),
        "vibrationRmsMmS": parse_float_value(
            "baseline.vibrationRmsMmS", source.get("vibrationRmsMmS"), 0.0
        ),
        "acousticDb": parse_float_value(
            "baseline.acousticDb", source.get("acousticDb"), 0.0
        ),
        "sampleCount": parse_int_value(
            "baseline.sampleCount", source.get("sampleCount"), 0
        ),
    }


def required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
    return value.strip()


def get_event(event_id: str) -> dict[str, Any]:
    normalized = event_id.strip().upper()
    # AI2 IDs include UUIDs, whose hexadecimal letters are lower-case. Match
    # without changing the durable ID referenced by checkpoints and evidence.
    event = next((item for item in EVENTS if item["id"].upper() == normalized), None)
    if not event:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event was not found.")
    return event


def append_audit_log(
    user: dict[str, Any],
    action: str,
    target_type: str,
    target_id: str,
    before: Any,
    after: Any,
    reason: str,
    *,
    site_id: str | None = None,
    site_ids: list[str] | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    normalized_site_ids = sorted(
        {str(value).strip().upper() for value in (site_ids or []) if str(value).strip()}
    )
    if site_id and site_id not in normalized_site_ids:
        normalized_site_ids.append(site_id)
        normalized_site_ids.sort()
    entry = {
        "id": f"AUDIT-{len(AUDIT_LOGS) + 1:06d}",
        "actor": {
            "id": user["id"],
            "name": user["name"],
            "role": user["role"],
        },
        "action": action,
        "targetType": target_type,
        "targetId": target_id,
        "siteId": site_id,
        "siteIds": normalized_site_ids,
        "before": copy_payload(before),
        "after": copy_payload(after),
        "reason": reason,
        "effectiveAt": effective_at,
        "changedAt": now_iso(),
    }
    AUDIT_LOGS.append(entry)
    return copy_payload(entry)


def event_reviews_for(
    user: dict[str, Any], event_id: str, *, page: int, size: int
) -> dict[str, Any]:
    require_permission(user, "event:read")
    event = get_event(event_id)
    require_site_access(user, event["siteId"])
    rows = sorted(
        (
            copy_payload(item)
            for item in EVENT_REVIEW_HISTORY
            if item["eventId"] == event["id"]
        ),
        key=lambda item: (
            parse_rfc3339("review.changedAt", item["changedAt"]),
            int(str(item["id"]).rsplit("-", 1)[-1]),
        ),
        reverse=True,
    )
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": rows[start : start + size],
        "page": page,
        "size": size,
        "total": total,
    }


def anomaly_rule_for(asset_id: str) -> dict[str, Any]:
    asset = get_asset_by_id(asset_id)
    rule = next(
        (item for item in ANOMALY_RULES if item["assetId"] == asset["id"]), None
    )
    if not rule:
        raise ApiError(404, "ANOMALY_RULE_NOT_FOUND", "Anomaly rule was not found.")
    return copy_payload(rule)


def anomaly_rule_version_for(asset_id: str, version: str) -> dict[str, Any]:
    rule = anomaly_rule_for(asset_id)
    current = {key: value for key, value in rule.items() if key != "history"}
    snapshots = [current, *rule.get("history", [])]
    snapshot = next(
        (item for item in snapshots if item.get("version") == version), None
    )
    if not snapshot:
        raise ApiError(
            409,
            "ANOMALY_RULE_VERSION_NOT_FOUND",
            "The anomaly rule version recorded by the event was not found.",
        )
    return copy_payload(snapshot)


def _freeze_event_evidence(
    event: dict[str, Any],
    points: list[dict[str, Any]] | None = None,
    *,
    creating: bool = False,
) -> dict[str, Any]:
    with STORE_LOCK:
        evidence = EVENT_EVIDENCE_SNAPSHOTS.setdefault(event["id"], {})
        if "installationSnapshot" not in evidence:
            from .installation_evidence import snapshot_for

            evidence["installationSnapshot"] = (
                snapshot_for(event)
                if creating
                else {
                    "status": "unavailable",
                    "source": "not_recorded_at_creation",
                    "points": [],
                }
            )

        if "deviceSnapshot" not in evidence:
            device_id = str(event.get("deviceId") or "").strip().upper()
            device = next(
                (
                    item
                    for item in DEVICES
                    if (device_id and item["id"] == device_id)
                    or (
                        not device_id
                        and item["assetId"] == event["assetId"]
                        and item.get("mappingStatus") == "active"
                    )
                ),
                None,
            )
            evidence["deviceSnapshot"] = copy_payload(device) if device else None
        occurred_at = parse_rfc3339("event.occurredAt", event["occurredAt"])
        if "featureSnapshot" not in evidence:
            if points is None:
                points = _event_evidence_points(
                    event,
                    from_timestamp=format_rfc3339(occurred_at - timedelta(minutes=5)),
                    to_timestamp=format_rfc3339(occurred_at + timedelta(minutes=5)),
                )
            feature_snapshot = (
                min(
                    points,
                    key=lambda point: (
                        abs(
                            (
                                parse_rfc3339("telemetry.timestamp", point["timestamp"])
                                - occurred_at
                            ).total_seconds()
                        ),
                        _telemetry_sort_key(point),
                    ),
                )
                if points
                else None
            )
            evidence["featureSnapshot"] = copy_payload(feature_snapshot)
        return copy_payload(evidence)


def _is_demo_event(event: dict[str, Any]) -> bool:
    return bool(event.get("isSynthetic")) and event.get("source") == DEMO_SIGNAL_SOURCE


def _event_evidence_points(
    event: dict[str, Any],
    *,
    from_timestamp: str,
    to_timestamp: str,
) -> list[dict[str, Any]]:
    """Return evidence from the event's own telemetry domain only.

    Dashboard charts may combine real and synthetic telemetry.  Event evidence
    must not: a physical-device event never inherits an AI-2 demo sample.
    """
    points = _stored_telemetry_for(
        event["siteId"],
        event["assetId"],
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        include_demo=_is_demo_event(event),
    )
    device_id = str(event.get("deviceId") or "").strip().upper()
    if device_id:
        points = [point for point in points if point.get("deviceId") == device_id]
    return points


def event_detail_for(user: dict[str, Any], event_id: str) -> dict[str, Any]:
    require_permission(user, "event:read")
    with STORE_LOCK:
        event = copy_payload(get_event(event_id))
    require_site_access(user, event["siteId"])
    occurred_at = parse_rfc3339("event.occurredAt", event["occurredAt"])
    context_from = format_rfc3339(occurred_at - timedelta(minutes=5))
    context_to = format_rfc3339(occurred_at + timedelta(minutes=5))
    points = _event_evidence_points(
        event,
        from_timestamp=context_from,
        to_timestamp=context_to,
    )
    is_demo = _is_demo_event(event)
    context_source = (
        "demo" if is_demo and points else "stored" if points else "unavailable"
    )
    _freeze_event_evidence(event, points)
    with STORE_LOCK:
        event_snapshot = copy_payload(get_event(event_id))
        latest_review = next(
            (
                copy_payload(item)
                for item in reversed(EVENT_REVIEW_HISTORY)
                if item["eventId"] == event_snapshot["id"]
            ),
            None,
        )
        evidence_snapshot = copy_payload(EVENT_EVIDENCE_SNAPSHOTS[event_snapshot["id"]])
    threshold_version = str(event_snapshot.get("thresholdVersion") or "").strip()
    if event_snapshot.get("eventType") == "sensor_fault":
        applied_rule = copy_payload(
            event_snapshot.get("ruleSnapshot")
            or {
                "version": "sensor-health-v1",
                "type": "device_sensor_health",
                "faultCode": event_snapshot.get("faultCode"),
                "source": "device-self-report",
                "assetEventExcluded": True,
            }
        )
    else:
        applied_rule = (
            anomaly_rule_version_for(event_snapshot["assetId"], threshold_version)
            if threshold_version
            else None
        )
    return {
        "event": event_snapshot,
        "context": {
            "from": context_from,
            "to": context_to,
            "points": copy_payload(points),
            "units": telemetry_units(
                event_snapshot["siteId"],
                event_snapshot["assetId"],
                include_demo=is_demo,
            ),
            "source": context_source,
            "rawDataMissing": not bool(points),
        },
        "featureSnapshot": evidence_snapshot["featureSnapshot"],
        "appliedRule": applied_rule,
        "modelVersion": event_snapshot.get("modelVersion"),
        "deviceSnapshot": evidence_snapshot["deviceSnapshot"],
        "installationSnapshot": evidence_snapshot["installationSnapshot"],
        "latestReview": latest_review,
    }


def _bounded_number(
    field: str, value: Any, minimum: float, maximum: float, *, integer: bool = False
) -> int | float:
    if (
        value is None
        or isinstance(value, bool)
        or (isinstance(value, str) and not value.strip())
    ):
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be a valid number.")
    parsed = parse_float_value(field, value)
    if parsed < minimum or parsed > maximum:
        raise ApiError(
            400,
            "VALUE_OUT_OF_RANGE",
            f"{field} must be between {minimum} and {maximum}.",
        )
    if integer:
        if not parsed.is_integer():
            raise ApiError(400, "INVALID_NUMBER", f"{field} must be an integer.")
        return int(parsed)
    return parsed


def update_anomaly_rule(
    user: dict[str, Any], asset_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "anomaly-rule:write")
    reason = required_text(payload, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400, "REASON_TOO_LONG", "reason must be 1000 characters or less."
        )
    with STORE_LOCK:
        asset = get_asset_by_id(asset_id)
        require_site_access(user, asset["siteId"])
        rule = next(
            (item for item in ANOMALY_RULES if item["assetId"] == asset["id"]),
            None,
        )
        new_rule = rule is None
        if rule is None:
            rule = build_anomaly_rules([asset])[0]
            rule["reason"] = "Initial anomaly rule for existing asset"
            rule["updatedAt"] = now_iso()
        before = copy_payload(
            {key: value for key, value in rule.items() if key != "history"}
        )
        score_threshold = _bounded_number(
            "scoreThreshold",
            payload.get("scoreThreshold", rule["scoreThreshold"]),
            0,
            100,
        )
        duration_sec = _bounded_number(
            "durationSec",
            payload.get("durationSec", rule["durationSec"]),
            1,
            3600,
            integer=True,
        )
        hysteresis = _bounded_number(
            "hysteresis",
            payload.get("hysteresis", rule["hysteresis"]),
            0,
            50,
        )
        merge_window_sec = _bounded_number(
            "mergeWindowSec",
            payload.get("mergeWindowSec", rule["mergeWindowSec"]),
            0,
            3600,
            integer=True,
        )
        active = boolean_field(payload, "active", default=rule["active"])
        if hysteresis >= score_threshold:
            raise ApiError(
                400,
                "INVALID_HYSTERESIS",
                "hysteresis must be lower than scoreThreshold.",
            )
        signal_baselines = rule.get("signalBaselines")
        if "signalBaselines" in payload:
            from .signal_rules import bind_baselines

            signal_baselines = bind_baselines(user, asset, payload["signalBaselines"])
        rule["history"].append(before)
        rule.update(
            {
                "version": f"RULE-{asset['id']}-v{len(rule['history']) + 1}",
                "scoreThreshold": score_threshold,
                "durationSec": duration_sec,
                "hysteresis": hysteresis,
                "mergeWindowSec": merge_window_sec,
                "active": active,
                "signalBaselines": signal_baselines,
                "reason": reason,
                "updatedAt": now_iso(),
            }
        )
        if new_rule:
            ANOMALY_RULES.append(rule)
        after = copy_payload(
            {key: value for key, value in rule.items() if key != "history"}
        )
        append_audit_log(
            user,
            "anomaly-rule.update",
            "anomalyRule",
            asset["id"],
            before,
            after,
            reason,
            site_id=asset["siteId"],
        )
        return copy_payload(rule)


def _alert_policy_site_ids(
    policy: dict[str, Any], *, use_stored_scope: bool = True
) -> list[str]:
    site_ids = {str(value).strip().upper() for value in policy.get("siteIds", [])}
    stored_site_ids = {
        str(value).strip().upper() for value in policy.get("scopeSiteIds", [])
    }
    if use_stored_scope and "scopeSiteIds" in policy:
        return sorted(site_ids | stored_site_ids)
    for asset_id in policy.get("assetIds", []):
        normalized_asset_id = str(asset_id).strip().upper()
        asset = next(
            (item for item in ASSETS if item["id"] == normalized_asset_id), None
        )
        if asset:
            site_ids.add(asset["siteId"])
            continue
        matching_site = next(
            (
                site["id"]
                for site in SITES
                if normalized_asset_id.startswith(f"{site['id']}-")
            ),
            None,
        )
        if not matching_site:
            raise ApiError(
                400,
                "INVALID_ALERT_POLICY_SCOPE",
                f"Cannot determine a site scope for assetId {normalized_asset_id}.",
            )
        site_ids.add(matching_site)
    return sorted(site_ids)


def alert_policies_for(user: dict[str, Any]) -> list[dict[str, Any]]:
    require_permission(user, "alert-policy:read")
    allowed = set(user.get("allowedSiteIds", []))
    rows = []
    for policy in ALERT_POLICIES:
        site_ids = set(_alert_policy_site_ids(policy))
        if "*" in allowed or not site_ids or site_ids.issubset(allowed):
            rows.append(copy_payload(policy))
    return rows


def _time_of_day(field: str, value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ApiError(400, "INVALID_TIME_OF_DAY", f"{field} must use HH:MM.") from exc
    return parsed.strftime("%H:%M")


def update_alert_policy(
    user: dict[str, Any], policy_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "alert-policy:write")
    reason = str(payload.get("reason") or "Alert policy updated").strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400, "REASON_TOO_LONG", "reason must be 1000 characters or less."
        )
    with STORE_LOCK:
        policy = next(
            (item for item in ALERT_POLICIES if item["id"] == policy_id), None
        )
        if not policy:
            raise ApiError(404, "ALERT_POLICY_NOT_FOUND", "Alert policy was not found.")
        before = copy_payload(policy)
        before_site_ids = _alert_policy_site_ids(before)
        for site_id in before_site_ids:
            require_site_access(user, site_id)
        severity = str(payload.get("severity", policy["severity"])).strip().lower()
        if severity not in EVENT_SEVERITIES - {"normal", "device"}:
            raise ApiError(
                400, "INVALID_SEVERITY", "severity must be warning or critical."
            )
        site_ids = (
            string_list(payload, "siteIds", uppercase=True)
            if "siteIds" in payload
            else copy_payload(policy["siteIds"])
        )
        asset_ids = (
            string_list(payload, "assetIds", uppercase=True)
            if "assetIds" in payload
            else copy_payload(policy["assetIds"])
        )
        for site_id in site_ids:
            get_site(site_id)
            require_site_access(user, site_id)
        for asset_id in asset_ids:
            asset = get_asset_by_id(asset_id)
            require_site_access(user, asset["siteId"])
            if site_ids and asset["siteId"] not in site_ids:
                raise ApiError(
                    400,
                    "ASSET_SITE_MISMATCH",
                    "assetIds must belong to the selected siteIds.",
                )
        recipients = (
            string_list(payload, "recipients", required=True)
            if "recipients" in payload
            else copy_payload(policy["recipients"])
        )
        channels = (
            string_list(payload, "channels", required=True)
            if "channels" in payload
            else copy_payload(policy["channels"])
        )
        if any(channel not in ALERT_CHANNELS for channel in channels):
            raise ApiError(
                400, "INVALID_ALERT_CHANNEL", "An alert channel is not supported."
            )
        work_hours = payload.get("workHours", policy["workHours"])
        if not isinstance(work_hours, dict):
            raise ApiError(400, "INVALID_WORK_HOURS", "workHours must be an object.")
        normalized_work_hours = {
            "start": _time_of_day("workHours.start", work_hours.get("start")),
            "end": _time_of_day("workHours.end", work_hours.get("end")),
        }
        policy.update(
            {
                "severity": severity,
                "siteIds": site_ids,
                "assetIds": asset_ids,
                "workHours": normalized_work_hours,
                "recipients": recipients,
                "channels": channels,
                "cooldownSec": _bounded_number(
                    "cooldownSec",
                    payload.get("cooldownSec", policy["cooldownSec"]),
                    0,
                    86400,
                    integer=True,
                ),
                "enabled": boolean_field(payload, "enabled", default=policy["enabled"]),
                "updatedAt": now_iso(),
            }
        )
        scope_changed = "siteIds" in payload or "assetIds" in payload
        after_site_ids = _alert_policy_site_ids(
            policy, use_stored_scope=not scope_changed
        )
        policy["scopeSiteIds"] = after_site_ids
        append_audit_log(
            user,
            "alert-policy.update",
            "alertPolicy",
            policy["id"],
            before,
            policy,
            reason,
            site_ids=sorted(set(before_site_ids) | set(after_site_ids)),
        )
        return copy_payload(policy)


def parameters_for(user: dict[str, Any], category: str = "") -> list[dict[str, Any]]:
    require_permission(user, "parameter:read")
    normalized_category = category.strip().lower()
    rows = []
    for key, definition in PARAMETER_DEFINITIONS.items():
        if normalized_category and definition["category"] != normalized_category:
            continue
        rows.append({"key": key, "value": PARAMETERS[key], **copy_payload(definition)})
    return rows


def update_parameter(
    user: dict[str, Any], key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "parameter:write")
    normalized_key = key.strip().upper()
    definition = PARAMETER_DEFINITIONS.get(normalized_key)
    if not definition:
        raise ApiError(404, "PARAMETER_NOT_FOUND", "Parameter was not found.")
    if definition.get("mutable") is False:
        raise ApiError(
            409,
            "PARAMETER_NOT_RUNTIME_MUTABLE",
            f"{normalized_key} is managed by {definition.get('managedBy', 'another runtime')}.",
        )
    if "value" not in payload:
        raise ApiError(400, "MISSING_FIELD", "value is required.")
    reason = required_text(payload, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400, "REASON_TOO_LONG", "reason must be 1000 characters or less."
        )
    effective_at = str(payload.get("effectiveAt") or "").strip() or None
    if effective_at:
        parse_rfc3339("effectiveAt", effective_at)
    if definition["type"] == "boolean":
        if not isinstance(payload["value"], bool):
            raise ApiError(400, "INVALID_BOOLEAN", "value must be a boolean.")
        value: int | float | bool = payload["value"]
    else:
        value = _bounded_number(
            "value",
            payload["value"],
            float(definition["minimum"]),
            float(definition["maximum"]),
            integer=definition["type"] == "integer",
        )
    with STORE_LOCK:
        rule_field = {
            "ANOMALY_SCORE_THRESHOLD": "scoreThreshold",
            "ANOMALY_HOLD_SEC": "durationSec",
            "ANOMALY_HYSTERESIS": "hysteresis",
            "ANOMALY_ENABLED": "active",
        }.get(normalized_key)
        if rule_field:
            for rule in ANOMALY_RULES:
                next_threshold = (
                    float(value)
                    if rule_field == "scoreThreshold"
                    else float(rule["scoreThreshold"])
                )
                next_hysteresis = (
                    float(value)
                    if rule_field == "hysteresis"
                    else float(rule["hysteresis"])
                )
                if next_hysteresis >= next_threshold:
                    raise ApiError(
                        400,
                        "INVALID_HYSTERESIS",
                        "hysteresis must be lower than scoreThreshold for every rule.",
                    )
        before = PARAMETERS[normalized_key]
        PARAMETERS[normalized_key] = value
        updated_at = now_iso()
        if rule_field:
            for rule in ANOMALY_RULES:
                snapshot = copy_payload(
                    {key: item for key, item in rule.items() if key != "history"}
                )
                rule.setdefault("history", []).append(snapshot)
                rule[rule_field] = value
                rule["version"] = f"RULE-{rule['assetId']}-v{len(rule['history']) + 1}"
                rule["reason"] = reason
                rule["updatedAt"] = updated_at
            _LIFECYCLE_RUNTIMES.clear()
        audit = append_audit_log(
            user,
            "parameter.update",
            "parameter",
            normalized_key,
            before,
            value,
            reason,
            effective_at=effective_at,
        )
        return {
            "key": normalized_key,
            "value": value,
            **copy_payload(definition),
            "reason": reason,
            "effectiveAt": effective_at,
            "updatedAt": updated_at,
            "auditId": audit["id"],
        }


def audit_logs_for(
    user: dict[str, Any],
    *,
    action: str = "",
    target_type: str = "",
    actor_id: str = "",
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    page: int,
    size: int,
) -> dict[str, Any]:
    require_permission(user, "audit-log:read")
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    allowed = user.get("allowedSiteIds", [])
    rows = []
    for entry in AUDIT_LOGS:
        entry_site_ids = set(entry.get("siteIds") or [])
        if entry.get("siteId"):
            entry_site_ids.add(entry["siteId"])
        if (
            entry_site_ids
            and "*" not in allowed
            and not entry_site_ids.issubset(set(allowed))
        ):
            continue
        if action and entry["action"] != action:
            continue
        if target_type and entry["targetType"] != target_type:
            continue
        if actor_id and entry["actor"]["id"] != actor_id:
            continue
        changed_at = parse_rfc3339("audit.changedAt", entry["changedAt"])
        if from_value and changed_at < from_value:
            continue
        if to_value and changed_at > to_value:
            continue
        rows.append(copy_payload(entry))
    rows.sort(key=lambda item: item["changedAt"], reverse=True)
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": rows[start : start + size],
        "page": page,
        "size": size,
        "total": total,
    }


def _inspection_status(field: str, value: Any) -> str:
    status = str(value or "").strip().lower()
    if status not in ENVIRONMENT_INSPECTION_STATUSES:
        allowed = ", ".join(sorted(ENVIRONMENT_INSPECTION_STATUSES))
        raise ApiError(
            400, "INVALID_INSPECTION_STATUS", f"{field} must be one of {allowed}."
        )
    return status


def create_environment_inspection(
    user: dict[str, Any], device_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "environment-inspection:write")
    inspected_at = required_text(payload, "inspectedAt")
    parse_rfc3339("inspectedAt", inspected_at)
    maintenance_action = str(payload.get("maintenanceAction") or "").strip() or None
    if maintenance_action and len(maintenance_action) > MAX_REVIEW_NOTE_LENGTH:
        raise ApiError(
            400,
            "MAINTENANCE_ACTION_TOO_LONG",
            "maintenanceAction must be 2000 characters or less.",
        )
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        record = {
            "id": f"ENV-{len(ENVIRONMENT_INSPECTIONS) + 1:06d}",
            "deviceId": device["id"],
            "siteId": device["siteId"],
            "assetId": device["assetId"],
            "inspectedAt": inspected_at,
            "dust": _inspection_status("dust", payload.get("dust")),
            "waterIngress": _inspection_status(
                "waterIngress", payload.get("waterIngress")
            ),
            "saltCorrosion": _inspection_status(
                "saltCorrosion", payload.get("saltCorrosion")
            ),
            "glandStatus": _inspection_status(
                "glandStatus", payload.get("glandStatus")
            ),
            "enclosureStatus": _inspection_status(
                "enclosureStatus", payload.get("enclosureStatus")
            ),
            "maintenanceAction": maintenance_action,
            "inspector": {"id": user["id"], "name": user["name"]},
            "createdAt": now_iso(),
        }
        statuses = [
            record["dust"],
            record["waterIngress"],
            record["saltCorrosion"],
            record["glandStatus"],
            record["enclosureStatus"],
        ]
        record["overallStatus"] = (
            "critical"
            if "critical" in statuses
            else (
                "attention"
                if "attention" in statuses
                else "not_checked" if "not_checked" in statuses else "ok"
            )
        )
        ENVIRONMENT_INSPECTIONS.append(record)
        previous_inspection_at = str(device.get("lastEnvironmentInspectionAt") or "")
        if not previous_inspection_at or parse_rfc3339(
            "lastEnvironmentInspectionAt", previous_inspection_at
        ) <= parse_rfc3339("inspectedAt", inspected_at):
            device["environmentStatus"] = record["overallStatus"]
            device["lastEnvironmentInspectionAt"] = inspected_at
        if maintenance_action:
            device.setdefault("maintenanceHistory", []).append(
                {
                    "inspectionId": record["id"],
                    "action": maintenance_action,
                    "recordedAt": record["createdAt"],
                    "recordedBy": user["id"],
                }
            )
        append_audit_log(
            user,
            "environment-inspection.create",
            "device",
            device["id"],
            None,
            record,
            maintenance_action or "Environment inspection recorded",
            site_id=device["siteId"],
        )
        return copy_payload(record)


def environment_inspections_for(
    user: dict[str, Any],
    device_id: str,
    *,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    page: int,
    size: int,
) -> dict[str, Any]:
    require_permission(user, "environment-inspection:read")
    device = get_device(device_id)
    require_site_access(user, device["siteId"])
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    rows = []
    for item in ENVIRONMENT_INSPECTIONS:
        if item["deviceId"] != device["id"]:
            continue
        inspected_at = parse_rfc3339("inspectedAt", item["inspectedAt"])
        if from_value and inspected_at < from_value:
            continue
        if to_value and inspected_at > to_value:
            continue
        rows.append(copy_payload(item))
    rows.sort(
        key=lambda item: (
            parse_rfc3339("inspectedAt", item["inspectedAt"]),
            int(str(item["id"]).rsplit("-", 1)[-1]),
        ),
        reverse=True,
    )
    total = len(rows)
    start = (page - 1) * size
    return {
        "latest": rows[0] if rows else None,
        "items": rows[start : start + size],
        "page": page,
        "size": size,
        "total": total,
    }


def _sensor_fault_record(
    device: dict[str, Any],
    fault_type: str,
    severity: str,
    detail: str,
    detected_at: str,
) -> dict[str, Any]:
    return {
        "id": f"FAULT-{device['id']}-{fault_type.upper()}",
        "deviceId": device["id"],
        "siteId": device["siteId"],
        "assetId": device["assetId"],
        "faultType": fault_type,
        "severity": severity,
        "status": "open",
        "detail": detail,
        "detectedAt": detected_at,
        "classification": "sensor_fault",
        "assetEventExcluded": True,
    }


def sensor_faults_for_device(
    user: dict[str, Any],
    device_id: str,
    *,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    status: str = "",
    fault_type: str = "",
    page: int,
    size: int,
) -> dict[str, Any]:
    require_permission(user, "sensor-fault:read")
    device = get_device(device_id)
    require_site_access(user, device["siteId"])
    detected_at = str(device.get("lastReceivedAt") or now_iso())
    rows: list[dict[str, Any]] = []
    if device.get("health") == "offline" or _device_elapsed_seconds(device) > int(
        PARAMETERS["DEVICE_OFFLINE_SEC"]
    ):
        rows.append(
            _sensor_fault_record(
                device,
                "no_signal",
                "critical",
                "Device telemetry is missing beyond the offline threshold.",
                detected_at,
            )
        )
    if int(device.get("rebootCount", 0)) >= 3:
        rows.append(
            _sensor_fault_record(
                device,
                "reboot_loop",
                "warning",
                "Repeated device reboots were observed.",
                detected_at,
            )
        )
    battery_pct = device.get("batteryPct")
    if battery_pct is not None and not isinstance(battery_pct, bool):
        if parse_float_value("device.batteryPct", battery_pct) <= 20:
            rows.append(
                _sensor_fault_record(
                    device,
                    "power",
                    "warning",
                    "Battery or power level is below the operating threshold.",
                    detected_at,
                )
            )
    samples = [item for item in TELEMETRY_RECORDS if item["deviceId"] == device["id"]][
        -5:
    ]
    if len(samples) >= 5:
        signatures = {
            (
                item.get("vibrationRmsRaw"),
                item.get("acousticRmsRaw"),
                item.get("rpm"),
            )
            for item in samples
        }
        if len(signatures) == 1:
            rows.append(
                _sensor_fault_record(
                    device,
                    "stuck_value",
                    "warning",
                    "The last five telemetry samples contain identical sensor values.",
                    samples[-1]["timestamp"],
                )
            )
        acoustic_values = [
            float(item["acousticRmsRaw"])
            for item in samples
            if item.get("acousticRmsRaw") is not None
        ]
        if acoustic_values and sum(acoustic_values) / len(acoustic_values) > 0.05:
            rows.append(
                _sensor_fault_record(
                    device,
                    "noise_floor",
                    "warning",
                    "Acoustic raw RMS remained above the sensor noise-floor threshold.",
                    samples[-1]["timestamp"],
                )
            )
    for event in EVENTS:
        if event.get("eventType") in {"sensor_fault_candidate", "sensor_fault"} and (
            event.get("deviceId") == device["id"]
            or (not event.get("deviceId") and event["assetId"] == device["assetId"])
        ):
            rows.append(
                {
                    **_sensor_fault_record(
                        device,
                        str(event.get("faultCode") or "sensor_event"),
                        str(event["severity"]),
                        str(event["note"]),
                        str(event["occurredAt"]),
                    ),
                    "eventId": event["id"],
                    "status": event.get("status", "open"),
                }
            )
    normalized_status = status.strip().lower()
    normalized_type = fault_type.strip().lower()
    if normalized_status:
        rows = [item for item in rows if item["status"] == normalized_status]
    if normalized_type:
        rows = [item for item in rows if item["faultType"] == normalized_type]
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    filtered = []
    for item in rows:
        timestamp = parse_rfc3339("fault.detectedAt", item["detectedAt"])
        if from_value and timestamp < from_value:
            continue
        if to_value and timestamp > to_value:
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: item["detectedAt"], reverse=True)
    total = len(filtered)
    start = (page - 1) * size
    return {
        "items": copy_payload(filtered[start : start + size]),
        "page": page,
        "size": size,
        "total": total,
    }


def acoustic_taxonomies_for(
    user: dict[str, Any], version: str = ""
) -> dict[str, Any] | list[dict[str, Any]]:
    require_permission(user, "label-taxonomy:read")
    normalized = version.strip().upper()
    if normalized:
        taxonomy = next(
            (
                item
                for item in ACOUSTIC_TAXONOMY_VERSIONS
                if item["version"].upper() == normalized
            ),
            None,
        )
        if not taxonomy:
            raise ApiError(
                404, "LABEL_TAXONOMY_NOT_FOUND", "Label taxonomy was not found."
            )
        return copy_payload(taxonomy)
    return copy_payload(ACOUSTIC_TAXONOMY_VERSIONS)


def update_acoustic_taxonomy(
    user: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "label-taxonomy:write")
    version = required_text(payload, "version").upper()
    reason = required_text(payload, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400, "REASON_TOO_LONG", "reason must be 1000 characters or less."
        )
    labels = payload.get("labels")
    if not isinstance(labels, list) or not labels:
        raise ApiError(400, "INVALID_LABELS", "labels must be a non-empty list.")
    normalized_labels = []
    codes: set[str] = set()
    for index, label in enumerate(labels):
        if not isinstance(label, dict):
            raise ApiError(400, "INVALID_LABEL", f"labels[{index}] must be an object.")
        code = required_text(label, "code").upper()
        if code in codes:
            raise ApiError(409, "DUPLICATE_LABEL_CODE", "Label codes must be unique.")
        codes.add(code)
        sample_refs = string_list(label, "sampleRefs")
        normalized_labels.append(
            {
                "code": code,
                "name": required_text(label, "name"),
                "criteria": required_text(label, "criteria"),
                "sampleRefs": sample_refs,
            }
        )
    with STORE_LOCK:
        if any(
            item["version"].upper() == version for item in ACOUSTIC_TAXONOMY_VERSIONS
        ):
            raise ApiError(
                409,
                "LABEL_TAXONOMY_VERSION_EXISTS",
                "Label taxonomy versions are immutable and must be unique.",
            )
        before = copy_payload(
            next(
                (item for item in ACOUSTIC_TAXONOMY_VERSIONS if item.get("active")),
                None,
            )
        )
        for item in ACOUSTIC_TAXONOMY_VERSIONS:
            item["active"] = False
        record = {
            "version": version,
            "labels": normalized_labels,
            "active": True,
            "reason": reason,
            "updatedAt": now_iso(),
        }
        ACOUSTIC_TAXONOMY_VERSIONS.append(record)
        append_audit_log(
            user,
            "label-taxonomy.update",
            "acousticLabelTaxonomy",
            version,
            before,
            record,
            reason,
        )
        return copy_payload(record)


def _dataset_source(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ApiError(400, "INVALID_DATASET_SOURCE", "source must be an object.")
    normalized_source = {}
    for field in ("type", "uri", "license", "checksum"):
        value = source.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ApiError(
                400,
                "INVALID_DATASET_SOURCE",
                f"source.{field} must be a non-empty string.",
            )
        normalized_source[field] = value.strip()
    source_type = normalized_source["type"].lower()
    uri = normalized_source["uri"]
    checksum = normalized_source["checksum"].lower()
    if source_type not in {"internal", "external"}:
        raise ApiError(
            400,
            "INVALID_DATASET_SOURCE",
            "source.type must be internal or external.",
        )
    if source_type == "internal" and uri != "api://telemetry":
        raise ApiError(
            400,
            "INVALID_DATASET_SOURCE",
            "Internal datasets must use source.uri api://telemetry.",
        )
    if source_type == "external" and uri == "api://telemetry":
        raise ApiError(
            400,
            "INVALID_DATASET_SOURCE",
            "source.uri api://telemetry is reserved for internal datasets.",
        )
    checksum_value = checksum.removeprefix("sha256:")
    if (
        not checksum.startswith("sha256:")
        or len(checksum_value) != 64
        or any(character not in "0123456789abcdef" for character in checksum_value)
    ):
        raise ApiError(
            400,
            "INVALID_DATASET_SOURCE",
            "source.checksum must contain exactly 64 hexadecimal SHA-256 characters.",
        )
    return {
        "type": source_type,
        "uri": uri,
        "license": normalized_source["license"],
        "checksum": checksum,
    }


def _normalized_operating_condition_value(field: str, value: Any, *, depth: int) -> Any:
    if depth > MAX_OPERATING_CONDITION_DEPTH:
        raise ApiError(
            400,
            "INVALID_OPERATING_CONDITIONS",
            "compatibility.operatingConditions is nested too deeply.",
        )
    if value is None:
        raise ApiError(
            400,
            "INVALID_OPERATING_CONDITIONS",
            f"{field} must not be null.",
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ApiError(
                400,
                "INVALID_OPERATING_CONDITIONS",
                f"{field} must be a finite number.",
            )
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                400,
                "INVALID_OPERATING_CONDITIONS",
                f"{field} must not be empty.",
            )
        return normalized
    if isinstance(value, list):
        if not value:
            raise ApiError(
                400,
                "INVALID_OPERATING_CONDITIONS",
                f"{field} must not be an empty list.",
            )
        return [
            _normalized_operating_condition_value(
                f"{field}[{index}]", item, depth=depth + 1
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return _normalized_operating_conditions(value, field=field, depth=depth)
    raise ApiError(
        400,
        "INVALID_OPERATING_CONDITIONS",
        f"{field} contains an unsupported value.",
    )


def _normalized_operating_conditions(
    operating_conditions: dict[str, Any],
    *,
    field: str = "operatingConditions",
    depth: int = 0,
) -> dict[str, Any]:
    if depth > MAX_OPERATING_CONDITION_DEPTH:
        raise ApiError(
            400,
            "INVALID_OPERATING_CONDITIONS",
            "compatibility.operatingConditions is nested too deeply.",
        )
    if not operating_conditions:
        raise ApiError(
            400,
            "INVALID_OPERATING_CONDITIONS",
            f"{field} must not be empty.",
        )
    normalized = {}
    normalized_keys = set()
    for raw_key, raw_value in operating_conditions.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ApiError(
                400,
                "INVALID_OPERATING_CONDITIONS",
                f"{field} keys must be non-empty strings.",
            )
        key = raw_key.strip()
        canonical_key = key.casefold()
        if canonical_key in normalized_keys:
            raise ApiError(
                400,
                "INVALID_OPERATING_CONDITIONS",
                f"{field} keys must be unique after normalization.",
            )
        normalized_keys.add(canonical_key)
        if canonical_key.endswith("range"):
            if not isinstance(raw_value, list) or len(raw_value) != 2:
                raise ApiError(
                    400,
                    "INVALID_OPERATING_CONDITIONS",
                    f"{field}.{key} must contain exactly two numeric bounds.",
                )
            bounds = []
            for index, bound in enumerate(raw_value):
                if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                    raise ApiError(
                        400,
                        "INVALID_OPERATING_CONDITIONS",
                        f"{field}.{key}[{index}] must be a finite number.",
                    )
                if isinstance(bound, float) and not math.isfinite(bound):
                    raise ApiError(
                        400,
                        "INVALID_OPERATING_CONDITIONS",
                        f"{field}.{key}[{index}] must be a finite number.",
                    )
                if isinstance(bound, int):
                    bounds.append(bound)
                else:
                    bounds.append(int(bound) if bound.is_integer() else bound)
            if bounds[0] > bounds[1]:
                raise ApiError(
                    400,
                    "INVALID_OPERATING_CONDITIONS",
                    f"{field}.{key} lower bound must not exceed its upper bound.",
                )
            normalized[key] = bounds
            continue
        normalized[key] = _normalized_operating_condition_value(
            f"{field}.{key}", raw_value, depth=depth + 1
        )
    return normalized


def _dataset_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    compatibility = payload.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ApiError(
            400, "INVALID_DATASET_COMPATIBILITY", "compatibility must be an object."
        )
    signal_types = string_list(compatibility, "signalType", required=True)
    normalized_signal_types = set()
    for signal_type in signal_types:
        canonical_signal_type = signal_type.strip().casefold()
        if canonical_signal_type in normalized_signal_types:
            raise ApiError(
                400,
                "INVALID_DATASET_COMPATIBILITY",
                "compatibility.signalType values must be unique after normalization.",
            )
        normalized_signal_types.add(canonical_signal_type)
    units = compatibility.get("units")
    if not isinstance(units, dict) or not units:
        raise ApiError(400, "INVALID_DATASET_UNITS", "compatibility.units is required.")
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in units.items()
    ):
        raise ApiError(
            400,
            "INVALID_DATASET_UNITS",
            "compatibility.units keys and values must be non-empty strings.",
        )
    normalized_units = {}
    normalized_unit_keys = set()
    for key, value in units.items():
        normalized_key = key.strip()
        canonical_key = normalized_key.casefold()
        if canonical_key in normalized_unit_keys:
            raise ApiError(
                400,
                "INVALID_DATASET_UNITS",
                "compatibility.units keys must be unique after normalization.",
            )
        normalized_unit_keys.add(canonical_key)
        normalized_units[normalized_key] = value.strip()
    operating_conditions = compatibility.get("operatingConditions")
    if not isinstance(operating_conditions, dict):
        raise ApiError(
            400,
            "INVALID_OPERATING_CONDITIONS",
            "compatibility.operatingConditions must be an object.",
        )
    return {
        "signalType": signal_types,
        "samplingRateHz": _bounded_number(
            "compatibility.samplingRateHz",
            compatibility.get("samplingRateHz"),
            1,
            1000000,
            integer=True,
        ),
        "units": normalized_units,
        "operatingConditions": _normalized_operating_conditions(
            operating_conditions,
            field="compatibility.operatingConditions",
        ),
    }


def _dataset_split(payload: dict[str, Any]) -> dict[str, float]:
    split = payload.get("split")
    if not isinstance(split, dict):
        raise ApiError(400, "INVALID_DATASET_SPLIT", "split must be an object.")
    expected_keys = {"train", "validation", "test"}
    if set(split) != expected_keys:
        raise ApiError(
            400,
            "INVALID_DATASET_SPLIT",
            "split must contain exactly train, validation, and test.",
        )
    result = {}
    for name in ("train", "validation", "test"):
        value = float(_bounded_number(f"split.{name}", split.get(name), 0, 1))
        result[name] = 0.0 if value == 0 else value
    if not math.isclose(sum(result.values()), 1.0, rel_tol=0, abs_tol=1e-9):
        raise ApiError(400, "INVALID_DATASET_SPLIT", "split ratios must sum to 1.0.")
    return result


def _dataset_source_filters(payload: dict[str, Any]) -> dict[str, Any] | None:
    source_filters = payload.get("sourceFilters")
    if source_filters is None:
        return None
    if not isinstance(source_filters, dict) or not source_filters:
        raise ApiError(
            400,
            "INVALID_SOURCE_FILTERS",
            "sourceFilters must be a non-empty object when provided.",
        )

    allowed_fields = {"siteId", "siteIds", "assetId", "assetIds", "from", "to"}
    unsupported = sorted(set(source_filters) - allowed_fields)
    if unsupported:
        raise ApiError(
            400,
            "INVALID_SOURCE_FILTERS",
            "Unsupported sourceFilters fields: " + ", ".join(unsupported),
        )

    normalized: dict[str, Any] = {}
    for singular_key in ("siteId", "assetId"):
        if singular_key not in source_filters:
            continue
        value = source_filters[singular_key]
        if not isinstance(value, str) or not value.strip():
            raise ApiError(
                400,
                "INVALID_SOURCE_FILTERS",
                f"sourceFilters.{singular_key} must be a non-empty string.",
            )
        normalized[singular_key] = value.strip().upper()

    for plural_key in ("siteIds", "assetIds"):
        if plural_key not in source_filters:
            continue
        values = string_list(source_filters, plural_key, required=True, uppercase=True)
        normalized[plural_key] = sorted(values)

    for singular_key, plural_key in (("siteId", "siteIds"), ("assetId", "assetIds")):
        if (
            singular_key in normalized
            and plural_key in normalized
            and normalized[singular_key] not in normalized[plural_key]
        ):
            raise ApiError(
                400,
                "INVALID_SOURCE_FILTERS",
                f"sourceFilters.{singular_key} must be included in {plural_key}.",
            )

    for timestamp_key in ("from", "to"):
        if timestamp_key not in source_filters:
            continue
        value = source_filters[timestamp_key]
        parsed = parse_rfc3339(f"sourceFilters.{timestamp_key}", value)
        normalized[timestamp_key] = format_rfc3339(parsed)

    if "from" in normalized and "to" in normalized:
        if parse_rfc3339("sourceFilters.from", normalized["from"]) > parse_rfc3339(
            "sourceFilters.to", normalized["to"]
        ):
            raise ApiError(
                400,
                "INVALID_SOURCE_FILTERS",
                "sourceFilters.from must not be later than sourceFilters.to.",
            )
    return normalized


def _canonical_dataset_source_filters(
    source_filters: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not source_filters:
        return None
    canonical = {
        key: source_filters[key] for key in ("from", "to") if key in source_filters
    }
    for singular_key, plural_key in (("siteId", "siteIds"), ("assetId", "assetIds")):
        values = set(source_filters.get(plural_key, []))
        if source_filters.get(singular_key):
            values.add(source_filters[singular_key])
        if values:
            canonical[plural_key] = sorted(values)
    return canonical


def _canonical_casefold_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key).strip().casefold(): _canonical_casefold_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical_casefold_keys(item) for item in value]
    return value


def _dataset_version_fingerprint(
    source: dict[str, Any],
    compatibility: dict[str, Any],
    source_filters: dict[str, Any] | None,
    label_taxonomy_version: str,
    label_mapping: dict[str, str],
    split: dict[str, float],
    label_policy_version: str,
    snapshot_schema_version: str,
    snapshot_checksum: str | None = None,
) -> str:
    canonical_compatibility = {
        "signalType": sorted(
            {
                str(value).strip().casefold()
                for value in compatibility.get("signalType", [])
            }
        ),
        "samplingRateHz": compatibility.get("samplingRateHz"),
        "units": _canonical_casefold_keys(compatibility.get("units", {})),
        "operatingConditions": _canonical_casefold_keys(
            compatibility.get("operatingConditions", {})
        ),
    }
    canonical_source = copy_payload(source)
    if snapshot_checksum:
        canonical_source.pop("checksum", None)
    canonical_definition = {
        "source": canonical_source,
        "compatibility": canonical_compatibility,
        "sourceFilters": _canonical_dataset_source_filters(source_filters),
        "labelTaxonomyVersion": label_taxonomy_version,
        "labelMapping": {key.casefold(): value for key, value in label_mapping.items()},
        "split": copy_payload(split),
        "splitPolicy": DATASET_SPLIT_POLICY,
        "labelPolicyVersion": label_policy_version,
        "snapshotSchemaVersion": snapshot_schema_version,
        "snapshotChecksum": snapshot_checksum,
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical_definition,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def create_dataset_version(
    user: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "dataset:write")
    name = required_text(payload, "name")
    source = _dataset_source(payload)
    compatibility = _dataset_compatibility(payload)
    label_taxonomy_version = required_text(payload, "labelTaxonomyVersion").upper()
    taxonomy = acoustic_taxonomies_for(user, label_taxonomy_version)
    label_mapping = payload.get("labelMapping")
    if not isinstance(label_mapping, dict) or not label_mapping:
        raise ApiError(
            400, "INVALID_LABEL_MAPPING", "labelMapping must be a non-empty object."
        )
    normalized_mapping = {}
    normalized_mapping_keys = set()
    for raw_key, raw_value in label_mapping.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip().upper()
        if not key or not value:
            raise ApiError(
                400,
                "INVALID_LABEL_MAPPING",
                "labelMapping entries must be non-empty.",
            )
        normalized_key = key.casefold()
        if normalized_key in normalized_mapping_keys:
            raise ApiError(
                400,
                "INVALID_LABEL_MAPPING",
                "labelMapping keys must be unique ignoring letter case.",
            )
        normalized_mapping_keys.add(normalized_key)
        normalized_mapping[key] = value
    taxonomy_codes = {str(item["code"]).strip().upper() for item in taxonomy["labels"]}
    unknown_codes = sorted(set(normalized_mapping.values()) - taxonomy_codes)
    if unknown_codes:
        raise ApiError(
            400,
            "INVALID_LABEL_MAPPING",
            "labelMapping contains codes outside the selected taxonomy: "
            + ", ".join(unknown_codes),
        )
    split = _dataset_split(payload)
    reason = required_text(payload, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise ApiError(
            400, "REASON_TOO_LONG", "reason must be 1000 characters or less."
        )
    source_filters = _dataset_source_filters(payload)
    with STORE_LOCK:
        created_at = now_iso()
        record = {
            "id": f"DATASET-{len(DATASET_VERSIONS) + 1:05d}",
            "name": name,
            "source": source,
            "compatibility": compatibility,
            "sourceFilters": copy_payload(source_filters),
            "labelTaxonomyVersion": label_taxonomy_version,
            "labelMapping": normalized_mapping,
            "split": split,
            "splitPolicy": DATASET_SPLIT_POLICY,
            "labelPolicyVersion": DATASET_LABEL_POLICY_VERSION,
            "snapshotSchemaVersion": DATASET_SNAPSHOT_SCHEMA_VERSION,
            "status": "frozen",
            "approvalStatus": "pending",
            "artifactRefs": [],
            "reason": reason,
            "createdAt": created_at,
            "frozenAt": created_at,
            "snapshotRecordCount": None,
            "snapshotChecksum": None,
            "versionFingerprint": None,
            "createdBy": user["id"],
        }
        snapshot = None
        if source["type"] == "internal":
            installation_validation = _dataset_installation_validation(user, record)
            record["installationValidation"] = installation_validation
            if installation_validation["status"] != "ready":
                raise ApiError(
                    409,
                    "DATASET_INSTALLATION_INCOMPLETE",
                    "Internal telemetry cannot be frozen until installation metadata is complete.",
                    installation_validation,
                )
            snapshot, source_records = _freeze_internal_dataset_snapshot(user, record)
            snapshot_rows = [
                row for scope_key in sorted(snapshot) for row in snapshot[scope_key]
            ]
            if not snapshot_rows:
                raise ApiError(
                    400,
                    "EMPTY_DATASET_SNAPSHOT",
                    "The selected sourceFilters contain no telemetry records.",
                )
            record["source"]["checksum"] = _canonical_telemetry_checksum(source_records)
            snapshot_checksum = hashlib.sha256(
                json.dumps(snapshot_rows, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            record["snapshotRecordCount"] = len(snapshot_rows)
            record["snapshotChecksum"] = f"sha256:{snapshot_checksum}"
        else:
            record["installationValidation"] = {
                "status": "not_applicable",
                "checkedAt": created_at,
                "assets": [],
                "missing": [],
            }
        record["versionFingerprint"] = _dataset_version_fingerprint(
            source,
            compatibility,
            source_filters,
            label_taxonomy_version,
            normalized_mapping,
            split,
            record["labelPolicyVersion"],
            record["snapshotSchemaVersion"],
            record["snapshotChecksum"],
        )
        if any(
            item.get("versionFingerprint") == record["versionFingerprint"]
            for item in DATASET_VERSIONS
        ):
            raise ApiError(
                409,
                "DATASET_VERSION_EXISTS",
                "An identical normalized dataset version is already registered.",
            )
        dataset_site_ids = _dataset_site_ids(record, snapshot)
        for site_id in dataset_site_ids:
            require_site_access(user, site_id)
        if snapshot is not None:
            DATASET_SNAPSHOTS[record["id"]] = snapshot
        DATASET_VERSIONS.append(record)
        append_audit_log(
            user,
            "dataset.create",
            "dataset",
            record["id"],
            None,
            record,
            reason,
            site_id=dataset_site_ids[0] if len(dataset_site_ids) == 1 else None,
            site_ids=dataset_site_ids,
        )
        return copy_payload(record)


def _dataset_installation_validation(
    user: dict[str, Any], dataset: dict[str, Any]
) -> dict[str, Any]:
    required_fields = ("position", "orientation", "mountingMethod", "photoRefs")
    assets = _dataset_scope_assets(user, dataset)
    missing = []
    validated_assets = []
    for asset in assets:
        active_points = [
            item
            for item in INSTALL_POINTS
            if item["assetId"] == asset["id"] and item.get("active") is True
        ]
        complete = []
        for point in active_points:
            point_missing = []
            for field in required_fields:
                value = point.get(field)
                if field == "photoRefs":
                    if not isinstance(value, list) or not value:
                        point_missing.append(field)
                elif not isinstance(value, str) or not value.strip():
                    point_missing.append(field)
            if not point_missing:
                complete.append(point)
        if complete:
            validated_assets.append(
                {
                    "siteId": asset["siteId"],
                    "assetId": asset["id"],
                    "installPointIds": sorted(item["id"] for item in complete),
                }
            )
        else:
            missing.append(
                {
                    "siteId": asset["siteId"],
                    "assetId": asset["id"],
                    "missingFields": (
                        ["activeInstallPoint"]
                        if not active_points
                        else sorted(
                            {
                                field
                                for point in active_points
                                for field in required_fields
                                if (
                                    not point.get(field)
                                    or (field == "photoRefs" and not point.get(field))
                                )
                            }
                        )
                    ),
                }
            )
    return {
        "status": "missing" if missing else "ready",
        "checkedAt": now_iso(),
        "assets": validated_assets,
        "missing": missing,
    }


def dataset_version_for(user: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    require_permission(user, "dataset:read")
    normalized = dataset_id.strip().upper()
    record = next((item for item in DATASET_VERSIONS if item["id"] == normalized), None)
    if not record:
        raise ApiError(404, "DATASET_NOT_FOUND", "Dataset version was not found.")
    for site_id in _dataset_site_ids(record):
        require_site_access(user, site_id)
    return copy_payload(record)


def dataset_versions_for(
    user: dict[str, Any], *, page: int = 1, size: int = 50
) -> dict[str, Any]:
    require_permission(user, "dataset:read")
    allowed_site_ids = set(user.get("allowedSiteIds", []))
    rows = []
    for record in DATASET_VERSIONS:
        site_ids = set(_dataset_site_ids(record))
        if "*" in allowed_site_ids or site_ids.issubset(allowed_site_ids):
            rows.append(copy_payload(record))
    rows.sort(
        key=lambda item: (
            parse_rfc3339("dataset.createdAt", item["createdAt"]),
            item["id"],
        ),
        reverse=True,
    )
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": rows[start : start + size],
        "page": page,
        "size": size,
        "total": total,
    }


def _split_for_group(group_id: str, split: dict[str, float]) -> str:
    value = (
        int(hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    )
    if value < split["train"]:
        return "train"
    if value < split["train"] + split["validation"]:
        return "validation"
    return "test"


def _dataset_group_key(row: dict[str, Any]) -> str:
    return f"{row.get('site_id')}:{row.get('asset_id')}"


def _dataset_group_stratum(rows: list[dict[str, Any]]) -> str:
    rpm_values = []
    for row in rows:
        rpm = row.get("rpm")
        if isinstance(rpm, bool) or rpm is None:
            continue
        try:
            parsed = float(rpm)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed):
            rpm_values.append(parsed)
    if not rpm_values:
        return "rpm-unknown"
    rpm_values.sort()
    midpoint = len(rpm_values) // 2
    median_rpm = (
        rpm_values[midpoint]
        if len(rpm_values) % 2
        else (rpm_values[midpoint - 1] + rpm_values[midpoint]) / 2
    )
    return f"rpm-{int(math.floor(median_rpm / 100.0) * 100)}"


def _assign_dataset_splits(
    rows: list[dict[str, Any]],
    split: dict[str, float],
    *,
    require_complete: bool,
) -> None:
    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped_rows.setdefault(_dataset_group_key(row), []).append(row)
    group_keys = list(grouped_rows)
    positive_splits = [
        name for name in ("train", "validation", "test") if split[name] > 0
    ]
    if require_complete and len(group_keys) < len(positive_splits):
        raise ApiError(
            400,
            "INVALID_DATASET_SPLIT",
            "The snapshot must contain enough asset groups for every positive split.",
        )

    assignments: dict[str, str] = {}
    if len(group_keys) >= len(positive_splits):
        split_names = ("train", "validation", "test")
        exact_counts = {name: len(group_keys) * split[name] for name in split_names}
        target_counts = {
            name: int(math.floor(exact_counts[name])) for name in split_names
        }
        unassigned = len(group_keys) - sum(target_counts.values())
        ranked = sorted(
            split_names,
            key=lambda name: (
                exact_counts[name] - target_counts[name],
                split[name],
                -split_names.index(name),
            ),
            reverse=True,
        )
        for name in ranked[:unassigned]:
            target_counts[name] += 1

        for name in positive_splits:
            if target_counts[name] > 0:
                continue
            donors = [
                candidate
                for candidate in positive_splits
                if target_counts[candidate] > 1
            ]
            if not donors:
                raise ApiError(
                    400,
                    "INVALID_DATASET_SPLIT",
                    "Every positive split requires at least one asset group.",
                )
            donor = max(
                donors,
                key=lambda candidate: (
                    target_counts[candidate] - exact_counts[candidate],
                    target_counts[candidate],
                    split[candidate],
                ),
            )
            target_counts[donor] -= 1
            target_counts[name] += 1
        groups_by_stratum: dict[str, list[str]] = {}
        for group_key in group_keys:
            stratum = _dataset_group_stratum(grouped_rows[group_key])
            groups_by_stratum.setdefault(stratum, []).append(group_key)
        remaining_counts = copy_payload(target_counts)
        for stratum in sorted(groups_by_stratum):
            stratum_groups = sorted(
                groups_by_stratum[stratum],
                key=lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
            for group_key in stratum_groups:
                candidates = [
                    name for name in split_names if remaining_counts[name] > 0
                ]
                selected = max(
                    candidates,
                    key=lambda name: (
                        remaining_counts[name] / target_counts[name],
                        split[name],
                        -split_names.index(name),
                    ),
                )
                assignments[group_key] = selected
                remaining_counts[selected] -= 1
    else:
        assignments = {
            group_key: _split_for_group(group_key, split) for group_key in group_keys
        }

    for row in rows:
        row["dataset_split"] = assignments[_dataset_group_key(row)]

    if require_complete:
        populated = {row["dataset_split"] for row in rows}
        missing = [name for name in positive_splits if name not in populated]
        if missing:
            raise ApiError(
                400,
                "INVALID_DATASET_SPLIT",
                "The snapshot leaves positive dataset splits empty: "
                + ", ".join(missing),
            )


def _event_for_dataset_point(
    asset_events: list[dict[str, Any]], timestamp: Any
) -> dict[str, Any] | None:
    point_time = parse_rfc3339("telemetry.timestamp", timestamp)
    for event in asset_events:
        event_start = parse_rfc3339("event.occurredAt", event["occurredAt"])
        duration_sec = max(0, int(event.get("durationSec") or 0))
        if event_start <= point_time <= event_start + timedelta(seconds=duration_sec):
            return event
    return None


def _event_matches_dataset_point(event: dict[str, Any], point: dict[str, Any]) -> bool:
    """Match real telemetry labels without mixing demo or source domains.

    Legacy physical events may not record a source.  Those events remain
    compatible with real telemetry from the same device/time window.  When an
    event does declare a source, it scopes the label to that exact collector.
    """
    if _is_demo_event(event) or point.get("source") == DEMO_SIGNAL_SOURCE:
        return False
    event_device_id = str(event.get("deviceId") or "").strip().upper()
    point_device_id = str(point.get("deviceId") or "").strip().upper()
    if event_device_id and event_device_id != point_device_id:
        return False
    event_source = str(event.get("source") or "").strip()
    point_source = str(point.get("source") or "").strip()
    return not event_source or event_source == point_source


def _dataset_export_window(
    dataset: dict[str, Any] | None,
    site_id: str,
    asset_id: str,
    from_timestamp: str | None,
    to_timestamp: str | None,
) -> tuple[str | None, str | None]:
    requested_from = (
        parse_rfc3339("from", from_timestamp) if from_timestamp is not None else None
    )
    requested_to = (
        parse_rfc3339("to", to_timestamp) if to_timestamp is not None else None
    )
    if requested_from and requested_to and requested_from > requested_to:
        raise ApiError(
            400,
            "INVALID_TIME_RANGE",
            "from must be earlier than or equal to to.",
        )
    source_filters = dataset.get("sourceFilters") if dataset else None
    if not source_filters:
        return from_timestamp, to_timestamp

    allowed_site_ids = set(source_filters.get("siteIds", []))
    if source_filters.get("siteId"):
        allowed_site_ids.add(source_filters["siteId"])
    allowed_asset_ids = set(source_filters.get("assetIds", []))
    if source_filters.get("assetId"):
        allowed_asset_ids.add(source_filters["assetId"])
    if allowed_site_ids and site_id not in allowed_site_ids:
        raise ApiError(
            409,
            "DATASET_SOURCE_FILTER_MISMATCH",
            "The requested site is outside the dataset sourceFilters.",
        )
    if allowed_asset_ids and asset_id not in allowed_asset_ids:
        raise ApiError(
            409,
            "DATASET_SOURCE_FILTER_MISMATCH",
            "The requested asset is outside the dataset sourceFilters.",
        )

    filter_from = (
        parse_rfc3339("sourceFilters.from", source_filters["from"])
        if source_filters.get("from")
        else None
    )
    filter_to = (
        parse_rfc3339("sourceFilters.to", source_filters["to"])
        if source_filters.get("to")
        else None
    )
    effective_from = max(
        (value for value in (requested_from, filter_from) if value is not None),
        default=None,
    )
    effective_to = min(
        (value for value in (requested_to, filter_to) if value is not None),
        default=None,
    )
    if effective_from and effective_to and effective_from > effective_to:
        raise ApiError(
            409,
            "DATASET_SOURCE_FILTER_MISMATCH",
            "The requested time range is outside the dataset sourceFilters.",
        )
    return (
        format_rfc3339(effective_from) if effective_from else None,
        format_rfc3339(effective_to) if effective_to else None,
    )


def _active_dataset_taxonomy(
    taxonomies: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    taxonomy_versions = ACOUSTIC_TAXONOMY_VERSIONS if taxonomies is None else taxonomies
    return next(
        (item for item in reversed(taxonomy_versions) if item.get("active")),
        None,
    )


def _dataset_event_has_verified_label(event: dict[str, Any] | None) -> bool:
    return bool(
        event
        and event.get("reviewed")
        and str(event.get("label") or "").strip()
        and str(event.get("label") or "").strip() != "needs_review"
    )


def _dataset_label_for_event(
    dataset: dict[str, Any] | None, event: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    if not event:
        return None, None
    source_label = str(event.get("label") or "").strip()
    if not source_label:
        return None, None
    # Unreviewed and still-pending events are candidate metadata, not taxonomy labels.
    if not _dataset_event_has_verified_label(event):
        return source_label, None
    if not dataset:
        return source_label, None
    label_mapping = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in dataset.get("labelMapping", {}).items()
    }
    mapped_label = label_mapping.get(source_label.casefold())
    return (
        (mapped_label, dataset["labelTaxonomyVersion"])
        if mapped_label
        else (None, None)
    )


def _dataset_target_label(
    dataset: dict[str, Any] | None,
    source_label: Any,
    taxonomies: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None]:
    normalized = str(source_label or "").strip()
    if not normalized:
        return None, None
    if dataset:
        label_mapping = {
            str(key).strip().casefold(): str(value).strip().upper()
            for key, value in dataset.get("labelMapping", {}).items()
        }
        mapped = label_mapping.get(normalized.casefold())
        if mapped:
            return mapped, dataset["labelTaxonomyVersion"]
        taxonomy = next(
            (
                item
                for item in (
                    ACOUSTIC_TAXONOMY_VERSIONS if taxonomies is None else taxonomies
                )
                if item["version"] == dataset["labelTaxonomyVersion"]
            ),
            None,
        )
    else:
        taxonomy = _active_dataset_taxonomy(taxonomies)
    taxonomy_codes = {
        str(item["code"]).strip().upper() for item in (taxonomy or {}).get("labels", [])
    }
    direct_code = normalized.upper()
    if direct_code in taxonomy_codes:
        return direct_code, taxonomy["version"]
    return None, None


def _dataset_ground_truth(
    dataset: dict[str, Any] | None,
    point: dict[str, Any],
    matching_event: dict[str, Any] | None,
    taxonomies: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if _dataset_event_has_verified_label(matching_event):
        raw_label = str(matching_event["label"]).strip()
        target_label, taxonomy_version = _dataset_target_label(
            dataset, raw_label, taxonomies
        )
        return (
            raw_label,
            "event_review",
            target_label,
            taxonomy_version,
        )
    trusted_fields = _trusted_telemetry_label_fields(point)
    candidates = (
        ("knownVibrationLabel", "known_vibration_label"),
        ("knownAcousticLabel", "known_acoustic_label"),
        ("scenarioLabel", "scenario_label"),
    )
    for field, source in candidates:
        if field not in trusted_fields:
            continue
        raw_label = str(point.get(field) or "").strip()
        if not raw_label:
            continue
        target_label, taxonomy_version = _dataset_target_label(
            dataset, raw_label, taxonomies
        )
        return raw_label, source, target_label, taxonomy_version
    return None, None, None, None


def _trusted_telemetry_label_fields(point: dict[str, Any]) -> set[str]:
    provenance = point.get("labelProvenance")
    if not (
        isinstance(provenance, dict)
        and provenance.get("verifiedByServer") is True
        and isinstance(provenance.get("fields"), list)
        and provenance.get("principalId")
        and provenance.get("submittedAt")
    ):
        return set()
    allowed_fields = {
        "scenarioLabel",
        "knownVibrationLabel",
        "knownAcousticLabel",
    }
    return {
        field
        for field in provenance["fields"]
        if isinstance(field, str) and field in allowed_fields
    }


def _dataset_snapshot_key(site_id: str, asset_id: str) -> str:
    return f"{site_id}:{asset_id}"


def _dataset_site_ids(
    dataset: dict[str, Any],
    snapshot_override: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    source_filters = dataset.get("sourceFilters") or {}
    explicit_site_ids = set(source_filters.get("siteIds", []))
    if source_filters.get("siteId"):
        explicit_site_ids.add(source_filters["siteId"])
    for site_id in explicit_site_ids:
        get_site(site_id)
    site_ids = set(explicit_site_ids)
    for asset_id in [
        *source_filters.get("assetIds", []),
        *([source_filters["assetId"]] if source_filters.get("assetId") else []),
    ]:
        asset = get_asset_by_id(asset_id)
        if explicit_site_ids and asset["siteId"] not in explicit_site_ids:
            raise ApiError(
                400,
                "INVALID_SOURCE_FILTERS",
                f"sourceFilters assetId {asset_id} is outside the selected siteIds.",
            )
        site_ids.add(asset["siteId"])
    if dataset.get("source", {}).get("type") == "internal":
        snapshot = (
            snapshot_override
            if snapshot_override is not None
            else DATASET_SNAPSHOTS.get(dataset.get("id"), {})
        )
        site_ids.update(key.split(":", 1)[0] for key, rows in snapshot.items() if rows)
    return sorted(site_ids)


def _dataset_scope_assets(
    user: dict[str, Any], dataset: dict[str, Any]
) -> list[dict[str, Any]]:
    source_filters = dataset.get("sourceFilters") or {}
    site_ids = set(source_filters.get("siteIds", []))
    if source_filters.get("siteId"):
        site_ids.add(source_filters["siteId"])
    asset_ids = set(source_filters.get("assetIds", []))
    if source_filters.get("assetId"):
        asset_ids.add(source_filters["assetId"])

    for site_id in site_ids:
        get_site(site_id)
        require_site_access(user, site_id)
    for asset_id in asset_ids:
        asset = get_asset_by_id(asset_id)
        require_site_access(user, asset["siteId"])
        if site_ids and asset["siteId"] not in site_ids:
            raise ApiError(
                400,
                "INVALID_SOURCE_FILTERS",
                f"sourceFilters assetId {asset_id} is outside the selected siteIds.",
            )

    allowed_site_ids = user.get("allowedSiteIds", [])
    assets = [
        asset
        for asset in ASSETS
        if (not site_ids or asset["siteId"] in site_ids)
        and (not asset_ids or asset["id"] in asset_ids)
        and ("*" in allowed_site_ids or asset["siteId"] in allowed_site_ids)
    ]
    if not assets:
        raise ApiError(
            400,
            "EMPTY_DATASET_SCOPE",
            "sourceFilters do not select any accessible assets.",
        )
    return sorted(assets, key=lambda item: (item["siteId"], item["id"]))


def _safe_tabular_value(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    significant = value.lstrip(" \t\r\n")
    if significant and significant[0] in "=+-@":
        return "'" + value
    return value


def _live_dataset_export_snapshot(
    site_id: str,
    asset_id: str,
    from_timestamp: str | None,
    to_timestamp: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, Any],
]:
    with STORE_LOCK:
        asset = copy_payload(get_asset(site_id, asset_id))
        # Dataset exports use persisted measurements, never display/demo data.
        points = _stored_telemetry_for(
            site_id,
            asset_id,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            include_demo=False,
        )
        events = copy_payload(
            [
                item
                for item in EVENTS
                if item["assetId"] == asset_id and not _is_demo_event(item)
            ]
        )
        taxonomies = copy_payload(ACOUSTIC_TAXONOMY_VERSIONS)
        units = copy_payload(telemetry_units(site_id, asset_id))
    return points, events, taxonomies, units, asset


def _dataset_rows_for_points(
    dataset: dict[str, Any] | None,
    site_id: str,
    asset_id: str,
    points: list[dict[str, Any]],
    *,
    events: list[dict[str, Any]] | None = None,
    taxonomies: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    event_source = EVENTS if events is None else events
    asset_events = sorted(
        (item for item in event_source if item["assetId"] == asset_id),
        key=lambda item: (
            _dataset_event_has_verified_label(item),
            parse_rfc3339("event.occurredAt", item["occurredAt"]),
            item["id"],
        ),
        reverse=True,
    )
    rows = []
    for point in points:
        matching_event = _event_for_dataset_point(
            [
                event
                for event in asset_events
                if _event_matches_dataset_point(event, point)
            ],
            point.get("timestamp"),
        )
        event_label, label_taxonomy_version = _dataset_label_for_event(
            dataset, matching_event
        )
        (
            ground_truth_label,
            ground_truth_source,
            target_label,
            target_label_taxonomy_version,
        ) = _dataset_ground_truth(dataset, point, matching_event, taxonomies)
        trusted_fields = _trusted_telemetry_label_fields(point)
        event_reviewed = bool(matching_event and matching_event.get("reviewed"))
        if ground_truth_label:
            label_status = "verified" if target_label else "unmapped"
        elif (
            matching_event
            and not _dataset_event_has_verified_label(matching_event)
            and str(event_label or "").strip()
        ):
            label_status = "weak"
        else:
            label_status = "unlabeled"
        training_eligible = label_status == "verified"
        row = {
            "site_id": site_id,
            "asset_id": asset_id,
            "device_id": point.get("deviceId"),
            "timestamp": point.get("timestamp"),
            "sequence": point.get("sequence"),
            "vibration_rms_raw": point.get("vibrationRmsRaw"),
            "vibration_rms_mm_s": point.get("vibrationRmsMmS"),
            "vibration_peak_hz": point.get("vibrationPeakHz"),
            "acoustic_rms_raw": point.get("acousticRmsRaw"),
            "acoustic_db": point.get("acousticDb"),
            "acoustic_peak_hz": point.get("acousticPeakHz"),
            "rpm": point.get("rpm"),
            "scenario_label": (
                point.get("scenarioLabel")
                if "scenarioLabel" in trusted_fields
                else None
            ),
            "known_vibration_label": (
                point.get("knownVibrationLabel")
                if "knownVibrationLabel" in trusted_fields
                else None
            ),
            "known_acoustic_label": (
                point.get("knownAcousticLabel")
                if "knownAcousticLabel" in trusted_fields
                else None
            ),
            "telemetry_source": point.get("source"),
            "is_synthetic": point.get("isSynthetic"),
            "vibration_unit_note": point.get("vibrationUnitNote"),
            "acoustic_unit_note": point.get("acousticUnitNote"),
            "event_id": matching_event.get("id") if matching_event else None,
            "event_label": event_label,
            "label_taxonomy_version": label_taxonomy_version,
            "ground_truth_label": ground_truth_label,
            "ground_truth_source": ground_truth_source,
            "target_label": target_label,
            "target_label_taxonomy_version": target_label_taxonomy_version,
            "label_status": label_status,
            "training_eligible": training_eligible,
            "event_reviewed": event_reviewed,
        }
        # Leave pre-extension/frozen rows unchanged. New observations retain their
        # own quality and provenance rather than borrowing asset.ratedRpm.
        if "rpmStatus" in point:
            row.update(
                rpm_status=point["rpmStatus"],
                rpm_measured_at=point.get("rpmMeasuredAt"),
                rpm_source=point.get("rpmSource"),
            )
        rows.append({key: _safe_tabular_value(value) for key, value in row.items()})
    return rows


def _freeze_internal_dataset_snapshot(
    user: dict[str, Any], dataset: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    snapshot = {}
    source_records = []
    for asset in _dataset_scope_assets(user, dataset):
        from_timestamp, to_timestamp = _dataset_export_window(
            dataset,
            asset["siteId"],
            asset["id"],
            None,
            None,
        )
        points = _stored_telemetry_for(
            asset["siteId"],
            asset["id"],
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            include_demo=False,
        )
        source_records.extend(points)
        snapshot[_dataset_snapshot_key(asset["siteId"], asset["id"])] = (
            _dataset_rows_for_points(
                dataset,
                asset["siteId"],
                asset["id"],
                points,
            )
        )
    snapshot_rows = [
        row for scope_key in sorted(snapshot) for row in snapshot[scope_key]
    ]
    if snapshot_rows:
        _assign_dataset_splits(
            snapshot_rows,
            dataset["split"],
            require_complete=True,
        )
    return snapshot, source_records


def _rows_in_time_range(
    rows: list[dict[str, Any]],
    from_timestamp: str | None,
    to_timestamp: str | None,
) -> list[dict[str, Any]]:
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400,
            "INVALID_TIME_RANGE",
            "from must be earlier than or equal to to.",
        )
    return [
        copy_payload(row)
        for row in rows
        if (
            not from_value or parse_rfc3339("timestamp", row["timestamp"]) >= from_value
        )
        and (not to_value or parse_rfc3339("timestamp", row["timestamp"]) <= to_value)
    ]


def dataset_export_for(
    user: dict[str, Any],
    site_id: str,
    asset_id: str,
    *,
    dataset_id: str = "",
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> dict[str, Any]:
    require_permission(user, "export:read")
    require_permission(user, "dataset:read")
    site = get_site(site_id)
    require_site_access(user, site["id"])
    asset = get_asset(site["id"], asset_id)
    dataset = dataset_version_for(user, dataset_id) if dataset_id else None
    if dataset and (
        str(dataset["source"].get("type", "")).lower() != "internal"
        or dataset["source"].get("uri") != "api://telemetry"
    ):
        raise ApiError(
            409,
            "DATASET_SOURCE_MISMATCH",
            "External dataset metadata cannot be used to export internal telemetry.",
        )
    from_timestamp, to_timestamp = _dataset_export_window(
        dataset,
        site["id"],
        asset["id"],
        from_timestamp,
        to_timestamp,
    )
    split = (
        dataset["split"] if dataset else {"train": 0.7, "validation": 0.2, "test": 0.1}
    )
    if dataset:
        snapshot = DATASET_SNAPSHOTS.get(dataset["id"])
        if snapshot is None:
            raise ApiError(
                409,
                "DATASET_SNAPSHOT_NOT_FOUND",
                "The frozen dataset snapshot is unavailable.",
            )
        snapshot_key = _dataset_snapshot_key(site["id"], asset["id"])
        if snapshot_key not in snapshot:
            raise ApiError(
                409,
                "DATASET_SOURCE_FILTER_MISMATCH",
                "The requested asset was not part of the frozen dataset snapshot.",
            )
        rows = _rows_in_time_range(snapshot[snapshot_key], from_timestamp, to_timestamp)
        source_record_count = len(rows)
    else:
        points, events, taxonomies, units, live_asset = _live_dataset_export_snapshot(
            site["id"],
            asset["id"],
            from_timestamp,
            to_timestamp,
        )
        rows = _dataset_rows_for_points(
            None,
            site["id"],
            asset["id"],
            points,
            events=events,
            taxonomies=taxonomies,
        )
        _assign_dataset_splits(rows, split, require_complete=False)
        source_record_count = len(points)
    split_counts = {name: 0 for name in ("train", "validation", "test")}
    for row in rows:
        split_counts[row["dataset_split"]] += 1
    checksum = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    export_checksum = f"sha256:{checksum}"
    populated_splits = [name for name, count in split_counts.items() if count]
    source = (
        copy_payload(dataset["source"])
        if dataset
        else {
            "type": "internal",
            "uri": "api://telemetry",
            "license": "project-internal",
            "checksum": export_checksum,
        }
    )
    is_current_label_schema = not dataset or (
        dataset.get("labelPolicyVersion") == DATASET_LABEL_POLICY_VERSION
        and dataset.get("snapshotSchemaVersion") == DATASET_SNAPSHOT_SCHEMA_VERSION
    )
    manifest = {
        "datasetId": dataset["id"] if dataset else None,
        "exportType": "internal_telemetry",
        "source": source,
        "compatibility": (
            dataset["compatibility"]
            if dataset
            else {
                "signalType": sorted(units),
                "samplingRateHz": None,
                "units": units,
                "operatingConditions": {
                    "ratedRpm": live_asset.get("ratedRpm"),
                    "source": "live_telemetry",
                },
            }
        ),
        "labelTaxonomyVersion": (
            dataset["labelTaxonomyVersion"]
            if dataset
            else (_active_dataset_taxonomy(taxonomies) or {}).get("version")
        ),
        "labelMapping": dataset["labelMapping"] if dataset else {},
        "labelPriority": copy_payload(
            DATASET_LABEL_PRIORITY
            if is_current_label_schema
            else DATASET_LABEL_PRIORITY_V1
        ),
        "sourceFilters": copy_payload(dataset["sourceFilters"]) if dataset else None,
        "siteId": site["id"],
        "assetId": asset["id"],
        "recordCount": len(rows),
        "sourceRecordCount": source_record_count,
        "normalizedRecordCount": len(rows),
        "splitCounts": split_counts,
        "split": split,
        "splitPolicy": DATASET_SPLIT_POLICY,
        "assignedSplit": populated_splits[0] if len(populated_splits) == 1 else None,
        "checksum": export_checksum,
        "generatedAt": dataset["frozenAt"] if dataset else now_iso(),
    }
    if is_current_label_schema:
        label_counts = {
            status: sum(row.get("label_status") == status for row in rows)
            for status in ("verified", "weak", "unlabeled", "unmapped")
        }
        training_eligible_split_counts = {
            name: sum(
                row.get("training_eligible") is True
                and row.get("dataset_split") == name
                for row in rows
            )
            for name in ("train", "validation", "test")
        }
        manifest.update(
            {
                "labelPolicyVersion": DATASET_LABEL_POLICY_VERSION,
                "snapshotSchemaVersion": DATASET_SNAPSHOT_SCHEMA_VERSION,
                "labelCounts": label_counts,
                "trainingEligibleCount": sum(
                    row.get("training_eligible") is True for row in rows
                ),
                "trainingEligibleSplitCounts": training_eligible_split_counts,
            }
        )
    return {"manifest": manifest, "rows": rows}


def _initialize_base_event_evidence_snapshots() -> None:
    for event in EVENTS:
        try:
            _freeze_event_evidence(event)
        except ApiError as error:
            if error.code != "ASSET_NOT_FOUND":
                raise
            EVENT_EVIDENCE_SNAPSHOTS.setdefault(event["id"], {})[
                "featureSnapshot"
            ] = None
    BASE_EVENT_EVIDENCE_SNAPSHOTS.clear()
    BASE_EVENT_EVIDENCE_SNAPSHOTS.update(copy_payload(EVENT_EVIDENCE_SNAPSHOTS))


_initialize_base_event_evidence_snapshots()
