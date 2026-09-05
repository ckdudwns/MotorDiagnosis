# AI2 운영 대시보드 연결

2026-09-05 추가 구현 목록의 AI2-01/02, UI-01~06 범위다. PR19가 병합된
`2811ca0`을 기준으로 기존 백엔드 API와 화면 구조를 사용하며, API 명세 파일은 수정하지 않는다.

## 범위와 기존 구현 재사용

| 항목 | 반영 내용 |
|---|---|
| AI2-01 | PR19의 자산별 규칙·점수·센서 고장 → lifecycle → 이벤트/checkpoint 저장 연결 재사용. 실제 HTTP 연동 회귀 테스트 추가 |
| AI2-02 | 기존 `min_duration_enter_sec`/`min_duration_exit_sec`와 측정 timestamp 기반 지속 조건 재사용. 0, 0.1, 9.9초에는 미생성, 10초에 생성하는 경계 검증 |
| UI-01 | 이벤트 상세 API의 전후 신호·특징·규칙/모델 버전·장치 스냅샷 표시. 원본 미수신/만료는 누락 상태 표시 |
| UI-02 | 이벤트 심각도·라벨·검수 여부·정렬과 서버 페이지 이동. 사이트 요약 전체를 12개씩 탐색 |
| UI-03 | 검수 사유·전체 검수 이력, 원인/점검/조치별 개별 메모 생성·수정·삭제·이력·최신 표시, 첨부 참조 |
| UI-04 | 진입/종료 점수 임계선·규칙 지속시간·이벤트 구간 표시. 확대·이동·시점 선택을 자동 갱신 중 유지. null/미수신은 결측 |
| UI-05 | 조회 완료된 사이트·설비·절대 기간 또는 확대 구간과 내부 데이터셋 버전 ID를 CSV/XLSX export로 전달 |
| UI-06 | 장치 RSSI/재부팅/버퍼/센서 오류와 서비스 상태. 권한별 기준정보·설치·통신망·규칙·알림 정책·파라미터·감사 관리 |

이벤트 연결 자체는 `motor_diagnosis/data.py`의 `_lifecycle_config_for`,
`_lifecycle_for`, `process_accepted_telemetry`, `_apply_sensor_health_boundary`를 사용한다.
현재 브랜치에서는 이 경로와 저장·ACK 동작을 다시 작성하지 않는다. AI1 모델 실행이나
모델 점수의 임의 환산, 승인/배포 워크플로도 추가하지 않는다.

## 사용 흐름

1. 기존 계정으로 로그인하고 사이트·설비·조회 기간을 선택한다.
2. 이벤트 목록을 필터링하고 상세 신호/스냅샷을 확인한다. 원본이 없으면 합성 데이터로 대체하지 않는다.
3. 검수 라벨/요약 메모는 변경 사유와 저장한다. 개별 메모는 분류·내용·첨부 참조와 별도로 저장한다.
4. 차트의 확대·이전/다음 구간·전체 구간 버튼과 클릭으로 신호를 탐색한다.
5. 실시간 실측 또는 등록된 내부 데이터셋 버전을 선택한 뒤 CSV/XLSX를 내려받는다.
6. 운영 관리에서 항목을 선택해 목록을 불러오고, 허용된 필드만 사유와 함께 변경한다.

등록된 사이트나 설비가 없어도 권한이 있으면 기준정보 신규 등록부터 시작할 수 있다.
장치 등록 전에는 설비의 설치 위치·확인된 보정 기준선과 장치 도입 대상/통신망 설정을
등록해야 한다. 설비 기준선은 측정 결과를 직접 입력하며 raw RMS를 물리 단위로 임의 환산하거나
미확인 값을 자동으로 `ready` 처리하지 않는다. 인증서 ID/지문도 기존 등록 조건을 따른다.
외부 데이터셋은 이 화면의 신호 내보내기 대상에서 제외된다. 첨부는 기존 API의 URI 참조만
저장하며 파일 업로드/다운로드 권한이나 외부 저장소 연동을 새로 만들지 않는다.

## API 및 권한 경계

| 화면 | 기존 API | 권한 |
|---|---|---|
| 상세/검수/메모 | `/api/anomaly/events/{id}`, `/api/events/{id}/review`, `/reviews`, `/notes`, `/notes/{noteId}/history` | `event:read`, 변경은 `event:review` |
| 내보내기 | `/api/datasets`, `/api/datasets/export` | `dataset:read`, `export:read` |
| 사이트·설비·장치 | `/api/sites`, `/api/sites/{id}/assets`, `/api/sites/{id}/devices`, `/api/devices/{id}` | 각 리소스의 `:read`/`:write` |
| 설치·통신망 | `/api/assets/{id}/install-points`, `/api/sites/{id}/network-profile` | `install-point:*`, `network-profile:*` |
| 장치 도입 대상 | `/api/sites/{id}/rollout-plan` | `rollout:read`, `rollout:write` |
| 규칙·알림 정책 | `/api/anomaly/rules/{assetId}`, `/api/alerts/policies` | `anomaly-rule:*`, `alert-policy:*` |
| 파라미터·감사 | `/api/parameters`, `/api/audit-logs` | `parameter:*`, `audit-log:read` |
| 장치·서비스 상태 | `/api/devices/{id}/health`, `/api/health/dependencies` | `device:read`, `service-health:read` |

표의 `:*`는 설명용 약기다. 실제 화면은 로그인 응답의 정확한 `:read`/`:write` 권한을
확인하고, 서버가 사이트 접근과 변경 권한을 다시 검증한다. `mutable:false` 파라미터는
시스템 관리자에게도 조회 전용이며 펌웨어 관리 값을 덮어쓰지 않는다.

내보내기 API의 신호 선택 계약은 `siteId`, `assetId`, `from`, `to`, `datasetId`, `format`이다.
심각도·라벨·검수 상태는 **이벤트 목록 필터**이며 신호 행의 필터가 아니므로 export에 전달하지
않는다. 이 차이를 화면에도 안내한다. 선택한 내부 데이터셋의 불변 버전은 해당 `datasetId`로
고정되며, 서버는 버전의 원래 범위와 요청한 기간/설비의 호환성을 검증한다.

## 상태 및 실패 처리

- 상세/목록/관리 요청에 순번을 부여해 늦게 도착한 이전 응답이 새 선택을 덮어쓰지 않는다.
- 조회 범위 변경 시 이전 신호와 상세를 비우고, 새 조회가 성공하기 전에는 export를 막는다.
- 5초 자동 갱신은 작성 중 검수/메모/관리 입력과 확대·선택 상태를 유지한다.
- 저장 중 추가 입력은 별도 미저장 초안으로 남긴다. 중복 클릭을 막고 실패를 화면에 표시한다.
- 검수 저장이 독립 메모 초안을 지우지 않도록 먼저 해당 메모 저장/취소를 안내한다.
- 사용자/서버 문자열과 첨부 참조는 `textContent`로 표시하고 HTML이나 링크로 실행하지 않는다.
- 신규 설치정보는 기존 API 계약에 따라 활성 상태로 생성하고, 비활성화는 등록 후 수정한다.
- 데모 권한/환경 차단과 실측·합성 분리 계약은 유지한다.

## 검증

```text
python -m unittest tests.test_ai2_dashboard
node tests/test_ai2_dashboard.mjs
node tests/test_week4_dashboard.mjs
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s ai/ai2/week1/tests
python -m unittest discover -s ai/ai2/week2/tests
python -m unittest discover -s ai/ai2/week3/tests
python tests/smoke_week1_http.py
python -m compileall -q motor_diagnosis ai/ai2 tests
git diff --check
```

`test_ai2_dashboard.mjs`는 실제 제공되는 스크립트를 Node VM과 DOM 모형으로 실행한다.
22개 동작 검사는 비동기 경합·페이지 이동·안전한 문자열 표시·편집/확대 보존·권한·내보내기를
검증한다. Python 테스트는 별도 메모리 서버에서 실제 HTTP 요청으로 lifecycle, 상세, 검수,
메모, CSV/XLSX·내부 버전 export 및 관리 API의 읽기/쓰기를 검증한다.

실물 장치/브로커, 장시간 운전, 외부 이메일/Webhook 발송과 실제 브라우저의 화면·다운로드
종단 간 검증은 수행 범위에 포함하지 않는다. 로컬 코드 변경만 하며 외부 배포는 하지 않는다.
