# 펌프 이력 기반 이상 확인 — 서버·IoT 연동 계약

> **이전 이력 모델 호환 규격**입니다. 최신 IoT 기획안은 [특징값 전용 규격](edge-feature-snapshots.md)을 우선합니다.
> 새 프로파일은 Raw를 받지 않으며 UTC 00·25·50초/이상 중 정기 중단 정책입니다. 아래 모델의 정확한 25초 이력 조건과 혼용하지 않습니다.

## 적용 범위

PR46의 원본 저장·추론 작업·이벤트 처리를 확장한다. 기존 RF66은 다시 연결하지 않는다.
기존 Raw 단건 API와 과거 결과는 보존한다. 이 문서는 **서버 구현 계약**이며 보드 변경·운영 배포 완료 보고가 아니다.

- 모델: `models/pump-event-verifier/event-verifier-model.json`
- 평가: 같은 폴더의 `event-verifier-evaluation.json`
- 모델 JSON 전체 SHA-256: `0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135`
- 학습 코드 계산 기준: PR45 `be6ec32`, `ai/ai2/pump_event_verifier.py`의 `verifier_vector`/`predict`.
- 모델 내부 `sourceSha256`은 학습 원본의 해시다. 위 모델 파일 해시와 혼용하지 않는다.
- 이 모델은 라벨 없는 학습 기준 대비 이탈 확인이다. 실제 고장 확률·고장 정확도·잔여수명을 출력하지 않는다.
- 최근 13개로 5분 뒤 특징을 예측하는 모델은 **이번 파일에 없으며 연결하지 않았다.** 학습을 다시 실행하지 않는다.

## 데이터와 판정 흐름

```text
센서별 25초 정기 특징 9개 ─── 원본 저장 ─── 같은 센서·부팅의 과거 24건
                                                  ↓
현재 정기 기록 또는 즉시/10초 이벤트 ─── 현재 특징 9개와 비교
                                                  ↓
                                   기준 초과 / 미초과 / 판정 불가
                                                  ↓
                                결과·근거 저장 → 화면 → 사건 처리
```

보드는 매 window 측정·1차 판단을 계속한다. **상태와 관계없이 25초 기록은 계속 보존·전송**한다.
정기 기록은 현재 기록을 제외한 직전 24건과 비교한다. 최초 24건은 이력 부족이고 보통 25번째부터 판정된다.
24건 자체의 시작~끝 범위는 575초, 다음 정기 기록까지 포함하면 600초다.

| 보고 조건 | reason | state | mode / intervalSec | historySequence | Raw |
| --- | --- | --- | --- | --- | --- |
| 상태와 관계없이 25초 정기 기록 | `history_periodic` | 현재 상태 | `periodic` / 25 | 1씩 증가하는 정기 순번 | 선택 |
| 보드 이상 연속 3회 진입 | `anomaly_enter` | `ANOMALY_ACTIVE` | `immediate` / 0 | null | 필수 |
| 이상 유지 중 10초 보고 | `anomaly_periodic` | `ANOMALY_ACTIVE` | `periodic` / 10 | null | 선택 |
| 정상 연속 5회 복귀 | `normal_recovered` | `NORMAL` | `immediate` / 0 | null | 선택 |

`anomalyCount`/`normalCount`는 보드 보고이며 모델의 정답 라벨이 아니다. 진입은 3/0, 복귀는 0/5다.
일반 NORMAL 보고에서 anomalyCount는 3 미만, ACTIVE 보고에서 normalCount는 5 미만이다.
품질 불량 보고는 두 카운터를 0으로 보내며 진입/복귀 확정 사유로 보내지 않는다.

정기·상태 전이가 **같은 측정 구간에 겹치면 정기 기록 하나를 우선**한다. `historySequence`를 유지하고,
새 보드 상태·카운터 및 진입 시 Raw를 포함해 그 구간을 즉시 전송한다. 같은 식별자를 다른 사유로 다시 보내지 않는다.
정기 기록도 서버 검증 대상이므로 이 경우 별도 이벤트 복제 없이 서버 사건으로 연결될 수 있다.

## 특징 정의 확인 — 장치 담당자에게 반드시 전달

순서는 `cf_a_1, cf_a_2, cf_a_3, sk_a_1, sk_a_2, sk_a_3, ku_a_1, ku_a_2, ku_a_3`이다.
모델에 없는 RMS·피크 주파수·음향·RF66 특징으로 대체하지 않는다.
원시 XYZ만으로 이 9개를 임의 계산하는 추출기는 제공하지 않는다.

담당자에게 다음을 확인받아 학습 엑셀과 일치시킨다.

- 축 1/2/3의 실제 축 매핑, 센서 종류, 샘플링 주파수, 특징 계산 창 길이와 실제 샘플 수.
- cf/sk/ku 계산식, 첨도 종류, 표준편차/편향 보정, 필터·중력 제거 여부, 단위·보정값.
- 학습은 기존 5초 기록 중 25초 격자 행을 선택했다. **25초 전체 평균을 새로 계산하는 것과 다르다.**
- 25초 시점의 기존 특징 생성 방식과 실센서 출력 의미를 맞춰야 한다. 값을 이름만 바꾸어 넣지 않는다.
- 센서 2/3의 학습 기준 중 어떤 것을 어떤 실제 sensorId에 적용할지 명시한다. 번호가 같다는 이유로 자동 매핑하지 않는다.

현재 서버 실행 인스턴스의 모델 설정은 **장치·사이트·설비·센서 한 조합과 모델 stream 하나**다.
다른 센서의 원본도 독립 저장하지만 이 모델로 자동 판정하지 않고 `waiting_model`로 둔다.
여러 센서에 모델을 동시에 배정하는 레지스트리는 이번 범위에 포함하지 않았다.

## 실제 HTTP 계약

`POST /api/devices/{deviceId}/periodic-snapshots` 및 같은 경로 GET을 사용한다.
POST는 기존 `telemetry:ingest` 장치 토큰·allowedDeviceIds·활성 매핑을 검사한다. health 전용 토큰은 사용할 수 없다.
GET은 운영자 로그인·device:read·telemetry:read 및 사이트 접근 권한이 필요하다.
원본 JSON 최대 64 KiB, 요청당 **window 하나**다. 배치 배열·payload 안의 history는 받지 않는다.

초안의 `measuredAt`은 아래 `timestamp`, 전체 보고 `sequence`는 `windowIndex`에 대응한다.
**정기 전용 순번 historySequence는 별도다.** 이벤트가 사이에 있어도 정기 순번이 건너뛰면 안 된다.
초안 JSON을 그대로 보내는 API가 아니라 아래 버전 계약으로 변환해야 한다.

```json
{
  "window": {
    "schemaVersion": 1,
    "deviceId": "DEV-01-MOT-02",
    "siteId": "SITE-01",
    "assetId": "SITE-01-MOT-02",
    "sensorId": "SENSOR-02",
    "bootId": "0123456789abcdef0123456789abcdef",
    "windowIndex": 100,
    "timestamp": "2026-09-12T10:00:00+09:00",
    "startUptimeUs": 600000000,
    "profileId": "pump-cf-sk-ku-summary-v1",
    "quality": "valid",
    "features": {
      "cf_a_1": 1.1, "cf_a_2": 1.2, "cf_a_3": 1.3,
      "sk_a_1": 0.01, "sk_a_2": 0.02, "sk_a_3": 0.03,
      "ku_a_1": 3.1, "ku_a_2": 3.2, "ku_a_3": 3.3
    },
    "historySequence": 24,
    "integrity": {"algorithm": "sha256", "digest": "실제 특징 바이트에서 계산한 64자리 소문자 16진수"}
  },
  "transmission": {
    "policyId": "pump-verifier-history-v1",
    "mode": "periodic",
    "intervalSec": 25,
    "reason": "history_periodic",
    "state": "NORMAL",
    "anomalyCount": 0,
    "normalCount": 0
  }
}
```

위 값은 형식 설명용이며 그대로 전송하는 실측 데이터가 아니다. timestamp·식별자·특징·해시는 실제 값으로 채운다.
`integrity.digest`는 **위 순서의 특징 9개를 IEEE-754 float64 little-endian 72바이트로 직렬화한 SHA-256**이다.
JSON 문자열 해시가 아니다. Python 기준 `sha256(struct.pack('<9d', *values)).hexdigest()`와 같아야 한다.
음수 0과 양수 0의 비트 표현도 일치시킨다. 무결성은 인증을 대체하지 않는다.

`quality` 허용값: `valid`, `fifo_overrun`, `sensor_unavailable`, `sample_gap`, `clipped`, `constant_axis`, `processing_overflow`.
누락/불량 기록도 정기 순번을 남기고 보낸다. features가 없으면 null, 그 해시는 빈 바이트열의 SHA-256이다.
valid에서 null·누락·문자열·NaN·무한대·임의 0 채우기는 허용하지 않는다.
통신 재시도만으로 측정 품질을 바꾸지 않는다. 통신 지연은 receivedAt과 timestamp 차이로 별도 확인한다.

### Raw 증거 첨부

window 안에 다음 `rawWindow`를 추가한다. 이상 진입에서 필수이고 나머지 사유에서는 선택이다.

```json
{
  "sampleRateHz": 800,
  "sampleCount": 512,
  "axes": ["X", "Y", "Z"],
  "encoding": "base64-int16le-xyz",
  "gPerCount": 0.0039,
  "samples": "실제 interleaved XYZ int16le 바이트의 base64",
  "integrity": {"algorithm": "sha256", "digest": "디코딩한 Raw 바이트의 SHA-256"}
}
```

Raw는 부모의 동일 측정 시각·센서·구간에 해당해야 한다. 품질 valid이면 축별 512개, 총 3072바이트다.
기존 ADXL345 count 범위·base64 정규형·길이·별도 해시를 검증한다. Raw만 보내면 모델 입력이 되지 않는다.
다른 센서의 Raw 인코딩은 이 ADXL345 규격에 억지로 맞추지 말고 별도 계약을 합의한다.

## 이력·시각·재전송 규칙

- 같은 device/site/asset/sensor/boot/profile의 **현재보다 먼저 측정되고, 현재 접수 전에 저장된** 정기 기록만 사용한다.
- timestamp는 실제 측정 시각이다. HTTP 전송 시각이나 서버 수신 시각으로 덮어쓰지 않는다.
- 과거 24건의 timestamp와 startUptimeUs 간격이 각각 25초이며 historySequence가 연속이어야 한다.
  구현의 1마이크로초 허용은 부동소수점 표현 오차용이지 수십 ms 측정 지터 허용이 아니다.
  격자 사이 기록을 서버가 반올림·복제·보간하여 정상 이력으로 만들지 않는다. 실제 타이밍 규격 변경은 학습 측과 재합의한다.
- 현재 이벤트는 마지막 정기 기록 이후 0초 초과, 50초 이내여야 한다. 현재가 정기 기록이면 25초·순번 +1이다.
- bootId는 재부팅마다 바뀌는 32자리 소문자 16진수다. 센서별 이력은 재부팅 시 다시 채운다.
- windowIndex는 같은 센서·boot 안의 측정 순서를 나타낸다. 미전송 window 때문에 건너뛸 수 있지만 역전/동일 시각 재할당은 거부한다.
- historySequence는 25초 **정기 슬롯마다** 증가한다. 누락 슬롯을 지운 뒤 남은 기록을 다시 1씩 번호 매기지 않는다.
- 같은 device/sensor/boot/windowIndex 재요청은 원본이 완전히 같으면 200/accepted=0, 다르면 409다.
- 신규 수신은 커밋 이후 202/accepted=1, ACK에 sensorId·bootId·windowIndex·전체 요청 digest·receivedAt을 반환한다.
  202는 접수 성공이지 AI 판정 완료가 아니다. 실패한 요청은 원본 그대로 재시도한다.
- 이상 시 이전 요약이 아직 장치 큐에 있으면 **과거 요약부터 측정 순서대로 POST하고 ACK를 받은 후** 이벤트를 보낸다.
  권장 운영은 25초마다 미리 저장하는 방식이다. 뒤늦게 온 history로 이미 끝난 판정을 덮어쓰지 않는다.
- 품질 불량·순번/시각 누락·부팅 변경 시 정상 대신 unavailable과 구체적 사유를 저장한다. 이후 유효한 24건이 확보되면 새 입력부터 재개한다.

## 점수·사건 해석

45개 입력 = 현재9 + 과거평균9 + 과거표준편차9(ddof=0) + 현재−직전9 + 현재−과거평균9.
학습 중심·스케일·활성 차원을 고정하여 robust 최대 편차와 PCA 잔차 평균제곱을 계산한다.
PCA 입력만 -20~20으로 제한한다. 원본 특징이나 robust 점수를 이 범위로 자르지 않는다.

`score = max(robust / robustThreshold, pcaResidual / pcaThreshold)`이며 **score > 1**이면 기준 초과다.
1과 같으면 미초과다. 0~1로 제한되는 확률이 아니다.

| 모델 내부 stream | robust 기준 | PCA 기준 |
| --- | ---: | ---: |
| freshwater_supply_motor2 | 4.248328226474374 | 0.7866649060294624 |
| freshwater_supply_motor3 | 8.012247840041935 | 0.6871139817485201 |

서버는 모델 해시·전처리 버전·센서 배정·과거 24건 ordinal/digest·원점수·기준을 결과 근거에 보존한다.
파일의 평가 결과는 라벨이 없고 groundTruthMetrics가 null이다. 검증 화면에도 고장 확률·확정 정상으로 표시하지 않는다.

`SNAPSHOT_EVENT_MODE=events`는 사건 기록만, `shadow`는 비교만, `alerts`는 추가 운영 정책을 통과한 알림을 허용한다.
기준 초과 1건으로 후보 사건을 만들고 같은 모델의 기준 미초과로 해제한다. 해제는 장비 정상 보장이 아니다.
이력 부족·품질 불량·수신 중단은 사건을 자동 정상 종료하지 않고 관측 불명으로 남긴다.
초기 `verificationLabel=unknown`을 별도 기록한다. 기존 운영 검수와 별개로 요청한
`confirmed_anomaly/transient_false_alarm/sensor_issue/unknown` 전용 라벨 편집·학습 반영은 후속 작업이다.

## EC2 설정·배포

먼저 이 변경을 커밋·푸시·병합한 뒤 서버를 업데이트한다. 운영 적용 전에 백업하며 기존 토큰·계정 설정은 건드리지 않는다.
모델 파일은 저장소에 들어 있으므로 새 업로드 SSH 키가 필요하지 않다.

아래 7개 설정을 **모두 명시**해야 한다. 모두 없으면 waiting_model, 일부만 있거나 해시/stream이 틀리면 기동을 거부한다.
예시는 `/etc/systemd/system/motordiagnosis.service.d/pump-event-verifier.conf`에 넣을 설정의 형태다.
REQUIRED_SELECTION 두 항목은 담당자 확인 후 바꾼다. 임의로 센서2를 선택하지 않는다.

```ini
[Service]
Environment="PUMP_EVENT_VERIFIER_ARTIFACT=/home/ubuntu/MotorDiagnosis/models/pump-event-verifier/event-verifier-model.json"
Environment="PUMP_EVENT_VERIFIER_CHECKSUM=0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135"
Environment="PUMP_EVENT_VERIFIER_STREAM=REQUIRED_SELECTION"
Environment="PUMP_EVENT_VERIFIER_DEVICE_ID=DEV-01-MOT-02"
Environment="PUMP_EVENT_VERIFIER_SITE_ID=SITE-01"
Environment="PUMP_EVENT_VERIFIER_ASSET_ID=SITE-01-MOT-02"
Environment="PUMP_EVENT_VERIFIER_SENSOR_ID=REQUIRED_SELECTION"
Environment="SNAPSHOT_EVENT_MODE=events"
```

모델 stream 허용값은 위 표의 두 문자열이며 sensorId는 실제 전송 ID와 일치해야 한다.
로더는 JSON·Python 표준 라이브러리만 사용한다. pickle/joblib 역직렬화나 NumPy 재설치가 필요하지 않다.

업데이트 후 다음 검증을 먼저 실행한다.

```bash
.venv/bin/python -m unittest tests.test_pump_event_model tests.test_snapshot_inference tests.test_snapshot_events tests.test_operations -q
```

설정 완료 후 daemon-reload·restart, 서비스 active와 /api/health를 확인한다.
인증한 GET `/api/devices/DEV-01-MOT-02/periodic-snapshots`에서 configuredModel의 contractId,
scope.sensorId, 모델 해시, inputContract.historyRows=24/historyIntervalSec=25를 확인한다.
웹 운영 현황의 새 단건 패널은 센서·모델·기준 초과/미초과·이력 확보 수를 표시한다.
GPT Sites의 별도 정적 배포본은 서버 코드 업데이트만으로 바뀌지 않으므로 별도 화면 배포가 필요하다.

`operations inspect --device DEV-01-MOT-02`의 새 DB rows·latest.sensorId·historySequence·receivedAtEpoch·reason을 비교한다.
최초에는 VERIFIER_REQUIRES_24_PRIOR_RECORDS가 예상되며, 이후 completed가 나타나는지 확인한다.
inspect의 디스크 결과는 저장 정보이지 현재 프로세스 모델 로딩을 직접 확인하는 API가 아니다.

## 저장·백업·복구 주의

periodic-snapshots.sqlite3는 스키마 4로 원자적 마이그레이션한다. 센서가 없던 이전 자료는 빈 sensor 영역에 유지한다.
기존 ordinal·원본·digest·수신 시각·작업·결과는 보존한다. 신규 고유키는 device/sensor/boot/windowIndex다.
코드만 PR46으로 되돌리는 다운그레이드는 지원하지 않으며 롤백 시 마이그레이션 전 백업을 사용한다.
새 JSON 모델과 선택 stream·장치·센서·해시 설정은 운영 백업/복구 검사 대상에 포함된다.
수신 용량 한도 100,000건은 유지한다. 가득 차면 재시도 가능한 503이며 원본 자동 삭제는 하지 않는다.
센서 1개의 25초 기록만으로 하루 3,456건이므로 추가 이벤트·센서 수를 포함해 저장/보존 기간을 운영 점검한다.
