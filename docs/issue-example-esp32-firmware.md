# ESP32 운영 서버 연동 및 HTTPS 통신 안정화

## 담당 및 우선순위

- 담당 파트: `IoT`
- 담당자: 하드웨어·ESP32 펌웨어 담당
- 우선순위: `P1`
- 관련 주차·기능 ID: 4주차 MVP / ESP32 telemetry·health 전송

## 현재 상태와 목표

현재:

- ESP32가 센서 데이터를 수집하고 서버로 전송함.
- 운영 HTTPS 인증서와 장치 상태 보고 인증 경로 점검이 필요함.

변경 후:

- 운영 endpoint·장치 ID·인증 설정이 적용됨.
- CA 인증서 검증을 유지한 상태로 telemetry와 health 요청이 동작함.
- 서버 ACK의 중첩 메타데이터를 허용하되 필수 ACK 필드는 엄격히 검증함.

## 작업 범위

- [x] 운영 endpoint, device/site/asset ID 적용
- [x] 장치 상태 보고용 Bearer token 경로 추가
- [x] CA 인증서 검증 활성화
- [x] LittleFS 이전 세션 격리 처리 지연 개선
- [x] API v1.3 중첩 ACK 메타데이터 및 회귀 테스트 반영

## 이번 작업에서 제외하는 내용

- 모터 사양 확정
- 실제 설치 환경 및 센서 부착 위치 확정
- AI 판정 로직 및 Backend·Frontend 기능 변경
- 실제 토큰 커밋

## 연동 사항

- 관련 API·데이터 필드: `/api/telemetry/ingest`, deviceId, siteId, assetId, telemetry ACK
- 요청·응답 형식 및 null 처리: HTTPS Bearer 인증; 필수 ACK 필드는 타입을 엄격히 검증하고 선택적 중첩 메타데이터는 허용
- 선행 작업 또는 관련 이슈: API v1.3 명세, 4주차 MVP
- 협의가 필요한 담당 파트: Backend — ACK·health 응답 계약 확인

## 완료 기준

- [x] 요구사항에 맞게 구현
- [x] 정상·오류·경계 조건 테스트 통과
- [x] 기존 기능 호환성 확인
- [x] 필요한 API 명세·사용 문서 갱신 여부 확인
- [x] 미구현 또는 미검증 항목 명시
- [ ] PR 리뷰 및 머지 완료

## 검증 방법

- 실행 명령 또는 재현 절차: Native firmware test 실행 후 PlatformIO build/upload 및 ESP32 serial monitor로 HTTPS 전송 확인
- 기대 결과: Native 56/56 PASS, telemetry HTTP 201 accepted=true, duplicate replay HTTP 200 duplicate=true
- 실장비 검증 필요 여부: `예`

## 참고 자료

- 기획서·기능정의서: `Bind_Edge_AI_4주_MVP_기획서_v1.2.docx`
- API 명세: `Bind_Edge_AI_API_명세서_v1.3.xlsx`
- 관련 이슈·PR: [PR #34](https://github.com/ckdudwns/MotorDiagnosis/pull/34)

