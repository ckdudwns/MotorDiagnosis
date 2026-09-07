# 4주차 백엔드 인계

기준: `Bind_Edge_AI_주차별_기능정의서_4주차_역할배정_선행배치_기존데이터셋학습.xlsx`
W4.2 (`기능정의서!A6:R10`, `역할배정!A11:I15`) 및 API 명세 v1.2
(`06_후속개발API!A18:H19`, `09_보완API상세!A31:J39`).

## 범위

| 기능 | 이번 백엔드 구현 | 다른 역할/후속 작업 |
| --- | --- | --- |
| ALERT_SEND_01 | 웹 알림, SMTP/Webhook 어댑터, Stub, 채널별 독립 작업자, SQLite 발송·재시도 이력 | 실제 수신 서버/메일 계정 설정 및 외부 종단 간 시험 |
| DATASET_MODEL_01 | 기존 동결 데이터셋에 기준선·모델·지표·보고서 연결, 불변 등록, 사이트 권한, 감사 기록 | 실제 모델 산출물 검증, 승인·배포·롤백 |
| EDGE_BUFFER_01 | 100건 이하 일괄 수신, 장치별 sequence 정렬, 단건 계약 재사용, 중복·충돌·부분 실패 결과 | IoT 장치의 24시간 실제 저장·용량 초과·전원 장애 시험 |
| SIM_ANOMALY_01 | 데모 API 기본 비활성, 운영 강제 차단, 권한 검사, 합성 표시, 주입 이벤트→웹 알림 연결 | 실제 센서 파형 및 운영 환경 시연 |
| AI_FREQ_MODEL_01 | 지표·오류 사례·도메인 차이·현장 보정 계획을 모델 버전에 기록/조회 | AI-1의 FFT/RMS/피크/대역 에너지 추출과 학습·평가 |

API 명세의 FUT 모델/기준선 API 중 **등록·조회만** 시연용으로 선행 구현했다.
`training-jobs`, `approve`, `deploy`, `rollback`, 고장 분류, RUL은 구현하지 않았다.
등록은 학습 성공이나 현장 성능 승인, 실제 배포를 의미하지 않는다.

## 알림 API

### POST /api/alerts/send

관리자 B / 시스템 관리자 C. 운영자 A는 발송 불가.

```json
{"eventId":"실제 이벤트 ID", "policyId":"ALERT-POLICY-DEFAULT", "isTest":false}
```

`policyId`와 `isTest`는 선택값이다. 응답 `202`의 `deliveries`는 발송 완료가 아니라
대기열 등록 결과이며, `suppressed`에는 disabled / scope_mismatch /
severity_mismatch / reviewed / outside_work_hours / cooldown 사유가 포함된다.

웹 알림은 이벤트·정책당 한 건, 외부/Stub 채널은 수신자별 한 건이다.
동일 이벤트·정책·채널·수신자·시험 여부로 다시 요청하면 기존 발송을 반환한다.
현재 정책의 활성·범위·심각도·검토·근무시간 조건을 통과한 재요청은 쿨다운보다
기존 발송 결과 조회가 우선이다. 새 채널/수신자 등 아직 없는 발송에는 쿨다운을 적용한다.
시험 알림은 `[TEST]`로 구분하고 실제 알림의 쿨다운에 영향을 주지 않는다.
시험 모드도 정책 활성 상태, 사이트/설비 범위, 심각도 조건은 우회하지 않는다.
정책 근무시간은 **UTC HH:MM**이며 자정을 넘는 범위를 지원한다.

### GET /api/alerts

운영자 이상. `siteId`, `channel`, `status`, `page`, `size`(최대 200) 필터.
응답은 `{items,page,size,total}`이다. 상태는 pending / sending / sent / failed.
`attemptCount`, `attempts[{number,at,success,error}]`, `lastError`, `nextRetryAt`
(Unix seconds), `deliveredAt`으로 실패·재시도 결과를 확인한다.
운영자에게는 다른 사이트, 정책 전체 스냅샷, 수신자 정보를 노출하지 않는다.
대시보드는 이 API의 현재 사이트 `channel=web&status=sent` 결과를 표시한다.

HTTP 서버는 새 이벤트를 관찰해 정책에 맞는 알림을 자동 등록한다.
이벤트 관찰·SQLite 대기열 처리는 전용 작업자 한 개가 0.5초 간격으로 실행하며,
HTTP 요청 수용 루프에서는 DB 작업을 하지 않는다. DB 잠금/일시 오류 중에도 다른
API는 요청을 수용하고, 알림 작업자는 다음 주기에 재시도한다. 알림 DB를 사용하는
API 자체는 잠금을 기다릴 수 있다. 서버 종료 시 작업자를 먼저 중지·합류한 다음
채널 발송 작업 완료를 기다리고 DB를 닫는다.
초기 화면용 기본 이벤트는 자동 발송하지 않는다. 실패한 이메일/Webhook과 별도로
웹 알림 작업자가 실행된다. 일시 오류는 2초·4초 후 재시도하며 총 3회까지만 시도한다.
미설정 채널과 영구 HTTP 거부는 `failed`로 저장한다. 네트워크 예외의 원문은
자격증명/URL 노출 방지를 위해 API에 반환하지 않는다.

### 저장과 전달 보장

`python app.py`는 `output/alerts.sqlite3`를 사용한다 (`ALERT_DB_PATH`로 변경).
재시작 후 pending/sending 항목을 복구하며 같은 전달 ID를 재사용한다.
한 DB는 한 HTTP 프로세스만 열 수 있다. 두 번째 프로세스는 시작을 거부한다.
단위 테스트의 `create_server()`는 기본적으로 메모리 DB와 외부 어댑터 없는 상태를 사용한다.
환경변수에 따른 외부 어댑터 구성은 `app.py` 실행 시에만 주입한다.

외부 발송과 DB 기록은 하나의 트랜잭션이 아니므로 **at-least-once**다.
Webhook에는 `Idempotency-Key: <delivery id>`를 전달한다. 수신 서버가 이 키로
중복을 제거해야 한다. SMTP에는 안정적인 Message-ID를 사용하지만, 메일 서버의
중복 제거를 보장하지 않는다. DB 결과 저장만 실패하면 살아 있는 프로세스는
외부 발송을 반복하지 않고 결과 저장부터 재시도한다.

### 외부 채널 설정

기본값은 웹/Stub 시연이다. 외부 요청은 아래 서버 환경변수를 설정한 채널에서만 발생한다.

- Webhook: `ALERT_WEBHOOK_URL` (HTTPS, 리다이렉트 및 URL 내 계정 정보 금지)
- 이메일: `ALERT_SMTP_HOST`, `ALERT_SMTP_PORT`(기본 465), `ALERT_EMAIL_FROM`
- SMTP 인증이 필요하면 `ALERT_SMTP_USERNAME`, `ALERT_SMTP_PASSWORD`

Webhook/SMTP 연결 timeout은 5초이며 TLS 인증서를 검증한다.
정책의 recipients는 이메일에서는 실제 주소, Webhook에서는 수신자 식별자로 사용한다.
Webhook URL은 정책/발송 요청이 아니라 서버 설정으로만 지정한다.
비밀값을 코드, PR, 예제 파일에 커밋하지 않는다.

## 모델·기준선 버전

등록은 시스템 관리자 C, 조회는 운영자 이상이다. 등록 데이터셋이 frozen인지 확인하고
데이터셋 원본 메타데이터와 기준선 스냅샷을 복사한다. 이후 반환 객체를 바꾸거나
새 데이터셋을 등록해도 기존 모델의 출처는 바뀌지 않는다.
dataset의 `frozen`은 버전 고정이지 승인 완료가 아니다 (`approvalStatus=pending`).

### POST /api/baseline-versions

```json
{
  "datasetId":"DATASET-00001",
  "siteId":"SITE-01",
  "assetId":"SITE-01-MOT-02",
  "timeSegment":"normal-load",
  "features":{"rms":{"mean":0.08,"std":0.01},"bandEnergy":[1.0,2.0]},
  "status":"draft"
}
```

`timeSegment`/`status`는 선택값이다. features는 비어 있지 않은 수치 객체이며,
NaN/Infinity/null/boolean/숫자 문자열, 과도한 크기·중첩은 거부한다.
응답 `201`의 `version`을 모델 등록 시 사용한다. 기준선이 참조하는 설비는 삭제하지 못한다.

### POST /api/model-versions

```json
{
  "version":"freq-baseline-v1",
  "artifactUri":"s3://project-models/freq-baseline-v1.pkl",
  "datasetId":"DATASET-00001",
  "baselineVersion":"BASELINE-00001",
  "metrics":{"f1":0.8,"falsePositiveRate":0.1},
  "errorCases":["고부하 정상 샘플의 오탐 사례"],
  "domainGap":"원본 데이터와 대상 모터·센서 사양이 다름",
  "fieldCalibrationPlan":"대상 모터 정상 운전 데이터를 수집한 뒤 임계값 보정"
}
```

예시 수치는 계약 설명용이며 실제 모델 성능이 아니다.
보고 필드 errorCases(문자열 목록), domainGap, fieldCalibrationPlan은 선택값이다.
artifactUri는 HTTPS/S3/file **참조만** 보관하고 파일을 다운로드·실행하지 않는다.
HTTPS는 올바른 DNS 호스트명/IP와 선택적 포트(1~65535), S3는 버킷 호스트와
경로를 요구한다. S3/file의 포트는 허용하지 않는다. file은 절대 경로와 선택적
공유 호스트를 지원한다. 빈 호스트, 잘못된 포트, 공백/제어문자, 계정 정보,
fragment는 `400 INVALID_ARTIFACT_URI`로 거부하며 DNS/파일 존재 여부는 검사하지 않는다.
응답은 `draft`, `approvalStatus=pending`, `deploymentStatus=not_deployed`,
`artifactVerified=false`다. 동일 version은 대소문자 구분 없이 409로 거부한다.
baseline과 model의 datasetId가 다르면 `409 MODEL_DATASET_MISMATCH`다.

조회:

- `GET /api/model-versions`, `GET /api/model-versions/{version}`
- `GET /api/baseline-versions`, `GET /api/baseline-versions/{version}`
- 목록 필터: siteId, assetId, status=draft, page, size(최대 200)

모델/기준선은 데이터셋 원본 사이트와 적용 대상 사이트의 **모든 권한**을 요구한다.
감사 기록도 같은 범위로 제한한다. 현재 일반 데이터 저장소는 기존 PoC와 동일한
**프로세스 메모리**이므로 서버 재시작 시 데이터셋·모델·기준선은 초기화된다.
운영용 영구 레지스트리와 승인 워크플로는 후속 범위다.

## 오프라인 일괄 재전송

`POST /api/telemetry/bulk`, 일반 telemetry:ingest 토큰 사용:

```json
{
  "items":[{
    "siteId":"SITE-01", "assetId":"SITE-01-MOT-02", "deviceId":"DEV-01-MOT-02",
    "timestamp":"2026-08-31T00:00:00Z", "sequence":1, "rpm":1796,
    "vibrationRmsRaw":0.08, "acousticRmsRaw":0.007,
    "vibrationPeakHz":29.9,
    "vibrationRmsMmS":null, "acousticDb":null,
    "scenarioLabel":null, "isSynthetic":true, "source":"week4-demo"
  }]
}
```

timestamp는 보존 기간 내 실제 샘플 시각으로, sequence는 장치의 새 시퀀스로 바꾼다.
HTTP 본문 한도는 기존 64KiB다.
단건과 동일한 raw-only 필드, 시각, 합성/출처, 장치 인증·매핑 검증을 적용한다.
배치 전체의 장치 토큰 권한을 먼저 검사하고, 같은 장치 안에서는 sequence 순으로 처리한다.
`scenarioLabel`은 필수 nullable이다. 일반 ESP/MQTT 토큰은 세 라벨 필드를 null로
보내야 하며, 비-null 라벨은 telemetry:label 권한이 있는 실험·검증 토큰만 제출한다.
결과 items의 index는 **원래 요청 배열 위치**다. 다른 배치 사이의 도착 순서는 보장하지 않는다.

모두 수용되면 200, 일부 거부되면 207과 accepted / duplicates / rejected 건수를 반환한다.
동일 deviceId+sequence+payload는 duplicate, 다른 payload는 SEQUENCE_CONFLICT다.
유효한 행은 저장되므로 전체 배치를 같은 payload로 재전송해도 된다.

시험은 가상 시각으로 24시간 전 데이터를 복구하는 경로를 검증한다.
실제 24시간 장치 시험이 아니다. 기존 PoC의 설비당 1,000건 메모리 보존 상한과
오래된 멱등성 키 제거 정책은 그대로이므로 대용량/장기 복구의 무손실 보장은 하지 않는다.

## 데모 실행과 검증

`DEMO_ENABLED=true`일 때만 주입 가능하며 `APP_ENV=production`이면 강제로 차단한다.
설정하지 않으면 버튼도 숨기고 API는 `403 DEMO_DISABLED`를 반환한다.
주입 API는 실측 장치의 `(deviceId, sequence)` 멱등성 영역·health·원본 저장소와 분리된
`DEMO-<assetId>` 식별자 및 데모 저장소에 정상 1건과 지속된 combined anomaly summary
telemetry를 생성한다. 진동·음향·RPM raw 값과 피크 주파수는 데모용 합성값이며 실제 파형,
보정된 mm/s·dB, AI-1 모델 추론값이 아니다. `vibrationRmsMmS`와 `acousticDb`는 raw-only
계약에 따라 null이다. 규칙 지속시간이 길면 최대 120개 이상 구간으로 균등 다운샘플링하여
실측 설비당 1,000건 보존 상한을 소모하지 않는다. 모든 행과 이벤트에는 `isSynthetic/source`를
기록하며 같은 종료 시각의 스냅샷이 알림에 연결된다.

대시보드 차트는 시연을 위해 실측·데모 telemetry를 같이 표시할 수 있다. 반면 실제 이벤트의
상세 증거는 `deviceId`가 같은 실측 저장소만 조회한다. 실측 point가 없을 때 데모 point를
대체 증거로 고정하지 않고 `unavailable`으로 반환한다.

`/api/export`와 `api/datasets/export`의 학습·내보내기 경로는 데모 telemetry와 데모 이벤트를
제외한다. 합성 이벤트의 reviewed label은 실측 telemetry의 ground truth 또는 training-eligible
결과에 연결되지 않는다. `source`가 없는 기존 실측 이벤트는 같은 장치·시간 구간의 실측
telemetry와 연결하되, event에 `source`가 있으면 동일한 source만 연결한다. 데모 이벤트 상세는
보정값이 없는 raw telemetry에 맞는 raw 단위만 반환한다.

대시보드는 등록된 model-version 메타데이터만 표시한다. AI-1 reconstruction error를 운영
`anomalyScore`(0–100)로 변환하는 규칙은 이 MVP에 없으므로, 모델 아티팩트를 실행하거나
점수를 임의 생성하지 않는다.

검증 명령:

```text
python -m unittest discover -s tests -v
python tests/smoke_week1_http.py
node tests/test_week4_dashboard.mjs
python -m compileall -q motor_diagnosis tests app.py
python -m black --check app.py motor_diagnosis tests/test_week4_backend.py
git diff --check
```

외부 서비스 없이 테스트에서는 어댑터를 대체한다. 실제 SMTP/Webhook 발송,
실제 MQTT 브로커·IoT 보드, 원본 데이터셋 학습은 별도로 확인해야 한다.

### 초기 구현 검증 결과 (2026-08-31)

- Python 3.12.13 + Paho 포함 전체 테스트: 166개 통과 (4주차 신규 21개 포함)
- HTTP smoke, 알림 DOM 동작/XSS 안전 텍스트 렌더링, 컴파일, Black, diff 검사 통과
- AI-2: 1·2주차 합계 14개 통과
- AI-1: 1주차 합성 파이프라인 통과, 2주차 18개 통과 / CWRU `97.mat` 없는 2개 skip
- AI-1의 librosa가 없어 MFCC는 기존 코드의 0벡터 fallback으로 실행됨. 실제 MFCC 검증 결과가 아님
- Graphify의 기존 정책·데이터셋·권한·감사 연결을 탐색하고 원문과 테스트로 검증함.
  새 4주차 모듈을 포함한 그래프 전체 재생성/무결성 검사는 실행하지 않음

### PR14 리뷰 보완 검증 (2026-08-31)

- 전체 백엔드 172개 통과 (4주차 27개, 이번 회귀 테스트 6개 추가)
- SQLite 쓰기 잠금 중 health/사이트 API 응답 및 잠금 해제 후 단일 발송 확인
- 알림 작업자의 일시 오류 재시도와 종료, 기존 발송 재조회/새 발송 쿨다운 구분 검증
- 산출물 URI 호스트·포트 검증 및 문서 bulk JSON의 실제 HTTP 수용 검증
- HTTP smoke, 대시보드 DOM/XSS, 컴파일, Black, diff 검사 통과
- 외부 이메일·Webhook 발송과 실물 장치 24시간 시험은 이번에도 미실행
