# 실측 원시 파형 → 전달 모델 입력

## 구현 범위와 현재 장비 제한

단일 진동 채널의 float32 원시 파형을 검증하고, 기존 AI1 2주차 `extract_all_features()`를 그대로 호출해 26개 특징을 계산한다. 특징 순서는 선택한 `.pt`의 `feature_names`를 따르며 정규화·가중치·임계값은 기존 체크포인트 실행기가 처리한다. 보고서를 모델 계약으로 사용하지 않는다.

**원시 파형 → 영속 접수 → 별도 작업자 특징 계산 → Dense/LSTM 비교 추론 → 저장·장치/이벤트 화면 조회**를 연결한다. 기존 준비된 특징 API도 유지한다.

다만 `.pt`에는 취득 샘플링률·단위·채널·윈도우 크기·MFCC 계산 설정이 없으므로 이 값은 자동 복원하지 않는다. 서버 운영자가 별도 프로필을 명시해야 하며, 설정했다고 원본 학습 전처리와 동일하거나 현장 정확도가 검증됐다는 뜻은 아니다. `trainingCompatibility:unverified`, `domainValidated:false`, `affectsAlerts:false`를 유지한다.

현재 PR29 펌웨어의 진동 채널은 **800Hz/512표본**이다. 이 26개 특징에 필요한 여러 주파수 대역을 측정하지 못하므로 이 변환기에 입력할 수 없다. 음향 채널은 진동으로 재분류하지 않는다. 파형 업샘플링, 24개 요약 특징 치환, 누락 특징 0 채움, 불완전 구간 이어붙이기/패딩은 하지 않는다.

현재 센서로 운영하려면 센서 수집 조건에 맞는 모델을 별도로 준비하거나, 승인된 호환 수집 장치·연속 취득 경로가 필요하다. 이번 변경은 펌웨어·RPM 취득·운영 알림을 수정하지 않는다.

## 명시적 전처리 프로필

모든 필드가 필수다. 아래는 **프로필 작성 형식 예시일 뿐, 전달 모델의 학습 설정을 복원한 값이나 배포 권장값이 아니다.** 실제 학습 설정/측정 단위를 확인한 뒤 채운다.

```json
{
  "schemaVersion": 1,
  "extractor": "ai1-week2-26-v1",
  "modelVersion": "sha256:8236289657a667147ca8cd01cf7b1610c417a0f55606836a74e0e2d66bf2cecf",
  "sampleRateHz": 12000,
  "windowSamples": 4096,
  "frameLength": 2048,
  "hopLength": 512,
  "nMfcc": 13,
  "bandEdgesHz": [0, 500, 1000, 2000, 4000, 8000],
  "signalType": "vibration",
  "channel": "vibrationX",
  "unit": "g"
}
```

- 프로필의 모델 SHA와 실제 체크포인트가 일치해야 한다. 지원 특징 26개의 집합도 정확히 같아야 한다.
- RMS 평균/표준편차, ZCR, 첨도 평균/표준편차, 스펙트럴 중심/대역폭/롤오프, 5개 대역 에너지, MFCC 13개를 계산한다. 27번째 `vibration_peak_hz`는 추가하지 않는다.
- MFCC는 기존 AI1 함수의 librosa 호출과 기본값을 그대로 쓴다. 내부 STFT 경계 처리도 기존 함수와 같다. 함수가 없는 값을 추정하거나 별도 근사식을 사용하지 않는다.
- 각 주파수 대역에 실제 FFT 표본이 있어야 하며, 빈 mel 필터가 있는 설정은 거부한다. 이 검사 통과는 샘플링/학습 호환성 인증이 아니다.
- 샘플링률은 8,000Hz 초과~192,000Hz, 파형은 최대 65,536표본이며 프레임 이상 길이여야 한다. 프레임은 64~8,192의 2의 거듭제곱, hop은 1~프레임 길이다.
- 한 구간의 STFT 배열은 최대 2,000,000개 셀로 제한한다. 긴 구간에 과도하게 작은 hop을 지정하는 설정은 시작 시 거부한다.
- 단위 변환·증폭률 보정·축 합성·다운/업샘플링·DC 제거를 암묵적으로 수행하지 않는다. 측정 장치가 보내는 단위와 프로필이 정확히 같아야 한다.
- NumPy, SciPy, librosa가 필요하며 누락 시 전처리 활성화를 거부한다. MFCC를 0으로 대체하지 않는다.

`preprocessingId`는 **모델 SHA를 포함한 전체 프로필 + 특징 계산/어댑터 소스 SHA + NumPy/SciPy/librosa 버전**으로 만든 SHA-256이다. 설정/코드/의존성 버전이 바뀌면 ID도 바뀐다. 해시는 동일 내용의 식별값이지 센서 진위나 현장 승인 서명이 아니다.

## 로컬 실행

```text
python -m ai.ai2.raw_preprocessing --artifact "<전달 ZIP 또는 .pt>" --profile profile.json
python -m ai.ai2.raw_preprocessing --artifact "<전달 ZIP>" --profile profile.json --input raw-windows.json --output result.json
```

첫 명령은 프로필/전처리 ID/모델 계약을 출력한다. 선택 모델은 기본 LSTM, `--candidate dense_autoencoder`로 Dense를 선택할 수 있다. `--expected-checksum`도 지원한다. 출력 파일은 새로 만들며 기존 파일을 덮어쓰지 않는다.

`raw-windows.json`은 `{"windows":[아래 원시 입력 객체들]}`이다. Dense는 1개, LSTM은 정확히 5개를 입력한다. 출력 `windows`는 준비된 특징 API에 보낼 수 있는 객체들이며 `result`는 비교 추론 결과다. 파일 단위 실행은 과거 데이터 비교도 허용한다. 서버 접수는 별도 보관기간 제한을 적용한다.

이미 다운로드한 PR29 파형의 체크섬·단일 진동 채널 검증도 지원한다.

```text
python -m ai.ai2.raw_preprocessing --artifact "<전달 ZIP>" --profile profile.json --capture analysis.json --quality valid
```

`--quality` 기본값은 `unknown`이므로 센서 유효 상태를 알 수 없는 입력은 추론하지 않는다. 체크섬을 먼저 검사한 후 원본 샘플링률·단위·길이를 검사한다. **현재 800Hz 다운로드는 호환성 오류로 종료하는 것이 정상이다.** 음향을 진동으로 바꾸지 않으며 서로 다른 요청의 파형으로 연속 5구간을 만들지 않는다. 단일 파형이 유효하더라도 LSTM은 `warming_up`이며 추론 결과를 만들지 않는다.

## 서버 접수

기존 `SHADOW_MODEL_ARTIFACT`, `SHADOW_MODEL_CHECKSUM` 등에 더해 `SHADOW_MODEL_PREPROCESSING_PROFILE`에 로컬 프로필 경로를 명시한다. 모델 없이 프로필만 설정하거나 프로필이 잘못되면 서버 시작을 거부한다. 기본 서버는 ML 전처리 의존성을 불러오지 않는다.

`POST /api/devices/{deviceId}/model-raw-inputs`는 기존 장치 ingest 인증과 현재 활성 사이트·설비 매핑을 확인한다. HTTP로 프로필·임의 코드를 업로드할 수 없다.

정확한 입력 필드:

```text
schemaVersion: 1
modelVersion: 선택된 모델 SHA
preprocessingId: 서버/CLI가 출력한 전처리 SHA
deviceId / siteId / assetId: 현재 장치 매핑
sourceId: 한 채널의 연속 측정 세션 ID (재부팅·취득 중단·설정 변경 시 새 ID)
signalType: vibration
channel / unit / sampleRateHz: 명시한 프로필과 정확히 동일
quality: valid (측정자의 선언이며 값만으로 센서 상태를 추정하지 않음)
windowIndex: 동일 source에서 0부터 증가
windowStartSample / windowEndSample: 원본 기준 [start,end) 표본 경계
timestamp: 해당 구간의 첫 표본 RFC3339 취득 시각 (Z 또는 명시적 UTC 오프셋, 소수초 1~6자리), 밀리초 이상 정밀도 권장
samplesFloat32LE: 정확히 windowSamples개의 float32 little-endian 표본, 표준 base64
```

형식·값·길이·범위를 검증하고 DB commit 후 202를 반환한다. **202는 접수 완료이지 변환/추론 성공이 아니다.** 동일 source/index의 동일 본문 재시도는 200, 다른 내용은 409다. NaN/Infinity, 알려지지 않은 품질, 바뀐 채널/단위/샘플링, 불완전한 표본, 미설정/다른 전처리 ID는 거부한다.

원시 입력은 최대 512KiB/65,536표본이다. 이 중 원시 기록은 최대 128개로 제한해 약 45MiB의 표본 본문 규모를 제한한다. 기존 전체 기록 10,000개, 큐 1,000개, 원본 watermark 10,000개 제한도 적용한다. 용량 초과는 503이며 기록을 강제 축출하지 않는다. 원시/준비된 특징을 같은 source에 섞을 수 없다.

원시 본문과 계산된 특징을 별도 보존한다. 계산된 특징/결과는 같은 트랜잭션으로 저장하고, 쓰기 실패 시 원시 대기 입력을 유지해 다시 처리한다. 일반 상태 저장소·HTTP 요청 수용 루프의 잠금을 잡고 특징을 계산하지 않는다.

LSTM은 `[0..4]`, `[5..9]`처럼 비중첩 5구간이며 원본·프로필·모델·단위/채널·표본 경계가 이어져야 한다. 원시 취득 시각은 표본 간격과 비교해 최대 **2ms 또는 2표본 시간** 오차를 허용한다. 네트워크 도착 시각이 아니라 취득 시각을 사용한다. 중간 변환 실패, 구간 누락, 겹침, 시간 불연속은 판정 불가다.

재시작해도 부분 시퀀스/특징/결과가 복구된다. 모델 또는 전처리가 바뀌었거나 비활성화된 경우 이전 대기 파형을 새 조건으로 계산하지 않고 `MODEL_CHANGED`, `PREPROCESSOR_CHANGED`, `PREPROCESSOR_NOT_CONFIGURED`로 기록한다. 계산 실패는 `PREPROCESSING_FAILED`, verdict null이다.

보관기간은 취득 시각 기준 7일이며 파형·특징·결과를 함께 삭제한다. 장치 이력/원본 watermark는 남겨 삭제·ID 재사용 및 과거 순번 재접수를 막는다. 이전 prepared-only DB에는 컬럼/인덱스를 추가하며 기존 입력/결과를 삭제하거나 다시 계산하지 않는다.

모델을 비활성화해도 같은 모델 DB 경로를 유지하면 별도 읽기 전용 이력 보호가 장치 삭제·ID 재등록을 차단한다. 이 보호는 모델 파일이나 전처리 의존성을 필요로 하지 않으며 대기 파형을 계산하지 않는다. 자세한 동작은 [모델 비활성화와 장치 이력 보호](model-shadow-inference.md#모델-비활성화와-장치-이력-보호)를 참고한다.

구형 서버로의 다운그레이드는 새 원시 큐 처리를 지원하지 않는다. 전처리 변경·업그레이드 시 프로필 ID를 취득 측에도 반영하고 새 source ID를 사용해야 한다. 운영 모델 교체/롤백 정책을 대신하는 기능은 아니다.

## 조회 및 검증

기존 `GET /api/devices/{deviceId}/model-inference`와 장치·이벤트 분석 영역에 원시/준비된 특징 구분, 전처리 ID·설정, 결과별 모델/전처리 버전·상태를 표시한다. 원시 입력 전체는 조회 응답에 노출하지 않는다. 사용자 권한과 설비/이벤트 시간 범위는 기존 계약을 따른다.

자동 등록/사람 승인 상태, telemetry 판정, 운영 이벤트·알림은 변경하지 않는다. UI도 전처리 설정됨을 **학습 호환성 미검증**으로 표시하며 실패를 정상으로 표시하지 않는다.

회귀 검사:

```text
python -m unittest tests.test_raw_model_input tests.test_model_inference -q
node tests/test_ai2_dashboard.mjs
```

`AI2_MODEL_BUNDLE_FIXTURE`에 원본 전달 ZIP 경로를 지정하면 실제 LSTM의 원시 5구간 HTTP 접수→특징 계산→추론→조회 연계 검사도 수행한다. 모델 바이너리는 저장소에 추가하지 않는다.

검증은 합성 원시 파형, 기존 AI1 함수와의 수치 일치, 경계/실패/재시작 및 로컬 HTTP 검사다. 실제 센서 파형 취득, 원본 학습 예제 기대값 재현, 보드 시험, 현장 정확도 또는 운영 TLS 검증을 의미하지 않는다.

### 실행 검증 (2026-09-07)

- 전체 백엔드 417개 중 415개 통과, 선택 Paho 의존성 2개 제외. 아래 전용 검사 포함.
- 원시 변환 전용 21개 및 기존 모델 연동 16개: 37개 모두 통과. 원본 전달 ZIP 사용.
- 실제 전달 Dense/LSTM의 합성 원시 파형 추론 통과. 실제 LSTM은 64KiB를 넘는 파형 본문과 5구간 HTTP 접수·작업자·조회 연계도 확인.
- 대시보드 88개, AI2 이벤트/스코어 54개 통과.
- AI1 4주차 272개 중 250개 통과, 외부 데이터/fixture 관련 22개 제외.
- Black, Python/JavaScript 문법, diff 검사 통과. 기본 서버가 NumPy/PyTorch/librosa를 import하지 않고 시작·종료하는 것도 확인.
