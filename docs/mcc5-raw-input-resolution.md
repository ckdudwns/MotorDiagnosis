# 입력 합의 반영·센서 분해능 대응 실험

기준: [실측 진동 모델 입력 합의안](model-input-agreement.md), 2026-09-08 초안. 사용자 요청에 따라 모델 쪽 변환과 비교 실험에 반영했다. 담당자 수치 오차 승인, 최종 운영 모델 선정, 새 API·원시 버퍼·보관 기간 확정을 대신하지 않는다.

## 반영한 입력 계약

`ai/ai1/mcc5_training/raw_input.py`는 한 구간의 원시 `[512,3]` XYZ, 800Hz, g를 입력으로 받는다. 기본 21개 또는 spectral66의 명시적 프로필 ID가 필요하며 다른 shape·속도·축 순서·단위는 거부한다. 필드 입력에 공개 데이터용 12.8kHz→800Hz 리샘플링을 다시 적용하지 않는다.

- 각 축 평균 제거, periodic Hann, 대역 정의·상관계수 순서는 기존 특징 계산을 재사용한다.
- 기본 21개를 float32로 변환한 뒤 float64로 승격한다. 추가 45개는 float64를 유지한다.
- 모델 입력은 `[1,66]`, sequenceLength=1이다. 3구간 연속 확인은 점수 산출 이후의 별도 정책이며 입력을 3구간/5구간 시퀀스로 바꾸지 않는다.
- 수집 단계의 추가 정규화 없음. 모델별 정규화는 해당 아티팩트 계약에 따른다.
- 기존 특징 21개나 음향/RPM 값을 원시 XYZ 대신 넣거나, 없는 특징을 0으로 채우지 않는다.
- 표본 누락, 알려진 센서 오류, 상수축·포화 등은 `InputUnavailable` 예외로 표현한다. API가 연결될 때 이를 `unavailable`로 전달해야 하며 예외를 정상 점수로 바꾸면 안 된다.
- `model_input()`의 `values`는 1차원 float64 배열이고, 모델 호출 때 `values[None, :]`로 `[1,66]`을 만든다. 반환 메타데이터의 `inputShape`와 실제 호출 shape를 구분한다.

`counts_to_g()`는 **명시적 정수 count 입력**만 받아 `0.0039 g/LSB`를 곱한다. 평균 제거 전 `<=-4096` 또는 `>=4095`이면 포화로 거부한다. 현재 C++의 count 경계와 맞춘 것이며 PC의 g 입력 `abs(g)>=16` 검사와 경계가 같다는 뜻은 아니다. 여러 오류가 동시에 있는 경우 C++과의 품질 우선순위 일치는 별도 검증이 필요하다.

이 모듈은 현재 PC/분석용 어댑터다. 66개 계산은 기존 연구 모듈을 불러오므로 현재 분석 환경의 NumPy/SciPy 및 연구 의존성이 필요하다. 운영 서버 배포 시 특징 계산 모듈의 의존성 분리·로더·처리량 검증을 추가해야 한다. 기존 `POST .../vibration-windows`의 21개 계약은 변경하지 않았다.

## 분해능 모사와 학습 비교

공개 데이터 원시 기록을 해시 검증한 뒤 기존 학습 분할의 188개 기록만 새로 원시 처리한다. 원본 ZIP 전체와 부모 캐시 무결성 검사를 위해 다른 분할의 바이트·캐시를 읽을 수 있지만 validation/test 원시 구간을 새로 특징 추출하거나 모델 평가하지 않는다.

각 800Hz 구간에 `round-nearest-even(g/0.0039)`를 적용해 정수 count로 만든 뒤 g로 복원한다. 포화 값을 안전 범위로 잘라 넣지 않는다. 실제 ADC의 전달 특성, 잡음, 중력 방향, 부착 강성, 앨리어싱, 클록 오차는 모사하지 않는다. 따라서 이 실험은 **분해능 스트레스 검사**이지 ADXL345 현장 데이터셋이 아니다.

원래 66개 입력과 새 어댑터의 66개를 전 구간 대조한다. 무효 양자화 구간은 이유와 함께 캐시에 보존하며, 하나라도 있으면 학습 비교를 중단해 평가 집합을 사람이 검토하도록 한다. 모델에 0/NaN을 넣거나 불량 구간을 조용히 삭제하지 않는다.

고정 후보:

1. `clean_fit`: 원래 입력으로 학습.
2. `half_quantized_fit`: **학습 partition 안의 짝수 구간 순번만** 양자화 입력으로 교체. 표본·운전 기록 수와 클래스/기록별 가중치는 유지.

기존처럼 조건 8개를 6개 학습·1개 임계값 조정·1개 평가로 분리한다. 두 후보 모두 같은 조정 조건의 **변형하지 않은 정상 입력**에서 q99(higher) 임계값을 정한다. 평가 조건은 학습·조정에서 제외하며, 동일 구간의 원래 입력과 양자화 입력 두 경우를 각각 점수화한다.

단일 판정과 사전에 고정한 3구간 연속 확인을 모두 보고한다. 두 규칙에 필요한 이력이 준비된 동일 구간을 비교하고, 전체 구간의 대기·탐지율도 별도 공개한다. 이번 공통 집합은 각 기록 처음 2개를 제외하므로, 5개 정책을 비교하며 처음 4개를 제외했던 `temporal-v1`의 수치와 직접 혼합하지 않는다.

모델 교체 후보의 사전 연구 기준:

- 원래 입력·양자화 입력 양쪽에서 3구간 확인의 평균 오탐률 ≤3%, 최악 조건 오탐률 ≤10%.
- 각 경우 기존 모델 대비 탐지율 손실 ≤5%p.
- 추가로 양자화 입력에서 오탐률이 **엄격히 감소**하고 탐지율은 악화되지 않아야 함.
- 모두 통과하지 않으면 선택 없음. 이 기준 자체도 운영 승인이나 독립 성능 검증은 아님.

## 재현·전달물

```bash
python -m ai.ai1.mcc5_training.sensor_robustness prepare \
  --source ../output/mcc5-thu/source \
  --parent ../output/mcc5-thu/prepared-v1 \
  --spectral ../output/mcc5-thu/spectral-cache-v1 \
  --output ../output/mcc5-thu/resolution-cache-new

python -m ai.ai1.mcc5_training.sensor_robustness compare \
  --parent ../output/mcc5-thu/prepared-v1 \
  --prepared ../output/mcc5-thu/resolution-cache-new \
  --reference-oof ../output/mcc5-thu/spectral-v1/rf66-oof.npz \
  --output ../output/mcc5-thu/resolution-fit-new
```

모두 새 출력 경로를 사용한다. 기존 모델·원본·실험 결과는 덮어쓰지 않는다. 원래 모델의 교차 예측 점수와 임계값이 이전 RF66 결과와 `atol=1e-14` 이내로 재현되는지도 검사한다.

캐시에는 행별 run/순번/정답/원래 특징/양자화 특징/불량 사유, 명세 해시를 보관한다. `golden-input.json`에는 공개 원시 XYZ·모사 count·각각의 기대 66개 특징을 담는다. 이 golden은 장치에서 측정한 정상 예제가 아니다. 특징 수치 허용 오차의 담당자 승인은 여전히 필요하다.

실험 보고서는 입력 합의 파일 해시, 모델 설정, fold, 코드·캐시 해시, 원래/변형 입력의 성능과 선택 여부를 보존한다. 새 가중치 파일을 자동 배포하거나 알림을 활성화하지 않는다.

## 2026-09-08 실행 결과

학습 기록 188개·26,320구간에서 새 변환기의 기존 입력 재현 검사를 통과했고, 양자화 후 무효 구간은 0개였다. 기존 RF66 교차 예측 점수·임계값도 `atol=1e-14` 기준으로 재현됐다.

아래는 **학습 분할 내 조건별 교차 평가**, 3구간 연속 확인, 각 기록 처음 2구간을 제외한 동일 집합의 결과다. 평균은 조건별 지표의 평균이며 독립 현장 시험 성능이 아니다.

| 학습 후보 | 평가 입력 | 평균 탐지율 | 평균 오탐률 | 최악 조건 오탐률 |
| --- | --- | ---: | ---: | ---: |
| 기존 clean_fit | 원래 입력 | 78.51% | 1.09% | 7.97% |
| 기존 clean_fit | 양자화 입력 | 78.37% | 1.45% | 10.87% |
| half_quantized_fit | 원래 입력 | 79.65% | 1.09% | 7.25% |
| half_quantized_fit | 양자화 입력 | 79.30% | 1.18% | 7.25% |

`half_quantized_fit`가 사전 연구 기준을 통과했다. 이는 반복 사용한 학습 조건에서의 후보 선별 결과이므로, 분해능 이외의 실제 센서 특성이나 현장 오탐 개선을 보장하지 않는다.

별도 연구용 모델을 `../output/mcc5-thu/resolution-candidate-v1/`에 저장했다.

- `candidate.joblib`: Random Forest, 2,137,327 bytes. 기존 후보는 보존했다.
- SHA256: `631c29cdcff2fc79843f607c38c0b37c9d1df63e36838bcd88a242942ba23c80`
- 최종 학습 26,320구간. 기존 validation 정상 280구간으로 q99(higher) 임계값 `0.79148320436784`를 설정했다. **같은 정상 보정 자료의 재사용이며 독립 평가가 아니다.** validation 이상·test 점수는 이번 작업에서 계산하지 않았다.
- `input-contract.json`: 66개 순서·shape·수치 정책·단위·버전. `golden.json`: 공개 원시 XYZ/모사 count, 기대 특징과 모델 점수·판정. `report.json`: 해시·출처·제약.
- 저장 후 모델 재로딩 점수를 `atol=1e-14`로 대조했다. golden의 원래 특징은 `rtol=atol=1e-12`, 양자화 특징은 정확 일치로 검증했다.
- 모델 관련 회귀 테스트 73개가 통과했다. 입력 거부·수치 정책·양자화·선별 기준·저장 차단을 포함하며, 실제 보드 시험은 포함하지 않는다.
- 데이터 출처: MCC5, DOI `10.17632/6s3dggj9mw.1`, Shijin Chen, Zeyi Liu, Chenyang Li, Dongliang Zou, Xiao He, Donghua Zhou. Mendeley CC BY 4.0 및 연결된 HF 카드 MIT 표기를 모두 보존했다.

내보내기 재현은 새 출력 경로에서만 가능하다.

```bash
python -m ai.ai1.mcc5_training.export_resolution_candidate \
  --parent ../output/mcc5-thu/prepared-v1 \
  --spectral ../output/mcc5-thu/spectral-cache-v1 \
  --cache ../output/mcc5-thu/resolution-cache-v1 \
  --experiment ../output/mcc5-thu/resolution-fit-v1 \
  --output ../output/mcc5-thu/resolution-candidate-new
```

**이 joblib은 현재 운영의 Dense/PyTorch 로더와 호환되지 않는다.** 신뢰할 수 없는 joblib은 로드하면 안 되며, 출처·해시 확인과 별도 지원 로더가 필요하다. 운영 모델·서버·알림은 변경하지 않았다.

## 남는 연동 작업

원시 XYZ의 별도 버전 전송 계약, 센서 버퍼·영속 큐, 저장·보관 기간, 장치 scope/중복/시각 검사, 운영 추론 로더 및 화면 연결은 별도다. B 경로의 모델 입력 변환을 준비한 것이며, 연속 원시 전송부터 운영 판정까지 완성한 것으로 해석하지 않는다.
