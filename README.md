# MotorDiagnosis — Bind Edge AI

도서·섬 지역 발전설비의 모터·펌프 등 회전설비에서 수집한 진동·음향 신호를 기반으로
상태를 모니터링하고 이상 후보를 확인하는 PoC + MVP 프로젝트다.

## AI 작업 구조

- [AI-1 1주차: 신호 전처리·라벨·데이터셋](./ai/ai1/week1/README.md)
- [AI-1 2주차: EDGE_FEATURE_01 — 특징량 계산·정상 기준선·품질 검증](./ai/ai1/week2/README.md)
- [AI-2 1주차: 분석/실시간 경로 분리·리플레이](./ai/ai2/week1/README.md)

AI-1의 공개 데이터 기반 합성 인계 파일을 AI-2가 분석 경로와 실시간 리플레이 경로로
분리한다. 이 데이터는 실제 한 설비에서 동시에 측정한 데이터가 아니므로, 모델 성능이나
실제 고장 진단의 근거로 사용하지 않는다.

## 2주차 백엔드 범위

- `POST /api/telemetry/ingest`: 단건 수집, raw-only 스키마 검증,
  `(deviceId, sequence)` 멱등성, 오류 격리
- `GET /api/telemetry`: 사이트·설비·기간별 수집 데이터 조회
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
