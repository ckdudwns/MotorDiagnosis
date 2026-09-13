# 오류 판정 + 5분 뒤 특징 예측 모델 연동

## 1. 적용 범위

2026-09-13 전달된 두 JSON의 **학습 파라미터는 수정하지 않는다**. 온라인 재학습, 임계값 재조정,
이전 RF66 재가동, 과거 기록의 자동 재분석·알림 재발송은 하지 않는다.

| 역할 | 저장소 파일 | 입력 | 출력 |
|---|---|---|---|
| 오류 판정 | `models/pump-event-verifier/event-verifier-model.json` | 같은 센서·부팅의 직전 24건 + 현재 9개 특징 | 학습 기준 대비 이상 가능성, 점수 > 1 |
| 예측 | `models/pump-forecast/model.json` (최신 `model.json`, v3) | 현재 포함 13건의 9개 특징 | 현재 측정 시각 + 300초의 9개 특징값 또는 예측 불가 |

오류 모델의 점수는 **고장 확률이 아닌 기준 대비 편차 비율**이다.
예측은 고장 발생 시각·잔여수명 예측이 아니다. 예측값은 사건·알림 판정에 사용하지 않는다.

오류 모델 SHA-256: `0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135`

예측 모델 v3 SHA-256: `f91bd1b999f213e7f24b07b51ba28970b13e0dbbec7bb6afaea5100cfb04d9a6`

두 파일의 학습 원본 해시는 동일하다. 각 파일에 `freshwater_supply_motor2`와
`freshwater_supply_motor3` 계수가 따로 있다. **실제 장치/센서 → 학습 stream 배정은 운영자가 명시해야 한다.**
장치 이름의 숫자로 자동 배정하지 않는다. 현재 구현은 한 서버 설정에서 한 센서를 명시적으로 연결한다.
다른 센서의 수신·저장은 계속되지만 이 모델로 자동 판정하지 않는다.

### 특징 의미에 관한 제한

전달된 파일은 `domainValidated=false`, 예측 파일은 `unit=unverified_source_values`,
`operationallyApproved=false`이다. 엑셀 원본 센서의 계산 공식이 알려지지 않아,
새 ADXL 특징과 학습 특징이 같다고 인증할 수는 없다. 따라서 `experimental-adxl25`를 명시한
**비교 적용**으로만 로드하며 결과/화면에 `sourceFeatureEquivalenceVerified=false`를 남긴다.
프로파일 이름만 바꾼 호환 인증이나 현장 성능 검증 완료를 뜻하지 않는다.
학습 담당자에게 원본 특징 공식 또는 동일 Raw에 대한 비교용 특징값을 받아 교차 확인해야 한다.

## 2. IoT 담당자에게 전달할 변경 규격

### 측정·전송 정책

1. 800Hz · XYZ · 512샘플 · 평균 제거 후 CF/왜도/Pearson 첨도 계산은 유지한다.
2. 정기 기록은 **Unix epoch 기준 25초 등간격**으로 생성한다. `slot % 25 == 0`이다.
   매분 00·25·50초로 재시작하지 않는다. 예: `00:00:00 → 00:00:25 → 00:00:50 → 00:01:15 → 00:01:40`.
3. 정상·이상 상태 모두 정기 이력을 유지한다. 이상 3회 진입 즉시, 유지 10초, 정상 5회 복귀 즉시 보고도 유지한다.
4. 정기 경계에서 끝난 가장 최근 **유효 window 1개**를 선택한다.
   샘플 시작은 `[slot−1.640초, slot−0.640초]`, 즉 끝난 지 최대 1초 이내인 window만 쓴다.
   해당 조건의 valid가 없으면 invalid/null/원인 1건을 보낸다. 오래된 valid를 반복하지 않는다.
5. `timestamp`는 실제 window 시작 UTC, `startUptimeUs`는 해당 시점의 uptime이다.
   `periodicSlotEpoch`와 실제 측정 시각을 별도 보존한다. 슬롯 시각을 실제 측정 시각으로 바꾸지 않는다.
6. `historySequence`는 같은 bootId에서 첫 정기 기록 0부터 **슬롯마다 1씩** 증가한다.
   invalid도 증가한다. 생성에 실패한 슬롯도 번호를 건너뛰어 결손을 드러내며, 즉시/10초 보고는 증가시키지 않는다.
7. `windowIndex`는 생성한 전송 레코드마다 증가한다. 재전송 때 bootId/index/시각/내용을 유지한다.
8. 정기와 즉시/10초 보고가 같은 window에서 겹치면 **한 레코드로 합친다**.
   `eventType`은 전환/유지 보고, `periodicSlotEpoch`와 `historySequence`는 정기 슬롯 값을 함께 싣는다.
   경계에 맞는 최신 window라는 조건은 그대로 검사한다. 같은 슬롯을 두 번 생성하지 않는다.
9. 정기와 겹치지 않은 이벤트의 `periodicSlotEpoch`, `historySequence`는 모두 `null`이다.
10. 기존 LittleFS 개별 파일, ACK 후 삭제, Wi-Fi/재부팅 후 원본 순차 재전송은 유지한다. Raw 저장/전송은 추가하지 않는다.

특징 직렬화 순서:
`cf_a_1, cf_a_2, cf_a_3, sk_a_1, sk_a_2, sk_a_3, ku_a_1, ku_a_2, ku_a_3`.
각 축 평균 제거 후 `CF=max(abs(x))/sqrt(mean(x²))`,
`sk=mean(x³)/mean(x²)^(3/2)`, `ku=mean(x⁴)/mean(x²)²`.
표본 보정/초과 첨도는 적용하지 않는다. 분산 0 또는 비유한 수치는 invalid이다.
이 식은 **보드 측 새 계약**이며 엑셀 원본과 동일하다고 아직 확인된 식은 아니다.

### Endpoint·새 버전

- `POST /api/devices/{deviceId}/periodic-snapshots`
- HTTPS 및 기존 `telemetry:ingest` 토큰 유지
- 최상위 `window`, `transmission` 중첩 유지
- `schemaVersion=3`
- `profileId=adxl345-ac-cf-sk-ku-25s-v1`
- `policyId=edge-feature-history-v1`
- `eventType=periodic | anomaly_start | anomaly_active | recovery`
- `periodic`도 `state=ANOMALY_ACTIVE`를 허용한다.
- 이전 `schemaVersion=2`, `edge-feature-snapshot-v1`은 저장 가능하지만 두 모델에는 연결하지 않는다.
- 펌웨어 전환 시 **새 bootId**를 사용한다. 같은 부팅에 프로파일을 바꾸면 409로 거부한다.

정기 요청 예시(과거 시각이므로 운영 서버에 시험 전송하지 말 것):

```json
{
  "window": {
    "schemaVersion": 3,
    "deviceId": "DEV-01-MOT-02",
    "siteId": "SITE-01",
    "assetId": "SITE-01-MOT-02",
    "sensorId": "SENSOR-02",
    "bootId": "0123456789abcdef0123456789abcdef",
    "windowIndex": 0,
    "timestamp": "2026-09-12T23:59:59Z",
    "startUptimeUs": 99000000,
    "sampleRateHz": 800,
    "sampleCount": 512,
    "profileId": "adxl345-ac-cf-sk-ku-25s-v1",
    "axes": ["X", "Y", "Z"],
    "unit": "dimensionless",
    "quality": "valid",
    "reason": null,
    "features": {
      "cf_a_1": 2, "cf_a_2": 2, "cf_a_3": 2,
      "sk_a_1": 0, "sk_a_2": 0, "sk_a_3": 0,
      "ku_a_1": 3, "ku_a_2": 3, "ku_a_3": 3
    },
    "integrity": {
      "algorithm": "sha256",
      "digest": "89a69bbab3610b08f31a96c9b988d947db12cae3daf37a7fb41ed81418cc6c2e"
    },
    "periodicSlotEpoch": 1789257600,
    "historySequence": 0
  },
  "transmission": {
    "policyId": "edge-feature-history-v1",
    "eventType": "periodic",
    "state": "NORMAL",
    "anomalyCount": 0,
    "normalCount": 0
  }
}
```

특징값 integrity는 기존과 같은 **9개 IEEE-754 float64 little-endian, 72바이트의 SHA-256**이다.
JSON 텍스트 해시가 아니다. invalid/features=null은 빈 바이트열의 SHA-256이다.

ACK는 기존과 같다. 신규 수신 202/accepted=1, 정확히 동일한 재전송 200/accepted=0/duplicate=true.
`acknowledged[0]`에 bootId/windowIndex/sensorId/digest/receivedAt/eventType/durablyStored/featureDigest를 반환한다.
`digest`는 서버 정규화 전체 요청 해시, `featureDigest`는 위 72바이트 해시다. 둘을 혼동하지 않는다.
ACK는 **영구 저장 성공**이며, 이력 확보 또는 모델 계산 완료를 의미하지 않는다.

## 3. 서버 처리

인증·장치 매핑 → 버전/품질/해시/시각/슬롯 검증 → 원본 저장·커밋 → ACK → 입력 준비 → 비동기 모델 계산.

- 이력은 같은 device/site/asset/sensor/boot/profile/policy에서 조회한다.
- 현재 요청이 **접수되기 전에 저장된**, 측정 시각상 과거인 정기 슬롯만 사용한다.
- 즉시/10초 이벤트는 정기 슬롯을 함께 가진 병합 보고가 아니면 이력에 포함하지 않는다.
- 슬롯 차이 25초와 historySequence +1을 모두 확인한다. 실제 측정 시작 지터는 최대 1초,
  UTC/uptime 일치는 별도로 확인한다. 재부팅, invalid, 슬롯 누락을 건너뛰거나 보간하지 않는다.
- 오류 판정: 직전 24건을 사용한다. 첫 정기 시점부터 약 10분 후 첫 판정이 가능하다.
- 예측: 직전 12건+현재 정기 1건이다. 첫 정기 시점부터 약 5분 후 첫 예측이 가능하다.
- 결과는 `analysis`의 기존 오류 판정 필드와 **별도 `analysis.forecast`**로 저장한다.
  오류 판정이 이력 부족이어도 예측 13건 조건을 만족하면 예측은 저장된다.
- 예측에는 features, predictedFor, basedOnMeasuredAt, targetSlotEpoch, modelVersion,
  inputOrdinals/inputDigests, affectsAlerts=false를 보존한다.
- 예측 실패·대기는 features=null이다. 0 또는 이전 예측을 현재 예측처럼 대신 표시하지 않는다.
- 모델 쌍/전처리/센서 배정이 바뀌면 새 binding을 사용한다. 이전 대기 작업을 새 모델로 실행하지 않는다.
- DB 원본·과거 결과를 수정하지 않는다. 뒤늦게 온 이력으로 과거 결과를 자동 갱신하지 않는다.

운영 현황의 기존 수신 카드에 **5분 뒤 특징값 예측**이 별도 표시된다.
`operations inspect`에도 `PERIODIC_SNAPSHOT_DB_PATH.latest.forecast`가 추가된다.
오류 판정만 기존 shadow/events/alerts 정책에 연결되며 예측값은 항상 알림과 분리된다.
초기 비교 적용은 events(사건 기록, 외부 알림 꺼짐)를 권장한다. 현재 경보 설정은 자동 변경하지 않는다.

## 4. 서버 탑재 설정

우선 코드 머지·서버 업데이트 후 보드 규격을 바꾼다. 이 문서는 실제 EC2/보드에 변경을 실행한 기록이 아니다.
장치/센서 기준을 확인한 뒤 설정한다. 아래 `<확정한 학습 stream>`은 반드시 담당자가 선택한 값으로 교체한다.
`SENSOR-02`라는 이름만으로 `freshwater_supply_motor2`에 자동 연결해서는 안 된다.

```ini
[Service]
Environment="PUMP_DUAL_EVENT_ARTIFACT=/home/ubuntu/MotorDiagnosis/models/pump-event-verifier/event-verifier-model.json"
Environment="PUMP_DUAL_EVENT_CHECKSUM=0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135"
Environment="PUMP_DUAL_FORECAST_ARTIFACT=/home/ubuntu/MotorDiagnosis/models/pump-forecast/model.json"
Environment="PUMP_DUAL_FORECAST_CHECKSUM=f91bd1b999f213e7f24b07b51ba28970b13e0dbbec7bb6afaea5100cfb04d9a6"
Environment="PUMP_DUAL_STREAM=<확정한 학습 stream>"
Environment="PUMP_DUAL_DEVICE_ID=DEV-01-MOT-02"
Environment="PUMP_DUAL_SITE_ID=SITE-01"
Environment="PUMP_DUAL_ASSET_ID=SITE-01-MOT-02"
Environment="PUMP_DUAL_SENSOR_ID=SENSOR-02"
Environment="PUMP_DUAL_INPUT_MODE=experimental-adxl25"
```

기존 `PUMP_EVENT_VERIFIER_*` 설정과 동시 지정하면 기동을 거부한다. 기존 drop-in의 해당 설정만 정리해야 하며,
인증/토큰/기타 운영 설정은 유지한다. 경로·체크섬·배정 하나라도 빠지거나 파일이 다르면 기동 전에 실패한다.
실제 센서 특징 동일성이 확인되지 않았다는 비교 적용 표시를 숨기지 않는다.

기동 변경 전 모델 사전 검사(조회만, 서버 설정·DB 수정 없음):

```bash
.venv/bin/python -m motor_diagnosis.pump_models \
  --event-artifact models/pump-event-verifier/event-verifier-model.json \
  --event-checksum 0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135 \
  --forecast-artifact models/pump-forecast/model.json \
  --forecast-checksum f91bd1b999f213e7f24b07b51ba28970b13e0dbbec7bb6afaea5100cfb04d9a6 \
  --stream '<확정한 학습 stream>' \
  --device DEV-01-MOT-02 --site SITE-01 --asset SITE-01-MOT-02 --sensor SENSOR-02 \
  --input-mode experimental-adxl25
```

`artifactsVerified=true`는 파일 검증이지 실제 모터 예측 정확도 검증이 아니다.
두 모델과 배정 설정은 운영 백업 대상에 함께 포함되며, 복구 검사에서 파일 해시와 쌍 구성을 다시 확인한다.

## 5. 검증 명령

### v3 예측 보호 규칙

v3는 기존 Ridge 계수와 표준화 통계를 유지하며 `inputRobustLimit`,
`targetLower/targetUpper`를 추가한 파일이다. 재학습이나 센서 특징 동일성 검증을 뜻하지 않는다.
동봉 `models/pump-forecast/evaluation.json`은 제공된 평가 기록이며,
새 차단 규칙을 적용한 현장 성능 또는 차단율로 해석하지 않는다.

- 13건으로 만든 last/mean/population-std/delta 36차원 벡터에 예측 stream의
  `max(abs((vector[i]-center[i])/scale[i]))` (`i in active`)를 계산한다.
  Ridge의 inputStd나 오류 모델의 24건 기준과 혼용하지 않는다.
- 이 값이 inputRobustLimit을 **초과**하면 `FORECAST_INPUT_OUT_OF_DISTRIBUTION`이다.
- 기존 Ridge 계산과 targetStd/targetMean 역변환은 변경하지 않는다.
- 결과가 targetLower/targetUpper의 닫힌 구간 밖이거나 CF/Pearson 첨도가 1 미만이면
  `FORECAST_OUTPUT_OUT_OF_RANGE`이다. NaN/inf/계산 실패는 `FORECAST_OUTPUT_INVALID`이다.
- 서버 정책 `pump-forecast-reject-v3`는 값을 자르거나 0으로 대체하지 않는다.
  차단 시 status=unavailable, features=null과 guard 근거를 저장한다.
  오류 판정 모델은 별도로 계속 실행하며 기존 사건·알림 모드는 바꾸지 않는다.
- 현재 예측이 차단되면 화면에서 이전 성공 예측을 대신 표시하지 않는다.
  과거 원문은 그대로 남고, v2 메타데이터도 이력 조회 시 인식한다.

배포 시 모델 파일만 먼저 덮어쓰지 않는다. 기존 운영 백업 후 **v3 서버 코드·파일과 위 체크섬을 함께**
적용하고 모델 사전 검사를 통과한 뒤 서비스를 재시작한다. 이전 체크섬이나 v2 파일을 계속
사용하면 새 로더가 거부한다. 새 preprocessingVersion/binding은 대기 작업을 분리하며
과거 결과 재계산·알림 재발송은 하지 않는다. 롤백은 대응되는 코드·v2 파일·체크섬을 함께 복원한다.

```bash
.venv/bin/python -m unittest tests.test_pump_dual_models tests.test_pump_event_model \
  tests.test_edge_feature_snapshots tests.test_snapshot_inference tests.test_snapshot_events \
  tests.test_snapshot_dashboard tests.test_operations -q
node --test tests/test_snapshot_dashboard.mjs
```

기본 서버 로더는 표준 라이브러리만 쓴다. NumPy는 독립 행렬 계산 비교 테스트에만 필요하다.
합성값 테스트를 실제 장비 측정이나 모델 성능 수치로 제출하지 않는다.
