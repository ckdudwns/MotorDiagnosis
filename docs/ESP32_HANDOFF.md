# ESP32 펌웨어 작업 인수인계

## 현재 상태

- 담당 범위: 하드웨어 및 ESP32 펌웨어
- 4주차 MVP: 완료
- 운영 endpoint: `https://motordiagnosis-api.duckdns.org`
- 수집 API: `/api/telemetry/ingest`
- 장치: `DEV-01-MOT-02`
- Site: `SITE-01`
- Asset: `SITE-01-MOT-02`
- 브랜치: `feat/esp32-telemetry-integration`
- PR: [#34](https://github.com/ckdudwns/MotorDiagnosis/pull/34)

## 완료된 변경

- 운영 HTTPS endpoint 및 장치 식별자 적용
- 장치 상태 보고용 Bearer 인증 경로 추가
- CA 인증서 검증 활성화
- LittleFS 이전 세션 격리 처리의 불필요한 directory scan 제거
- API v1.3 `lifecycleUpdates` 중첩 메타데이터 허용
- 필수 ACK 필드 타입 검증은 엄격하게 유지
- 실제 서버 ACK 형상 회귀 테스트 추가
- 실제 토큰은 ignored `firmware/esp32_edge_node/include/secrets.h`에만 저장

## 검증 결과

- Native firmware tests: `56/56 PASS`
- Live HTTPS telemetry: `HTTP 201`, `accepted=true`
- Replay duplicate: `HTTP 200`, `duplicate=true`
- 최근 서버 수신 시각: `2026-09-07T13:29:16.046Z`
- ESP32 build/upload 완료

## 다음 작업

- PR #34 리뷰 및 머지
- 머지 후 운영 장치 재전송 로그 확인
- 모터 사양과 실제 설치 환경 확정
- 센서 부착 위치 및 측정 목표를 확정한 뒤 하드웨어 검증 계획 수립

## 주의사항

- `secrets.h`와 실제 인증 토큰은 커밋하지 않는다.
- 현재 작업 트리의 `app.py`, `platformio.ini`, `ISOLATION_CLEANUP_FIX.md`, `pr-push.cmd`, `scripts/`, `verify.cmd` 변경은 기존 unrelated 작업이므로 이번 작업에 포함하지 않는다.
- 새 컨텍스트에서는 이 문서와 `docs/issue-template-iot.md`를 먼저 확인한다.

## 관련 문서

- [이슈 템플릿](issue-template-iot.md)
- [ESP32 이슈 작성 예시](issue-example-esp32-firmware.md)
- [펌웨어 테스트 안내](../firmware/esp32_edge_node/TESTING.md)
