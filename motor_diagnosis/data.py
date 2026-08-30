from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_LOGIN_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 60 * 60
MAX_REVIEW_NOTE_LENGTH = 2000
MAX_REASON_LENGTH = 1000
MAX_OPERATING_CONDITION_DEPTH = 8
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
ALERT_CHANNELS = {"web", "email", "webhook", "stub"}
ENVIRONMENT_INSPECTION_STATUSES = {
    "not_checked",
    "ok",
    "attention",
    "critical",
}
DATASET_SPLIT_POLICY = "asset_grouped"
DATASET_LABEL_PRIORITY = [
    "event_review",
    "known_vibration_label",
    "known_acoustic_label",
    "scenario_label",
    "event_candidate",
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
        "requiredEquipment": ["Pico 2 W", "device certificate", "stable power"],
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
    },
    "ANOMALY_SCORE_THRESHOLD": {
        "category": "anomaly",
        "type": "number",
        "minimum": 0,
        "maximum": 100,
        "unit": "score",
    },
    "ANOMALY_HOLD_SEC": {
        "category": "anomaly",
        "type": "integer",
        "minimum": 1,
        "maximum": 3600,
        "unit": "seconds",
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
}

HARDWARE_PROFILES = [
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


STORE_LOCK = threading.RLock()
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
                "firmware": "edge-0.1.0",
                "firmwareVersion": "edge-0.1.0",
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
                "hardwareProfileId": "HW-PICO2W-DUAL-SENSOR-WIFI",
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
            "scoreThreshold": 75.0,
            "durationSec": 10,
            "hysteresis": 5.0,
            "mergeWindowSec": 30,
            "active": True,
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
TELEMETRY_RECORDS: list[dict[str, Any]] = []
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
DATASET_SNAPSHOTS: dict[str, dict[str, list[dict[str, Any]]]] = {}
ACOUSTIC_TAXONOMY_VERSIONS: list[dict[str, Any]] = [
    {
        "version": "ACOUSTIC-V1",
        "labels": copy_payload(ACOUSTIC_LABEL_TAXONOMY),
        "active": True,
        "reason": "Initial acoustic label taxonomy",
        "updatedAt": "2026-08-24T00:00:00Z",
    }
]


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
        TELEMETRY_RECORDS.clear()
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
        EVENT_EVIDENCE_SNAPSHOTS.clear()
        EVENT_EVIDENCE_SNAPSHOTS.update(copy_payload(BASE_EVENT_EVIDENCE_SNAPSHOTS))
        AUDIT_LOGS.clear()
        ENVIRONMENT_INSPECTIONS.clear()
        DATASET_VERSIONS.clear()
        DATASET_SNAPSHOTS.clear()
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
            "ratedRpm": parse_int_field(payload, "ratedRpm", "rpm", default=0),
            "rpm": parse_int_field(payload, "ratedRpm", "rpm", default=0),
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
            candidate["ratedRpm"] = parse_int_field(
                payload, "ratedRpm", "rpm", default=candidate["ratedRpm"]
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


def create_device(
    user: dict[str, Any], site_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    require_permission(user, "device:write")
    require_site_access(user, site_id)
    device_id = required_text(payload, "id").upper()
    asset_id = required_text(payload, "assetId").upper()

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
            payload.get("firmwareVersion") or payload.get("firmware") or "edge-0.1.0"
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
            str(payload.get("hardwareProfileId") or "HW-PICO2W-DUAL-SENSOR-WIFI")
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


def delete_device(user: dict[str, Any], device_id: str) -> dict[str, Any]:
    require_permission(user, "device:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
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
    if service:
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


def normalize_telemetry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "timestamp",
        "sequence",
        "siteId",
        "assetId",
        "deviceId",
        "rpm",
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

    scenario_label = payload.get("scenarioLabel")
    if scenario_label not in TELEMETRY_SCENARIOS:
        raise ApiError(
            400,
            "INVALID_TELEMETRY_PAYLOAD",
            "scenarioLabel is not supported.",
        )
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
        "rpm": telemetry_number(payload, "rpm", required=False, nullable=True),
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

    topic_parts = [part for part in topic.strip("/").split("/") if part]
    device_id = None
    if (
        len(topic_parts) == 3
        and topic_parts[0] == "devices"
        and topic_parts[2] == "telemetry"
    ):
        device_id = topic_parts[1].strip().upper() or None

    with STORE_LOCK:
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
        }
        QUARANTINED_DEVICE_MESSAGES.append(record)
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
        _freeze_event_evidence(recovery_event)


def ingest_telemetry(
    principal: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    with STORE_LOCK:
        TELEMETRY_METRICS["requests"] = int(TELEMETRY_METRICS["requests"]) + 1
        try:
            record = normalize_telemetry_payload(payload)
            principal_can_ingest(principal, record["deviceId"])
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
                TELEMETRY_METRICS["duplicates"] = (
                    int(TELEMETRY_METRICS["duplicates"]) + 1
                )
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
            stored_record = {**record, "receivedAt": received_at}
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
            TELEMETRY_METRICS["accepted"] = int(TELEMETRY_METRICS["accepted"]) + 1
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
                },
                201,
            )
        except ApiError as error:
            TELEMETRY_METRICS["rejected"] = int(TELEMETRY_METRICS["rejected"]) + 1
            if error.code == "SEQUENCE_CONFLICT":
                TELEMETRY_METRICS["conflicts"] = int(TELEMETRY_METRICS["conflicts"]) + 1
            quarantine_telemetry_error(payload, error)
            latency_ms = (time.monotonic() - started) * 1000
            TELEMETRY_METRICS["lastLatencyMs"] = round(latency_ms, 2)
            update_ingest_dependency(False, latency_ms, error)
            raise


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
) -> list[dict[str, Any]]:
    get_asset(site_id, asset_id)
    stored = [
        copy_payload(record)
        for record in TELEMETRY_RECORDS
        if record["siteId"] == site_id and record["assetId"] == asset_id
    ]
    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None
    if from_value and to_value and from_value > to_value:
        raise ApiError(
            400, "INVALID_TIME_RANGE", "from must be earlier than or equal to to."
        )
    filtered = []
    for record in stored:
        timestamp = parse_rfc3339("timestamp", record["timestamp"])
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
) -> list[dict[str, Any]]:
    asset = get_asset(site_id, asset_id)
    stored = _stored_telemetry_for(
        site_id,
        asset_id,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
    )
    if stored or any(
        record["siteId"] == site_id and record["assetId"] == asset_id
        for record in TELEMETRY_RECORDS
    ):
        return stored

    from_value = parse_rfc3339("from", from_timestamp) if from_timestamp else None
    to_value = parse_rfc3339("to", to_timestamp) if to_timestamp else None

    seed = sum(ord(ch) for ch in asset["id"])
    points = []
    current = datetime.now(timezone.utc)
    for index in range(72):
        vibration = (
            1.8
            + math.sin(index / 7 + seed / 11) * 0.28
            + ((seed + index * 13) % 19) / 100
        )
        acoustic = (
            52 + math.sin(index / 8 + seed / 8) * 4 + ((seed + index * 7) % 9) / 2
        )
        rpm = asset["ratedRpm"] + math.sin(index / 11) * 18 + ((seed + index * 3) % 12)
        score = 34 + max(0, vibration - 1.9) * 18 + max(0, acoustic - 54) * 1.6
        if asset["id"].endswith("MOT-02") and index > 52:
            score += 25
        points.append(
            {
                "minute": index - 71,
                "timestamp": format_rfc3339(current - timedelta(minutes=71 - index)),
                "vibration": round(vibration, 2),
                "vibrationRmsMmS": round(vibration, 2),
                "acoustic": round(acoustic, 1),
                "acousticDb": round(acoustic, 1),
                "rpm": round(rpm),
                "score": min(98, round(score)),
                "anomalyScore": min(98, round(score)),
            }
        )
    if not from_value and not to_value:
        return points
    return [
        point
        for point in points
        if (
            not from_value
            or parse_rfc3339("timestamp", point["timestamp"]) >= from_value
        )
        and (not to_value or parse_rfc3339("timestamp", point["timestamp"]) <= to_value)
    ]


def telemetry_units(
    site_id: str | None = None, asset_id: str | None = None
) -> dict[str, str]:
    if (
        site_id
        and asset_id
        and any(
            record["siteId"] == site_id and record["assetId"] == asset_id
            for record in TELEMETRY_RECORDS
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
        device["lastSeenSecAgo"] = max(0, elapsed)
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
            _freeze_event_evidence(event)
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
            "lastSeenSecAgo": device["lastSeenSecAgo"],
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
        }


def dashboard_sites_summary(
    user: dict[str, Any],
    region: str = "",
    status: str = "",
    live_asset_statuses: dict[str, str] | None = None,
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
    dependencies = copy_payload(SERVICE_DEPENDENCIES)
    degraded = any(item["status"] not in {"healthy", "ready"} for item in dependencies)
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
            device.get("hardwareProfileId") or "HW-PICO2W-DUAL-SENSOR-WIFI"
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
    site_id = str(payload.get("siteId") or "SITE-01").strip().upper()
    with STORE_LOCK:
        site = get_site(site_id)
        asset_id = (
            str(payload.get("assetId") or assets_for(site["id"])[0]["id"])
            .strip()
            .upper()
        )
        asset = get_asset(site["id"], asset_id)
        event_id = f"EV-{250 + len(EVENTS)}"
        event = {
            "id": event_id,
            "siteId": site["id"],
            "assetId": asset["id"],
            "severity": "critical",
            "eventType": "asset_anomaly_candidate",
            "title": f"{asset['name']} anomaly injection event",
            "occurredAt": now_iso(),
            "time": now_text(),
            "duration": "10s",
            "durationSec": 10,
            "score": 94,
            "maxScore": 94,
            "label": "needs_review",
            "note": "This event was generated by the demo anomaly injection API.",
            "reviewed": False,
            "status": "open",
            "thresholdVersion": anomaly_rule_for(asset["id"])["version"],
            "modelVersion": "demo-anomaly-injection-v1",
        }
        _freeze_event_evidence(event)
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
        event["reviewed"] = True
        event["reviewedAt"] = changed_at
        event["reviewedBy"] = user["id"]
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
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
    return value


def get_event(event_id: str) -> dict[str, Any]:
    normalized = event_id.strip().upper()
    event = next((item for item in EVENTS if item["id"] == normalized), None)
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
    event: dict[str, Any], points: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    with STORE_LOCK:
        evidence = EVENT_EVIDENCE_SNAPSHOTS.setdefault(event["id"], {})

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
                points = telemetry_for(
                    event["siteId"],
                    event["assetId"],
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


def event_detail_for(user: dict[str, Any], event_id: str) -> dict[str, Any]:
    require_permission(user, "event:read")
    event = get_event(event_id)
    require_site_access(user, event["siteId"])
    occurred_at = parse_rfc3339("event.occurredAt", event["occurredAt"])
    context_from = format_rfc3339(occurred_at - timedelta(minutes=5))
    context_to = format_rfc3339(occurred_at + timedelta(minutes=5))
    points = telemetry_for(
        event["siteId"],
        event["assetId"],
        from_timestamp=context_from,
        to_timestamp=context_to,
    )
    context_from_value = occurred_at - timedelta(minutes=5)
    context_to_value = occurred_at + timedelta(minutes=5)
    has_stored_raw = any(
        record["siteId"] == event["siteId"]
        and record["assetId"] == event["assetId"]
        and context_from_value
        <= parse_rfc3339("telemetry.timestamp", record["timestamp"])
        <= context_to_value
        for record in TELEMETRY_RECORDS
    )
    context_source = "stored" if has_stored_raw else "demo" if points else "unavailable"
    evidence = _freeze_event_evidence(event, points)
    latest_review = next(
        (
            item
            for item in reversed(EVENT_REVIEW_HISTORY)
            if item["eventId"] == event["id"]
        ),
        None,
    )
    threshold_version = str(event.get("thresholdVersion") or "").strip()
    applied_rule = (
        anomaly_rule_version_for(event["assetId"], threshold_version)
        if threshold_version
        else None
    )
    return {
        "event": copy_payload(event),
        "context": {
            "from": context_from,
            "to": context_to,
            "points": copy_payload(points),
            "units": telemetry_units(event["siteId"], event["assetId"]),
            "source": context_source,
            "rawDataMissing": not has_stored_raw,
        },
        "featureSnapshot": evidence["featureSnapshot"],
        "appliedRule": applied_rule,
        "modelVersion": event.get("modelVersion"),
        "deviceSnapshot": evidence["deviceSnapshot"],
        "latestReview": copy_payload(latest_review) if latest_review else None,
    }


def _bounded_number(
    field: str, value: Any, minimum: float, maximum: float, *, integer: bool = False
) -> int | float:
    if isinstance(value, bool):
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
        if rule is None:
            rule = build_anomaly_rules([asset])[0]
            rule["reason"] = "Initial anomaly rule for existing asset"
            rule["updatedAt"] = now_iso()
            ANOMALY_RULES.append(rule)
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
        if hysteresis >= score_threshold:
            raise ApiError(
                400,
                "INVALID_HYSTERESIS",
                "hysteresis must be lower than scoreThreshold.",
            )
        rule["history"].append(before)
        rule.update(
            {
                "version": f"RULE-{asset['id']}-v{len(rule['history']) + 1}",
                "scoreThreshold": score_threshold,
                "durationSec": duration_sec,
                "hysteresis": hysteresis,
                "mergeWindowSec": merge_window_sec,
                "active": boolean_field(payload, "active", default=rule["active"]),
                "reason": reason,
                "updatedAt": now_iso(),
            }
        )
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
        datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ApiError(400, "INVALID_TIME_OF_DAY", f"{field} must use HH:MM.") from exc
    return text


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
    value = _bounded_number(
        "value",
        payload["value"],
        float(definition["minimum"]),
        float(definition["maximum"]),
        integer=definition["type"] == "integer",
    )
    with STORE_LOCK:
        before = PARAMETERS[normalized_key]
        PARAMETERS[normalized_key] = value
        updated_at = now_iso()
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
        if (
            event.get("eventType") == "sensor_fault_candidate"
            and event["assetId"] == device["assetId"]
        ):
            rows.append(
                {
                    **_sensor_fault_record(
                        device,
                        "sensor_event",
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
            "status": "frozen",
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
        record["versionFingerprint"] = _dataset_version_fingerprint(
            source,
            compatibility,
            source_filters,
            label_taxonomy_version,
            normalized_mapping,
            split,
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


def _dataset_label_for_event(
    dataset: dict[str, Any] | None, event: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    if not event:
        return None, None
    source_label = str(event.get("label") or "").strip()
    if not source_label:
        return None, None
    if not dataset:
        return source_label, None
    label_mapping = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in dataset.get("labelMapping", {}).items()
    }
    mapped_label = label_mapping.get(source_label.casefold())
    if not mapped_label:
        raise ApiError(
            409,
            "DATASET_LABEL_MAPPING_MISSING",
            f"No labelMapping entry exists for event label '{source_label}'.",
        )
    return mapped_label, dataset["labelTaxonomyVersion"]


def _dataset_target_label(
    dataset: dict[str, Any] | None, source_label: Any
) -> tuple[str | None, str | None]:
    normalized = str(source_label or "").strip()
    if not normalized:
        return None, None
    if not dataset:
        return normalized, None
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
            for item in ACOUSTIC_TAXONOMY_VERSIONS
            if item["version"] == dataset["labelTaxonomyVersion"]
        ),
        None,
    )
    taxonomy_codes = {
        str(item["code"]).strip().upper() for item in (taxonomy or {}).get("labels", [])
    }
    direct_code = normalized.upper()
    if direct_code in taxonomy_codes:
        return direct_code, dataset["labelTaxonomyVersion"]
    return None, None


def _dataset_ground_truth(
    dataset: dict[str, Any] | None,
    point: dict[str, Any],
    matching_event: dict[str, Any] | None,
    event_label: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if (
        matching_event
        and matching_event.get("reviewed")
        and str(matching_event.get("label") or "").strip()
    ):
        return (
            str(matching_event["label"]).strip(),
            "event_review",
            event_label,
            dataset["labelTaxonomyVersion"] if dataset and event_label else None,
        )
    candidates = (
        ("knownVibrationLabel", "known_vibration_label"),
        ("knownAcousticLabel", "known_acoustic_label"),
        ("scenarioLabel", "scenario_label"),
    )
    for field, source in candidates:
        raw_label = str(point.get(field) or "").strip()
        if not raw_label:
            continue
        target_label, taxonomy_version = _dataset_target_label(dataset, raw_label)
        return raw_label, source, target_label, taxonomy_version
    if matching_event and str(matching_event.get("label") or "").strip():
        return (
            str(matching_event["label"]).strip(),
            "event_candidate",
            event_label,
            dataset["labelTaxonomyVersion"] if dataset and event_label else None,
        )
    return None, None, None, None


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


def _dataset_rows_for_points(
    dataset: dict[str, Any] | None,
    site_id: str,
    asset_id: str,
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    asset_events = sorted(
        (item for item in EVENTS if item["assetId"] == asset_id),
        key=lambda item: item["occurredAt"],
        reverse=True,
    )
    rows = []
    for point in points:
        matching_event = _event_for_dataset_point(asset_events, point.get("timestamp"))
        event_label, label_taxonomy_version = _dataset_label_for_event(
            dataset, matching_event
        )
        (
            ground_truth_label,
            ground_truth_source,
            target_label,
            target_label_taxonomy_version,
        ) = _dataset_ground_truth(dataset, point, matching_event, event_label)
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
            "scenario_label": point.get("scenarioLabel"),
            "known_vibration_label": point.get("knownVibrationLabel"),
            "known_acoustic_label": point.get("knownAcousticLabel"),
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
        }
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
        points = telemetry_for(
            site["id"],
            asset["id"],
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
        )
        rows = _dataset_rows_for_points(None, site["id"], asset["id"], points)
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
    manifest = {
        "datasetId": dataset["id"] if dataset else None,
        "exportType": "internal_telemetry",
        "source": source,
        "compatibility": (
            dataset["compatibility"]
            if dataset
            else {
                "signalType": sorted(telemetry_units(site["id"], asset["id"])),
                "samplingRateHz": None,
                "units": telemetry_units(site["id"], asset["id"]),
                "operatingConditions": {
                    "ratedRpm": asset.get("ratedRpm"),
                    "source": "live_or_demo_telemetry",
                },
            }
        ),
        "labelTaxonomyVersion": dataset["labelTaxonomyVersion"] if dataset else None,
        "labelMapping": dataset["labelMapping"] if dataset else {},
        "labelPriority": copy_payload(DATASET_LABEL_PRIORITY),
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
