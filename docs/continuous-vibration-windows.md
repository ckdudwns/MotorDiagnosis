# 640ms 연속 진동 구간 수집·분석

> 2026-09-08 확장: N8 기본 빌드는 현재 원시 XYZ 전송 모드다. 아래는 `RAW_VIBRATION_ENABLED=0`인
> 기본 21개 특징 경로 설명이다. 원시 모드의 API·저장·8구간 큐·제한은 [원시 전송 명세](raw-vibration-windows.md)를 따른다.

## 이번 구현의 범위

- ADXL345의 800Hz XYZ 샘플을 FIFO로 연속 읽고, 겹치지 않는 512샘플 단위로 처리한다.
- SPI 수집, 특징/음향 계산, HTTPS 전송은 별도 작업이다. 기존 요약 전송 간격은 이 수집 간격을 바꾸지 않는다.
- 각 구간의 기본 특징 21개를 계산하며, 서버는 각 구간을 독립적으로 검증·저장·모델 입력으로 변환한다.
- 모델 미설정은 `waiting_model`, 불량 구간/추론 실패는 `unavailable`이다. 정상으로 간주하거나 0으로 채우지 않는다.
- 호환되는 단일 구간 dense 체크포인트를 명시한 경우에만 실제 비교 추론한다. 알림·기존 이상 점수는 변경하지 않는다.
- **재학습 중인 최종 모델을 선택하거나 현장 성능을 검증한 작업은 아니다.** 기존 CWRU 26개 입력 LSTM과 새 입력을 혼용하지 않는다.

## 특징 계약

프로필: `mcc5-vibration-800hz-xyz-v1`. 입력은 XYZ, `g`, 800Hz, 축별 512개.

각 축에서 구간 평균을 제거한 뒤 다음 7개를 계산한다. X 7개 → Y 7개 → Z 7개 순서다.

1. `rms_ac_g`
2. `peak_ac_g`
3. `kurtosis_pearson`
4. `power_0_50_g2`
5. `power_50_100_g2`
6. `power_100_200_g2`
7. `power_200_350_g2`

`w[n] = 0.5 - 0.5 cos(2πn/512)`인 periodic Hann을 사용한다. 단측 파워는
`abs(FFT((x-mean(x))*w))² / (512 * sum(w²))`이며, DC를 제외하고 Nyquist를 제외한 양의 주파수에 2를 곱한다.
대역은 하한 포함·상한 제외다. Pearson 첨도는 중심 4차 모멘트 / 분산²다.
분산 ≤ 1e-16인 축은 불량이다. 펌웨어는 13비트 레일(-4096/4095)을 보수적으로 포화 처리한다.
ADXL345 원시 카운트에 `0.0039 g/LSB`를 적용하며 개별 센서 교정·축 부착 오차 검증은 별도다.

서버의 `ratios36`은 원래 21개 뒤에 축별로 4개 대역 파워 / 그 4개 합, peak/RMS를 추가한다.
`log_ratios36`은 RMS/peak에 `log1p(value/0.01)`, 파워에 `log1p(value/0.0001)`를 적용하고 같은 비율 15개를 덧붙인다.
추가 강화 실험에서 정의가 달라지면 기존 프로필을 조용히 수정하지 말고 새 버전을 사용한다.
실행 코드가 연구용 `ai/ai1/mcc5_training` 패키지를 import하지 않으므로 학습용 의존성은 서버 수집에 필요 없다.

## API

`POST /api/devices/{deviceId}/vibration-windows`

기존 장치 전용 수집 토큰을 사용한다. body는 `{"windows": [구간, ...]}`이며 1~16개, 요청당 최대 64KiB다.
구간 객체는 다음 필드만 허용한다.

| 필드 | 값 |
|---|---|
| schemaVersion | 정수 1 |
| deviceId / siteId / assetId | 현재 활성 매핑과 일치 |
| bootId | 부팅별 고유 32자리 소문자 hex |
| windowIndex | 부팅 내 0부터 증가하는 순번, 누락 시에도 재번호 부여 금지 |
| timestamp | 구간 시작 UTC(RFC3339, 소수 초 지원) |
| startUptimeUs | 구간 시작 단조 시계(µs) |
| sampleRateHz / sampleCount | 800 / 512(불량 구간은 실제 0~512) |
| profileId | mcc5-vibration-800hz-xyz-v1 |
| axes / unit | `["X","Y","Z"]` / `g` |
| quality | valid / fifo_overrun / sensor_unavailable / sample_gap / clipped / constant_axis / processing_overflow |
| features | 순서가 고정된 21개 숫자 배열, 불량이면 null |

성공은 신규 포함 시 202, 전체 중복은 200이다. `acknowledged`는 각 `bootId`, `windowIndex`, 서버 정규화 본문의 SHA-256 digest를 반환한다.
같은 식별자에 다른 내용은 409, 역순/이미 보관 종료된 식별자는 409다. 배치 전체는 한 트랜잭션이며 부분 ACK하지 않는다.
장치 권한·매핑 검사는 전체 배치에 적용한다. 잘못된 스키마·단위·특징은 400으로 거부한다.
서버는 순번 차이를 `missingWindowsBefore`로 기록한다. 센서 FIFO 손실의 정확한 샘플 수를 추정해서 꾸며내지는 않는다.

`GET /api/devices/{deviceId}/vibration-windows`

운영 계정의 device/telemetry/model 읽기 권한 및 사이트 범위 검사를 적용한다.
최신 수신 순서 100개에 원래 특징, 누락 개수, 변환 입력, 처리 상태·판정 사유를 반환한다.
저장 데이터는 48시간, 전역 최대 500,000개이며 처리 대기 최대 4,096개·스트림 식별자 최대 10,000개다.
용량 초과는 503으로 재시도하도록 한다. 이미 수신했지만 처리 전인 구간을 보관 종료로 삭제하지 않는다.
장치 이력 보호 식별자는 데이터 보관 종료나 모델 비활성화 후에도 유지된다.

## 서버 설정

기본 `app.py`는 `output/vibration-windows.sqlite3`에 모델 없이도 수집·변환 결과를 저장한다.
경로는 `VIBRATION_WINDOW_DB_PATH`로 지정한다. 기존 운영 계정/장치 토큰 설정은 유지한다.

새 호환 모델이 준비된 뒤에만 다음 설정을 지정한다.

```ini
[Service]
Environment=SHADOW_MODEL_ARTIFACT=/absolute/path/to/verified-dense.pt
Environment=SHADOW_MODEL_CANDIDATE=dense_autoencoder
Environment=SHADOW_MODEL_CHECKSUM=sha256:실제_파일_체크섬
Environment=WINDOW_MODEL_VARIANT=ratios36
```

`WINDOW_MODEL_VARIANT`는 `base21`, `ratios36`, `log_ratios36` 중 하나다. 체크포인트 특징 순서와 단일 구간 구조가 정확히 일치해야 한다.
기존 `SHADOW_MODEL_PREPROCESSING_PROFILE`(CWRU 원시 변환 설정)은 새 모델에 재사용하지 않는다.
이번 런타임은 기존 안전한 weights-only dense 로더를 이용한다. 연구용 RandomForest `candidate.joblib`은 직접 로딩하지 않는다.
최종 모델이 RF 등 다른 구조로 확정되면 해당 모델의 안전한 배포 형식/런타임 연결이 추가로 필요하다.
이미 `waiting_model`로 처리한 과거 구간을 재시작만으로 다시 판정하지 않는다. 활성화 후 새 구간부터 판정하며 과거 재평가는 별도 명시적 작업이다.

## 펌웨어·운영 화면

기본 빌드 환경은 `esp32-s3-devkitc-1-n8`(8MB Flash, No PSRAM)이다.
기존 16MB·PSRAM 프로필은 별도 환경으로 남아 있고 연속 모드가 아니다. 실제 보드에는 N8 환경을 사용한다.
핀은 ADXL345 CS10/MOSI11/SCK12/MISO13, INMP441 BCLK4/WS5/SD6 그대로다.
기존 `secrets.h`의 HTTPS 주소·CA·장치 수집 토큰에서 새 endpoint를 구성한다. 토큰을 출력하거나 커밋하지 않는다.

```text
pio run -e esp32-s3-devkitc-1-n8
```

FIFO 접근은 [ADXL345 데이터시트](https://www.analog.com/media/en/technical-documentation/data-sheets/adxl345.pdf)의 6바이트 burst와 최소 5µs 간격을 따른다.
FIFO full은 보수적으로 불량 처리하고 초기화 후 새 구간을 시작한다. FIFO 깊이로 구간 시작 시각을 추정하므로 하드웨어 트리거 수준의 UTC/음향 동기 정확도를 보장하지 않는다.
진동은 640ms마다 처리하며 음향은 16kHz 수집을 유지한다. 음향 FFT는 2048-point float 연산으로 메모리를 절약하며 모델 입력에는 넣지 않는다.
TLS 메모리 피크를 제한하기 위해 기존 요약/상태 요청과 새 요청은 한 연결씩 실행한다.

**연속 모드에서는 기존 `edge-statistics-v1` 원시 파형 요청 수집/전송을 중지한다.** 옛 전용 배열을 새 측정으로 보내지 않는다.
원시 파형을 연속 경로에서 보존하는 별도 설계는 포함하지 않는다. 기존 파일을 삭제하지도 않는다.

백엔드 내장 화면은 `장치 운영 → 장치 선택 → 최근 진동 구간 100건 조회`에서 XYZ RMS와 각 구간의 상태를 보여준다.
원문을 펼치면 21개 특징과 파워·모델 입력을 볼 수 있다. 별도 GPT Sites 프론트엔드는 이 파일과 별개이며 이번에 배포하지 않았다.

## 유실 한계 및 실보드 필수 시험

‘매 구간 분석’은 정상 수집·처리·통신 용량 내에서 모든 구간을 처리한다는 뜻이지 무한 오프라인 무손실 보장이 아니다.
현재 새 특징 큐는 **내부 RAM 64구간(40.96초)**, raw 처리 큐 3구간이다.
넘치면 전송 중인 앞 구간을 덮어쓰지 않고 새 구간을 버리며, 누적 drop 로그와 서버의 순번 공백으로 드러낸다.
ACK를 잃으면 같은 본문을 재전송한다. **전원 손실/재부팅 시 미전송 RAM 특징은 사라진다.** 기존 24시간 요약 플래시 큐와 혼동하지 않는다.
장시간 네트워크 단절·전원 차단에서도 모든 구간 보존이 필수라면 저장 용량·Flash 수명 기준을 합의한 영속 큐 확장이 필요하다.

서버 먼저 배포한 뒤 보드를 올리고 다음을 확인한다. 파티션 변경이 기존 LittleFS 데이터에 미치는 영향은 업로드 전 백업·비교하며 자동 포맷하지 않는다.

1. 10분 이상 실행해 약 937~938개의 정상 연속 구간, 증가 순번, 구간 시작 간격 약 640ms를 확인한다. 샘플레이트 허용 오차는 실측 후 AI 담당자와 확정한다.
2. Wi-Fi/HTTPS 지연 중에도 센서 수집이 계속되는지 확인한다. 정상 운용 중 FIFO·raw 큐·특징 큐 drop이 0이어야 한다.
3. 10초 단절 후 동일 순번 재전송·중복 저장 방지를 확인하고, 큐 용량 초과 시험에서는 누락이 표시되는지 확인한다.
4. 센서 분리·FIFO 과부하·포화·상수축에서 `unavailable`, 모델 미설정에서 `waiting_model`인지 확인한다.
5. 남은 최소 heap, 최대 연속 heap, 태스크 stack 여유를 측정한다. 컴파일 시 정적 RAM 수치는 런타임 여유 검증이 아니다.
6. 학습 코드의 기대 특징/판정과 동일 원시 구간을 대조하고 현장 정상/이상 데이터를 검증한 뒤 알림 반영을 별도로 결정한다.

## 재현 테스트

```text
python -m unittest tests.test_vibration_windows
node tests/test_ai2_dashboard.mjs
pio test -e native
```

`test_vibration_window`는 실제 C++ 추출기와 FIFO 전송 큐의 순서/포화 정책을 검사한다.
그 실행 파일의 `--emit-fixture` 출력을 Python의 독립 DFT 기준 및 서버에 연결하려면 `IOT_WINDOW_FIXTURE_EXE` 환경 변수에 실행 파일 경로를 지정한다.
PyTorch가 있으면 합성 36개 입력 dense 체크포인트로 실제 계산 연결도 검사한다. 합성 시험은 실제 모델 정확도 검증이 아니다.

### 이번 로컬 검증 기록

- N8 연속 프로필 및 기존 프로필 모두 빌드 성공. N8 정적 RAM 185,560 bytes / 327,680 bytes(56.6%).
- C++ 네이티브 134개 통과(Windows MSVC로 동일 Unity 소스를 컴파일). PlatformIO native는 이 PC에 GCC가 없어 MSVC로 대체 검증했다.
- 화면 동작 91개 통과. Week4 화면 검사도 통과.
- 전체 백엔드 회귀 실행 시점에 521개 실행, 94개 선택 의존성/fixture 검사 제외, 실패 없음.
- 이후 새 구간 연동 및 기존 모델을 실제 PyTorch 환경에서 29개 실행, 2개 선택 검사 제외, 실패 없음. C++ 출력→독립 DFT→서버 수신 검사 포함.
- 실제 보드 연속 가동·네트워크 단절·heap/stack 여유 시험은 아직 하지 않았다. 운영 배포/보드 업로드/커밋/푸시는 수행하지 않았다.
