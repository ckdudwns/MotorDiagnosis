# 5분 정기 단건 원시 구간 수신 — 1단계

## 범위와 현재 상태

- 보드는 측정을 계속하되 대부분 버리고, **정상·이상 여부와 관계없이 5분마다 그 시점의 구간 하나**를 선택합니다.
- 즉시 전송, 우선 전송, 과거 5분치 묶음 전송은 새 정책에 포함하지 않습니다.
- 서버는 인증 → 입력·무결성 검증 → 원본/수신 시각/처리 상태 저장 → 커밋 → ACK까지 구현합니다.
- `quality=valid`는 `queued`로 보존합니다. 새 추론 작업자는 아직 없으므로 자동 분석하지 않습니다.
- 오류 품질 구간도 원본을 보존하되 `unavailable`로 기록합니다. 이를 정상으로 판정하거나 0으로 채우지 않습니다.
- 기존 API, 기존 RF66 모델·연속 3구간·이벤트·알림 경로와 과거 데이터는 변경하지 않습니다.
- 펌웨어 수정, 모델 교체, 새 경로의 이벤트·알림과 웹 화면 연결은 이번 단계에 포함하지 않습니다.

`5분`은 전송 주기입니다. **샘플링 800 Hz, 512개 XYZ 샘플, 구간 길이 640 ms**와 다른 개념입니다.
원시 구간 하나를 보내는 것이며, 스칼라 측정값 한 개나 5분 평균값을 보내는 것이 아닙니다.

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

### 보드 전달용 JSON 형태

아래 `<...>`는 실제 값으로 대체할 설명 자리이며 그대로 전송하는 테스트 데이터가 아닙니다.

```json
{
  "transmission": {
    "policyId": "periodic-single-v1",
    "mode": "periodic",
    "intervalSec": 300
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
`transmission` 역시 위 세 필드만 사용합니다. `priority`, `immediate`, `replay`, 이상 트리거 필드는 없습니다.
전송 실패 후 재시도할 때도 `mode`를 바꾸지 않고 **동일한 요청**을 다시 보냅니다.

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
- 서버가 보드 타이머를 제어하지는 않습니다. 메타데이터는 고정 300초만 허용하지만,
  네트워크 지연·재전송으로 HTTP 도착 간격이 짧아졌다는 이유만으로 버리지 않습니다.

ACK 예시의 의미:

```json
{
  "deviceId": "DEV-01-MOT-02",
  "policyId": "periodic-single-v1",
  "accepted": 1,
  "acknowledged": [{"bootId": "<전송한 bootId>", "windowIndex": 469, "digest": "<서버 전체 요청 digest>", "receivedAt": "<최초 서버 수신 UTC 시각>"}],
  "duplicate": false,
  "lateArrival": false,
  "processingStatus": "queued",
  "processingEnabled": false
}
```

202는 **저장 완료**이며 추론 완료가 아닙니다. `queued`를 이상·정상으로 표시해서는 안 됩니다.
품질 불량 신규 구간도 저장되면 202이며 `processingStatus: unavailable`입니다.
400/409는 규격·내용 문제를 수정해야 합니다. 저장 용량·DB 쓰기·커밋 실패 시 503이며 ACK를 주지 않습니다.
타임아웃·응답 유실·503은 동일 요청으로 재시도할 수 있습니다. 성공한 요청을 재시도해도 중복 저장하지 않습니다.

## 저장 파일·백업·관찰

- 기본 파일: `output/periodic-snapshots.sqlite3`; 환경 변수: `PERIODIC_SNAPSHOT_DB_PATH`.
- 다른 저장소와 같은 파일을 사용하면 시작 단계에서 거부합니다.
- WAL + `synchronous=FULL`. 서버 재시작 후 원본·중복 식별자·처리 대기 상태가 남습니다.
- 이번 단계에서는 대기 자료를 자동 삭제하지 않습니다. 전체 최대 100,000행, 최대 10,000개 부팅 스트림.
  용량 도달 시 503으로 재시도를 요청하며 오래된 자료나 미처리 자료를 몰래 버리지 않습니다.
- `operations inspect --device ...`에 별도 저장소의 행 수·상태·최근 측정/수신 시각이 표시됩니다.
  `stage: ingest_only`, `processingEnabled: false`이고, 대기는 아직 시작하지 않은 추론 작업이지 RF66 작업자 장애가 아닙니다.
- 백업 v2에 새 DB를 포함합니다. 기존 v1 백업도 계속 검증·복구할 수 있습니다.
  새 코드를 운영에 적용한 뒤 서비스를 시작하여 DB가 생성된 상태에서 백업합니다.
- 기존 화면의 온라인 판정·이벤트 상태를 이 단계에서 변경하지 않습니다. 새 자료 확인은 위 GET API를 사용합니다.

## 검증 및 다음 단계

```text
python -m unittest tests.test_periodic_snapshots tests.test_operations tests.test_transmission_policy tests.test_vibration_windows tests.test_raw_vibration tests.test_rf66_confirmation tests.test_rf66_events -q
```

단건·누락/추가 필드·단위·인코딩·해시·권한·매핑·중복·충돌·늦은 도착·시각 정지·쓰기 및 커밋 실패·
재시작·백업 복구·기존 raw 경로 분리를 로컬 테스트합니다. 실보드 전송을 수행했다는 의미는 아닙니다.

다음 단계에서 이 inbox를 읽는 새 모델 입력 어댑터와 추론 작업자를 연결합니다.
전송 프로필 검증과 모델 입력 구성은 분리하며, 기존 12개 이력/3구간 확인을 임의로 재사용하지 않습니다.
