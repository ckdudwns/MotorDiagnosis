# AI-2 4주차 — 합성 이상 시연·모델 결과 표시

AI-2의 4주차 범위는 `SIM_ANOMALY_01`의 최종 시연과 AI-1 모델 결과를 관제 화면에
안전하게 표시하는 일이다. 모델 학습·승인·배포는 이 범위가 아니다.

## 구현 내용

- `POST /api/demo/inject-anomaly`은 기존의 권한 검사와 환경 차단을 그대로 사용한다.
  `DEMO_ENABLED=true`에서만 열리고, `APP_ENV=production`에서는 항상 차단된다.
- 주입 시 실측 장치·멱등성 키·health와 분리된 `DEMO-<assetId>` 식별자와 전용 데모 저장소에
  정상 1건 뒤 진동·음향·RPM이 함께 변한 raw telemetry를 생성한다. 모든 행은
  `isSynthetic: true`, `source: ai2-week4-combined-signal-demo`으로 표시되며 이벤트와 같은
  종료 시각을 사용한다.
- 긴 규칙 지속시간은 최대 120개 이상 샘플(+정상 기준점)으로 균등 다운샘플링한다. 따라서
  데모가 설비별 실측 1,000건 보존 상한을 소모하거나 기존 실측 데이터를 삭제하지 않는다.
- raw-only 계약을 지킨다. `vibrationRmsMmS`, `acousticDb`는 항상 `null`이고, 생성값은
  물리 보정값이 아닌 데모용 raw RMS다.
- 데모 라벨은 전용 데모 저장소에만 기록한다. 일반 ESP/MQTT 수집 토큰은 계속 nullable
  라벨 계약을 사용하며, 데모가 실측 수집 권한·멱등성 경로를 우회해 사용하지 않는다.
- 대시보드 차트만 실측·데모 telemetry를 함께 표시한다. 실제 이벤트의 상세 증거는 해당
  `deviceId`의 실측 저장소만 사용하며, 실측 point가 없으면 합성 signal을 대체 증거로 쓰지
  않고 `unavailable`/missing으로 남긴다.
- 대시보드는 선택한 사이트·설비에 등록된 `GET /api/model-versions` 메타데이터를 표시한다.
  문자열은 DOM `textContent`로만 넣어 등록 메타데이터가 HTML로 실행되지 않는다.

## AI-1 4주차 인계와의 경계

AI-1 인계의 모델 산출물은 저장소에 포함되지 않았고, reconstruction error/binary verdict를
기존 운영 `anomalyScore`(0–100)로 바꾸는 규칙도 합의되지 않았다. 따라서 이 구현은
AI-1 모델을 실행하거나 점수를 임의 변환하지 않는다. 모델·기준선·지표가 시스템 관리자에
의해 등록되면 화면이 그 메타데이터를 표시하고, 등록 전에는 미등록 상태를 명확히 보인다.

AI-1의 `operating_condition_holdout` F1=1.0은 운전조건 데모 결과일 뿐이다. CWRU NORMAL
베어링 specimen이 하나여서 specimen-independent 일반화 검증은 불가능했다. 현장 적용 전에는
대상 모터 정상 데이터 수집, 센서/설치 조건 확인, 기준선 및 임계값 보정, 독립 설비 검증이
필요하다.

## 확인 명령

```bash
.venv/bin/python -m unittest tests.test_week4_backend -v
node tests/test_week4_dashboard.mjs
.venv/bin/python -m compileall -q motor_diagnosis ai/ai2/week4 tests
.venv/bin/python -m black --check motor_diagnosis/demo_signals.py motor_diagnosis/data.py motor_diagnosis/web.py tests/test_week4_backend.py
git diff --check
```

원본 신호·학습 모델·대용량 데이터셋은 저장소에 넣지 않는다.
