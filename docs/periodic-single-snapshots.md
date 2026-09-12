# IoT 상태 기반 단건 원시 구간 수신·입력 준비 — 2단계

## 범위와 현재 상태

- 현재 권장 계약은 **`edge-state-snapshot-v1`**입니다. 보드는 모든 측정 구간을 판단하되 아래 네 조건에서만 현재 Raw 하나를 선택합니다.
- 이전의 **이상 여부와 관계없는 5분 단건** 계약(`periodic-single-v1`)은 이미 전송 대기 중인 요청과 기존 보드의 호환을 위해 유지합니다. 새 정책을 이전 정책 ID로 보내지 않습니다.
- 서버는 인증 → 입력·무결성 검증 → 원본/수신 시각/처리 상태 저장 → 커밋 → ACK를 반환하고, 별도 입력 준비 작업이 대기 구간을 처리합니다.
- `quality=valid`는 우선 `queued`로 저장합니다. 입력 준비가 끝나면 `waiting_model`로 바뀝니다. 아직 새 모델이 없으므로 정상/이상 점수를 만들지 않습니다.
- 오류 품질 구간도 원본을 보존하되 `unavailable`로 기록합니다. 이를 정상으로 판정하거나 0으로 채우지 않습니다.
- 기존 RF66 서버 로딩·자동 추론·자동 이벤트 투영은 중지합니다. 과거 원본·판정 결과는 삭제하지 않습니다. 기존 통계 경로와 별도 LSTM shadow 경로는 이 변경의 대상이 아닙니다.
- 펌웨어 수정, 모델 교체, 새 경로의 이벤트·알림과 웹 화면 연결은 이번 단계에 포함하지 않습니다.

`5분`/`10초`는 전송 선택 주기입니다. **샘플링 800 Hz, 512개 XYZ 샘플, 구간 길이 640 ms**와 다른 개념입니다.
보드의 약 0.66초 실행 간격을 이유로 원시 입력의 샘플링 속도·샘플 수를 바꾸지 않습니다.
원시 구간 하나를 보내는 것이며, 스칼라 측정값 한 개나 5분 평균값을 보내는 것이 아닙니다.

## 통일할 보드 전송 정책

| 선택 시점 | `reason` | `state` | `mode` | `intervalSec` | Raw 선택 |
| --- | --- | --- | --- | ---: | --- |
| 정상 상태의 시각 기준 5분 경계 | `normal_periodic` | `NORMAL` | `periodic` | 300 | 현재 구간 1개 |
| 이상 조건 연속 3회로 이상 상태 진입 | `anomaly_enter` | `ANOMALY_ACTIVE` | `immediate` | 0 | 진입 확정 구간 1개 즉시 |
| 이상 상태 유지 중 10초 경과 | `anomaly_periodic` | `ANOMALY_ACTIVE` | `periodic` | 10 | 현재 구간 1개 |
| 정상 조건 연속 5회로 정상 복귀 | `normal_recovered` | `NORMAL` | `immediate` | 0 | 복귀 확정 구간 1개 즉시 |

- 정상 정기는 시각 기준 `00, 05, 10, ...분` 경계마다 선택합니다. 경계가 window 사이이면 경계 이후 첫 완료 구간 하나를 사용합니다. 과거 경계마다 뒤늦게 새 구간을 만들어 채우지 않습니다.
- 이상 유지의 10초는 진입 또는 직전 정기 선택 시점부터 계산합니다. HTTP ACK 시각을 기준으로 타이머를 밀지 않습니다. 정상 복귀 후에는 다시 시각 기준 5분 경계에 맞춥니다.
- 같은 window에 정기 시점과 진입/복귀가 겹치면 **진입/복귀 사유 우선으로, 큐에 넣기 전에 하나만 선택**합니다. 같은 식별자를 다른 전송 사유로 두 번 보내면 내용 충돌입니다.
- 진입/복귀 즉시 전송은 **그 구간 하나**를 뜻합니다. 이전·이후 모든 Raw를 보내거나, 남은 과거 Raw를 묶어서 보내지는 않습니다. 전송 큐에는 위 조건에서 이미 선택한 요청만 남깁니다.
- 정상 상태에서 이상 연속 1~2회는 아직 `NORMAL`입니다. 정상값이 나오면 이상 연속 카운터를 초기화합니다. 이상 상태에서 정상 연속 1~4회는 아직 `ANOMALY_ACTIVE`이며, 다시 이상값이 나오면 정상 연속 카운터를 초기화합니다.
- 품질 불량·측정 누락은 연속 횟수에 포함하지 않고 양쪽 카운터를 초기화합니다. 품질 불량을 정상 복귀로 해석하지 않습니다. 재부팅 시에는 새 `bootId`와 초기화된 카운터로 시작합니다.
- 위 타이머·카운터·임계값 판단은 **보드 담당 범위**입니다. 이 변경은 서버 수신 계약만 맞추며, 보드의 구체적인 이상 임계값 알고리즘이나 펌웨어를 수정하지 않습니다.

서버에는 선택되지 않은 중간 구간이 없습니다. 따라서 `anomalyCount=3`/`normalCount=5`의 **보고 형식 일관성만 검사**하며,
실제 3회·5회 연속 관측을 독립적으로 증명하지 않습니다. 첫 수신이 이상 유지나 정상 복귀여도 허용합니다.
보드 `ANOMALY_ACTIVE`는 전송 상태 이름이지 서버의 모델 판정·고장 확정·학습 정답 라벨이 아닙니다.

## API 및 인증

```text
POST /api/devices/{deviceId}/periodic-snapshots
GET  /api/devices/{deviceId}/periodic-snapshots
```

- POST: 기존 장치 Bearer 토큰의 `telemetry:ingest` 및 해당 `allowedDeviceIds` 범위를 검사합니다.
- `device-health:write`만 있는 토큰은 사용할 수 없습니다. 장치·사이트·설비가 등록되고 활성 매핑되어야 합니다.
- GET: 운영자 로그인과 `device:read`, `telemetry:read`, 해당 사이트 조회 권한이 필요합니다.
- JSON 요청 최대 64 KiB. 구간 배열이 아닌 **`window` 객체 하나**만 받습니다.
- 새 API에 구형 배치 요청을 보내거나, 구형 API에 새 단건 요청을 보내면 400입니다.
- 경로 이름의 `periodic-snapshots`는 호환을 위해 유지합니다. 새 정책의 두 즉시 전송 사유도 **같은 경로**로 보냅니다.

### 보드 전달용 JSON 형태

아래 `<...>`는 실제 값으로 대체할 설명 자리이며 그대로 전송하는 테스트 데이터가 아닙니다.

```json
{
  "transmission": {
    "policyId": "edge-state-snapshot-v1",
    "mode": "periodic",
    "intervalSec": 300,
    "reason": "normal_periodic",
    "state": "NORMAL",
    "anomalyCount": 0,
    "normalCount": 5
  },
  "window": {
    "schemaVersion": 1,
    "deviceId": "DEV-01-MOT-02",
    "siteId": "SITE-01",
    "assetId": "SITE-01-MOT-02",
    "bootId": "<32자리 소문자 16진수 부팅 ID>",
    "windowIndex": 469,
    "timestamp": "<실제 구간 시작 시각, 시간대 포함 RFC3339>",
    "startUptimeUs": 300000000,
    "sampleRateHz": 800,
    "sampleCount": 512,
    "profileId": "adxl345-800hz-xyz-counts-v1",
    "axes": ["X", "Y", "Z"],
    "unit": "count",
    "encoding": "base64-int16le-xyz",
    "gPerCount": 0.0039,
    "quality": "valid",
    "samples": "<3072바이트를 인코딩한 4096문자 canonical base64>",
    "integrity": {
      "algorithm": "sha256",
      "digest": "<디코딩된 원시 샘플 바이트 SHA-256, 소문자 hex 64자리>"
    }
  }
}
```

`window`는 위 키를 정확히 사용합니다. 추가 필드나 임의 단위 변환을 자동으로 무시하지 않습니다.
새 정책의 `transmission`은 위 **일곱 필드**를 정확히 사용합니다. `priority`, `replay` 모드는 없습니다.
전송 실패 후 재시도할 때는 보드가 이미 다른 상태로 바뀌었더라도 `mode`, `reason`, 카운터, 시각을 바꾸지 않고 **동일한 요청**을 다시 보냅니다.

나머지 세 가지 `transmission` 예시입니다. `window`는 각 시점에 실제 측정한 원시 구간을 넣습니다.

```json
{"policyId":"edge-state-snapshot-v1","mode":"immediate","intervalSec":0,"reason":"anomaly_enter","state":"ANOMALY_ACTIVE","anomalyCount":3,"normalCount":0}
```

```json
{"policyId":"edge-state-snapshot-v1","mode":"periodic","intervalSec":10,"reason":"anomaly_periodic","state":"ANOMALY_ACTIVE","anomalyCount":19,"normalCount":0}
```

```json
{"policyId":"edge-state-snapshot-v1","mode":"immediate","intervalSec":0,"reason":"normal_recovered","state":"NORMAL","anomalyCount":0,"normalCount":5}
```

카운터는 선택 구간에서 보드가 보고하는 연속 횟수이며, 정수 0~2³¹−1만 허용합니다(`true`, 소수 불가).
두 카운터가 동시에 양수일 수 없습니다. 진입은 정확히 `(3, 0)`, 복귀는 정확히 `(0, 5)`입니다.
정상 정기는 `anomalyCount < 3`, 이상 정기는 `normalCount < 5`여야 합니다.
정상/이상 정기의 반대쪽 카운터는 연속 조건이 끊기면 0으로 두며, 유지 중인 같은 조건의 횟수는 누적 또는 포화 카운터를 사용할 수 있습니다.
품질 불량은 정기 사유에서 두 카운터를 0으로 보내 원본을 보존할 수 있지만, 진입·복귀 즉시 사유로는 보낼 수 없습니다.
`state`·`reason`·`mode`·`intervalSec`가 위 표와 다르거나 카운터가 모순이면 HTTP 400입니다.

기존 요청은 아래 세 필드를 그대로 보낼 수 있습니다. 기존 정책에는 즉시 전송을 추가하지 않습니다.

```json
{"policyId":"periodic-single-v1","mode":"periodic","intervalSec":300}
```

기존 저장 원본·ACK·digest는 수정하지 않습니다. 새 정책은 같은 DB의 원본 `body`에 보존되므로 DB 스키마 변경은 필요하지 않습니다.

### 필드 의미

| 필드 | 계약 |
| --- | --- |
| `deviceId` / `siteId` / `assetId` | 서버에 등록한 대문자 ID. `DEV.REVIEW.01`처럼 점이 있는 기존 ID도 허용 |
| `bootId` | 32자리 소문자 hex. 재부팅 시 변경, 재시도할 때는 유지 |
| `windowIndex` | 실제 측정 구간 번호, 정수 0~2³¹−1. 선택되지 않은 구간 때문에 번호가 크게 건너뛰어도 허용 |
| `timestamp` | 측정 구간 **시작** 시각. HTTP 전송·재전송 시각으로 바꾸지 않음 |
| `startUptimeUs` | 같은 시작 시점의 장치 uptime, 마이크로초 단위 정수 0~2⁵³−1 |
| `sampleRateHz` / `sampleCount` | 현재 입력 프로필은 800 Hz / 축별 512개. 오류 구간은 실제 샘플 수 0~512 |
| `profileId` | 현재는 위 ADXL345 counts 프로필만 지원. 미래 모델 입력과 다르면 새 검증 프로필 추가 필요 |
| `axes` / `unit` / `encoding` | X→Y→Z, 센서 count, signed int16 little-endian 인터리브의 base64 |
| `schemaVersion` | 현재 1. 모델 버전이 아니라 전송 스키마 버전 |
| `gPerCount` | 현재 프로필의 count→g 환산값 0.0039. 보드의 센서 모드와 일치해야 함 |
| `quality` | `valid`, `fifo_overrun`, `sensor_unavailable`, `sample_gap`, `clipped`, `constant_axis`, `processing_overflow` |
| `samples` | 각 시점의 X,Y,Z 순서. 정상은 512×3×2 = 3072바이트. full-resolution ADXL345 count 범위 검사 |
| `integrity` | 샘플 원본 바이트에 대한 SHA-256. 전송 내용 손상 검사이며 장치 인증은 Bearer/TLS가 별도로 담당 |

## 무결성 해시 두 종류를 구분합니다

1. **보드 `window.integrity.digest`**: base64를 풀었을 때의 샘플 바이트만 해싱합니다.
   JSON 전체, base64 문자열, 부동소수점 g 값, ZIP 파일을 해싱하는 것이 아닙니다.
2. **서버 `acknowledged[].digest`**: `window`와 `transmission`을 포함한 전체 수신 객체를
   키 정렬·공백 제거한 canonical JSON으로 만들어 해싱합니다. 같은 식별자의 내용 충돌을 검사합니다.
   보드는 ACK의 `bootId`·`windowIndex`를 전송 대상과 대조하고, ACK digest는 수신 증빙으로 보관할 수 있습니다.

샘플 해시의 계산 의미는 다음과 같습니다(설명 코드이며 네트워크 전송은 하지 않습니다).

```python
raw_bytes = b"".join(struct.pack("<hhh", x, y, z) for x, y, z in measured_xyz_counts)
samples = base64.b64encode(raw_bytes).decode("ascii")
integrity = {"algorithm": "sha256", "digest": hashlib.sha256(raw_bytes).hexdigest()}
```

오류 구간에 원시 샘플이 전혀 없다면 `quality`를 해당 오류로 설정하고 `samples: null`,
`integrity: {"algorithm": "sha256", "digest": null}`로 보냅니다. 없는 값을 0 배열로 만들지 않습니다.
일부 샘플이 남아 있으면 실제 `sampleCount`, 해당 바이트와 그 SHA-256을 보냅니다.
`quality=valid`는 512개 샘플이 필요하며 샘플·해시를 null로 보낼 수 없습니다.
서버의 형식·해시 검증 통과는 물리적인 센서 정상 상태나 모델 성능을 보증하지 않습니다.

## 저장·중복·시간 순서

- 식별자: `(deviceId, bootId, windowIndex)`.
- 신규 요청: 원본·측정 시각·최초 수신 시각·digest·처리 상태를 SQLite 트랜잭션으로 저장한 후 **HTTP 202**, `accepted: 1`.
- 같은 식별자·같은 내용 재시도: 추가 저장 없이 **HTTP 200**, `accepted: 0`, `duplicate: true`. 최초 수신 시각을 유지합니다.
- 같은 식별자·다른 내용: **HTTP 409 `SNAPSHOT_CONFLICT`**. 기존 원본을 덮어쓰지 않습니다.
- 늦게 도착한 과거 구간은 시간 순서가 일치하면 저장하고 `lateArrival: true`로 표시합니다.
  GET은 **측정 시각 내림차순**으로 조회하므로 과거 재전송이 최신 측정값을 덮어쓰지 않습니다.
- 같은 부팅 세션의 측정 시각 누적 경과와 uptime 경과를 1초 허용차로 대조합니다.
  시각 정지·역행·부팅 ID 재사용·프로필/매핑 변경을 검증합니다. 630~670 ms 연속성 검사는 적용하지 않습니다.
- 새 구간의 timestamp는 기존 raw 입력과 동일하게 최근 48시간~미래 300초 범위입니다.
  이미 저장된 동일 요청의 재시도는 그 기간이 지나도 ACK할 수 있습니다.
- 서버가 보드 타이머를 제어하지는 않습니다. 메타데이터의 허용 값은 위 표의 조합으로 검사하지만,
  네트워크 지연·재전송으로 HTTP 도착 간격이 짧아졌다는 이유만으로 버리지 않습니다.
  단건 서버 수신 이력만으로 보드의 매 window 상태 전이를 재구성하거나 RF66 연속 3구간 확인을 적용하지 않습니다.

ACK 예시의 의미:

```json
{
  "deviceId": "DEV-01-MOT-02",
  "policyId": "edge-state-snapshot-v1",
  "accepted": 1,
  "acknowledged": [{"bootId": "<전송한 bootId>", "windowIndex": 469, "digest": "<서버 전체 요청 digest>", "receivedAt": "<최초 서버 수신 UTC 시각>"}],
  "duplicate": false,
  "lateArrival": false,
  "processingStatus": "queued",
  "processingEnabled": true,
  "inferenceEnabled": false
}
```

202는 **저장 완료**이며 추론 완료가 아닙니다. `queued`를 이상·정상으로 표시해서는 안 됩니다.
ACK의 `policyId`는 해당 요청에 저장된 정책 ID이며 기존 정책의 재전송에는 기존 정책 ID를 그대로 반환합니다.
품질 불량 신규 구간도 저장되면 202이며 `processingStatus: unavailable`입니다.
400/409는 규격·내용 문제를 수정해야 합니다. 저장 용량·DB 쓰기·커밋 실패 시 503이며 ACK를 주지 않습니다.
타임아웃·응답 유실·503은 동일 요청으로 재시도할 수 있습니다. 성공한 요청을 재시도해도 중복 저장하지 않습니다.

## 비동기 입력 준비와 모델 대기

처리 흐름은 **수신 저장/ACK → 대기 구간 선택 → 입력 재검증·변환 → 결과 커밋 → 모델 대기**입니다.
HTTP 처리는 변환 작업의 완료를 기다리지 않습니다. 서버 시작 시 `snapshot-input` 작업이 시작되고, 종료 시 작업을 정리한 후 DB를 닫습니다.

| 상태 | 의미 | 정상/이상 판정 |
| --- | --- | --- |
| `queued` | 저장됐지만 입력 준비 전 | 없음 |
| `waiting_model` | 입력 준비 완료, 교체 모델 미설정 | 없음 |
| `unavailable` | 보고 품질 불량 또는 입력 준비 실패 | 없음, 정상으로 대체하지 않음 |

- 입력 어댑터 ID: `adxl345-xyz-g-unmodified-v1`. 실제 원본 `counts`를 고정 환산값 `0.0039`로 g 단위로 변환합니다.
- 준비 자료는 `analysis.preparedInput`에 저장합니다. `shape: [512, 3]`, `axes: [X,Y,Z]`, `unit: g`, `sampleRateHz: 800`, 실제 `values`를 포함합니다.
- 평균/DC/중력 제거, 표준화, FFT, RF66 특징 추출, 12개 구간 시계열 구성은 하지 않습니다. **최종 모델의 특징 목록이나 입력 텐서를 확정한 것이 아니라 재현 가능한 원시 입력 준비**입니다.
- 신호가 일정하다는 이유만으로 이전 RF66 규칙을 적용해 버리지 않습니다. 다만 ADXL345 포화 경계 count는 `clipped`로 처리합니다. 센서가 보고한 품질 불량은 준비 작업에 넣지 않습니다.
- 원본 전체 digest와 샘플 SHA-256을 다시 검사합니다. 저장 무결성 실패는 `unavailable`로 기록해 다음 구간 처리를 막지 않습니다.
- 준비된 결과에는 `inputPreparation: {adapterId, status: ready}`, `preparedAt`, `reason: MODEL_NOT_CONFIGURED`를 기록합니다.
  `score`, `threshold`, `verdict`, `modelVersion`은 null이고 `inferenceEnabled`, `affectsAlerts`는 false입니다. 보드의 `ANOMALY_ACTIVE`로 모델 판정을 대신 채우지 않습니다.
- `waiting_model`은 이 단계의 완료 상태입니다. 매 작업 주기마다 같은 구간을 다시 계산하지 않습니다. 미래 모델 연결 시의 명시적 재처리 정책은 별도로 구현해야 합니다.
- 준비 작업은 짧은 SQLite 트랜잭션 하나로 선택·결과 저장을 처리합니다. 두 작업 연결이 겹쳐도 한 번만 처리하며, 커밋 실패/서버 중단 시 원본은 `queued`로 남아 재시작 후 처리됩니다.
- 기존 DB 스키마 1과 원본 digest는 유지합니다. 수신 때 이미 허용된 대기 자료가 48시간보다 오래됐다는 이유로 준비 단계에서 버리지 않습니다. 새로 수신하는 오래된 요청에 대한 제한은 그대로입니다.

## 기존 RF66 연결 중지

- `app.py`는 `RF66_MODEL_ARTIFACT`, `RF66_MODEL_CHECKSUM`, `RF66_EVENT_MODE`를 모델 기동에 사용하지 않습니다.
  `create_server`에 구형 RF66 인자가 직접 전달돼도 로더를 호출하지 않습니다. 설정을 삭제하지 않아도 재시작으로 RF66이 활성화되지 않습니다.
- 서버의 기존 Raw 작업은 `processingEnabled: false`, `runtimeStatus: disabled`입니다. `start()`/`tick()`로 자동 66개 특징 추출·추론을 실행하지 않습니다.
- 기존 `/raw-vibration-windows` POST는 호환을 위해 원본 저장·중복 ACK만 유지합니다. ACK의 `reason: LEGACY_RF66_DISABLED`, `replacementPath`를 확인하고 새 보드는 `/periodic-snapshots`를 사용해야 합니다. 두 경로 사이의 자동 이관은 없습니다.
- 기존 Raw GET의 `configuredModel`은 null, `eventPolicy.mode`는 `disabled`입니다. 저장된 예전 결과는 그대로 조회하되 현재 판정으로 취급하지 않습니다. 화면에서도 연결 중지와 과거 이력을 표시합니다.
- 기존 RF66 자동 사건 투영과 알림 발송은 중지합니다. 운영자의 명시적 과거 사건 검수/수동 종료 기능은 유지합니다. 기존 Raw 이력의 자동 정리도 중지하며 용량 현황은 계속 표시합니다.
- RF66 로더·평가 코드 파일 자체는 과거 결과 재현을 위한 오프라인 테스트용으로 남깁니다. 서버의 자동 호출 경로와는 분리되어 있습니다.
- 기존 모델 패키지가 백업 대상에 포함된 경우 그 파일 보존·무결성 검증은 유지합니다. 이는 모델 실행이나 활성화를 뜻하지 않습니다.

## 저장 파일·백업·관찰

- 기본 파일: `output/periodic-snapshots.sqlite3`; 환경 변수: `PERIODIC_SNAPSHOT_DB_PATH`.
- 다른 저장소와 같은 파일을 사용하면 시작 단계에서 거부합니다.
- WAL + `synchronous=FULL`. 서버 재시작 후 원본·중복 식별자·처리 대기 상태가 남습니다.
- 이번 단계에서는 대기 자료를 자동 삭제하지 않습니다. 전체 최대 100,000행, 최대 10,000개 부팅 스트림.
  용량 도달 시 503으로 재시도를 요청하며 오래된 자료나 미처리 자료를 몰래 버리지 않습니다.
- `operations inspect --device ...`에 별도 저장소의 행 수·상태·최근 측정/수신 시각이 표시됩니다.
  `stage: input_preparation`, `processingEnabled: true`, `inferenceEnabled: false`입니다. `waiting_model`은 의도된 상태이며 준비 지연 경고가 아닙니다.
  준비 전 `queued`가 수신 후 120초를 넘으면 `PREPARATION_BACKLOG` 경고를 표시합니다.
- GET의 `preferredPolicyId`·`transmissionPolicies`는 지원 계약을 알려줍니다. `latestTransmission`은 최신 **측정 시각**의 보고 원본이며 아직 수신이 없으면 null입니다.
  `intervalSec`는 해당 상태에서 기대하는 다음 **정기** 간격(정상 300/이상 10초)입니다. 즉시 보고의 원본 `transmission.intervalSec=0`과 구분합니다.
  이는 반복 주기이며 다음 시각 경계까지 남은 시간이 아닙니다. 복귀 직후 다음 5분 경계가 가까우면 정상 정기 구간이 300초보다 빨리 선택될 수 있습니다.
  inspect의 `latest.transmission`·`expectedReportIntervalSec`에도 이를 표시합니다.
  `boardStateSource: device_report`, `boardStateVerifiedByServer: false`로 서버가 연속 관측을 검증한 값이 아님을 표시합니다.
  이는 마지막 보고 이력이지 현재도 그 상태라는 실시간 보장은 아닙니다. inspect는 상태별 정기 주기 + 60초를 넘어선 측정값과 입력 준비 불가를 경고합니다. 웹의 기존 온라인 판정은 변경하지 않습니다.
- `operations observe`는 새 단건 저장소의 순번·측정 시각 진행을 검사합니다. 기존 RF66 이력의 정지를 현재 수신 장애로 판단하지 않습니다.
- 백업 v2에 새 DB를 포함합니다. 기존 v1 백업도 계속 검증·복구할 수 있습니다.
  새 코드를 운영에 적용한 뒤 서비스를 시작하여 DB가 생성된 상태에서 백업합니다.
- 기존 화면의 온라인 판정·이벤트 상태를 이 단계에서 변경하지 않습니다. 새 자료 확인은 위 GET API를 사용합니다.

## 검증 및 다음 단계

```text
python -m unittest tests.test_snapshot_preparation tests.test_edge_state_snapshots tests.test_periodic_snapshots tests.test_operations tests.test_transmission_policy tests.test_vibration_windows tests.test_raw_vibration tests.test_rf66_confirmation tests.test_rf66_events -q
```

단건·누락/추가 필드·단위·인코딩·해시·권한·매핑·중복·충돌·늦은 도착·시각 정지·쓰기 및 커밋 실패·
재시작·백업 복구·기존 raw 경로 분리를 로컬 테스트합니다. 실보드 전송을 수행했다는 의미는 아닙니다.
새 정책은 네 사유의 수신·카운터 모순 거부·품질 불량·즉시 요청 재시도·늦은 진입 도착·기존 정책 공존·
정상 복귀 뒤 300초 표시·재시작 후 보고 원본 유지·운영 조회·RF66 미실행을 검증합니다.

2단계에서는 단위·축 순서 보존, 원본 재검증, 준비 결과 재시작 유지, 동시 처리 중복 방지,
커밋 실패 복구, RF66 설정 무시, 기존 결과 보존을 추가 검증합니다.

다음 단계는 교체 모델의 버전·입력 규격·전처리·출력 계약이 주어진 뒤 실제 추론 어댑터를 연결하는 것입니다.
준비된 `[512,3]` 값이 새 모델에 곧바로 호환된다고 가정하지 않으며, 기존 12개 이력/3구간 확인도 임의로 재사용하지 않습니다.
