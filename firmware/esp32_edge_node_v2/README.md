# ESP32 Edge Node v2

기존 `firmware/esp32_edge_node`와 충돌하지 않도록 별도 경로에 둔 ADXL345 진동 특징값 전송 펌웨어입니다.

## 구현 범위

- ADXL345 3축 센서에서 800 Hz로 512샘플 수집
- 평균 제거 후 축별 CF, Skewness, Pearson Kurtosis 9개 특징값 계산
- Raw 진동 샘플은 전송·LittleFS 저장하지 않음
- 정상 상태: UTC 25초 슬롯마다 최신 유효 window 1개 전송
- 이상 시작·이상 유지·정상 복귀: 기존 상태 머신의 후보 전송 정책 유지
- 센서 수집, Wi-Fi/NTP, LittleFS 저장, HTTPS 업로드를 FreeRTOS task로 분리
- 전송 실패 후보는 LittleFS에 보존하고 ACK identity 확인 후 삭제
- `summarySequence`는 정기 기록에만 사용하고 `windowIndex`는 모든 측정 window에 사용

## 필요한 설정

실제 장비에서는 이 파일을 복사해 `include/config/secrets.h`를 만들고 값을 입력합니다.

```powershell
Copy-Item include/config/secrets.example.h include/config/secrets.h
```

- Wi-Fi SSID / 비밀번호
- 특징값 수집 endpoint
- ingest token
- 서버 CA 인증서
- 장치 health endpoint와 health token

장치·사이트·설비·센서 ID와 센서 핀, 9개 특징 계산은 `include/config/app_config.h`에서 확인합니다.
현재 센서 ID는 `SENSOR-02`, 장치 ID는 `DEV-01-MOT-02`입니다.
이상 임계값은 아직 테스트용 비활성 상태이며 모델/서버 계약 확정 후 조정합니다.

## 검증

```powershell
pio test -e native
pio run -e esp32-s3-devkitc-1-n16r8
```

실제 보드 검증은 ESP32-S3 DevKitC-1 WROOM-1-N16R8, ADXL345, USB 시리얼 115200 baud 기준입니다.
서버 endpoint와 ACK JSON 계약은 서버 구현 완료 후 `secrets.h`와 업로드 parser를 맞춰야 합니다.
