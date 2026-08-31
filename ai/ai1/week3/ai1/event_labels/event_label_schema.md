# 이벤트 라벨 지정 스키마 (EVENT_LABEL_01)

기능ID: `EVENT_LABEL_01` (3주차 실행순서 2번, AI-1 주담당)
산출: `event_label.py` — 라벨 변경 이력 로직 + 기존 데이터셋 라벨 매핑

## 라벨 코드 — 백엔드 구현을 그대로 재사용

`motor_diagnosis/data.py`의 `review_event()`가 **이미 프로토타입에서 사용 중인 라벨
5종**을 그대로 가져온다. 새로 정의하지 않는 이유: 이미 구현·검증된 어휘가 있는데
AI-1이 별도 체계를 새로 만들면 백엔드·AI-2와 어긋난다.

| 라벨 코드 | 의미 |
|---|---|
| `needs_review` | 확인 필요 (기본값, 이벤트 생성 직후) |
| `normal_false_positive` | 정상/오탐 |
| `confirmed_anomaly` | 이상 확인 |
| `repair_completed` | 정비 완료 |
| `sensor_issue` | 센서 이상 (설비 이상이 아니라 센서 자체 문제로 판정된 경우) |

`LABEL_TAXONOMY_VERSION = "EVENT-LABEL-V1"` — 향후 라벨 체계가 바뀌면 버전을 올리고
이전 버전으로 라벨링된 이벤트는 구버전 참조를 유지한다 (`ACOUSTIC_LABEL_01`의
`labelTaxonomyVersion` 관례와 동일).

## 원본 이벤트 값 불변 원칙 (수용 기준)

기능정의서 수용 기준: **"원본 이벤트 값은 변경하지 않는다."** 이벤트의 아래 필드는
탐지 시점에 한 번 결정되며 라벨 지정 과정에서 다시 쓰지 않는다:

- `id`, `siteId`, `assetId`, `severity`, `eventType`, `title`, `time`, `duration`, `score`

라벨 지정으로 바뀌는 필드는 `label`, `note`, `reviewedAt` **뿐**이다. 이 세 필드가
바뀔 때마다 변경 이력을 별도로 append한다 — 원본 이벤트 레코드를 덮어써서 이전 값을
잃어버리면 안 되므로, "현재 값"은 이벤트에 유지하되 "변경 과정"은 히스토리에 남기는
구조다 (API 명세서 MVP-033 `GET /api/events/{eventId}/reviews`가 이 히스토리를
조회하는 계약).

## 변경 이력(history) 스키마

```json
{
  "history_id": "EV-241-H0001",
  "event_id": "EV-241",
  "changed_by": "operator-01",
  "changed_at": "2026-08-24T09:05:00+00:00",
  "previous_label": "needs_review",
  "new_label": "confirmed_anomaly",
  "previous_note": null,
  "new_note": "진동과 음향이 같은 시간대에 동시 상승. 현장 점검 요청.",
  "reason": "베어링 결함 패턴과 일치, 정비팀에 통보"
}
```

- `reason`은 필수다 (빈 문자열/공백만 있는 값은 거부) — 파라미터 변경 감사(`PARAM_AUDIT_01`)와
  동일하게 "왜 바꿨는지"를 남겨야 나중에 라벨 품질을 추적할 수 있다.
- `new_note`를 생략하면 기존 note를 유지한다 (라벨만 바꾸는 경우가 흔함).
- `note`는 `MAX_NOTE_LENGTH = 2000`자 제한 (`motor_diagnosis.data.MAX_REVIEW_NOTE_LENGTH`와 동일).

## 기존 데이터셋 라벨 매핑 (`seed_label_from_dataset`)

`DATA_EXPORT_01`(`../datasets/register_dataset.py`)이 CWRU 원본 라벨을 공통 라벨
(`NORMAL`/`ANOMALY`)로 정규화했다. 이 매핑 결과를 재사용해, CWRU 윈도우를 리플레이해
만든 합성 이벤트의 **초기 운영자 라벨을 자동으로 채워** 학습 라벨을 축적한다
("후속 학습 데이터 라벨 축적" — 3주차 적용 메모):

| 데이터셋 공통 라벨 (`common_label`) | 자동 채움 라벨 | 근거 |
|---|---|---|
| `NORMAL` | `normal_false_positive` | 정상 구간에서 이벤트가 잡혔다면 정의상 오탐 |
| `ANOMALY` | `confirmed_anomaly` | 실제 결함 구간이므로 이상 확인으로 간주 |

이 자동 라벨은 **출발점일 뿐 최종 확정이 아니다** — 운영자가 검토 후 `apply_label_change()`로
언제든 재조정할 수 있고, 그 변경도 위 history 스키마에 그대로 기록된다.

## 대상 확정 후 보완

- 대상 모터의 실제 고장 유형이 정해지면, `NORMAL`/`ANOMALY` 2분류가 아니라
  `acoustic_label_criteria.md`(1주차)처럼 세부 유형(`BEARING_FAULT`, `FRICTION`, `IMBALANCE`)
  라벨과 연결해야 한다 — 지금은 `known_label`(CWRU 원본 세부 라벨)이 이벤트 메타데이터에
  남아 있으므로 나중에 그대로 연결 가능하다.
- 현장 라벨이 쌓이면 `changed_by`를 실제 계정 시스템(`AUTH_ROLE_01`)의 사용자 ID로 연결.
