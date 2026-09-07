# MCC5-THU 재학습 작업 — 800Hz XYZ 단일 구간 후보

사용자 선택 원본: https://data.mendeley.com/datasets/6s3dggj9mw/1  
원본 논문 v2: https://arxiv.org/abs/2601.02278v2  
배포 원본: Mendeley에서 연결된 Samlzy/MCC5-THU-Motor, revision `897bc5d2504980418249f98e733bfd7f61056186`.

## 2026-09-07 사용자 확인 하드웨어 및 실제 전송

- ESP32-S3 + ADXL345(SPI) + INMP441(I2S).
- ESP32-S3-DevKitC-1-N8 / Flash 8MB 프로파일. 실제 칩 PSRAM 8MB 감지 보고가 있지만 현재 PlatformIO는 No PSRAM이고 펌웨어는 이에 의존하지 않는다. PSRAM 사용 가능하다고 가정하지 않는다.
- ADXL345: CS 10, MOSI 11, SCK 12, MISO 13, 3.3V/GND.
- INMP441: BCLK 4, WS 5, SD 6, L/R GND, 3.3V/GND.
- 진동: ±16g full-resolution, 800Hz, XYZ 축별 512표본 / 640ms. 축별 RMS와 합성 RMS, RMS 최대 축의 FFT peak 추출.
- 음향: 16kHz 연속 취득, 동일 640ms / 10,240표본. 2048점 FFT 5개 스펙트럼 평균.
- **현재 최종 전송은 vibrationRmsRaw, vibrationPeakHz, acousticRmsRaw, acousticPeakHz 네 특징. 원시 파형은 RAM 처리 후 폐기.**
- 네트워크 장애 시 특징 패킷을 LittleFS binary ring에 보관하고 복구 후 FIFO 재전송.
- 설치 기준: bearing housing top, bolt-fixed bracket, horizontal X / vertical Z, cooling side 약 1.5m 마이크. 최종 외함 체결 후 실제 축 방향 재확인 필요.
- 진동 mm/s와 음향 dB는 교정 전이므로 사용하지 않는다. 품질 허용오차·조건별 데이터 분량·ML 구조는 미확정.

로컬 저장소의 별도 분석 경로에는 PSRAM 확인 및 요청 원시 창 저장 구현이 존재하지만, 사용자 확인 실제 펌웨어의 가용 기능으로 간주하지 않는다. 실제 펌웨어 파일/버전 확인 전에는 변경하지 않는다.

## 원본 조사 결과

- 공개 카드의 8열 설명과 달리, 실제 정상 CSV 첫 행 및 논문 v2는 **시간 포함 9열**이다.
- 순서: 시간, keyphase, torque, 진동 X/Y/Z, 전류 A/B/C. 헤더가 없는 수치 CSV다.
- 논문 v2는 원본 전압값, 진동 센서 감도 100mV/g라고 명시한다. 따라서 진동 전압을 0.1V/g로 나눠 g로 변환하는 계약을 사용한다. 카드의 `0.1g` 표기만으로 임의 단위 변환하지 않는다.
- ZIP에는 `._*.csv` Apple sidecar가 섞여 있다. 이를 실제 운전 기록으로 읽지 않는다.
- 파일별 표본률·채널·라벨·내용 중복과 ZIP/member 무결성은 전체 준비 단계에서 추가 검증한다.
- Mendeley는 CC BY 4.0, 연결된 배포 카드는 MIT로 표기한다. 원본 저자·DOI·각 출처 이용 조건을 함께 기록하며, 원본 재배포는 하지 않는다.

## 현재 진행 방향

`ai.ai1.mcc5_training`은 운영 서버와 분리된 오프라인 실험 코드다. 결과 생성 여부와 실제 성능은 실행별 `training_report.json`으로 확인한다. 코드가 존재한다는 사실만으로 학습 완료를 뜻하지 않는다.

1. 원본 ZIP 두 개(약 12.7GB)를 pinned SHA256로 검증한다. 중단된 `.part`는 재개 가능하게 보존한다.
2. XYZ 전압을 g로 변환한 뒤 저역통과 FIR + 16배 다운샘플링으로 800Hz 후보 데이터를 만든다.
3. 원시 XYZ 각 창에서 21개 특징을 만드는 후보 프로파일을 준비했다. 이는 기존 전달 모델의 26개 특징이나 현재 펌웨어의 4개 특징과 다르다.
4. 파일/운전 프로파일 단위로 train/validation/test를 분리한다. 같은 기록의 창을 무작위로 섞지 않는다. 같은 시험장치의 운전조건 홀드아웃일 뿐 독립 모터·ADXL345 실측 평가가 아니다.
5. 사용자가 현재 구조에서 더 적합한 방식의 판단을 요청했다. **기존 4개 운영 특징은 유지하고 새 진동 특징을 추가하며, 연속 5창이 필요하지 않은 Dense 오토인코더를 우선 학습하는 방향**을 선택했다. 공개 원시 데이터 사전 학습만 진행하며, 실제 펌웨어 변경은 별도 구현·검증 범위다.

**추천 연결:** 기존 원시 XYZ가 RAM에 있을 때 동일한 새 특징을 계산해 별도 버전 필드로 전송한다. 640ms 한 창만으로 판정하므로 기존 측정 간격을 유지할 수 있다. No PSRAM 조건을 유지하고 실제 CPU·RAM 여유를 보드에서 확인해야 한다. 공개 원시 신호와 실측 비교용 원시 수집 경로는 별도로 준비한다.

**채택하지 않은 대안:** 현행 진동 RMS·peak 2개만 쓰면 축별 정보·대역 에너지를 잃는다. LSTM부터 쓰면 현재 불연속 창의 시간 간격과 연속 데이터 확보 문제가 추가되므로 1차 후보에서는 사용하지 않는다.

MCC5에는 음향이 없으므로 현재 음향 2개 특징을 이 데이터만으로 학습하지 않는다. 전류·토크를 음향 대용으로 쓰거나 없는 채널을 0으로 채우지 않는다.

공개 데이터 800Hz 변환은 ADXL345의 센서 잡음·양자화·부착·에일리어싱 특성을 재현한 것이 아니다. 실제 현장 검증 전 운영 판정·알림에 사용하지 않는다. 이번 작업은 커밋/푸시/배포 및 운영 모델 교체를 포함하지 않는다.

## 학습 계약

- 입력: 512 × XYZ, 800Hz, g. 평균 제거 후 축별 RMS/peak/Pearson 첨도와 [0,50), [50,100), [100,200), [200,350)Hz 에너지, 총 21개 특징.
- 원본 12.8kHz 변환: 350Hz cutoff, 1025-tap Kaiser(8.6) FIR, `resample_poly(1,16,padtype=line)`, 양끝 32개 목표 표본 제거. ADXL345 잡음·양자화 모사는 하지 않는다.
- 후보: 기존 AI1 DenseAutoencoder 구조(21→16→8→16→21), 정상 train 창만으로 학습. 150 epochs, Adam 0.001, batch 64, seed 42.
- 정규화: 정상 train 평균/표준편차. 임계값: validation 정상 재구성 오차 99% 분위수(`higher`). legacy checkpoint의 `sigma`는 호환 메타데이터이며 이 임계값 계산에 사용하지 않는다.
- 분할: 각 speed/torque 운전 모드 내 전체 프로파일을 고정 seed 해시 정렬 후 test 1개, validation 1개, 나머지 train으로 배정. 같은 프로파일의 모든 고장·정상 기록은 같은 split.
- 최종 test는 모델·임계값 고정 후 한 번 평가한다. 전체 및 고장별/운전 기록별 탐지율·오탐률을 보고한다. 전기적 고장도 별도 라벨별 결과를 남기며 진동만으로 모두 탐지한다고 주장하지 않는다.
- 실제 독립 모터/베어링 또는 ADXL345 성능이 아니다. 같은 기록 내 창들은 상관되어 있어 창 수를 독립 시험 횟수로 보고하지 않는다.

## 재현

저장소 루트에서 NumPy/SciPy/PyTorch가 있는 환경으로 실행한다. 예시 경로는 사용자 지정 로컬 출력 경로이며 운영 DB가 아니다.

```text
python -m ai.ai1.mcc5_training.download --destination <source-dir> --workers 4
python -m ai.ai1.mcc5_training.prepare --source <source-dir> --output <new-prepared-dir>
python -m ai.ai1.mcc5_training.train --prepared <prepared-dir> --output <new-model-dir> --epochs 150
python -m ai.ai1.mcc5_training.score_window --artifact <model-dir>/dense_autoencoder.pt --checksum sha256:<model-sha256> --input <model-dir>/golden-example.json
python -m unittest discover -s tests -p test_mcc5_training.py -v
```

원본·준비 데이터·모델 출력은 Git에 커밋하지 않는다. 기존 원본이 있으면 SHA256 일치 여부를 확인하고 재사용하며, 준비 데이터/모델 디렉터리는 덮어쓰지 않는다. 생성 모델의 기존 prepared-feature runtime 로딩은 별도로 검증한다. 기존 CWRU용 raw preprocessor에는 연결하지 않는다.

## 후속 펌웨어·백엔드 연동 인계 조건 (이번 변경에는 미구현)

- 현재 네 특징과 기존 LittleFS FIFO 동작은 유지한다. 새 프로파일 ID와 21개 특징을 별도로 추가하고, 구버전 패킷을 새 모델 입력으로 간주하지 않는다.
- 현재 `vibrationRmsRaw`의 raw 수치를 g로 간주하지 않는다. ADXL345 취득 배열의 단위·스케일 변환과 실제 고정 XYZ 축을 먼저 확인한다. 가속도 g는 진동 속도 mm/s와 다르다.
- 새 특징은 **고정 XYZ 세 축 모두**에서 계산한다. 기존의 RMS 최대 축 하나를 고르는 peak 계산으로 대체하지 않는다. FFT 윈도 함수·정규화·DC 처리·대역 경계는 `preprocessing.json`과 동일해야 한다.
- 장치 ID, 취득 시각, 구간 ID, 표본률, 표본 수, 단위, 프로파일 버전, 누락/포화 등 품질 상태를 함께 전달한다. 같은 구간의 재전송은 중복 추론 결과를 만들지 않도록 식별한다.
- 보드에서 음향 연속 수집과 함께 실행하여 연산 시간·최대 RAM·누락을 측정한다. 새 계산이 640ms 취득 및 기존 송신을 방해하지 않는지 확인하기 전에는 실시간 동작을 보장하지 않는다.
- `golden-example.json`으로 펌웨어와 PC의 21개 특징을 비교한다. 수치 허용오차는 보드의 float/FFT 구현 측정 후 확정하며, 임계값 근처의 판정 민감도도 확인한다.
- 공개 데이터 예제는 수치 재현 검사 전용이다. 실제 정상 장비의 속도·부하·설치 조건별 데이터를 추가 확보하여 오탐과 임계값을 검증한다. 공개 test로 임계값을 반복 조정하지 않는다.
- 운영 모델 교체 전에는 shadow 비교만 허용한다. 모델 로딩 성공, 공개 데이터 점수, 보드 수집 성공은 각각 다른 검증이며 어느 하나만으로 운영 정확도를 주장하지 않는다.

## 2026-09-07 실제 실행 결과 — 운영 채택 보류

두 원본 ZIP의 SHA256 및 282개 실제 CSV를 검증했다. Apple sidecar를 제외했으며, torque 파일의 날짜 뒤 `d` 접미사를 확인해 허용했다. 24개 상태 라벨, 12개 운전 프로파일에서 39,480개 단일 창을 생성했다. 품질 검사로 제외된 완전한 창은 0개이며, FIR 가장자리와 512표본 미만의 끝부분은 계약대로 제외한다.

- 분할: train 188기록/26,320창, validation 47기록/6,580창, test 47기록/6,580창. 학습에는 train의 **정상 1,120창만** 사용했다.
- test 프로파일: `speed_40Nm_2000rpm`, `torque_20Nm_1000rpm`.
- 임계값: `0.2938220202922821`. validation 정상 280창 중 오탐 2개, test 정상 280창 중 오탐 11개.
- test 이상 6,300창 중 탐지 3,543개, 미탐 2,757개: **탐지율 56.24%, 오탐률 3.93%, 균형 정확도 76.15%, ROC AUC 0.8108**.
- 특히 `bearing_outer_h` 탐지율은 6.43%, `bearing_ball_h`는 18.21%다. 정밀도 99.69%만 강조하면 이상 표본이 많은 구성과 높은 미탐을 숨기므로 채택 근거로 쓰지 않는다.
- 실제 체크포인트 SHA256: `1dd3273160ce50d5a67aeee100aabce23fca8307debe334a55801bbff4ebb75a`. 크기 10,265 bytes. 출력은 저장소 바깥 `../output/mcc5-thu/model-v1/`에 보관한다.
- 기존 Checkpoint 로더와 새 로컬 원시 입력 경로에서 모델 재로딩 및 golden 예제 재현을 확인했다. 이 예제는 숫자 재현용이며 실제 고장 탐지 성공 예제가 아니다.

**판단:** XYZ 정보 추가 + 단일 창 판정이라는 연결 방향은 유지하되, 이번 21개 특징과 Dense 후보는 최종 운영 사양으로 확정하지 않는다. 다음 후보는 train/validation에서 특징·모델을 비교하고, 현장 정상/이상 데이터 및 새로운 독립 평가 기록으로 확인한다. 이미 본 test를 계속 재사용하며 미관측 성능이라고 주장하지 않는다. 이번 결과만으로 800Hz 센서 전체의 가능/불가능 또는 성능 저하 원인을 단정할 수 없다. 실제 펌웨어·서버 설정·알림은 변경하지 않았다.
