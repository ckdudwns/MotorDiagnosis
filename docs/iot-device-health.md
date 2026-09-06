# IoT 장치 상태 보고·센서 오류/회복

추가 구현 목록 IOT-01/02에 대응한다. 펌웨어 버전은 `v1.3-iot-health.1`이며,
기존 ESP32-S3 / ADXL345(SPI) / INMP441(I2S), 텔레메트리 링·sequence·ACK·TLS 경로를 유지한다.
운영 백엔드 API나 학습 라벨 권한을 확대하지 않는다.

## 설정

버전 관리에서 제외된 `firmware/esp32_edge_node/include/secrets.h`에 다음을 설정한다.
실제 값은 `secrets.example.h` 또는 Git에 넣지 않는다.

```cpp
#define DEVICE_HEALTH_URL_VALUE "https://backend.example.com/api/devices/DEV-01-MOT-02/health"
#define DEVICE_HEALTH_TOKEN_VALUE "<별도로 발급한 health 전용 토큰>"
```

URL의 장치 ID는 펌웨어의 `DEVICE_ID`와 일치해야 한다. 보고용 응답에서도 장치 ID와
요청 `reportedAt`에 대응하는 `lastReceivedAt`을 검사한다. 200만 반환되거나 응답이
손상돼 확인할 수 없으면 대기열을 지우지 않는다.

백엔드의 현재 운영 주입 경로는 환경변수 `DEVICE_HEALTH_TOKEN`이다.
이 토큰은 `device-health:write`만 갖고 텔레메트리 수집·관리자 API 권한은 없다.
현재 백엔드의 이 환경변수 경로는 여러 장치에 쓰는 공통 서비스 토큰이다.
장치별 토큰 발급·회수 및 `allowedDeviceIds` 제한을 운영에서 요구한다면 백엔드의
자격증명 공급 체계를 별도로 구성해야 한다. 이 펌웨어는 관리자 토큰을 사용하지 않는다.

기존 `secrets.h`에 새 설정이 없어도 컴파일은 가능하지만 상태 보고는 비활성이다.
시작 로그의 미설정 경고를 확인한다. 센서 오류 관측은 계속 보존하며, 전이 대기열이
가득 차면 새 수집을 보류하므로 운영 전 보고용 토큰·URL 설정이 필요하다.
HTTPS와 CA 검증이 기본이며 리다이렉트는 따르지 않는다. 기존의 명시적 로컬 개발용
HTTP 허용 설정만 재사용한다. 기본 예제에서 TLS 검증을 끄지 않는다.

## IOT-01: 보고 주기·내용

`POST /api/devices/{deviceId}/health`에 다음을 보낸다.

- `reportedAt`: UTC RFC3339 밀리초 시각
- `rssiDbm`: 현재 Wi-Fi RSSI(-120~0 dBm)
- `rebootCount`: 기존 NVS `bootSession` 증가값에서 최초 부팅을 제외한 횟수
- `bufferUsagePct`: 기존 영속 텔레메트리 링의 논리 사용량/용량 비율
- `firmwareVersion`: 실제 빌드 버전
- `sensorFaults`: 아래 오류 코드의 active/recovered 전이 또는 확인된 현재 상태

최초 연결 및 상태 변화 시 보고하고, 정상 상태도 30초마다 갱신한다.
RSSI 5 dB 또는 버퍼 사용률 5%p 변화는 조기 보고 조건이다. 요청 간 최소 간격은
5초이며, 실패 시 10→20→40→60초 상한의 backoff를 적용한다. 이 주기는 메인 루프의
기존 동기식 시간 동기화·재전송에 따라 늦어질 수 있으며 실시간 실행 보장은 아니다.
health HTTP 연결·읽기 timeout은 각각 1.5초, TLS handshake timeout은 3초다.

`rebootCount`는 기존 bootSession 기능 적용 이후의 로컬 부팅 이력이다.
장치 제조 이후 전체 이력을 추정하지 않으며 백엔드 정수 범위 상한으로 제한한다.
일반 ESP 텔레메트리의 `rpm:null`과 미라벨 정책은 바꾸지 않는다.

## IOT-02: 오류·회복

| 코드 | 관측 조건 |
|---|---|
| `adxl345_init_failed` | ADXL345 ID·설정 read-back 실패 |
| `inmp441_init_failed` | I2S 드라이버 설치 또는 핀 설정 실패 |
| `adxl345_channel_error` | 측정 전후 ADXL345 ID 확인 실패 |
| `inmp441_channel_error` | I2S 읽기 오류·무수신·잘못된 바이트 정렬 또는 전체 공통 윈도우의 디지털 값 고착 의심 |
| `vibration_acquisition_timeout` | 진동 worker 취득 시간 초과 또는 이전 취득 미완료 |
| `acoustic_window_unavailable` | 음향 공통 윈도우 대기 시간 초과 또는 링 범위 복사 실패 |
| `sensor_features_invalid` | 동기화된 특징값이 NaN/Inf |

INMP441은 I2S 드라이버 설치 성공만으로 물리적 마이크 연결을 확인할 수 없다.
0.64초 전체 윈도우가 같은 디지털 값이면 정상 무음이라고 단정하지 않고 채널 고착을
의심해 수집을 보류한다. 작은 값이라도 변화가 있는 신호는 허용한다.
이 진단은 고장 원인 확정이나 학습 정답 라벨이 아니다.

각 센서는 부팅당 초기화/재초기화를 합쳐 최대 3회 시도한다. 실패 후 재시도 간격은
사용한 시도 횟수에 따라 5초, 10초로 늘어난다. 소진 시 재부팅/현장 조치가 필요하며
Wi-Fi, 상태 보고, 기존 텔레메트리 backlog 재전송은 계속 동작한다.

I2S 설치·읽기·해제·재초기화는 audio task 하나에서만 실행한다. 읽기는 100 ms로
제한하고 실패한 DMA 구간 양쪽의 샘플을 하나의 특징 윈도우로 합치지 않는다.
진동 worker가 시간 초과 후에도 실행 중이면 완료 전 재요청이나 SPI 재설정을 하지 않는다.
회복은 정상 동기화·유한 특징값 확인 후 기록한다. 초기화 오류 중 ADXL 설정 복구는
설정 read-back 성공으로도 확인할 수 있지만 런타임 오류는 정상 취득 전에 해제하지 않는다.

## 전이 보존과 시각

센서 task의 오류 관측은 크기 제한 RTOS 큐로 전달한다. 메인 루프가 통신 중이어도
오류 플래그가 먼저 해제돼 관측이 사라지지 않는다. 큐가 가득 차면 덮어쓰지 않고
센서 task가 기다린다. NVS 접근은 메인 task에서만 수행한다.

NVS `healthJournal`은 CRC와 write/read-back 검증을 사용하는 32개 전이 FIFO다.
동일 오류 상태는 재등록하지 않아 최초 시각을 유지하고 flash 쓰기를 줄인다.
전이는 **한 요청에 하나씩** 보내고 확인 응답 및 NVS ACK 마커 저장 후에만 다음으로
넘어간다. active/recovered를 한 요청에 묶어 재시도하면서 종료된 이벤트가 다시 열리는
경로를 피한다. 전이가 없을 때만 알고 있는 현재 상태들을 주기 보고한다.

저장 실패는 RAM의 미저장 관측을 유지하며 저장 재시도 동안 새 센서 수집을 보류한다.
보고 실패·ACK 마커 저장 실패도 현재 전이를 남긴다. 전이 큐가 가득 차도 오래된
전이를 덮어쓰지 않는다. 손상된 journal은 임의 초기화하지 않고 수집을 보류한다.
이때 로그와 NVS 상태를 운영자가 확인해야 한다.

UTC가 없는 동일 부팅의 관측은 ESP의 64비트 uptime 차이로 복원하며, 약 49일마다
순환하는 `millis()`를 발생 시각 복원에 사용하지 않는다. 이전 부팅의 관측에
새 부팅의 anchor를 적용하지 않는다. 복원이 불가능하면 `occurredAt`을 생략하고
`detail`에 원인을 명시해 백엔드 수신 시각을 사용한다. 이 경계 이후의 회복이 이미
확인된 발생 시각보다 과거로 기록되지 않도록 같은 fallback을 적용하며, 원본 관측
메타데이터는 journal의 최신 상태에 유지한다. UTC가 전혀 없을 때는 보고를 보류한다.

기존 NVS/sequence·LittleFS·링 복구 실패, mutex/task 할당 실패의 안전 중단은 유지한다.
장애 시점 이전에 이미 확인·저장된 텔레메트리는 기존 FIFO/ACK 규칙대로 처리한다.

## 검증·제외 범위

실행 절차는 [펌웨어 테스트 안내](../firmware/esp32_edge_node/TESTING.md)를 참고한다.
네이티브 테스트는 생산 코드의 journal·payload·응답 검사·재시도 정책을 호출한다.
선택 실행 HTTP 검사는 C++에서 생성한 실제 요청을 로컬 백엔드에 전달한다.

보드 업로드, 실제 센서 탈착/전원 차단, Wi-Fi·TLS 장애 주입 및 24시간 연속 시험은
자동 테스트로 대체하지 않았으며 실제 장비에서 별도로 수행해야 한다.
실제 RPM 센서 추가, 원격 설정, 통신 품질 자동 수집, 자동 모델 배포는 이번 범위에서 제외한다.
