"""
이벤트 라벨 지정 + 변경 이력 (AI-1, 3주차 EVENT_LABEL_01)

백엔드(motor_diagnosis/data.py review_event())가 이미 사용 중인 라벨 코드 5종을
그대로 재사용한다. 이 모듈이 추가하는 것은:

1. 라벨 변경을 이력(history)으로 남기는 로직 (원본 이벤트 값은 label/note/reviewedAt만
   갱신하고, 변경 전후 값은 별도 history 엔트리에 append) — EVENT_LABEL_01 수용 기준
   "원본 이벤트 값은 변경하지 않는다"를 만족.
2. DATA_EXPORT_01(../datasets/register_dataset.py)이 정규화한 기존 데이터셋(CWRU) 라벨을
   이 이벤트 라벨 체계로 매핑해 초기 라벨을 자동으로 채우는 로직 (후속 학습 데이터 라벨 축적).

필드/근거는 event_label_schema.md 참고.
"""

from datetime import datetime, timezone

EVENT_REVIEW_LABELS = {
    "needs_review",
    "normal_false_positive",
    "confirmed_anomaly",
    "repair_completed",
    "sensor_issue",
}

LABEL_TAXONOMY_VERSION = "EVENT-LABEL-V1"
MAX_NOTE_LENGTH = 2000  # motor_diagnosis.data.MAX_REVIEW_NOTE_LENGTH과 동일 값 유지

# DATA_EXPORT_01의 DATASET_LABEL_MAPPING(common_label) -> 이벤트 초기 라벨.
DATASET_COMMON_LABEL_TO_EVENT_LABEL = {
    "NORMAL": "normal_false_positive",
    "ANOMALY": "confirmed_anomaly",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_label_change(
    event: dict,
    *,
    new_label: str,
    changed_by: str,
    reason: str,
    new_note: str = None,
    history_id: str = None,
) -> tuple:
    """이벤트의 label/note를 갱신하고, 변경 이력 엔트리를 함께 반환한다.

    원본 이벤트(event)는 변경하지 않고 복사본을 갱신해 반환한다 — 호출자가
    저장소에 반영할지는 별도로 결정한다.

    반환: (updated_event, history_entry)
    """
    if new_label not in EVENT_REVIEW_LABELS:
        raise ValueError(
            f"허용되지 않은 라벨입니다: {new_label!r} "
            f"(허용값: {sorted(EVENT_REVIEW_LABELS)})"
        )
    if not isinstance(changed_by, str) or not changed_by.strip():
        raise ValueError(
            "changed_by는 공백이 아닌 문자열이어야 합니다 (감사 추적을 위해 변경자를 "
            "식별할 수 있어야 함)."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "reason은 공백이 아닌 문자열이어야 합니다 (빈 문자열/공백만 있는 값이나 "
            "숫자·list 등 비문자열 값은 허용하지 않음)."
        )
    if new_note is not None:
        if not isinstance(new_note, str):
            raise ValueError("note는 문자열이어야 합니다.")
        if len(new_note) > MAX_NOTE_LENGTH:
            raise ValueError(f"note는 {MAX_NOTE_LENGTH}자를 초과할 수 없습니다.")

    previous_label = event.get("label")
    previous_note = event.get("note")
    changed_at = _now_iso()

    updated_event = dict(event)
    updated_event["label"] = new_label
    if new_note is not None:
        updated_event["note"] = new_note
    updated_event["reviewedAt"] = changed_at

    history_entry = {
        "history_id": history_id or f"{event['id']}-H-{changed_at}",
        "event_id": event["id"],
        "changed_by": changed_by,
        "changed_at": changed_at,
        "previous_label": previous_label,
        "new_label": new_label,
        "previous_note": previous_note,
        "new_note": updated_event.get("note"),
        "reason": reason,
    }

    return updated_event, history_entry


def seed_label_from_dataset(common_label: str) -> str:
    """DATA_EXPORT_01의 공통 라벨(NORMAL/ANOMALY)을 이벤트 초기 라벨로 매핑한다.

    기존 데이터셋(CWRU) 윈도우를 리플레이해 만든 합성 이벤트의 label 필드를
    자동으로 채우는 데 쓴다. NORMAL 구간에서 이벤트가 만들어졌다면 정의상
    오탐(normal_false_positive)이고, ANOMALY 구간이면 이상 확인(confirmed_anomaly)이다.
    이 값은 출발점일 뿐이며 운영자가 apply_label_change()로 재조정할 수 있다.
    """
    if common_label not in DATASET_COMMON_LABEL_TO_EVENT_LABEL:
        raise ValueError(
            f"알 수 없는 공통 라벨입니다: {common_label!r} "
            f"(허용값: {sorted(DATASET_COMMON_LABEL_TO_EVENT_LABEL)})"
        )
    return DATASET_COMMON_LABEL_TO_EVENT_LABEL[common_label]
