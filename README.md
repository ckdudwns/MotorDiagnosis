# MotorDiagnosis — Bind Edge AI

도서·섬 지역의 모터·펌프 등 회전설비에서 **진동·음향 데이터를 수집하고, 설비 상태를 모니터링하며 이상 후보를 확인하는 PoC/MVP 프로젝트**입니다.

텔레메트리 수집·저장, 기준선 기반 이상 이벤트 처리, 대시보드 조회·검수, 알림, 학습용 데이터 내보내기를 제공합니다. 공개 데이터 기반 AI 모델의 오프라인 학습·평가 경로도 함께 관리합니다.

> 문서 기준: `main` 커밋 `9f7e550`
>
> 코드 구현, 자동 테스트 통과, 실제 장비 검증은 서로 다른 단계입니다. 본 프로젝트는 현장 진단 성능이나 고장 유형·잔여수명 예측 성능이 검증된 완성 제품을 의미하지 않습니다.

## 1. 주요 기능

| 영역 | 제공 기능 |
|---|---|
| 센서 수집 | ESP32-S3 기반 진동·음향 측정, 특징값 생성, 서버 전송 |
| 통신 복구 | 장치 측정값의 로컬 버퍼 보관, 순서 기반 재전송, 서버 중복 수집 방지 |
| 백엔드 | HTTP 수집 API, MQTT/TLS 메시지의 중앙 수집 API 전달, 데이터 검증·격리·저장 |
| 이상 이벤트 | AI2 기준선 기반 점수와 설비별 규칙을 이용한 이벤트 생성·갱신·종료 |
| 대시보드 | 사이트·설비 현황, 신호 차트, 이벤트 목록·상세, 장치·서비스 상태 조회 |
| 운영자 검수 | 이벤트 판정, 변경 사유, 검수 이력, 원인·점검·조치 메모 관리 |
| 알림 | 웹 알림 및 SMTP/Webhook 연동, 발송 상태·실패·재시도 이력 관리 |
| 데이터 내보내기 | 조회 조건 또는 내부 데이터셋 버전에 따른 CSV/XLSX 내보내기 |
| AI1 오프라인 분석 | 데이터 전처리·특징 추출, 기준선 생성, Dense/LSTM 후보 학습·평가, 산출물 관리 |

실시간 운영 이상 판정은 현재 **AI2의 진동 RMS 기준선 기반 경로**를 사용합니다. 음향 데이터 수집과 AI1 음향 학습 코드가 존재한다는 것만으로, 음향·진동 통합 AI 모델이 실시간 운영 경로에 연결되었다는 뜻은 아닙니다.

## 2. 시스템 구성

### 실시간 수집·모니터링 경로

```text
진동·음향 센서
      ↓
ESP32-S3
측정 · 특징값 생성 · 로컬 버퍼 · 재전송
      ↓
중앙 텔레메트리 수집 API
검증 · 중복 방지 · 오류 격리
      ↓
SQLite 저장
      ↓
AI2 기준선 점수 + 설비별 이상 규칙
      ↓
이상 이벤트 생성·갱신·종료
      ↓
대시보드 · 운영자 검수 · 알림
```

MQTT/TLS 수집을 사용하는 경우에도 메시지는 중앙 HTTP 수집 API로 전달되어 동일한 검증·저장 경로를 거칩니다.

### AI1 오프라인 학습·평가 경로

```text
공개 데이터 또는 학습용 내보내기 데이터
      ↓
데이터 등록 · 전처리 · 분할 · 특징 추출
      ↓
기준선 생성 / 모델 학습
      ↓
평가 · 오류 사례 분석 · 산출물 저장
```

AI1 학습 결과의 생성과 백엔드 모델 등록, 사용 승인, 실제 운영 배포는 별개의 단계입니다. 오프라인 학습 완료를 운영 모델 자동 적용으로 간주하지 않습니다.

RF66 원시 구간별 비교 추론은 [RF66 연결·검증 안내](docs/rf66-shadow-inference.md)를
참고하십시오. 별도 모델 설정이 필요하며 기존 운영 점수·알림에는 영향을 주지 않습니다.

## 3. 저장소 구조

```text
MotorDiagnosis/
├── app.py                 # 로컬 서버 실행 진입점
├── motor_diagnosis/       # 백엔드 API, 상태 저장, 알림, 웹 화면
├── firmware/
│   └── esp32_edge_node/   # ESP32-S3 펌웨어 및 회귀 테스트
├── ai/
│   ├── ai1/              # 전처리, 특징 추출, 오프라인 학습·평가
│   └── ai2/              # 리플레이, 이상 점수, 이벤트 생명주기
├── tests/                # 백엔드·화면·연동 테스트
├── docs/                 # 기능별 상세 설명 및 인계 문서
├── requirements.txt      # 루트 Python 의존성
└── pyproject.toml        # Python 코드 스타일 설정
```

AI 디렉터리는 주차별 작업 구조를 유지합니다. 전체 기능의 진입점과 사용 범위는 이 README에서 확인하고, 세부 실행 방법은 각 파트 문서를 참고합니다.

## 4. 개발 환경 및 빠른 실행

### 준비 사항

- Git
- Python 3.12.x
- 화면 회귀 테스트 실행 시 Node.js
- 펌웨어 테스트·빌드 시 PlatformIO와 해당 환경의 C/C++ 도구 모음

AI1 학습에 필요한 추가 패키지는 각 AI1 문서에 따라 별도로 설치합니다. 루트 의존성 설치만으로 모든 학습 환경이 구성되지는 않습니다.

### 저장소 받기

```powershell
git clone https://github.com/ckdudwns/MotorDiagnosis.git
cd MotorDiagnosis
```

### Windows에서 백엔드·대시보드 실행

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

실행 후 브라우저에서 다음 주소로 접속합니다.

```text
http://127.0.0.1:8787
```

로그인 계정에 따라 접근 가능한 사이트와 조회·변경 권한이 달라집니다. 개발용 로그인 정보는 프로젝트 담당자를 통해 확인합니다.

> 기본 서버 주소는 로컬 접속용입니다. ESP32 등 외부 장치 연결에는 장치에서 접근 가능한 서버 주소와 네트워크·인증·TLS 구성이 별도로 필요합니다.

### 로컬 데모 모드

합성 이상 데이터 주입 기능은 기본적으로 비활성화되어 있습니다. 로컬 시연이 필요한 경우 서버를 시작하기 전에 설정합니다.

```powershell
$env:DEMO_ENABLED = "true"
.\.venv\Scripts\python.exe app.py
```

`APP_ENV=production`에서는 데모 주입이 차단됩니다.

합성 데이터와 리플레이는 기능 연결을 확인하기 위한 자료입니다. 실제 장비의 고장 진단 성능을 입증하는 자료로 사용하지 않습니다.

## 5. 데이터 저장 및 주요 계약

### 저장 위치

`app.py` 실행 시 다음 SQLite 파일을 사용합니다.

| 용도 | 기본 경로 | 설정 |
|---|---|---|
| 백엔드 상태 | `output/runtime.sqlite3` | `STATE_DB_PATH` |
| 알림 발송·재시도 이력 | `output/alerts.sqlite3` | `ALERT_DB_PATH` |
| 상태별 선택 단건 원본·처리 대기 | `output/periodic-snapshots.sqlite3` | `PERIODIC_SNAPSHOT_DB_PATH` |
| MQTT 재시도 큐 | `output/mqtt_retry.sqlite3` | MQTT 서비스의 `--retry-db` 옵션 |

테스트에서는 별도의 메모리 저장소를 사용할 수 있습니다. 테스트 환경의 저장 방식과 실제 애플리케이션 실행 시의 저장 방식을 구분해야 합니다.

### 텔레메트리 처리 원칙

**IoT 상태 기반 단건 수신(1단계):** `POST /api/devices/{deviceId}/periodic-snapshots`는
`edge-state-snapshot-v1` 정책으로 선택한 원시 진동 구간 하나를 받습니다.
정상 상태에서는 시각 기준 5분마다, 보드 이상 조건 연속 3회로 이상 상태에 진입할 때는 즉시,
이상 유지 중에는 10초마다, 정상 조건 연속 5회로 복귀할 때는 즉시 각각 현재 구간 하나를 전송합니다.
모든 Raw 또는 과거 5분치를 묶어서 보내는 정책이 아닙니다.
서버는 전송 사유·상태·카운터와 샘플 SHA-256을 검증하고 원본·처리 대기 상태를 저장한 뒤 ACK합니다.
보드의 상태/연속 횟수는 **장치 보고값**이며 서버 모델 판정이나 정답 라벨이 아닙니다.
이 경로는 아직 모델 추론·이벤트·알림을 실행하지 않습니다. 기존 `periodic-single-v1` 요청과
기존 API·RF66 경로는 호환 유지합니다.
입력 필드·4종 전송 예시·재전송·검증 절차는 [IoT 단건 수신 규격](docs/periodic-single-snapshots.md)을 참고하세요.

- 수집 데이터는 서버에서 형식·권한·장치 정보를 검증합니다.
- 기존 요약 telemetry는 `(deviceId, sequence)`, 정기 단건 구간은 `(deviceId, bootId, windowIndex)`를 기준으로 재전송 중복을 처리합니다.
- 일반 실측 데이터는 `scenarioLabel: null`로 전송합니다.
- 정상·고장 상태가 외부에서 확인된 실험·검증 데이터에만 해당 라벨을 부여합니다.
- ESP32가 정상·고장을 자체 확정하여 정답 라벨을 생성하지 않습니다.
- 시간 계약은 시간대가 포함된 RFC3339 형식을 사용합니다. `/api/health`의 `timestamp`는 UTC 문자열입니다.
- raw 특징값과 보정된 물리 단위를 구분합니다. 검증된 보정 정보 없이 raw RMS를 물리 단위로 간주하지 않습니다.
- 실제 데이터가 없는 구간을 합성 데이터나 임의의 `0`으로 대체하지 않습니다.

### 주요 API

| 목적 | API |
|---|---|
| 서비스 상태 | `GET /api/health` |
| 단건 수집 | `POST /api/telemetry/ingest` |
| 일괄 재전송 수집 | `POST /api/telemetry/bulk` |
| 측정 데이터 조회 | `GET /api/telemetry` |
| 이벤트 목록·페이지 조회 | `GET /api/events` |
| 이벤트 상세 | `GET /api/anomaly/events/{eventId}` |
| 이벤트 검수 | `POST /api/events/{eventId}/review` |
| 장치 상태 조회·보고 | `GET/POST /api/devices/{deviceId}/health` |
| 학습용 데이터 내보내기 | `GET /api/datasets/export` |
| 모델·기준선 등록·조회 | `/api/model-versions`, `/api/baseline-versions` |

세부 요청·응답, 권한, 오류 처리와 제약은 기능별 문서를 확인합니다. 모델·기준선의 등록은 해당 산출물의 승인이나 운영 배포를 의미하지 않습니다.

## 6. 기본 사용 흐름

1. 로그인 후 사이트·설비·조회 기간을 선택합니다.
2. 운영 현황에서 신호 차트, 사이트 요약, 장치·서비스 상태를 확인합니다.
3. 이벤트 목록을 필터링하고 상세 신호와 판정 근거를 확인합니다.
4. 검수 결과와 변경 사유를 기록합니다.
5. 필요한 경우 원인·점검·조치 메모를 작성합니다.
6. 알림 상태를 확인하고, 필요한 데이터를 CSV 또는 XLSX로 내보냅니다.

운영 관리 기능은 계정 권한에 따라 제공됩니다. 신호 데이터 내보내기 조건과 이벤트 목록 필터는 서로 다르므로, 이벤트 필터가 측정 데이터 전체에 적용된다고 가정하지 않습니다.

## 7. 테스트 및 자동 검사

### 백엔드 회귀 테스트

저장소 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

### 대시보드 동작 테스트

Node.js가 설치된 환경에서 실행합니다.

```powershell
node tests/test_ai2_dashboard.mjs
node tests/test_week4_dashboard.mjs
```

### 펌웨어 테스트·빌드

PlatformIO 명령을 사용할 수 있는 환경에서 실행합니다.

```powershell
pio test -d firmware/esp32_edge_node -e native
pio run -d firmware/esp32_edge_node -e esp32-s3-devkitc-1
```

네이티브 테스트에는 호스트 C/C++ 컴파일 환경이 필요합니다. 상세 환경 설정과 실제 보드 시험 항목은 펌웨어 테스트 문서를 참고합니다.

### GitHub Firmware CI

펌웨어 또는 해당 검사 설정이 변경된 PR·push에서 다음 검사가 실행됩니다.

- 핵심 펌웨어 로직 테스트
- 데이터 보존·통신 안전 관련 소스 정책 검사
- 비밀 설정 파일이 없는 상태의 ESP32 빌드

> 자동 검사 통과는 실제 센서 측정 정확도, 장시간 동작, 전원·네트워크 장애 복구까지 검증되었다는 의미가 아닙니다. 실제 장비 시험 결과는 별도로 기록합니다.

## 8. 구현 범위와 한계

### 현재 구분해야 하는 사항

- 실시간 이상 이벤트 처리와 AI1 오프라인 모델 학습은 별도의 경로입니다.
- 공개 데이터 평가 결과를 현장 모터의 성능으로 일반화하지 않습니다.
- 서로 다른 출처의 진동·음향 데이터를 조합한 자료는 동일 설비의 동시 실측 자료가 아닙니다.
- 모델 파일 참조 등록만으로 파일 검증·사용 승인·운영 배포가 완료되지는 않습니다.
- 고장 유형 분류와 잔여수명 예측 성능을 보장하지 않습니다.
- 실제 센서 배선·보정, 장시간 수집, 전원·통신 장애 후 복구는 현장 검증이 필요합니다.
- 개발용 계정·토큰·설정을 그대로 소규모 운영에 사용하지 않습니다.

### 실제 장비 PoC를 위한 후속 범위

다음 항목은 기존 구현과 진행 중 작업을 재사용하면서 별도로 개발·통합·검증 범위를 관리합니다.

- 실제 RPM 취득과 측정 시각·출처·유효 상태의 연결
- 제한된 원격 장치 설정과 요청 버전·실제 적용 결과 관리
- 전송 실패·재시도·응답시간·버퍼 적체 등 통신 품질 자동 수집 확장
- AI1 산출물의 자동 인계·등록과 사람의 승인 절차
- 현장 데이터 기반 기준선 보정 및 장시간 동작·복구 검증

진행 중 PR의 기능은 병합된 기능과 구분하고, 병합 후 README와 관련 문서의 상태를 함께 갱신합니다.

## 9. 상세 문서

- [백엔드 영구 저장·실시간 이벤트 연결](https://github.com/ckdudwns/MotorDiagnosis/blob/main/docs/backend-production-integrations.md)
- [운영 계정 설정·데모 로그인 차단 및 EC2 적용](docs/production-auth.md)
- [운영 장치·MQTT 수집 토큰 발급·교체·폐기](docs/production-ingest-auth.md)
- [640ms 연속 진동 구간 수집·특징·비교 추론](docs/continuous-vibration-windows.md)
- [실측 진동 모델 입력 합의안 — 담당자 승인 전 초안](docs/model-input-agreement.md)
- [원시 XYZ 전송·보존 및 서버 66개 특징 변환](docs/raw-vibration-windows.md)
- [AI2 운영 대시보드 연결 및 사용 흐름](https://github.com/ckdudwns/MotorDiagnosis/blob/main/docs/ai2-dashboard-integration.md)
- [AI1 음향 학습·음향/RPM 기준선](https://github.com/ckdudwns/MotorDiagnosis/blob/main/docs/ai1-acoustic-baselines.md)
- [AI1 4주차 학습·데이터셋·모델 버전](https://github.com/ckdudwns/MotorDiagnosis/blob/main/ai/ai1/week4/ai1/README.md)
- [AI2 리플레이 도구](https://github.com/ckdudwns/MotorDiagnosis/blob/main/ai/ai2/week1/README.md)
- [AI2 4주차 시연·모델 결과 표시](https://github.com/ckdudwns/MotorDiagnosis/blob/main/ai/ai2/week4/README.md)
- [ESP32 펌웨어 테스트·빌드·실기 시험](https://github.com/ckdudwns/MotorDiagnosis/blob/main/firmware/esp32_edge_node/TESTING.md)

## 10. 개발·협업 규칙

EC2 백업·복구 점검, 용량·장시간 수집 검사 및 임시 SSH 키 정리는
[운영 마무리 절차](docs/production-operations.md)를 참고하세요.

신호별 실시간 기준선, 요청 파형·AI1 인계, 이벤트 설치 정보와 건강 보고 주기 확장은
[신호 분석 운영 확장 안내](docs/signal-analysis-operations.md)를 참고하세요.

- `main`에 직접 push하지 않고 작업 브랜치와 Pull Request를 사용합니다.
- 기능 브랜치는 `feat/<kebab-description>` 형식을 사용합니다.
- PR에는 변경 목적, 구현 범위, 테스트 결과, 남은 제약을 작성합니다.
- 실제로 실행하지 않은 테스트를 통과했다고 기록하지 않습니다.
- 최종 병합은 저장소 소유자가 수행합니다.
- Python·CSV 내부 이름은 `snake_case`, 외부 JSON은 기존 `camelCase` 계약을 유지합니다.
- Python 코드는 Black, 줄 길이 88을 기준으로 관리합니다.
- 비밀번호·토큰·인증서 비밀키·개인별 `secrets.h`는 커밋하지 않습니다.
- 원본 `.mat/.wav`, 생성 데이터셋·리플레이 결과, 내부 참고 문서, 가상환경은 커밋하지 않습니다.
- 외부 데이터셋은 원본 출처와 라이선스·이용 조건을 확인하고 해당 조건을 준수합니다.
