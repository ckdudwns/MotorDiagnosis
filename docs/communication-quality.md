# 통신 품질 자동 수집 — HTTP 장치 관측

## 범위와 활성화

현재 ESP32의 **HTTP 텔레메트리 전송 경로**에서 실패·반복 시도·유효 ACK 왕복 지연·버퍼 지표를 자동 관측한다. 별도 품질 API로 보고하고 독립 SQLite DB에 저장해 기간별로 집계한다. 모터 상태 판정이나 학습 데이터가 아니며, 센서 오류 이벤트·텔레메트리·라벨·원격 설정 상태를 변경하지 않는다.

기본 예제는 품질 채널을 비활성화한다. 신규 관리 화면이나 MQTT 브로커/PUBACK 계측은 이번 범위에 포함하지 않는다. 기존 장치 health API의 RSSI/버퍼 사용률 보고는 유지한다.

1. 서버에 장치별 전용 토큰을 `DEVICE_QUALITY_TOKENS_JSON`으로 주입한다. 형식은 `{"DEV-01-MOT-02":"<장치별로 생성한 충분히 무작위적인 토큰>"}`이다. 공통 데모 토큰이나 관리자·ingest·health·원격 설정 토큰의 대체 경로는 없다. 설정과 품질 채널에 같은 토큰을 재사용하지 않는다.
2. 토큰은 ASCII 영숫자·`_`·`-` 32–128자이며, 중복 장치 키나 장치 간 토큰 중복은 인증 오류로 거부한다. 길이 검사가 무작위성을 보장하지는 않는다. 토큰은 품질 DB나 로그에 저장하지 않는다.
3. 펌웨어의 Git 제외 `secrets.h`에 `DEVICE_QUALITY_TOKEN_VALUE`와 `DEVICE_QUALITY_URL_VALUE`를 설정한다. URL 예시는 `https://backend.example.com/api/devices/DEV-01-MOT-02/communication-quality`다. 다른 장치 ID·끝 `/`·query를 허용하지 않는다. 기존 HTTPS/CA 검증과 명시적인 로컬 HTTP 개발 예외만 사용하며, 리디렉션은 따르지 않는다.
4. 장치·사이트·설비 ID는 모두 `[A-Z0-9][A-Z0-9_.-]{0,62}`로 실제 활성 매핑과 일치해야 한다. 서버는 활성·등록 인증서 상태를 확인하고 다른 매핑의 보고를 거부한다.
5. 운영 실행 `app.py`는 `COMMUNICATION_QUALITY_DB_PATH`를 사용한다. 기본 경로는 `output/communication-quality.sqlite3`이다. 일반 상태 DB나 알림 DB와 **별도 파일**을 사용한다. 테스트용 `create_server()`는 기본 메모리 저장소이며 영속 시험에는 `communication_database=...`를 전달한다.

이 작업은 토큰 주입·서비스 배포·보드 업로드를 수행하지 않는다.

## 무엇을 세는가

| 필드 | 정의 |
| --- | --- |
| `attempts` | 연결된 상태에서 `postPacket()`이 시도한 텔레메트리 전달. 전송 준비 중 로컬 전송 정책 거부도 설정 실패로 포함한다. Wi-Fi 미연결 상태에서 버퍼에만 넣은 기록은 시도가 아니다. |
| `retries` | 같은 부팅에서 바로 이전 시도와 같은 sequence를 다시 전달한 횟수. 창이 바뀌어도 이어지지만 재부팅 전 시도 여부를 추측하지 않는다. |
| `replayAttempts` | 영속 버퍼에서 꺼내 전송한 시도. 오프라인에서 보관했다가 처음 보낸 기록도 포함하므로 `retries`와 다르다. |
| `acknowledged` | 기존 장치 ID·sequence·accepted·duplicate 계약에 맞는 HTTP 201/200 ACK를 관측한 시도. 유일한 저장 레코드 수가 아니며, 같은 기록의 재전송 ACK도 포함한다. |
| `transportFailures` | 전송 오류 등 HTTP 응답을 받지 못한 실패. |
| `retryableResponses` | 기존 분류기가 재시도 가능으로 판정한 서버 응답. |
| `configurationFailures` | 인증·매핑·로컬 전송 정책·응답 계약 문제 등 기존 설정/프로토콜 오류 분류. 2xx여도 유효 ACK가 아니면 성공으로 세지 않는다. |
| `rejectedPackets` | 기존 분류기가 영구 패킷 거절로 판정한 응답. |
| `ackLatencyTotalMs`, `ackLatencyMaxMs` | 성공 ACK에 한해 HTTP POST 시작부터 응답 본문 수신·검증까지 걸린 단조 시계 시간. 서버 처리/전송을 포함한 애플리케이션 왕복 지연이며, 브로커 PUBACK 지연이나 네트워크만의 RTT는 아니다. 응답 로깅·다음 측정 대기는 제외한다. |
| `bufferSamples`, `bufferDepthSum`, `bufferDepthMax`, `bufferDepthLast`, `bufferCapacity` | 메인 루프와 버퍼 추가/제거 시 관측한 깊이·용량. 평균은 **샘플 평균**이며 시간 가중 평균이나 정확한 체류 시간이 아니다. |
| `bufferDropped` | 해당 관측 구간에서 기존 링의 오래된 기록 삭제 카운터가 증가한 양. 부팅 전 누적값을 다시 더하지 않는다. 저장 장치 수준의 유실 전체를 추정하는 수치는 아니다. |
| `wifiSamples`, `offlineSamples` | 메인 루프에서 확인한 연결 상태 표본 수와 그중 미연결 표본 수. 연속 가동시간/정확한 장애 지속시간/전파 손실률이 아니다. |

`attempts = acknowledged + transportFailures + retryableResponses + configurationFailures + rejectedPackets`이며, 각 시도는 한 결과에만 속한다. 품질·health·원격 설정·시간 동기화 요청 자체는 이 텔레메트리 지표에 더하지 않는다.

## 관측 구간, 전송과 재부팅

- 올바른 UTC와 로컬 설정을 확보한 뒤 관측을 시작한다. 이전 시간대의 수치를 0이나 임의 시각으로 생성하지 않는다. 부팅별 UTC anchor 하나와 64비트 단조 uptime으로 구간을 매핑해, 재전송 시각으로 관측 시각을 바꾸지 않는다. 장치 UTC의 정확도 자체는 기존 시간 동기화에 의존한다.
- 정상적으로 약 60초 이상 관측한 뒤 창을 닫는다. 기존 측정·전송은 동기식이므로 정확히 60초마다 실행된다는 보장은 없다. 측정 간격 원격 설정에 따라 지연될 수 있다.
- `{deviceId, bootId, windowId}`가 보고 식별자다. `bootId`는 기존 NVS가 증가·읽기 검증하는 bootSession을 32자리 hex로 표현한다. windowId는 해당 부팅에서만 증가한다. 기존 NVS/장치 ID를 초기화하거나 같은 ID를 새 물리 장치에 재사용하지 않는다.
- 닫힌 창 하나를 기존 NVS `telemetry` namespace의 **`qualityPending` 424바이트**에 저장하고 CRC·전체 읽기 검증을 마쳐야 전송한다. 매 패킷마다 flash를 쓰지는 않는다. 버퍼·sequence·health journal·원격 설정 키는 바꾸지 않는다.
- 같은 창을 재전송해도 서버는 한 번만 저장한다. 서버 commit 이후 ACK 유실, 재부팅, ACK 후 로컬 제거 실패에서도 원래 식별자/본문을 유지한다. 같은 식별자의 다른 내용은 409이고 ACK로 간주하지 않는다. 확인된 ACK 뒤 NVS 제거 실패도 재시도하며, 이전 제거가 실제로 완료돼 키가 없는 경우에는 복구할 수 있다.
- 대기 창은 덮어쓰지 않고, 다음 구간의 카운터는 RAM에서 계속 수집한다. 장시간 오프라인에서는 다음 창이 수분/수시간으로 길어질 수 있다. 전송·저장 실패는 5→10→20→40→60초 상한으로 재시도한다. 호출마다 최대 HTTP 요청 하나, 연결/읽기 각각 1.5초·TLS handshake 3초이며 Content-Length 1024바이트 이하 ACK만 읽는다.
- **완료됐으나 아직 NVS에 저장되지 않은 창과 현재 RAM 관측 구간은 전원 차단 시 유실될 수 있다.** 대기 창이 오래 막혀 있으면 RAM 구간도 길어진다. 모든 시도의 평생 무손실 계수가 아니다. 저장된 대기 창만 재부팅 복원을 보장하며, 새 부팅은 새 식별자로 시작해 관측 공백을 숨기지 않는다.
- 품질 NVS가 손상되거나 다른 대상 ID의 보고가 남으면 보존하고 품질 채널만 비활성화한다. 자동 초기화나 센서 수집 중단은 하지 않는다. 숫자 표현 범위를 초과하면 잘못된 카운터를 발행하지 않는다. 로컬 경고를 확인해야 한다.

## HTTP API

### POST `/api/devices/{deviceId}/communication-quality`

전용 장치 토큰이 필요하다. JSON body는 다음 필드를 모두 포함한다.

```json
{
  "schemaVersion": 1,
  "transport": "http",
  "deviceId": "DEV-01-MOT-02",
  "siteId": "SITE-01",
  "assetId": "SITE-01-MOT-02",
  "bootId": "00000000000000000000000000000001",
  "windowId": 1,
  "startedAt": "2026-09-06T09:00:00.000Z",
  "endedAt": "2026-09-06T09:01:00.000Z",
  "startUptimeMs": 12000,
  "endUptimeMs": 72000,
  "metrics": {
    "attempts": 2, "retries": 1, "replayAttempts": 1, "acknowledged": 1,
    "transportFailures": 1, "retryableResponses": 0,
    "configurationFailures": 0, "rejectedPackets": 0,
    "ackLatencyTotalMs": 125, "ackLatencyMaxMs": 125,
    "bufferSamples": 2, "bufferDepthSum": 5,
    "bufferDepthMax": 3, "bufferDepthLast": 2, "bufferCapacity": 24999,
    "bufferDropped": 0, "wifiSamples": 2, "offlineSamples": 1
  }
}
```

카운터는 boolean·문자열·실수를 허용하지 않는 0–2^53−1 정수다. windowId는 1–2^32−1, 버퍼 용량은 1–1,000,000이다. UTC 형식은 `Z` 종료·소수 최대 밀리초이며, 실제 달력 유효성과 단조 시계 지속시간의 일치도 검사한다. 최소 관측 구간은 60초이고 서버 시각보다 5분 넘게 미래인 창은 거부한다. 같은 부팅의 겹치는 관측 구간을 다른 windowId로 제출해도 거부한다.

새 창은 DB commit 후 201, 같은 창 재시도는 200이다. 응답에는 `accepted: true`, `deviceId`, `bootId`, `windowId`, `duplicate`, `disposition`이 있다. 정상 보관은 `disposition: "stored"`, 30일 보관기간 밖의 창은 200/`disposition: "expired"`로 확인하되 저장·집계하지 않는다. 만료된 장치 대기 창이 영구 재시도하거나 삭제 후 다시 통계를 생성하지 않게 한다. 펌웨어는 단순 2xx가 아닌 이 계약 전체를 검사한다.

DB 저장 실패는 `503 COMMUNICATION_QUALITY_STORAGE_UNAVAILABLE`이며 성공 ACK를 보내지 않는다. 같은 보고를 그대로 재시도한다. 실제 오류 세부 정보나 인증 토큰은 응답에 노출하지 않는다.

### GET `/api/devices/{deviceId}/communication-quality`

기존 사용자 세션·`device:read`·사이트 접근 권한이 필요하다. 품질 보고용 장치 토큰으로 읽을 수 없다. 현재 장치의 사이트·설비 범위로 제한해 이전 매핑의 기록을 새 매핑으로 돌려주지 않는다.

Query:

- `from`, `to`: RFC3339 UTC, 생략하면 현재 기준 최근 24시간. 최대 30일.
- `bucketSeconds`: `60`, `300`, `3600`, `86400`, 기본 3600. 최대 1000개 구간.

응답의 `summary`와 `items`는 시도·실패·재시도 합계, 성공 ACK 수, 가중 ACK 평균/최댓값, 버퍼 샘플 평균/최댓값/최신값, 삭제 건수, 관측 지속시간을 제공한다. ACK 평균은 전체 지연 합계/전체 ACK 수로 계산하며 구간별 평균을 다시 평균내지 않는다.

**귀속 기준:** `from < window.endedAt <= to`인 창 전체를 종료 시각이 속하는 구간에 배정한다. 정확한 구간 경계에서 끝난 창은 직전 구간에 속한다. 긴 창을 분·시간별로 임의 분할하지 않는다. `attribution: "whole_window_at_end"`와 구간별 `crossBoundaryWindows`로 경계를 넘어온 창을 표시한다. 따라서 긴 장애 창의 합계는 요청한 분/시간 안에서만 발생한 건수라고 해석하면 안 된다. `firstWindowStartedAt`, `lastWindowEndedAt`, `observedDurationMs`를 함께 확인한다.

창이 없는 구간은 `hasData: false`, 지연·실패율·버퍼값은 `null`이다. 합산 건수가 0이라는 이유로 정상 통신이라고 판단하지 않는다. ACK가 없으면 지연은 `null`, 실제 지연 0ms나 버퍼 깊이 0은 유효한 0으로 유지한다.

## 저장·보관·운영 한계

별도 SQLite에 원본 창과 불변 범위·해시·서버 수신 시각을 저장하고 인덱스로 조회한다. 종료 시각 기준 30일만 보관한다. 시작 및 백그라운드 정리에서 만료 창을 삭제하고, 새 쓰기가 없어도 조회에서 만료 창을 제외한다. 일반 상태 DB를 매 보고마다 전체 체크포인트로 저장하지 않는다. 이전 서버가 이 별도 DB를 덮어쓰는 경로도 없다.

서비스 종료는 진행 중 요청이 끝난 뒤 품질 DB를 닫는다. DB 잠금 대기는 HTTP 요청 수용 루프나 텔레메트리 상태 잠금 안에서 수행하지 않는다. 전체 서비스의 기존 단일 백엔드 상태 소유 모델은 그대로이며, 품질 DB의 원자적 중복 처리만으로 전체 서비스의 다중 프로세스 운영을 지원하는 것은 아니다.

실제 ESP32 업로드, 실제 Wi-Fi/TLS 장애, 전원 차단, 장기 무선 품질/flash 수명 시험은 별도로 필요하다. 호스트 저장 모형과 빌드가 실물 시험을 대신하지 않는다. 이 기능은 관측되지 않은 구간의 지표를 추정하거나 기존 설정으로 전파 품질을 자동 조절하지 않는다.

## 회귀 시험

- `tests/test_communication_quality.py`: 인증·범위·입력 경계, 중복/충돌/겹침, 동시 제출, 가중 평균/빈 구간, 장시간 창, 저장 실패/재시작, 보관기간과 실제 HTTP API.
- `firmware/esp32_edge_node/test/test_communication_quality/test_main.cpp`: 실제 Collector/Outbox의 카운터, 재시도/버퍼 구분, 64비트 uptime, CRC, 저장/제거 실패, ACK 유실/재부팅 모사, 불변 대기 창과 재시도 간격.
- `IOT_QUALITY_FIXTURE_EXE`에 빌드한 호스트 실행 파일을 지정하면 C++에서 만든 보고 → 실제 백엔드 HTTP 저장/중복 확인 → C++ ACK 검증을 연결한다.
- 호스트 실행은 `platformio test -e native -f test_communication_quality`, ESP32 빌드는 `platformio run -e esp32-s3-devkitc-1`이다. 기존 Zig/Unity 환경으로 동일 제품 소스를 빌드할 수도 있다.
