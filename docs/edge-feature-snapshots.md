# 특징값 전용 전송 규격 — IoT 기획안 기준

서버 구현 기준: 2026-09-13. 브랜치 `feat/event-verifier-history`.
이 문서는 **이전 버전 호환 수신 규격**이다. 두 AI 모델을 연결하는 새 보드는
[25초 등간격 이력 규격](pump-dual-model-serving.md)을 우선한다.
아래 00·25·50초 및 이상 중 정기 중단 정책을 새 규격과 혼용하지 않는다.
보드 펌웨어·운영 서버 배포 완료를 의미하지 않는다.

## 1. 기획안과 일치시킨 범위

| 항목 | 규격 |
| --- | --- |
| 측정 | ADXL345, 800Hz, 512개 XYZ, 약 640ms 창 |
| 축 | `a_1=X`, `a_2=Y`, `a_3=Z` |
| 전송 입력 | 평균 제거 후 CF·왜도·Pearson 첨도, 총 9개 |
| 정상 | **매 분 UTC 00·25·50초**, 해당 구간의 최근 valid 창 하나 |
| 이상 진입 | valid 이상 3회 연속 → `anomaly_start` 즉시 |
| 이상 유지 | monotonic uptime 기준 10초마다 `anomaly_active` |
| 복귀 | valid 정상 5회 연속 → `recovery` 즉시 |
| 이상 상태의 정기 보고 | 생성하지 않음 |
| 정기/진입 겹침 | `anomaly_start` 하나만 생성, 같은 정기 슬롯 표시 |
| 품질 불량 | `quality=invalid`, `features=null`, `reason` 필수 |
| Raw | **신규 규격에서 저장·전송하지 않음**. Raw/Base64 필드를 넣으면 서버 400 |
| 재전송 | 같은 센서·부팅·순번·내용 유지, 저장 ACK 확인 후 파일 삭제 |

`00→25→50→다음 분 00`의 간격은 **25·25·10초**다. 정확히 25초 간격의 연속 시계열로 이름을 바꾸거나 재해석하지 않는다.
서버는 보드가 보고한 상태·횟수를 저장한다. 보내지 않은 모든 창을 서버가 관측하거나 3/5회 조건을 재검증했다는 뜻은 아니다.
현재 ML 모델의 임계값을 보드로 내려주는 API는 추가하지 않았다. 보드 개발용 임계값과 서버 모델 임계값을 혼용하지 않는다.

## 2. 특징 계산 프로파일

- `schemaVersion`: `2`
- `profileId`: `adxl345-ac-cf-sk-ku-v1`
- 특징 순서: `cf_a_1`, `cf_a_2`, `cf_a_3`, `sk_a_1`, `sk_a_2`, `sk_a_3`, `ku_a_1`, `ku_a_2`, `ku_a_3`
- 원시 값은 g, 최종 특징은 무차원(`unit=dimensionless`). 축별 독립 계산.

"표준 왜도"의 구현 차이를 없애기 위해 이 프로파일의 계산식을 다음과 같이 고정한다.

```text
N = 512
y[i] = x_g[i] - mean(x_g)
m2 = sum(y[i]^2) / N
m3 = sum(y[i]^3) / N
m4 = sum(y[i]^4) / N
CF = max(abs(y[i])) / sqrt(m2)
Skewness = m3 / (m2 ** 1.5)
Kurtosis = m4 / (m2 ** 2)
```

모집단 모멘트(`/N`), bias 보정 없음, 첨도에서 3을 빼지 않음. 별도 필터·축 재배치·임의 보정을 적용하려면 프로파일 버전을 바꾼다.
분산이 0이어서 나눌 수 없으면 `zero_variance`, 계산 결과 NaN/inf이면 `non_finite`로 보낸다. 0이나 이전 특징값으로 채우지 않는다.
프로파일은 보드에 요구하는 계산 계약이며, Raw가 없으므로 서버가 실제 센서 계산 과정을 다시 계산해 증명할 수는 없다.

invalid는 두 연속 카운트를 **0으로 초기화**하고 기존 NORMAL/ANOMALY_ACTIVE 상태는 유지한다.
따라서 `이상, invalid, 이상, 이상`은 연속 3회가 아니다. invalid만으로 이상 진입하거나 정상 복귀하지 않는다.

## 3. Endpoint와 인증

```text
POST /api/devices/DEV-01-MOT-02/periodic-snapshots
Content-Type: application/json
Authorization: Bearer <해당 장치의 telemetry:ingest 토큰>

GET /api/devices/DEV-01-MOT-02/periodic-snapshots
Authorization: Bearer <조회 권한이 있는 사용자 세션 토큰>
```

운영 base URL은 `https://motordiagnosis-api.duckdns.org`이다. 장치·설비의 활성 매핑 검사를 그대로 적용한다.
`/api/health`나 `/raw-vibration-windows`로 새 특징값을 보내지 않는다. Health 전송은 별도 권한/경로다.
요청 하나에 레코드 하나, 루트 키는 `window`, `transmission`만 허용한다. 배열·batch·history·Raw는 이 새 프로파일에서 허용하지 않는다.
이전 전송 API/프로파일과 이미 저장된 원본은 호환·과거 조회용으로 유지한다. 기존 보드가 계속 옛 API로 보내면 새 규격으로 자동 변환되지 않는다.

## 4. 필드

### window

| 키 | 형식·의미 |
| --- | --- |
| `schemaVersion`, `profileId` | `2`, `adxl345-ac-cf-sk-ku-v1` |
| `deviceId`, `siteId`, `assetId`, `sensorId` | 대문자/숫자로 시작, 대문자·숫자·점·밑줄·하이픈, 1~100자. 센서별 ID 구분 |
| `bootId` | 재부팅마다 새로 생성하는 소문자 16진수 32자. 재전송 파일의 기존 bootId는 유지 |
| `windowIndex` | 센서·bootId 내 레코드 단조증가 순번, 0~2147483647. invalid도 독립 순번. 재전송 시 그대로 |
| `timestamp` | valid: 선택한 창의 **측정 시작 UTC**, RFC3339. 서버 수신 시각 아님 |
| `startUptimeUs` | 같은 측정 시작의 monotonic uptime, 정수 마이크로초. UTC와 누적 경과 차이 1초 이내 |
| `sampleRateHz` | 정수 `800` |
| `sampleCount` | valid는 정수 `512`. invalid는 실제 확보 수 0~512, 측정 창이 없으면 0 |
| `axes`, `unit` | `["X","Y","Z"]`, `dimensionless` |
| `quality`, `reason` | valid면 reason=null. invalid면 아래 원인 중 하나 |
| `features` | valid면 정확히 9개 유한 숫자. invalid면 null |
| `integrity` | `{algorithm:"sha256", digest:"특징 바이트 SHA-256"}` |
| `periodicSlotEpoch` | 정기 구간을 닫는 UTC 경계의 Unix **정수 초**. 정기 보고 필수, 슬롯과 무관한 이벤트는 null |

`timestamp`는 전송 재시도나 Wi-Fi 재접속 시 바꾸지 않는다. invalid 정기 보고는 구간 종료 경계를 timestamp로 사용하고, 그 시점의 uptime을 함께 기록한다. 이는 유효 샘플의 측정 시각을 꾸며 내는 것이 아니라 "해당 구간의 유효 측정 없음"을 기록하는 시각이다.
invalid active 보고는 실패한 측정/상태 점검의 실제 시각과 uptime을 사용한다.

허용 원인: `fifo_overrun`, `insufficient_samples`, `timeout`, `non_finite`, `zero_variance`, `sensor_unavailable`, `no_valid_window`.
실제로 확인한 원인을 보내며 원인 미상일 때 무조건 fifo_overrun으로 표시하지 않는다.

### UTC 슬롯 정의

경계 T가 `:25`, `:50`이면 `[T-25초, T]`, `:00`이면 `[T-10초, T]` 안에서 **시작하고 수집 완료한** valid 창 중 가장 최근 하나를 선택한다.
`periodicSlotEpoch=T`, 창 시작 시각은 별도의 timestamp다. 슬롯보다 늦게 수신됐다는 이유로 측정 시각을 T로 바꾸지 않는다.
구간 안에 유효 창이 없으면 invalid 한 건을 생성한다. 이전 슬롯에서 선택한 창을 다시 보내지 않는다.
서버는 선택 창의 시간 범위를 검사하지만, 전송되지 않은 다른 창보다 정말 최신인지는 보드가 책임진다.

정기 보고 생성 전에 상태 전이를 먼저 처리한다. 경계와 anomaly_start가 겹치면 정기 보고를 만들지 않고 anomaly_start에 해당 슬롯을 표시한다.
복귀 창이 경계와 겹쳐 같은 창을 중복 선택하는 경우에도 recovery 한 건에 슬롯을 표시한다. anomaly_active는 슬롯=null이다.
슬롯이 표시된 이벤트의 창 역시 그 슬롯의 시간 범위를 만족해야 한다.
동일 센서·bootId·슬롯에 다른 순번의 두 번째 보고를 보내면 409다. 서버가 이미 ACK한 정기 보고를 나중에 이상 이벤트로 덮어쓰지 않는다.

### transmission

| eventType | state | anomalyCount / normalCount | 전송 기준 |
| --- | --- | --- | --- |
| `periodic` | `NORMAL` | 이상 0~2, 정상 0 이상. 둘 다 양수 금지 | UTC 00·25·50 |
| `anomaly_start` | `ANOMALY_ACTIVE` | 정확히 3 / 0, valid 필수 | 즉시 |
| `anomaly_active` | `ANOMALY_ACTIVE` | 정상 0~4. 둘 다 양수 금지 | uptime 기준 10초 |
| `recovery` | `NORMAL` | 정확히 0 / 5, valid 필수 | 즉시 |

공통 `policyId=edge-feature-snapshot-v1`. 카운트는 0~2147483647 정수다. invalid 보고의 카운트는 모두 0.
`mode`, `intervalSec`, 이전 `reason` 이벤트명은 새 transmission에 넣지 않는다. 품질 원인은 window.reason으로 분리한다.
이상 중에도 sensorTask는 계속 측정한다. 10초에 한 번 측정한다는 뜻이 아니다.
서버는 HTTP 도착 간격을 10초로 강제하지 않는다. LittleFS 재전송은 한꺼번에 도착할 수 있기 때문이다.

## 5. 요청 예시 — 정상 특징 한 건

아래 값은 **통신 규격용 합성 예시**다. 실제 측정값이나 고장 임계값이 아니다. 날짜도 실시간 측정 시각으로 교체해야 한다.

```json
{
  "window": {
    "schemaVersion": 2,
    "deviceId": "DEV-01-MOT-02",
    "siteId": "SITE-01",
    "assetId": "SITE-01-MOT-02",
    "sensorId": "SENSOR-02",
    "bootId": "0123456789abcdef0123456789abcdef",
    "windowIndex": 1,
    "timestamp": "2026-09-13T00:00:24Z",
    "startUptimeUs": 24000000,
    "sampleRateHz": 800,
    "sampleCount": 512,
    "profileId": "adxl345-ac-cf-sk-ku-v1",
    "axes": ["X", "Y", "Z"],
    "unit": "dimensionless",
    "quality": "valid",
    "reason": null,
    "features": {
      "cf_a_1": 2.0, "cf_a_2": 2.0, "cf_a_3": 2.0,
      "sk_a_1": 0.0, "sk_a_2": 0.0, "sk_a_3": 0.0,
      "ku_a_1": 3.0, "ku_a_2": 3.0, "ku_a_3": 3.0
    },
    "integrity": {
      "algorithm": "sha256",
      "digest": "89a69bbab3610b08f31a96c9b988d947db12cae3daf37a7fb41ed81418cc6c2e"
    },
    "periodicSlotEpoch": 1789257625
  },
  "transmission": {
    "policyId": "edge-feature-snapshot-v1",
    "eventType": "periodic",
    "state": "NORMAL",
    "anomalyCount": 0,
    "normalCount": 0
  }
}
```

### 특징 무결성 바이트

키 정렬된 JSON 문자열의 해시가 아니다. 지정된 특징 순서로 **IEEE-754 float64 little-endian 9개, 총 72바이트**를 연결해 SHA-256을 계산한다.
JSON으로 보낸 수치를 서버가 float64로 읽은 값과 같아야 한다. 소수 자릿수를 줄였다면 줄인 전송 값 기준으로 해시를 계산한다. signed zero도 일치시킨다.
invalid의 features=null은 빈 바이트열 SHA-256, `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`다.

## 6. ACK와 재전송

신규 저장·커밋 완료: **HTTP 202**, `accepted=1`, `duplicate=false`.
동일 내용 재전송: **HTTP 200**, `accepted=0`, `duplicate=true`.
둘 다 아래 acknowledged 한 건을 반환한다.

```text
deviceId: 요청의 장치 ID
policyId: edge-feature-snapshot-v1
acknowledged[0]:
  sensorId, bootId, windowIndex: 요청과 동일
  eventType: 요청과 동일
  featureDigest: 요청 integrity.digest와 동일
  durablyStored: true
  digest: 서버가 canonical JSON 전체에 대해 계산한 SHA-256
  receivedAt: 최초 저장 UTC (재전송 때 갱신하지 않음)
```

보드는 HTTP 200/202뿐 아니라 장치·정책·센서·부팅·순번·eventType·featureDigest가 일치하고 durablyStored=true인 ACK를 확인한 뒤 **그 파일만** 삭제한다.
루트 accepted=0이어도 정확히 같은 파일의 duplicate ACK이면 삭제 가능하다. HTTP 성공 코드만 보고 삭제하지 않는다.
`digest`는 서버 원본 추적용이며 `featureDigest`와 다르다. 펌웨어가 임의 JSON 직렬화 해시와 비교하지 않는다.
202는 서버 저장 완료이지 AI 분석 완료가 아니다. 결과는 GET의 items[].analysis로 조회한다.

- 같은 ID인데 내용이 다름: 409 `SNAPSHOT_CONFLICT`.
- 같은 슬롯인데 다른 보고: 409 `SNAPSHOT_SLOT_CONFLICT`.
- 장치·센서·부팅의 UTC/uptime 불일치: 409 `TIMESTAMP_UPTIME_MISMATCH`.
- 규격·특징 해시 오류: 400. 같은 잘못된 요청을 빠르게 무한 재시도하지 말고 파일을 보존해 진단한다.
- 저장 공간/DB 오류: 503. ACK하지 않는다. 파일을 유지하고 backoff 후 재시도한다.
- 401/403: 토큰·매핑 수정이 필요하다. 파일 삭제 금지.
- 신규 입력 측정 시각은 최근 48시간~미래 5분 범위. 이미 ACK된 정확한 재전송은 48시간을 넘어도 중복 ACK 가능.

LittleFS의 전원 장애 대비 파일 확정, 재부팅 시 기존 파일 우선 복구, 저장 공간 한도·초과 시 명시적 오류, 재시도 backoff는 보드 구현 책임이다. 서버가 파일을 삭제하거나 센서 태스크를 제어하지 않는다.

## 7. 서버 처리와 모델 경계

```text
인증·매핑 → 새 envelope/품질/해시/슬롯/순번·시각 검사
  → 원본 특징 + 수신 시각 + 수신 당시 모델 연결 정보 저장 → 커밋 → ACK
  → valid: 9개 특징 입력 준비
      → 미설정/미배정: waiting_model
      → 연결 모델과 입력 불일치: unavailable / MODEL_INPUT_CONTRACT_MISMATCH
      → 호환 모델: queued_inference → completed 또는 unavailable
  → invalid: unavailable, 구체적 품질 사유 보존
  → 화면 조회 / 호환 서버 모델의 완료 결과만 사건·알림 처리
```

기존 `pump-event-verifier-v1` JSON 모델은 엑셀 원본 특징과 정확한 25초 간격의 과거 24건을 요구한다.
이번 평균 제거·모집단 모멘트 특징, 25·25·10초 정기 간격, 이상 중 정기 중단과 **동일 입력이라고 검증된 모델이 아니다**.
따라서 이름만 바꾸어 실행하거나 이력을 보간하지 않는다. 같은 장치/센서에 이전 모델이 설정되어 있어도 새 입력은 저장·준비 후 **규격 불일치 판정 불가**로 남는다. 모델 파일·평가 파일·기존 결과는 수정하지 않았다.

후속 모델은 `edge-feature-event-v1` 어댑터로 해당 profile/계산식/센서 범위를 명시해야 한다. 공통 결과 저장·사건·알림 연결은 마련되어 있지만 **호환 학습 모델이 기본 제공되지는 않는다**.
수신 당시 모델이 없었던 기록은 후에 모델을 설정해도 자동 재추론·과거 알림 발송하지 않는다.
invalid·모델 미설정·규격 불일치를 정상으로 처리하지 않는다. 보드 anomaly_start도 서버 AI 이상으로 대신 쓰지 않는다.
RF66 로딩·자동 추론은 계속 중지 상태다.

## 8. 인수 확인

- 새 POST 202 이후 같은 내용 재전송 200, ACK identity/featureDigest/최초 receivedAt 유지.
- 정기 00·25·50·다음 00의 네 건을 저장·조회. active 중 periodic 없음.
- Raw 없는 anomaly_start, 10초 active, recovery를 저장·조회. 상태·AI 결과가 화면에서 구분됨.
- invalid/null/reason 저장, 잘못된 샘플 수·NaN·Raw·해시·상태·중복 슬롯 거절.
- Wi-Fi 단절/재부팅 후 원래 bootId·순번·시각으로 재전송, 보드 파일은 일치 ACK 이후에만 삭제.
- 새 모델 전에는 수신 정상과 모델 대기/규격 불일치를 구분. 모델 추론 성공으로 보고하지 않음.

자동 검증: `tests.test_edge_feature_snapshots` 및 `tests/test_snapshot_dashboard.mjs`.
실제 보드에서 9개 특징/바이트 해시 일치와 LittleFS 복구를 검증하는 작업은 이번 서버 수정에 포함하지 않았다.
