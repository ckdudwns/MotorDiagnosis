from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MAX_LOGIN_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 60 * 60
MAX_REVIEW_NOTE_LENGTH = 2000
DEVICE_CERTIFICATE_STATUSES = {"pending", "registered", "revoked", "expired"}
DEVICE_MAPPING_STATUSES = {"active", "inactive"}

NETWORK_PROFILES = [
    {
        "id": "NET-DIRECT",
        "type": "A",
        "name": "Direct upload",
        "condition": "Stable wired or Wi-Fi network is available at the site.",
        "architecture": "Sensor node -> MQTT/TLS or HTTP -> central collection API",
        "requiredEquipment": ["Pico 2 W", "device certificate", "stable power"],
        "qualityChecks": ["RSSI", "packet loss", "display latency under 5 seconds"],
    },
    {
        "id": "NET-GATEWAY",
        "type": "B",
        "name": "Gateway relay",
        "condition": "Equipment network is available but external access is unstable.",
        "architecture": "Sensor nodes -> local gateway -> central server",
        "requiredEquipment": ["field gateway", "LTE or wired backhaul", "offline buffer"],
        "qualityChecks": ["gateway power", "site survey", "device heartbeat"],
    },
    {
        "id": "NET-STORE-FWD",
        "type": "C",
        "name": "Limited network",
        "condition": "Always-on communication is hard or bandwidth is constrained.",
        "architecture": "Summary upload + store-and-forward + event priority retry",
        "requiredEquipment": ["local storage", "time sync", "summary feature extraction"],
        "qualityChecks": ["offline retention hours", "capacity threshold", "duplicate removal"],
    },
    {
        "id": "NET-OFFLINE",
        "type": "D",
        "name": "Offline verification",
        "condition": "The site is in early verification or network type is undecided.",
        "architecture": "Local storage and manual export before central ingestion",
        "requiredEquipment": ["field storage media", "manual export tool", "temporary collector"],
        "qualityChecks": ["missing data prevention", "time sync", "field self-check"],
    },
]

NETWORK_PROFILE_IDS = {profile["type"]: profile["id"] for profile in NETWORK_PROFILES}
NETWORK_PROFILE_TYPES = {profile["id"]: profile["type"] for profile in NETWORK_PROFILES}

PROFILE_CYCLE = ["A", "B", "C", "A", "B", "C", "D", "A", "B", "C"]
STATUS_CYCLE = ["normal", "normal", "warning", "normal", "critical", "normal", "device"]
ASSET_TEMPLATES = [
    {"suffix": "GEN-01", "name": "Generator 1", "assetType": "generator", "ratedRpm": 1800},
    {"suffix": "MOT-02", "name": "Cooling pump motor", "assetType": "motor", "ratedRpm": 1450},
    {"suffix": "FAN-03", "name": "Ventilation fan motor", "assetType": "motor", "ratedRpm": 1200},
    {"suffix": "PMP-04", "name": "Fuel transfer pump", "assetType": "pump", "ratedRpm": 1600},
]

PARAMETERS = {
    "TELEMETRY_INTERVAL_SEC": 1,
    "ANOMALY_SCORE_THRESHOLD": 75,
    "ANOMALY_HOLD_SEC": 10,
    "DEVICE_OFFLINE_SEC": 120,
    "EDGE_BUFFER_HOURS": 24,
    "RETENTION_RAW_DAYS": 30,
}

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
            "telemetry:read",
            "export:read",
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
            "export:read",
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


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()


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
        region = "west_coast" if index < 13 else "south_coast" if index < 33 else "east_jeju"
        sites.append(
            {
                "id": f"SITE-{index + 1:02d}",
                "code": f"ISLAND-{index + 1:02d}",
                "name": f"Island Plant {index + 1:02d}",
                "region": region,
                "timezone": "Asia/Seoul",
                "network": profile_type,
                "networkType": profile_type,
                "priority": "high" if index % 5 == 0 else "medium" if index % 3 == 0 else "normal",
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
                    "baselineStatus": "draft" if site["rolloutStage"] == "site_survey" else "ready",
                    "baseline": {
                        "status": "draft" if site["rolloutStage"] == "site_survey" else "ready",
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
            "fingerprint": hashlib.sha256(f"device-certificate-{index}".encode("utf-8")).hexdigest(),
            "status": "registered",
            "issuedAt": "2026-08-01T00:00:00Z",
            "expiresAt": "2027-08-01T00:00:00Z",
        }
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
                "lastSeenSecAgo": 18 + index * 3,
                "health": health,
                "mappingStatus": "active",
                "mappingHistory": [{"assetId": asset["id"], "mappedAt": "initial", "status": "active"}],
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
        raise ApiError(404, "NETWORK_PROFILE_NOT_FOUND", "Network profile was not found.")
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
        target_asset_ids = [asset["id"] for asset in assets if asset["siteId"] == site["id"]]
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


def build_site_network_profile_records(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            "time": "19:42:12",
            "duration": "48s",
            "score": 92,
            "label": "needs_review",
            "note": "Vibration and acoustic features rose together in the same time window.",
        },
        {
            "id": "EV-238",
            "siteId": "SITE-02",
            "assetId": "SITE-02-GEN-01",
            "severity": "warning",
            "eventType": "asset_anomaly_candidate",
            "title": "Generator acoustic spectrum drift",
            "time": "19:31:05",
            "duration": "31s",
            "score": 78,
            "label": "needs_review",
            "note": "High-frequency acoustic band increased compared with the normal baseline.",
        },
        {
            "id": "EV-233",
            "siteId": "SITE-05",
            "assetId": "SITE-05-FAN-03",
            "severity": "device",
            "eventType": "sensor_fault_candidate",
            "title": "Acoustic sensor noise floor fault",
            "time": "19:20:44",
            "duration": "8m",
            "score": 64,
            "label": "sensor_issue",
            "note": "Classified as sensor state event rather than equipment abnormality.",
        },
    ]


BASE_SITES = build_sites()
BASE_ASSETS = build_assets(BASE_SITES)
BASE_DEVICES = build_devices(BASE_ASSETS)
BASE_EVENTS = build_events()
BASE_ROLLOUT_PLANS = build_rollout_plan_records(BASE_SITES, BASE_ASSETS)
BASE_SITE_NETWORK_PROFILES = build_site_network_profile_records(BASE_SITES)
BASE_INSTALL_POINTS = build_install_point_records(BASE_ASSETS)

SITES = copy_payload(BASE_SITES)
ASSETS = copy_payload(BASE_ASSETS)
DEVICES = copy_payload(BASE_DEVICES)
EVENTS = copy_payload(BASE_EVENTS)
ROLLOUT_PLAN_RECORDS = copy_payload(BASE_ROLLOUT_PLANS)
SITE_NETWORK_PROFILE_RECORDS = copy_payload(BASE_SITE_NETWORK_PROFILES)
INSTALL_POINTS = copy_payload(BASE_INSTALL_POINTS)
QUARANTINED_DEVICE_MESSAGES: list[dict[str, Any]] = []


def reset_runtime_state() -> None:
    with STORE_LOCK:
        SITES[:] = copy_payload(BASE_SITES)
        ASSETS[:] = copy_payload(BASE_ASSETS)
        DEVICES[:] = copy_payload(BASE_DEVICES)
        EVENTS[:] = copy_payload(BASE_EVENTS)
        ROLLOUT_PLAN_RECORDS[:] = copy_payload(BASE_ROLLOUT_PLANS)
        SITE_NETWORK_PROFILE_RECORDS[:] = copy_payload(BASE_SITE_NETWORK_PROFILES)
        INSTALL_POINTS[:] = copy_payload(BASE_INSTALL_POINTS)
        QUARANTINED_DEVICE_MESSAGES.clear()
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
            raise ApiError(423, "ACCOUNT_LOCKED", "The account is locked after repeated login failures.")
        if not verify_password(password, user):
            state["failures"] = int(state["failures"]) + 1
            if int(state["failures"]) >= MAX_LOGIN_FAILURES:
                state["lockedUntil"] = time.time() + LOCK_SECONDS
                raise ApiError(423, "ACCOUNT_LOCKED", "The account is locked after repeated login failures.")
            raise ApiError(400, "INVALID_CREDENTIALS", "Check the username or password.")

        state["failures"] = 0
        state["lockedUntil"] = 0.0
        token = "demo-" + secrets.token_urlsafe(24)
        SESSIONS[token] = {
            "userId": user["id"],
            "username": username,
            "expiresAt": time.time() + SESSION_SECONDS,
        }

    session = {"token": token, "issuedAt": now_text(), "expiresInSec": SESSION_SECONDS}
    return {"user": public_user(user), "session": session, "rolePolicy": role_policy(user["role"])}


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
        raise ApiError(403, "FORBIDDEN", "The user does not have permission for this API.")


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
    return copy_payload([site for site in SITES if site["id"] in user["allowedSiteIds"]])


def get_site(site_id: str) -> dict[str, Any]:
    site = next((item for item in SITES if item["id"] == site_id), None)
    if not site:
        raise ApiError(404, "SITE_NOT_FOUND", "Site was not found.")
    return site


def get_asset(site_id: str, asset_id: str) -> dict[str, Any]:
    asset = next((item for item in ASSETS if item["siteId"] == site_id and item["id"] == asset_id), None)
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
    record = next((item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None)
    if not record:
        raise ApiError(404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found.")
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
            "installationReady": bool(record["networkProfileId"] and record["targetAssetIds"]),
        }
    )
    return response


def update_rollout_plan(user: dict[str, Any], site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "rollout:write")
    require_site_access(user, site_id)
    network_profile_id = canonical_network_profile_id(required_text(payload, "networkProfileId"))
    profile_type = network_profile_type(network_profile_id)
    configuration = rollout_configuration_for_profile(network_profile_id)
    target_asset_ids = string_list(payload, "targetAssetIds", required=True, uppercase=True)

    with STORE_LOCK:
        site = get_site(site_id)
        site_asset_ids = {asset["id"] for asset in ASSETS if asset["siteId"] == site_id}
        invalid_asset_ids = [asset_id for asset_id in target_asset_ids if asset_id not in site_asset_ids]
        if invalid_asset_ids:
            raise ApiError(400, "ROLLOUT_ASSET_INVALID", "All rollout target assets must belong to the site.")
        record = next((item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None)
        if not record:
            raise ApiError(404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found.")
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
    return [site_network_profile(record["siteId"]) for record in SITE_NETWORK_PROFILE_RECORDS]


def site_network_profile(site_id: str) -> dict[str, Any]:
    site = get_site(site_id)
    record = next((item for item in SITE_NETWORK_PROFILE_RECORDS if item["siteId"] == site_id), None)
    if not record:
        raise ApiError(404, "SITE_NETWORK_PROFILE_NOT_FOUND", "The site network profile was not found.")
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
    record = next((item for item in SITE_NETWORK_PROFILE_RECORDS if item["siteId"] == site_id), None)
    if not record:
        raise ApiError(404, "SITE_NETWORK_PROFILE_NOT_FOUND", "The site network profile was not found.")
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
    rollout = next((item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None)
    if not rollout:
        raise ApiError(404, "ROLLOUT_PLAN_NOT_FOUND", "The site rollout plan was not found.")
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
    network_profile_id = canonical_network_profile_id(required_text(payload, "networkProfileId"))
    profile_type = network_profile_type(network_profile_id)
    reason = required_text(payload, "reason")

    with STORE_LOCK:
        site = get_site(site_id)
        record = next((item for item in SITE_NETWORK_PROFILE_RECORDS if item["siteId"] == site_id), None)
        if not record:
            raise ApiError(404, "SITE_NETWORK_PROFILE_NOT_FOUND", "The site network profile was not found.")
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


def install_points_for_asset(asset_id: str, active: bool | None = None) -> list[dict[str, Any]]:
    get_asset_by_id(asset_id)
    rows = [item for item in INSTALL_POINTS if item["assetId"] == asset_id]
    if active is not None:
        rows = [item for item in rows if bool(item["active"]) is active]
    return copy_payload(rows)


def create_install_point(user: dict[str, Any], asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "install-point:write")
    asset = get_asset_by_id(asset_id)
    require_site_access(user, asset["siteId"])
    ambient_noise_sources = string_list(payload, "ambientNoiseSources")
    photo_refs = string_list(payload, "photoRefs", required=True)

    with STORE_LOCK:
        install_point_id = str(payload.get("id") or f"IP-{asset_id}-{len(install_points_for_asset(asset_id)) + 1:02d}").strip().upper()
        if any(item["id"] == install_point_id for item in INSTALL_POINTS):
            raise ApiError(400, "INSTALL_POINT_DUPLICATED", "Install point ID is duplicated.")
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
            (item for item in INSTALL_POINTS if item["assetId"] == asset_id and item["id"] == install_point_id),
            None,
        )
        if not install_point:
            raise ApiError(404, "INSTALL_POINT_NOT_FOUND", "Install point was not found.")
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
                {"reason": reason, "changedAt": candidate["updatedAt"], "before": before, "after": after}
            )
        install_point.update(candidate)
        return copy_payload(install_point)


def parse_int_value(field: str, value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be a valid number.") from exc


def parse_float_value(field: str, value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be a valid number.") from exc
    if not math.isfinite(parsed):
        raise ApiError(400, "INVALID_NUMBER", f"{field} must be a finite number.")
    return parsed


def parse_int_field(payload: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in payload:
            return parse_int_value(key, payload[key], default)
    return default


def boolean_field(payload: dict[str, Any], key: str, default: bool | None = None) -> bool:
    if key not in payload:
        if default is None:
            raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
        return default
    value = payload[key]
    if isinstance(value, bool):
        return value
    raise ApiError(400, "INVALID_BOOLEAN", f"{key} must be a boolean.")


def string_list(
    payload: dict[str, Any], key: str, *, required: bool = False, uppercase: bool = False
) -> list[str]:
    value = payload.get(key)
    if value is None:
        if required:
            raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ApiError(400, "INVALID_LIST", f"{key} must be a list of non-empty strings.")
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


def update_site(user: dict[str, Any], site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "site:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        site = get_site(site_id)
        candidate = copy_payload(site)
        if "code" in payload:
            next_code = str(payload["code"]).strip().upper()
            if any(item["id"] != site_id and item["code"] == next_code for item in SITES):
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
    return update_site(user, site_id, {"operationStatus": "inactive", "status": "inactive"})


def delete_site(user: dict[str, Any], site_id: str) -> dict[str, Any]:
    require_permission(user, "site:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        get_site(site_id)
        if any(asset["siteId"] == site_id for asset in ASSETS):
            raise ApiError(400, "SITE_HAS_ASSETS", "A site with assets cannot be deleted.")
        SITES[:] = [site for site in SITES if site["id"] != site_id]
        ROLLOUT_PLAN_RECORDS[:] = [record for record in ROLLOUT_PLAN_RECORDS if record["siteId"] != site_id]
        SITE_NETWORK_PROFILE_RECORDS[:] = [
            record for record in SITE_NETWORK_PROFILE_RECORDS if record["siteId"] != site_id
        ]
        return {"deleted": True, "siteId": site_id}


def create_asset(user: dict[str, Any], site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "asset:write")
    require_site_access(user, site_id)
    asset_code = required_text(payload, "assetCode").upper()
    asset_id = str(payload.get("id") or f"{site_id}-{asset_code}").strip().upper()

    with STORE_LOCK:
        get_site(site_id)
        if any(asset["id"] == asset_id for asset in ASSETS):
            raise ApiError(400, "ASSET_DUPLICATED", "Asset ID is duplicated.")
        if any(asset["siteId"] == site_id and asset["assetCode"] == asset_code for asset in ASSETS):
            raise ApiError(400, "ASSET_CODE_DUPLICATED", "Asset code must be unique within a site.")
        baseline = baseline_payload(payload)
        timestamp = now_iso()
        asset = {
            "id": asset_id,
            "assetCode": asset_code,
            "siteId": site_id,
            "name": required_text(payload, "name"),
            "type": str(payload.get("assetType") or payload.get("type") or "motor"),
            "assetType": str(payload.get("assetType") or payload.get("type") or "motor"),
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
        get_site(site_id)["assetCount"] += 1
        return copy_payload(asset)


def update_asset(user: dict[str, Any], site_id: str, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "asset:write")
    require_site_access(user, site_id)
    with STORE_LOCK:
        asset = get_asset(site_id, asset_id)
        candidate = copy_payload(asset)
        if "assetCode" in payload:
            next_code = str(payload["assetCode"]).strip().upper()
            if any(
                item["id"] != asset_id and item["siteId"] == site_id and item["assetCode"] == next_code
                for item in ASSETS
            ):
                raise ApiError(400, "ASSET_CODE_DUPLICATED", "Asset code must be unique within a site.")
            candidate["assetCode"] = next_code
        for key in ("name", "operationStatus", "installLocation", "baselineStatus"):
            if key in payload:
                candidate[key] = str(payload[key])
        if "status" in payload:
            candidate["operationStatus"] = str(payload["status"])
        if "assetType" in payload or "type" in payload:
            candidate["assetType"] = str(payload.get("assetType") or payload.get("type"))
            candidate["type"] = candidate["assetType"]
        if "ratedRpm" in payload or "rpm" in payload:
            candidate["ratedRpm"] = parse_int_field(payload, "ratedRpm", "rpm", default=candidate["ratedRpm"])
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
        if any(device["assetId"] == asset_id and device["mappingStatus"] == "active" for device in DEVICES):
            raise ApiError(400, "ASSET_HAS_DEVICE", "An asset with an active device mapping cannot be deleted.")
        ASSETS[:] = [asset for asset in ASSETS if asset["id"] != asset_id]
        INSTALL_POINTS[:] = [item for item in INSTALL_POINTS if item["assetId"] != asset_id]
        rollout = next((item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None)
        if rollout and asset_id in rollout["targetAssetIds"]:
            rollout["targetAssetIds"] = [item for item in rollout["targetAssetIds"] if item != asset_id]
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
            "Complete the asset master data and ready baseline before device mapping: " + ", ".join(missing),
        )

    rollout = next((item for item in ROLLOUT_PLAN_RECORDS if item["siteId"] == site_id), None)
    if not rollout or not rollout.get("networkProfileId") or asset_id not in rollout.get("targetAssetIds", []):
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
        raise ApiError(400, "INVALID_CERTIFICATE_STATUS", "certificateStatus is not allowed.")
    if not certificate["id"] or not certificate["fingerprint"]:
        raise ApiError(
            400,
            "DEVICE_CERTIFICATE_REQUIRED",
            "certificateId and certificateFingerprint are required for device registration.",
        )
    return certificate


def validate_certificate_for_mapping(certificate: dict[str, Any], mapping_status: str) -> None:
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
    normalized = expires_at_text[:-1] + "+00:00" if expires_at_text.endswith("Z") else expires_at_text
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
        raise ApiError(409, "CERTIFICATE_EXPIRED", "An active device mapping cannot use an expired certificate.")


def validate_mapping_status(mapping_status: str) -> None:
    if mapping_status not in DEVICE_MAPPING_STATUSES:
        raise ApiError(400, "INVALID_MAPPING_STATUS", "mappingStatus must be active or inactive.")


def apply_certificate_fields(device: dict[str, Any], certificate: dict[str, Any]) -> None:
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


def create_device(user: dict[str, Any], site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
            device["assetId"] == asset_id and device["mappingStatus"] == "active" for device in DEVICES
        ):
            raise ApiError(400, "DEVICE_ASSET_DUPLICATED", "The asset already has an active device mapping.")
        sensor_channels = string_list(
            {"sensorChannels": payload.get("sensorChannels") or ["vibration", "acoustic", "rpm"]},
            "sensorChannels",
            required=True,
        )
        firmware_version = str(payload.get("firmwareVersion") or payload.get("firmware") or "edge-0.1.0")
        certificate = certificate_payload(payload)
        validate_certificate_for_mapping(certificate, mapping_status)
        timestamp = now_iso()
        device = {
            "id": device_id,
            "siteId": site_id,
            "assetId": asset_id,
            "sensorChannels": sensor_channels,
            "firmware": firmware_version,
            "firmwareVersion": firmware_version,
            "lastSeenSecAgo": parse_int_field(payload, "lastSeenSecAgo", default=0),
            "health": str(payload.get("health") or "online"),
            "mappingStatus": mapping_status,
            "mappingHistory": [{"assetId": asset_id, "mappedAt": now_text(), "status": mapping_status}],
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


def update_device(user: dict[str, Any], device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require_permission(user, "device:write")
    with STORE_LOCK:
        device = get_device(device_id)
        require_site_access(user, device["siteId"])
        candidate = copy_payload(device)
        previous_health = str(device.get("health") or "")
        previous_asset_id = str(device["assetId"])
        next_asset_id = str(payload.get("assetId", previous_asset_id)).strip().upper()
        next_mapping_status = str(
            payload.get("mappingStatus", device.get("mappingStatus") or "active")
        ).strip().lower()
        validate_mapping_status(next_mapping_status)
        get_asset(device["siteId"], next_asset_id)
        if next_asset_id != previous_asset_id or (
            next_mapping_status == "active" and device.get("mappingStatus") != "active"
        ):
            validate_asset_device_mapping(device["siteId"], next_asset_id)
        if next_mapping_status == "active" and any(
            item["id"] != device_id and item["assetId"] == next_asset_id and item["mappingStatus"] == "active"
            for item in DEVICES
        ):
            raise ApiError(400, "DEVICE_ASSET_DUPLICATED", "The asset already has an active device mapping.")
        if next_asset_id != previous_asset_id or next_mapping_status != device.get("mappingStatus"):
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
            candidate["sensorChannels"] = string_list(payload, "sensorChannels", required=True)
        if "firmware" in payload or "firmwareVersion" in payload:
            firmware_version = str(payload.get("firmwareVersion") or payload.get("firmware") or "").strip()
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
            and (device.get("mappingStatus") != "active" or next_asset_id != previous_asset_id)
        ):
            validate_certificate_for_mapping(next_certificate, next_mapping_status)
        if "lastSeenSecAgo" in payload:
            candidate["lastSeenSecAgo"] = parse_int_value("lastSeenSecAgo", payload["lastSeenSecAgo"])
        before = device_replacement_snapshot(device)
        after = device_replacement_snapshot(candidate)
        if before != after:
            reason = str(payload.get("replacementReason") or payload.get("reason") or "").strip()
            if not reason:
                raise ApiError(
                    400,
                    "REPLACEMENT_REASON_REQUIRED",
                    "reason is required when device mapping, firmware, sensors, or certificate changes.",
                )
            replaced_at = now_iso()
            candidate.setdefault("replacementHistory", []).append(
                {"reason": reason, "replacedAt": replaced_at, "before": before, "after": after}
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
            raise ApiError(400, "DEVICE_REGISTERED", "The device is already registered.")
        record = {
            "id": f"Q-{len(QUARANTINED_DEVICE_MESSAGES) + 1:04d}",
            "deviceId": device_id,
            "reason": "unregistered_device",
            "receivedAt": now_text(),
            "payload": copy_payload(payload),
        }
        QUARANTINED_DEVICE_MESSAGES.append(record)
        return copy_payload(record)


def telemetry_for(site_id: str, asset_id: str) -> list[dict[str, Any]]:
    asset = get_asset(site_id, asset_id)
    seed = sum(ord(ch) for ch in asset["id"])
    points = []
    for index in range(72):
        vibration = 1.8 + math.sin(index / 7 + seed / 11) * 0.28 + ((seed + index * 13) % 19) / 100
        acoustic = 52 + math.sin(index / 8 + seed / 8) * 4 + ((seed + index * 7) % 9) / 2
        rpm = asset["ratedRpm"] + math.sin(index / 11) * 18 + ((seed + index * 3) % 12)
        score = 34 + max(0, vibration - 1.9) * 18 + max(0, acoustic - 54) * 1.6
        if asset["id"].endswith("MOT-02") and index > 52:
            score += 25
        points.append(
            {
                "minute": index - 71,
                "timestamp": index - 71,
                "vibration": round(vibration, 2),
                "vibrationRmsMmS": round(vibration, 2),
                "acoustic": round(acoustic, 1),
                "acousticDb": round(acoustic, 1),
                "rpm": round(rpm),
                "score": min(98, round(score)),
                "anomalyScore": min(98, round(score)),
            }
        )
    return points


def telemetry_units() -> dict[str, str]:
    return {
        "vibrationRmsMmS": "mm/s RMS",
        "acousticDb": "dB",
        "rpm": "rev/min",
        "anomalyScore": "0-100",
    }


def inject_anomaly(payload: dict[str, Any]) -> dict[str, Any]:
    site_id = str(payload.get("siteId") or "SITE-01").strip().upper()
    with STORE_LOCK:
        site = get_site(site_id)
        asset_id = str(payload.get("assetId") or assets_for(site["id"])[0]["id"]).strip().upper()
        asset = get_asset(site["id"], asset_id)
        event_id = f"EV-{250 + len(EVENTS)}"
        event = {
            "id": event_id,
            "siteId": site["id"],
            "assetId": asset["id"],
            "severity": "critical",
            "eventType": "asset_anomaly_candidate",
            "title": f"{asset['name']} anomaly injection event",
            "time": now_text(),
            "duration": "10s",
            "score": 94,
            "label": "needs_review",
            "note": "This event was generated by the demo anomaly injection API.",
        }
        EVENTS.insert(0, event)
        site["status"] = "critical"
        site["eventCount"] = int(site["eventCount"]) + 1
        return copy_payload(event)


def review_event(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with STORE_LOCK:
        event = next((item for item in EVENTS if item["id"] == event_id), None)
        if not event:
            raise ApiError(404, "EVENT_NOT_FOUND", "Event was not found.")
        label = str(payload.get("label", event["label"]))
        if label not in {"needs_review", "normal_false_positive", "confirmed_anomaly", "repair_completed", "sensor_issue"}:
            raise ApiError(400, "INVALID_EVENT_LABEL", "Event label is not allowed.")
        note = str(payload.get("note", event["note"]))
        if len(note) > MAX_REVIEW_NOTE_LENGTH:
            raise ApiError(400, "NOTE_TOO_LONG", "Review note must be 2000 characters or less.")
        event["label"] = label
        event["note"] = note
        event["reviewedAt"] = now_text()
        return copy_payload(event)


def baseline_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "vibrationRmsMmS": parse_float_value("baseline.vibrationRmsMmS", source.get("vibrationRmsMmS"), 0.0),
        "acousticDb": parse_float_value("baseline.acousticDb", source.get("acousticDb"), 0.0),
        "sampleCount": parse_int_value("baseline.sampleCount", source.get("sampleCount"), 0),
    }


def required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ApiError(400, "MISSING_FIELD", f"{key} is required.")
    return value
