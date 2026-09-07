# RF66 원시 구간 비교 추론

## 범위

ADXL345 800Hz × 512 XYZ 원시 count 구간을 기존
`POST /api/devices/{deviceId}/raw-vibration-windows`로 받습니다.
서버는 count × 0.0039g 변환 후 `mcc5-vibration-800hz-spectral66-v1`의
66개 특징을 생성하여 각 구간을 독립적으로 판정합니다. 기본 21개 특징만
보내는 API 또는 음향 값에는 이 모델을 연결하지 않습니다.

`score`는 RandomForestClassifier의 True 클래스 확률(0~1)이며,
`verdict = score > threshold`입니다. 임계값과 같으면 false입니다.
20260908 전달 모델의 임계값은 `0.79148320436784`입니다.
기존 LSTM/Dense의 재구성 오차 `error`, 대시보드 `anomalyScore`와 다릅니다.

이번 구현은 **shadow 비교 추론·저장만** 수행합니다. 3구간 연속 확인,
알림·이벤트 발행, 화면 변경, 현장 정확도 검증, 운영 배포는 포함하지 않습니다.
`affectsAlerts`, `fieldValidated`, `domainValidated`, `confirmationApplied`는
모두 false입니다. 전달된 fault 예시에도 임계값 아래의 미탐이 있으므로,
예제 점수 재현 성공을 현장 정상/이상 성능 검증으로 해석하면 안 됩니다.

## 모델 및 실행 환경

ZIP 안의 Python 스크립트는 실행하거나 설치하지 않습니다. 로더는 크기를
제한해 메모리에서 읽고 manifest 파일 목록·해시, 별도로 지정한 모델 SHA256,
66개 특징 순서·전처리 계약, 판정 규칙, 모델 내부 메타데이터를 검사합니다.
전처리 계약 전체의 지문을 고정했으므로 새 특징 규격은 코드와 수치 검증을
갱신하기 전에는 거부합니다. 학습 문서에 남은 Dense 후보명을 사용하지 않습니다.

joblib은 실행 가능한 pickle입니다. **신뢰한 담당자에게 받은 모델에 대해서만**
별도 경로로 확인한 SHA256을 설정하십시오. ZIP 내부 manifest만 신뢰하여
임의 파일을 로딩하면 안 됩니다. ZIP·모델은 저장소에 커밋하지 않습니다.

필수 패키지는 `ai/ai2/requirements-rf66.txt`로 분리했습니다.
RF66 설정이 없으면 이 패키지를 import하지 않습니다. 설정이 있으면
패키지 버전 불일치·모델 오류 시 서버 시작을 중단합니다.
기존 운영 가상환경을 바로 변경하지 말고 별도 환경에서 먼저 확인하십시오.
학습 환경은 Windows Python 3.13.5이며 Ubuntu/Python 3.14 호환성은 별도 시험이 필요합니다.

설정 예시(실제 ZIP 위치는 배포자가 준비):

```ini
[Service]
Environment=RF66_MODEL_ARTIFACT=/home/ubuntu/.local/share/motordiagnosis/models/AI2_RF66_handoff_20260908.zip
Environment=RF66_MODEL_CHECKSUM=sha256:631c29cdcff2fc79843f607c38c0b37c9d1df63e36838bcd88a242942ba23c80
Environment=RAW_VIBRATION_WINDOW_DB_PATH=/home/ubuntu/.local/share/motordiagnosis/raw-vibration-windows.sqlite3
```

기존 `SHADOW_MODEL_*`, `WINDOW_MODEL_VARIANT`와 독립적입니다.
RF66 ZIP을 `SHADOW_MODEL_ARTIFACT`로 지정하지 마십시오.
DB 경로는 **이미 사용 중인 원시 구간 DB의 절대 경로를 유지**하십시오.
새 DB를 임의로 지정하면 기존 결과·장치 이력 보호를 이어받지 못합니다.

## 처리·저장·조회

접수 시 원시 구간을 SQLite에 먼저 저장하고 작업자가 순서대로 추론합니다.
`GET /api/devices/{deviceId}/raw-vibration-windows`는 기존 로그인·권한·사이트
범위 검사를 유지하며, 최신 구간의 `analysis`와 현재 `configuredModel`을 반환합니다.
구간 결과에는 입력 특징, score, threshold, verdict, 모델 SHA256,
특징 규격, shadow/미검증 상태를 저장합니다. 재시작 이후에도 조회 가능합니다.

| 상태 | 의미 |
| --- | --- |
| queued | 접수 완료, 처리 대기 |
| completed | 해당 모델의 구간별 비교 추론 저장 완료 |
| waiting_model | 유효 특징을 생성했으나 RF66 미설정 |
| unavailable | 품질 불량, 전처리 또는 추론 실패; score/verdict는 null |

센서 장애·누락·포화·상수 축을 0으로 채워 정상 판정하지 않습니다.
입력 품질이 valid여도 포화/상수 축은 특징 계산 시 다시 거부합니다.
단일 구간 모델이므로 앞 구간 누락은 `missingWindowsBefore`로 남기고,
현재 구간 자체가 유효하면 독립 판정합니다. 연속 3구간 확인은 적용하지 않습니다.

동일 device/boot/windowIndex의 동일 내용 재전송은 기존 접수 결과를 반환하고
재추론하지 않습니다. 이미 처리한 completed/unavailable/waiting_model은
모델을 바꾸거나 켜도 **자동 재계산하지 않습니다**. 과거 자료 재처리는 별도 작업입니다.
재시작 당시 queued인 구간과 새 구간은 시작 시 로딩한 모델로 처리합니다.
DB 저장 실패 시 queued로 남아 재시도하므로 계산은 반복될 수 있으나 결과 행은
중복 생성되지 않습니다. 한 DB에는 하나의 서버 프로세스를 사용하십시오.

RF66 설정 두 개를 함께 제거하고 재시작하면 새 구간은 waiting_model이 됩니다.
원시 DB는 모델 설정과 무관하게 열려 장치 ID 보호와 과거 결과 조회를 유지합니다.
기존 48시간 보존·전체 용량·접수 제한 정책은 그대로입니다. 접수되지 않은
503 응답 구간은 장치 재시도가 필요하며 모든 구간의 무손실 전달을 보장하지 않습니다.

## 개발 검증

### RF66 운영 현황 화면

운영 현황에서 사이트·설비를 선택하고 새로고침하면 별도 RF66 영역이
선택 설비 장치들의 원시 구간 조회 API를 호출합니다. 기존 통계 차트와 모델
등록·승인 정보는 그대로 유지합니다. RF66 영역은 기간 필터와 별개인 최근
저장 구간 중 최대 5개를 장치별로 표시합니다.

현재 적용 모델/임계값과 결과에 저장된 모델 버전을 구분하며, 모델이 꺼지거나
교체된 경우 이전 결과에 과거 모델 표시를 붙입니다. 입력 대기·처리 대기·모델
대기·판정 불가·조회 실패는 정상 후보와 구분합니다. 점수 0은 유효한 값입니다.
최근 판정 대상 구간의 측정 시각을 표시하며 실제 추론 실행 시각으로 표현하지
않습니다. 과거 기록 조회 성공은 실시간 장치 온라인의 증거가 아닙니다.

device:read, telemetry:read, model:read 권한을 모두 확인합니다.
설비·장치 매핑이 다른 응답과 오래된 비동기 응답은 표시하지 않습니다.
새로고침과 기존 자동 갱신 경로를 사용하며 수집/설정 변경 API는 호출하지 않습니다.
화면 회귀 검사는 `node --test tests/test_rf66_dashboard.mjs`로 실행합니다.

일반 회귀 테스트:

```text
python -m unittest tests.test_rf66 tests.test_raw_vibration tests.test_vibration_windows
```

신뢰한 전달 ZIP 경로를 `RF66_TEST_ARTIFACT` 환경 변수로 설정하고 같은 테스트를
실행하면 실모델 전용 테스트도 실행합니다. 모델 SHA256은 테스트에도 별도로
고정돼 있습니다. 원시 count → 특징 → 점수 → 저장 경로 및 정상/고장 예제의
기대 점수를 확인합니다. 시험 없이 운영 서비스를 재시작하지 마십시오.
