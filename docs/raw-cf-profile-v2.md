# Raw CF + centered SK/KU 입력 규격 v2

이 문서는 서버 지원 규격과 IoT 전달사항입니다. 운영 서버 설정·보드 배포를 실행한 기록은 아닙니다.
학습 엑셀 CF의 원래 계산식은 미확정입니다. 새 프로파일은 **보드 계산식 구분**이며
학습 데이터와의 동등성 인증, 재학습, 예측 차단 해제를 뜻하지 않습니다.

## 1. 식별자와 계산식

| 항목 | 값 |
|---|---|
| `window.profileId` | `adxl345-raw-cf-centered-sk-ku-25s-v2` |
| `window.schemaVersion` | `3` (기존 중첩 요청 구조 유지) |
| `transmission.policyId` | `edge-feature-history-v1` (25초 등간격 정책 유지) |
| 서버 adapterId | `adxl345-raw-cf-centered-moments-v2` |
| 서버 model contractId | `edge-feature-history-model-v2` |
| 서버 선택 설정 | `PUMP_DUAL_INPUT_MODE=experimental-adxl25-raw-cf` |

각 축 X/Y/Z는 각각 a_1/a_2/a_3입니다. 800Hz·512샘플·보정된 g 단위 입력에 대해:

```text
raw = 해당 축의 512개 g 값 (DC/평균을 제거하지 않은 값)
centered = raw - mean(raw)
CF = max(abs(raw)) / sqrt(mean(raw**2))
SK = mean(centered**3) / mean(centered**2)**1.5
KU = mean(centered**4) / mean(centered**2)**2
```

- Peak는 양의 최댓값이 아니라 **절댓값의 최댓값**입니다.
- mean의 분모는 모두 N=512입니다. SK는 population skewness, KU는 population Pearson kurtosis입니다.
  표본 보정·Fisher excess(`-3`)는 적용하지 않습니다.
- 최종 9개 특징값은 무단위입니다. CF에만 평균/중력 제거를 하지 않으며 SK/KU는 중심 모멘트입니다.
- 별도 소프트웨어 필터·클리핑·축 재매핑을 추가하지 않습니다. 변경이 필요하면 별도 프로파일을 협의합니다.
- RMS 또는 centered variance가 0인 경우 `invalid/zero_variance`, 비유한 계산은 `invalid/non_finite`입니다.
  FIFO overrun·샘플 부족·timeout도 invalid이며 `features=null`로 보냅니다. 이전 값으로 메우지 않습니다.
- 서버는 **받은 9개 수치를 그대로** 순서화하여 모델에 전달합니다. CF를 재계산하거나 평균 제거하지 않습니다.
  Raw를 받지 않으므로 수치만으로 실제 계산식 준수를 증명할 수는 없습니다.

모델 입력 계약은 전체에 대한 `meanRemoved` 대신 `featureDefinitions`로 위 네 식을 명시합니다.
기존 v1의 `meanRemoved=true` 계약은 수정하지 않습니다.

## 2. 요청·ACK·시각 규격

기존 [전체 요청 JSON과 ACK 규격](pump-dual-model-serving.md#2-iot-담당자에게-전달할-변경-규격)을 유지하고
**window.profileId만 위 v2로 변경**합니다. 최상위는 `window`와 `transmission`입니다.
계산식/adapterId/inputMode는 요청에 추가하지 않습니다. 서버의 프로파일 정의와 운영 설정입니다.

- endpoint: `POST /api/devices/DEV-01-MOT-02/periodic-snapshots`
- `eventType`: `periodic`, `anomaly_start`, `anomaly_active`, `recovery`
- 정기 슬롯은 `periodicSlotEpoch % 25 == 0`인 Unix epoch 기준입니다. 매분 00·25·50초 반복이 아닙니다.
- 정상/이상 상태 모두 정기 이력을 유지합니다. 즉시·10초 보고는 정기 이력을 대신하지 않습니다.
  정기 슬롯과 상태 전환이 겹치면 슬롯과 historySequence를 가진 한 건으로 병합합니다.
- `timestamp`와 `startUptimeUs`는 선택한 window의 **측정 시작** 시각입니다.
  유효 정기 window는 종료 후 슬롯까지 최대 1초: `slot - 1.640 <= start <= slot - 0.640`초입니다.
- `historySequence`는 정기 슬롯마다 +1(유효하지 않은 슬롯도 포함), 비정기 보고는 null입니다.
- 원시 샘플·Base64는 보내지 않습니다. LittleFS 재시도 파일에는 원래 프로파일과 식별자를 그대로 보존합니다.

해시는 기존 그대로입니다. 다음 순서의 **IEEE-754 float64 little-endian 9개, 총 72바이트**에 SHA-256을 적용합니다.

```text
cf_a_1, cf_a_2, cf_a_3, sk_a_1, sk_a_2, sk_a_3, ku_a_1, ku_a_2, ku_a_3
```

`features=null`이면 빈 바이트열의 SHA-256입니다. JSON 텍스트 해시가 아닙니다.
직렬화 시 숫자를 반올림하여 서버가 읽는 값과 해시 입력 값이 달라지지 않도록 합니다.

신규 저장은 HTTP 202 / accepted=1, 동일 재전송은 HTTP 200 / accepted=0 / duplicate=true입니다.
기존 `acknowledged[]`의 `durablyStored`, 원본 식별자, `featureDigest`를 확인한 뒤 파일을 삭제합니다.
ACK는 저장 성공이지 모델 호환·추론 완료·실제 고장 확정이 아닙니다.

## 3. 전환 순서와 이력 보호

1. 이 서버 코드를 먼저 배포합니다. v1과 v2 수신을 모두 지원하지만 모델 선택은 명시적입니다.
2. 기존 모델 파일·체크섬·stream·장치/센서 배정을 유지하고 **선택 설정만** 아래 값으로 변경합니다.
   `PUMP_DUAL_INPUT_MODE=experimental-adxl25-raw-cf`
   기존 CLI 사전 검사도 `--input-mode experimental-adxl25-raw-cf`로 실행할 수 있습니다.
   사전 검사는 파일·계약 검사이며 운영 설정을 바꾸지 않습니다.
3. systemd의 적용 예정 Environment를 확인하고 재시작합니다. 실행 중 프로세스의 환경을 읽는
   `operations.discover()`만으로 재시작 전 drop-in 변경을 검증하지 않습니다.
4. 펌웨어에서 실제 계산식과 v2 profileId를 함께 적용하고 **새 bootId**로 시작합니다.
   historySequence는 새 부팅에서 0부터 시작합니다. 같은 bootId에서 프로파일을 바꾸면 409입니다.
5. 과거 재시도 파일을 v2로 재라벨링하지 않습니다. v1 파일은 원본 그대로 수신·보존되고,
   v2 모델과 비호환이면 `MODEL_INPUT_CONTRACT_MISMATCH`로 표시됩니다. 추론하지 않습니다.
6. 새 부팅·같은 센서·v2의 유효 연속 정기 이력을 확보합니다. 예측은 현재 포함 13건(약 5분),
   오류 확인은 과거 24건+현재(약 10분)가 필요합니다. invalid·누락·재부팅은 연속성을 끊습니다.

새 모드는 별도 preprocessingVersion/bindingId를 만듭니다. 기존 대기 작업을 새 계약으로 실행하지 않으며
과거 원본·결과를 수정하거나 자동 재분석/알림 재발송하지 않습니다.
되돌릴 때도 v1 보드 계산식·profileId·새 bootId와 기존 `experimental-adxl25` 설정을 함께 맞춥니다.

## 4. 변하지 않는 모델 보호 장치

- 오류/예측 JSON 파일, 계수, 학습 분포와 임계값은 그대로입니다.
- `sourceFeatureEquivalenceVerified=false`, `domainValidated=false` 유지.
- 입력 분포 이탈·출력 범위 초과 시 예측을 차단하고 features=null을 유지합니다.
- 차단 뒤 비정기 보고가 와도 오래된 성공 예측을 최신 예측으로 되살리지 않습니다.
- 예측은 사건·알림에 미사용입니다. 오류 판정의 기존 운영 모드를 자동 변경하지 않습니다.
- 프로파일 분리 후에도 학습 특징과의 차이는 남을 수 있습니다. 원본 계산식 확인과 동일 신호 비교가 별도 필요합니다.

## 5. 로컬 회귀 검사

```bash
python -m unittest tests.test_raw_cf_profile tests.test_pump_dual_models tests.test_pump_forecast_v3 -q
node --test tests/test_raw_cf_dashboard.mjs tests/test_pump_live_dashboard.mjs
```

합성 입력으로 수신/중복 ACK/값 보존/이력 분리/추론·예측/차단/화면 연결을 검사합니다.
실장비 수집 또는 예측 정확도 평가 결과가 아닙니다.
