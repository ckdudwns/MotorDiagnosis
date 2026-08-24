# MotorDiagnosis — Bind Edge AI

도서·섬 지역 발전설비의 모터·펌프 등 회전설비에서 수집한 진동·음향 신호를 기반으로
상태를 모니터링하고 이상 후보를 확인하는 PoC + MVP 프로젝트다.

## AI 작업 구조

- [AI-1 1주차: 신호 전처리·라벨·데이터셋](./ai/ai1/week1/README.md)
- [AI-1 2주차: EDGE_FEATURE_01 — 특징량 계산·정상 기준선·품질 검증](./ai/ai1/week2/README.md)
- [AI-2 1주차: 분석/실시간 경로 분리·리플레이](./ai/ai2/week1/README.md)
- [AI-2 2주차: 이상 점수 초안·관제 시각화](./ai/ai2/week2/README.md)

AI-1의 공개 데이터 기반 합성 인계 파일을 AI-2가 분석 경로와 실시간 리플레이 경로로
분리한다. 이 데이터는 실제 한 설비에서 동시에 측정한 데이터가 아니므로, 모델 성능이나
실제 고장 진단의 근거로 사용하지 않는다.

## 2주차 백엔드 범위

- `POST /api/telemetry/ingest`: 단건 수집, raw-only 스키마 검증,
  `(deviceId, sequence)` 멱등성, 오류 격리
- `GET /api/telemetry`: 사이트·설비·기간별 수집 데이터 조회
- `GET /api/events`: **2주차 이벤트 시각 조회 확장(PoC)**. 현재 배열 응답에
  `occurredAt`(시간대 포함 RFC3339)을 추가해 UTC 최신순 정렬·기간 필터를 시연한다.
  이는 공식 API v1.2의 `EVENT_LIST_01`(페이지네이션 포함) 구현이나 정식 호환 계약이 아니다.
- `GET /api/dashboard/sites-summary`: 권한 범위 내 사이트 상태 요약
- `GET /api/devices/{deviceId}/health`: 오프라인·복구·누락 구간과 장치 상태 조회
- `GET /api/health/dependencies`: 수집·저장·분석·알림 의존성 상태와 오류율 조회
- `POST/GET /api/devices/{deviceId}/connectivity-tests`: 설치 전후 망 품질 이력
- `GET /api/device-hardware-profiles`,
  `PUT /api/devices/{deviceId}/hardware-profile`: 하드웨어 프로필 조회·교체 이력

로컬 AI-2 리플레이의 기본 수집 토큰은 `demo-telemetry-ingest-token`이다.
운영 환경에서는 이 데모 토큰을 사용하지 않고 별도 장치/서비스 토큰으로 교체한다.

MQTT/TLS 수집기는 별도 프로세스의 메모리에 저장하지 않고 중앙 HTTP 수집 API로
메시지를 전달한다. 따라서 MQTT로 들어온 값도 HTTP 서버의 검증·멱등성·저장 경로를
거쳐 `GET /api/telemetry`와 대시보드에서 동일하게 조회된다.

QoS 1/2 메시지는 HTTP 처리가 끝난 뒤에만 수동 ACK한다. 일시적인 네트워크·5xx 오류와
`408`, `425`, `429` 응답은 SQLite 재시도 큐(`output/mqtt_retry.sqlite3`)에 보존하며,
작업자가 지수 백오프로 실제 HTTP 전송에 성공한 뒤 ACK한다. 인증·권한·경로·본문 크기
오류도 ACK하지 않으며, 백엔드 격리가 보장된 수집 validation 오류만 ACK한다. 잘못된
MQTT JSON·토픽은
`POST /api/telemetry/quarantine`에 원문과 오류 사유가 저장된 뒤 ACK된다. 브로커 상태는
`POST /api/health/dependencies/mqtt`에 보고하되, SUBACK 승인 QoS가 요청 QoS 이상일 때만
`healthy`로 전환한다.

## 3주차 백엔드 범위

- `GET /api/events`: 기간·사이트·설비·심각도·라벨·검토 여부 필터와 페이지 조회
- `GET /api/anomaly/events/{eventId}`: 이벤트 전후 신호, 특징·규칙·모델·장치 스냅샷 조회
- `POST /api/events/{eventId}/review`, `GET /api/events/{eventId}/reviews`:
  운영자 판정과 변경 전후·담당자·사유 이력
- `GET/PUT /api/anomaly/rules/{assetId}`: 점수·지속시간·히스테리시스·병합 구간 버전 관리
- `GET /api/alerts/policies`, `PUT /api/alerts/policies/{policyId}`:
  심각도·수신자·채널·근무시간·중복 알림 방지 구간 관리
- `GET /api/parameters`, `PUT /api/parameters/{key}`, `GET /api/audit-logs`:
  운영 파라미터 변경과 감사 이력
- `POST/GET /api/devices/{deviceId}/environment-inspections`:
  방진·방수·염해·케이블 글랜드·함체 점검 및 조치 이력
- `GET /api/devices/{deviceId}/faults`: 센서 고장 후보를 설비 이상 이벤트와 분리 조회
- `GET/PUT /api/label-taxonomies/acoustic`, `POST/GET /api/datasets`:
  음향 라벨 버전과 기존 데이터셋의 출처·라이선스·호환성·체크섬·분할 정책 관리
- `GET /api/datasets/export`: 학습 누수 방지를 위한 설비 단위 분할과 manifest 메타데이터를
  포함한 내부 텔레메트리 CSV 내보내기. 등록된 외부 데이터셋은 원본 저장소에서 별도로
  내보내며, XLSX는 후속 범위다.
### 이벤트 시각 PoC 범위

이 PoC에서 `occurredAt`은 이벤트의 단일 기준 시각이며 브라우저는 이를 지역 시각으로
변환해 표시한다. `time`은 과거 화면용 문자열로서 정렬·필터에 사용하지 않는다. 공식 API
v1.2로 승격할 때는 `EVENT_LIST_01`의 `{items, page, size, total}` 응답, `time` 레거시
호환 처리, 기존 이벤트의 RFC3339 이관 정책을 명세·서버·클라이언트에 함께 반영한다.

```bash
python -m motor_diagnosis.mqtt_service \
  --host mqtt.example.com \
  --ca-cert ca.pem \
  --ingest-endpoint http://127.0.0.1:8787/api/telemetry/ingest \
  --quarantine-endpoint http://127.0.0.1:8787/api/telemetry/quarantine \
  --retry-db output/mqtt_retry.sqlite3
```

## 개발 환경

- Python 3.12.4 (허용 범위: 3.12.x)
- 저장소 루트 `.venv`
- Black, line length 88

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m black --check .
```

## 저장소 규칙

- Python·CSV 내부 이름은 snake_case, 외부 JSON은 기존 camelCase 계약을 유지한다.
- 원본 `.mat/.wav`, 생성 데이터셋·리플레이 결과, 내부 참고 문서, 가상환경은 커밋하지 않는다.
- `main`에 직접 푸시하지 않는다. `feat/<kebab-description>` 브랜치와 Pull Request를 사용하고,
  최종 병합은 저장소 소유자만 수행한다.
