# 전달된 .pt 모델 기준의 비교 추론

## 이번 구현 범위

전달된 `dense_autoencoder.pt`, `lstm_autoencoder.pt` **체크포인트 자체**를 기준으로 CPU 추론한다.
`training_job_report.json`을 읽어서 모델 종류·특징·정규화·임계값을 정하지 않는다.
모델 파일에 저장된 `model_type`, `feature_names`, `input_dim`, `seq_len`, `scaler_mean`, `scaler_std`, `threshold`, `state_dict`를 검증하고 복원한다.
ZIP은 선택한 `.pt` 멤버만 크기 제한을 두고 메모리로 읽는다. 압축 해제·보고서의 URI 다운로드·임의 pickle 객체 실행은 하지 않는다.
`weights_only=True`, CPU 로딩, 동일 바이트의 SHA-256 검사와 텐서 shape/유한값 검사를 사용한다.

확인한 전달본의 계약:

| 모델 | 입력 | 임계값 | 모델 버전(바이트 SHA-256) |
|---|---|---|---|
| Dense | 배치 × 26개 특징 | 35218.21875 | `sha256:3a104962c0534847573fb1ff9b8c1b84a5484ac6e92e31ed4602607e939b9b48` |
| LSTM | 배치 × 연속 5구간 × 26개 특징 | 26439.5234375 | `sha256:8236289657a667147ca8cd01cf7b1610c417a0f55606836a74e0e2d66bf2cecf` |

SHA-256은 파일 식별/무결성 값이지 신뢰 서명이나 사람의 운영 승인이 아니다.
기본 선택은 LSTM이며 Dense도 동일 코드로 PC 시험할 수 있다. 모델 파일은 Git에 복사하지 않는다.

**원시 신호→26개 특징 변환은 별도의 명시적 전처리 프로필로 연결할 수 있다.** 사용법은 [원시 파형 변환](raw-model-input.md)을 참고한다. 체크포인트에는 원시 센서 채널·샘플링률·윈도우 크기·MFCC 등의 정확한 계산 설정/코드가 없다.
특징 이름만으로 그 설정을 복원했다고 주장하지 않는다. 현재 장비의 24개 통계 특징이나 요청 파형은 이 모델 입력이 아니며, 이름 치환·0 채움·강제 업샘플링하지 않는다.
기본은 **준비된 특징의 실제 추론 + 저장·재시작·조회·화면 연결**이며, 전처리를 설정하면 원시 파형을 서버 작업자에서 특징으로 변환한다. 학습 전처리와의 호환성이 확인됐다는 의미는 아니다.

## PC 실행

기존 AI1의 NumPy/PyTorch CPU 환경을 사용한다. 이번 검증 환경은 Python 3.13, PyTorch 2.14.0+cpu, NumPy 2.5.2였다.
백엔드의 기존 기능은 PyTorch 없이도 실행되며 모델 설정을 명시한 경우에만 불러온다. 필요한 의존성이 없거나 모델이 손상됐으면 구성된 모델의 시작을 거부한다.

저장소 루트에서:

```text
python -m ai.ai2.model_runtime --artifact "<전달 ZIP 또는 .pt 경로>"
python -m ai.ai2.model_runtime --artifact "<전달 ZIP 경로>" --candidate dense_autoencoder --smoke
python -m ai.ai2.model_runtime --artifact "<전달 ZIP 경로>" --candidate lstm_autoencoder --input prepared-input.json --output result.json
```

`--expected-checksum sha256:<해시>`로 별도로 확인한 파일을 고정할 수 있다.
`--smoke`는 저장된 평균 벡터를 사용하는 **합성 실행 점검**일 뿐 정상 설비 재현/정확도 검증이 아니다.
출력은 항상 `domainValidated:false`, `affectsAlerts:false`다. `--output`은 기존 파일을 덮어쓰지 않는다.
`prepared-input.json`은 `featureNames`와 `matrix`를 가진다. Dense는 2차원, LSTM은 정확히 3차원/5구간이다.
열 이름의 순서만 다르면 체크포인트 순서로 정렬한다. 누락·중복·추가 특징, 문자열·bool·null·NaN/Infinity, 잘못된 차원/길이는 거부한다.
PC 행렬 실행은 입력이 실제 시간상 연속인지 증명하지 않는다. 아래 서버 경로는 별도의 연속성 정보를 요구한다.

## 서버 설정과 입력

다음 환경변수는 서버 운영자가 직접 설정한다. HTTP로 임의 모델 경로나 가중치를 올릴 수 없다.

- `SHADOW_MODEL_ARTIFACT`: 로컬 ZIP 또는 .pt 경로. 생략 시 비교 추론 비활성.
- `SHADOW_MODEL_CANDIDATE`: 기본 `lstm_autoencoder`, 또는 `dense_autoencoder`.
- `SHADOW_MODEL_CHECKSUM`: 선택한 파일의 SHA-256 고정값(권장).
- `SHADOW_MODEL_DB_PATH`: 기본 `output/model-inference.sqlite3`, 기존 일반 상태/분석 DB와 분리.
- `SHADOW_MODEL_PREPROCESSING_PROFILE`: 선택한 모델에 바인딩한 원시 전처리 JSON 경로. 미설정이면 기존 준비된 특징 입력만 허용.

레지스트리에서 후보를 승인해도 이 설정이나 운영 모델은 자동으로 바뀌지 않는다.
현재 기능은 명시적으로 선택한 후보의 비교 시험이며 모델 레지스트리 `deploymentStatus`, 운영 anomalyScore, 이벤트, 알림은 변경하지 않는다.

전달된 LSTM의 로컬 실행 예시(PowerShell, 저장소 루트):

```powershell
$env:SHADOW_MODEL_ARTIFACT = '<전달 ZIP 경로>'
$env:SHADOW_MODEL_CANDIDATE = 'lstm_autoencoder'
$env:SHADOW_MODEL_CHECKSUM = 'sha256:8236289657a667147ca8cd01cf7b1610c417a0f55606836a74e0e2d66bf2cecf'
python app.py
```

다른 `.pt`로 교체하면 검증한 파일의 해시도 함께 바꾼다. 운영 배포 명령이 아니라 로컬 비교 서버 실행 예시다.

`POST /api/devices/{deviceId}/model-inputs`는 기존 장치 ingest 인증과 현재 활성 매핑을 검사한다.
준비된 특징 생성기는 다음 **정확한 필드**를 보낸다(특징 개수/이름은 선택한 `.pt`의 계약과 정확히 같아야 한다).

```text
schemaVersion: 1
modelVersion: 선택한 .pt의 "sha256:<해시>"
deviceId / siteId / assetId: 현재 장치 매핑
sourceId: 연속된 한 취득 세션·채널/원본의 고유 ID (새 세션에는 새 ID)
preprocessingId: 특징 생성 코드·설정의 불변 식별자
sampleRateHz: 실제 샘플링률, 양의 정수 (이 선언만으로 학습 호환성을 인증하지 않음)
windowIndex: 동일 source에서 0부터 증가하는 정수
windowStartSample / windowEndSample: 원본 기준 반열림 [start,end) 표본 경계
timestamp: 취득 시각, timezone을 가진 RFC3339
features: .pt의 feature_names 전체를 키로 갖는 유한 숫자 사전
```

입력 접수는 commit 후 `202 {accepted:true,inputId,status:"queued",mode:"shadow"}`다. **202는 추론 완료가 아니다.**
동일 장치/모델/원본/윈도우의 같은 내용 재시도는 200, 다른 내용은 409다. 보관 만료 후에도 원본별 순번 watermark를 유지해 과거 입력 재사용을 차단한다.
미등록/구형 장치의 다른 설비 입력, 모델 해시 불일치, 뒤로 간 순번, 같은 원본에서 바뀐 전처리/샘플링/창 크기는 거부한다.
LSTM은 `[0..4]`, `[5..9]`처럼 **5개 비중첩 구간**을 처리한다. 부족하면 `warming_up`, 누락·표본 겹침·시각 불연속이면 `not_evaluated`이고 verdict는 null이다.
취득 시각은 표본 간격과 일치해야 하며 기존 초 단위 시각의 오차로 최대 1초(또는 구간 길이의 2%)를 허용한다.
전처리 ID와 샘플링률은 생성자의 선언이지 원본 학습 코드와 일치한다는 독립 검증이 아니다.

추론은 HTTP 요청 수용 루프와 분리된 단일 작업자에서 실행한다. DB 저장 실패 시 대기 입력을 남기며, 재시작 후 연속 구간을 DB에서 재구성한다.
실패/수치 오류는 `INFERENCE_FAILED`와 verdict null로 저장한다. 모델 교체 후 예전 대기 입력에는 새 모델을 적용하지 않는다(`MODEL_CHANGED`).
큐 최대 1,000개, 입력/결과 최대 10,000개, 취득 시각 기준 7일 보관, 입력 본문 최대 16KiB다. 용량 초과는 503이며 기존 입력을 축출하지 않는다.
원본 watermark는 최대 10,000개까지 보존하며 만료로 제거하지 않는다. 그 한도에서는 새 원본 접수를 503으로 거부한다. 장기간 운영 시 원본 식별/보존 확장 정책은 별도다.
모델 입력 이력이 있는 장치는 삭제·ID 재사용을 금지한다.

## 조회와 화면

`GET /api/devices/{deviceId}/model-inference`: 사용자 `device:read`, `telemetry:read`, `model:read` 및 사이트 접근 검사.
현재 매핑의 최근 20개 결과와 모델 계약을 반환한다. 원시 전처리 미설정이면 판정 불가, 설정하면 `configured_unverified`로 표시하고 프로필·버전을 함께 반환한다. 원시 파형 결과는 기존 이벤트/알림에 적용하지 않는다.
장치 운영의 분석 기록과 과거 이벤트의 분석 영역에도 모델 비교 결과를 표시한다. 과거 이벤트는 자기 사이트·설비·장치 및 문맥 시간 범위로 제한한다.
`inferred`만 bool 판정을 표시한다. 임계값 이내는 현장 정상 보장이 아니며 대기/실패/판정 불가는 정상으로 표시하지 않는다.
기존 승인 화면은 그대로 유지한다. 운영 적용/알림 연결은 별도 현장 검증과 명시적 결정이 필요하다.

## 남은 연동 조건

- 학습과 동일한 원시 전처리 코드·설정 또는 현재 센서 조건으로 준비한 모델/특징 생성기
- 실제 입력 예제와 기대 출력에 대한 재현 검증 (합성 평균 입력 점검과 구분)
- IoT의 연속 구간 취득·전달 및 현장 교정/오탐 평가
- 실제 운영 모델 선택·승인에 따른 배포/롤백 정책과 운영 알림 연결

이 조건을 입력 특징의 이름만 보고 추정하거나, 이번 비교 추론의 성공으로 완료 처리하지 않는다.

## 준비된 특징 연동 단계 검증 (2026-09-07)

아래는 원시 변환 추가 전 단계의 결과다. 원시 변환 통합 검증은 [원시 파형 변환](raw-model-input.md)의 검증 결과를 참고한다.

- 백엔드 396개 실행: 394개 통과, 선택 Paho 의존성 2개 제외. 모델 연동 전용 16개 포함.
- 화면 DOM/API 검사 87개 통과. 실제 브라우저 종단 간 검사는 별도다.
- AI2 1~3주차 54개 통과, AI1 4주차 272개 중 250개 통과·22개 데이터/fixture 조건 제외.
- 실제 전달된 Dense/LSTM 파일로 CPU 합성 평균 입력 실행 통과. 보고서에 의존하지 않는 체크포인트 계약 로딩, 기존 AI1 함수와의 수치 일치 검사 통과.
- 실제 전달 LSTM + 로컬 HTTP + 5구간 접수 + 작업자 추론 + DB 결과 조회 연계 통과.
- 누락/중복/잘못된 특징, 순번·표본·시각 불연속, 저장 실패·재시작·모델 변경, 권한·보관/ID 재사용, 판정 불가 표시 검사 통과.
- Black·Python/JavaScript 문법·diff 검사 통과. 기본 모델 비활성 서버의 선택 의존성 없는 시작도 확인.
- 실제 센서 입력·원본 학습 예제 재현·현장 정확도·운영 TLS·보드 시험은 미실행. 펌웨어/RPM 취득은 변경하지 않았다.

모델 파일 없이 회귀 테스트를 실행하면 실제 전달본 검사는 제외될 수 있다. 해당 검사에는 `AI2_MODEL_BUNDLE_FIXTURE`에 원본 ZIP 경로를 설정하고 NumPy/PyTorch CPU 환경을 사용한다.
`python -m unittest tests.test_model_inference -q`와 `node tests/test_ai2_dashboard.mjs`로 관련 검사를 재실행할 수 있다.
