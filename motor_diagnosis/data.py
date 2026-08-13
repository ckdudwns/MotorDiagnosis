from __future__ import annotations

import math
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


NETWORK_PROFILES = [
    {
        "type": "A",
        "name": "직접 전송형",
        "condition": "Wi-Fi 또는 유선망이 안정적인 발전소",
        "architecture": "센서 단말 -> MQTT/TLS 또는 HTTP -> 중앙 수집 API",
        "requiredEquipment": ["Pico 2 W", "장치 인증서", "전원 어댑터"],
        "qualityChecks": ["RSSI", "패킷 손실률", "5초 이내 표시 지연"],
    },
    {
        "type": "B",
        "name": "게이트웨이형",
        "condition": "설비 주변망은 가능하지만 외부망이 불안정한 발전소",
        "architecture": "여러 센서 단말 -> 현장 게이트웨이 -> 중앙 서버",
        "requiredEquipment": ["현장 게이트웨이", "LTE/유선 백홀", "오프라인 버퍼"],
        "qualityChecks": ["게이트웨이 전원", "재전송 순서", "장치별 식별"],
    },
    {
        "type": "C",
        "name": "제한망형",
        "condition": "상시 통신이 어렵거나 대역폭이 낮은 발전소",
        "architecture": "요약 전송 + Store-and-forward + 이벤트 중심 원시 구간 업로드",
        "requiredEquipment": ["로컬 저장소", "시간 동기화", "요약 특징값 생성"],
        "qualityChecks": ["오프라인 보존 시간", "용량 초과 정책", "중복 제거"],
    },
    {
        "type": "D",
        "name": "독립 검증형",
        "condition": "통신 실사 전 또는 통신 구축이 지연되는 초기 검증 현장",
        "architecture": "로컬 저장 후 수동 반출 또는 임시망으로 분석 서버 반영",
        "requiredEquipment": ["현장 저장 매체", "수동 반출 절차", "임시 수집 노트북"],
        "qualityChecks": ["데이터 누락 방지", "시간 동기화", "현장 점검 절차"],
    },
]

ISLAND_NAMES = [
    "울릉",
    "추자",
    "백령",
    "소청",
    "대청",
    "비금",
    "홍도",
    "가거",
    "덕적",
    "연평",
    "거문",
    "욕지",
    "우도",
    "흑산",
    "금오",
    "하조",
    "상조",
    "자은",
    "암태",
    "팔금",
    "안좌",
    "장산",
    "노화",
    "보길",
    "청산",
    "소안",
    "신의",
    "도초",
    "압해",
    "임자",
    "낙월",
    "위도",
    "선유",
    "어청",
    "개야",
    "삽시",
    "원산",
    "외연",
    "장고",
    "연화",
    "사량",
    "한산",
    "매물",
    "가조",
    "칠천",
    "비양",
    "마라도",
    "가파",
    "추봉",
    "두미",
    "국도",
    "소매물",
    "장봉",
    "신도",
    "시도",
    "모도",
    "무의",
    "영흥",
    "이작",
    "승봉",
    "자월",
    "풍도",
    "육도",
    "효자",
    "난지도",
]

PROFILE_CYCLE = ["A", "B", "C", "A", "B", "C", "D", "A", "B", "C"]
STATUS_CYCLE = ["normal", "normal", "warning", "normal", "critical", "normal", "device"]
ASSET_TEMPLATES = [
    {"suffix": "GEN-01", "name": "1호 발전기", "assetType": "발전기", "ratedRpm": 1800},
    {"suffix": "MOT-02", "name": "냉각수 펌프", "assetType": "펌프", "ratedRpm": 1450},
    {"suffix": "FAN-03", "name": "환기 모터", "assetType": "모터", "ratedRpm": 1200},
    {"suffix": "PMP-04", "name": "연료 이송 펌프", "assetType": "펌프", "ratedRpm": 1600},
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
        "password": "operator123",
        "name": "운영자",
        "role": "A",
        "allowedSiteIds": ["SITE-01", "SITE-02", "SITE-03", "SITE-04"],
    },
    {
        "id": "user-admin",
        "username": "admin",
        "password": "admin123",
        "name": "관리자",
        "role": "B",
        "allowedSiteIds": ["*"],
    },
    {
        "id": "user-system",
        "username": "system",
        "password": "system123",
        "name": "시스템 관리자",
        "role": "C",
        "allowedSiteIds": ["*"],
    },
]

ROLE_POLICIES = [
    {
        "role": "A",
        "name": "운영자",
        "description": "허용된 사이트의 조회, 이벤트 검토, 데이터 확인 가능",
        "permissions": ["site:read", "asset:read", "device:read", "event:review"],
    },
    {
        "role": "B",
        "name": "관리자",
        "description": "사이트·설비·장치·네트워크 기준정보 관리 가능",
        "permissions": [
            "site:read",
            "site:write",
            "asset:write",
            "device:write",
            "rollout:write",
        ],
    },
    {
        "role": "C",
        "name": "시스템 관리자",
        "description": "시스템 설정, 파이프라인, 라벨 기준 관리 가능",
        "permissions": ["*"],
    },
]

ACOUSTIC_LABEL_TAXONOMY = [
    {
        "code": "normal",
        "name": "정상음",
        "description": "정상 기준선 범위의 반복 운전음",
        "reviewRule": "동일 설비 정상 구간 3개 이상과 비교한다.",
    },
    {
        "code": "bearing_suspect",
        "name": "베어링 이상 추정음",
        "description": "고주파 대역 상승 또는 금속성 반복음이 관찰되는 상태",
        "reviewRule": "진동 RMS와 피크 대역을 함께 확인한다.",
    },
    {
        "code": "friction_noise",
        "name": "마찰음",
        "description": "불규칙한 긁힘음 또는 접촉음이 발생하는 상태",
        "reviewRule": "주변 설비와 보호 케이스 접촉 여부를 확인한다.",
    },
    {
        "code": "mixed_noise",
        "name": "혼합 소음",
        "description": "여러 설비 운전음 또는 바람·물 튐이 혼합된 상태",
        "reviewRule": "설치 위치와 주변 소음원을 라벨 메모에 남긴다.",
    },
    {
        "code": "sensor_noise",
        "name": "센서 노이즈",
        "description": "설비 이상보다 센서 고착, 케이블, 방수 문제 가능성이 높은 상태",
        "reviewRule": "장치 상태와 내환경성 점검 결과를 먼저 대조한다.",
    },
]

DATA_PIPELINES = [
    {
        "id": "realtime-observability",
        "name": "실시간 관제 수집 경로",
        "purpose": "운영자가 현재 설비 상태와 이상 후보를 빠르게 확인",
        "data": ["요약 특징값", "장치 상태", "이벤트", "최근 원시 구간"],
        "cadence": "초 단위 수집·표시",
        "guardrail": "AI 학습 부하가 관제 응답 시간을 지연시키지 않도록 분리",
    },
    {
        "id": "ai-frequency-analysis",
        "name": "AI 학습·주파수 분석 경로",
        "purpose": "3개월 주파수 기반 모델 후보 검증과 라벨 데이터 축적",
        "data": ["원시 파형", "FFT/스펙트럼", "장기 추이", "전문가 라벨"],
        "cadence": "일/주 단위 배치 분석",
        "guardrail": "실시간 장애가 학습 데이터셋 생성에 전파되지 않도록 분리",
    },
]

EVENTS = [
    {
        "id": "EV-241",
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "severity": "critical",
        "eventType": "asset_anomaly_candidate",
        "title": "냉각수 펌프 베어링 이상 후보",
        "time": "19:42:12",
        "duration": "48초",
        "score": 92,
        "label": "확인 필요",
        "note": "진동과 음향이 같은 시간축에서 동시 상승. 주변 펌프 기동 여부 확인 필요.",
    },
    {
        "id": "EV-238",
        "siteId": "SITE-02",
        "assetId": "SITE-02-GEN-01",
        "severity": "warning",
        "eventType": "asset_anomaly_candidate",
        "title": "발전기 음향 스펙트럼 변화",
        "time": "19:31:05",
        "duration": "31초",
        "score": 78,
        "label": "확인 필요",
        "note": "정상 운전 기준선 대비 음향 고주파 성분이 증가.",
    },
    {
        "id": "EV-233",
        "siteId": "SITE-05",
        "assetId": "SITE-05-FAN-03",
        "severity": "device",
        "eventType": "sensor_fault_candidate",
        "title": "음향 센서 노이즈 플로어 고착",
        "time": "19:20:44",
        "duration": "8분",
        "score": 64,
        "label": "이상 확인",
        "note": "설비 이상이 아닌 센서 상태 이벤트로 분류.",
    },
]


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str


def copy_payload(value: Any) -> Any:
    return deepcopy(value)


def now_text() -> str:
    return time.strftime("%H:%M:%S")


def build_sites() -> list[dict[str, Any]]:
    sites = []
    for index, name in enumerate(ISLAND_NAMES):
        total_devices = 3 + (index % 5)
        offline = 1 if index % 9 == 0 else 2 if index % 17 == 0 else 0
        profile_type = PROFILE_CYCLE[index % len(PROFILE_CYCLE)]
        sites.append(
            {
                "id": f"SITE-{index + 1:02d}",
                "code": f"ISLAND-{index + 1:02d}",
                "name": f"{name} 발전소",
                "region": "서해권" if index < 13 else "남해권" if index < 33 else "동해·제주권",
                "timezone": "Asia/Seoul",
                "network": profile_type,
                "networkType": profile_type,
                "priority": "상" if index % 5 == 0 else "중" if index % 3 == 0 else "일반",
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


def rollout_stage(index: int) -> str:
    if index < 4:
        return "PoC 선정"
    if index < 12:
        return "1차 후보"
    if index < 32:
        return "현장 조사"
    return "후속 검토"


SITES = build_sites()


def get_site(site_id: str) -> dict[str, Any]:
    site = next((item for item in SITES if item["id"] == site_id), None)
    if not site:
        raise ApiError(404, "SITE_NOT_FOUND", "존재하지 않는 발전소입니다.")
    return site


def get_asset(site_id: str, asset_id: str) -> dict[str, Any]:
    asset = next((item for item in assets_for(site_id) if item["id"] == asset_id), None)
    if not asset:
        raise ApiError(404, "ASSET_NOT_FOUND", "존재하지 않는 설비입니다.")
    return asset


def network_profile(profile_type: str) -> dict[str, Any]:
    profile = next((item for item in NETWORK_PROFILES if item["type"] == profile_type), None)
    if not profile:
        raise ApiError(404, "NETWORK_PROFILE_NOT_FOUND", "존재하지 않는 네트워크 유형입니다.")
    return profile


def visible_sites_for_role(role: str) -> list[dict[str, Any]]:
    if role in {"B", "C"}:
        return copy_payload(SITES)
    return copy_payload(SITES[:4])


def assets_for(site_id: str) -> list[dict[str, Any]]:
    site = get_site(site_id)
    assets = []
    for index, template in enumerate(ASSET_TEMPLATES[: site["assetCount"]], 1):
        asset_id = f"{site['id']}-{template['suffix']}"
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
                "installLocation": f"{index}번 회전설비 베이스",
                "baselineStatus": "draft" if site["rolloutStage"] == "현장 조사" else "ready",
            }
        )
    return assets


def devices_for(site_id: str) -> list[dict[str, Any]]:
    devices = []
    for index, asset in enumerate(assets_for(site_id), 1):
        last_seen_sec = 18 + index * 9
        health = "online" if index % 4 else "warning"
        devices.append(
            {
                "id": f"DEV-{asset['id'].replace('SITE-', '')}",
                "siteId": site_id,
                "assetId": asset["id"],
                "sensorChannels": ["vibration", "acoustic", "rpm"],
                "firmware": "edge-0.1.0",
                "certificateStatus": "registered",
                "lastSeenSecAgo": last_seen_sec,
                "health": health,
                "mappingStatus": "active",
            }
        )
    return devices


def rollout_plans() -> list[dict[str, Any]]:
    plans = []
    for site in SITES:
        profile = network_profile(site["networkType"])
        plans.append(
            {
                "siteId": site["id"],
                "siteName": site["name"],
                "region": site["region"],
                "priority": site["priority"],
                "stage": site["rolloutStage"],
                "networkType": site["networkType"],
                "networkName": profile["name"],
                "targetAssetCount": site["targetAssetCount"],
                "recommendedArchitecture": profile["architecture"],
                "installationReady": site["rolloutStage"] in {"PoC 선정", "1차 후보"},
            }
        )
    return plans


def network_profiles_for_sites() -> list[dict[str, Any]]:
    return [
        {
            "siteId": site["id"],
            "siteName": site["name"],
            "networkType": site["networkType"],
            "profile": network_profile(site["networkType"]),
            "signalQuality": site["signalQuality"],
            "lastSurveyedAt": "2026-08-11",
        }
        for site in SITES
    ]


def install_points_for(site_id: str) -> list[dict[str, Any]]:
    points = []
    for index, asset in enumerate(assets_for(site_id), 1):
        points.append(
            {
                "id": f"IP-{asset['id']}",
                "siteId": site_id,
                "assetId": asset["id"],
                "vibrationMount": "베어링 하우징 상단" if index % 2 else "모터 본체 측면",
                "vibrationAxis": "수평 X축 + 수직 Z축",
                "mountingMethod": "볼트 고정 브라켓",
                "acousticDirection": "모터 냉각팬 방향 1.5m",
                "noiseSources": ["주변 펌프", "냉각팬", "파도·바람"] if index == 1 else ["주변 설비 운전음"],
                "photoRequired": True,
                "baselineRequired": True,
            }
        )
    return points


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


def authenticate(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    user = next((item for item in USERS if item["username"] == username), None)
    if not user or user["password"] != password:
        raise ApiError(400, "INVALID_CREDENTIALS", "아이디 또는 비밀번호를 확인해 주세요.")
    session = {
        "token": f"demo-{user['id']}",
        "issuedAt": now_text(),
        "expiresInSec": 3600,
    }
    return {"user": public_user(user), "session": session, "rolePolicy": role_policy(user["role"])}


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "allowedSiteIds": user["allowedSiteIds"],
    }


def role_policy(role: str) -> dict[str, Any]:
    policy = next((item for item in ROLE_POLICIES if item["role"] == role), None)
    if not policy:
        raise ApiError(404, "ROLE_NOT_FOUND", "존재하지 않는 권한입니다.")
    return copy_payload(policy)


def inject_anomaly(payload: dict[str, Any]) -> dict[str, Any]:
    site_id = str(payload.get("siteId") or "SITE-01")
    site = get_site(site_id)
    asset_id = str(payload.get("assetId") or assets_for(site["id"])[0]["id"])
    asset = get_asset(site["id"], asset_id)
    event_id = f"EV-{250 + len(EVENTS)}"
    event = {
        "id": event_id,
        "siteId": site["id"],
        "assetId": asset["id"],
        "severity": "critical",
        "eventType": "asset_anomaly_candidate",
        "title": f"{asset['name']} 이상 주입 이벤트",
        "time": now_text(),
        "duration": "10초",
        "score": 94,
        "label": "확인 필요",
        "note": "데모 주입으로 생성된 이벤트입니다.",
    }
    EVENTS.insert(0, event)
    site["status"] = "critical"
    site["eventCount"] += 1
    return copy_payload(event)


def review_event(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = next((item for item in EVENTS if item["id"] == event_id), None)
    if not event:
        raise ApiError(404, "EVENT_NOT_FOUND", "존재하지 않는 이벤트입니다.")
    label = str(payload.get("label", event["label"]))
    if label not in {"확인 필요", "정상/오탐", "이상 확인", "정비 완료"}:
        raise ApiError(400, "INVALID_EVENT_LABEL", "허용되지 않는 이벤트 라벨입니다.")
    event["label"] = label
    event["note"] = str(payload.get("note", event["note"]))
    event["reviewedAt"] = now_text()
    return copy_payload(event)

