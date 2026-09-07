# 동일 21개 특징 분류기 비교 — 2026-09-07

## 결과 및 판단

검증 데이터로 선택한 후보는 **Random Forest**다. 입력 특징, 원본 및 분할은 이전 Dense 오토인코더와 동일하다. 다만 이번에는 정상뿐 아니라 고장 라벨도 학습했으므로 순수한 모델 구조만의 비교는 아니며, 지도학습 방식으로 전환한 효과를 함께 측정한다.

| 모델 | validation 탐지율 | 참고 test 탐지율 | 참고 test 정상 오탐 | 균형 정확도 | ROC AUC |
| --- | ---: | ---: | ---: | ---: | ---: |
| 기존 Dense AE | 63.86% | 56.24% | 11/280 (3.93%) | 76.15% | 0.8108 |
| Histogram Gradient Boosting | 77.57% | 55.14% | 0/280 (0%) | 77.57% | 0.9864 |
| Random Forest | 84.14% | 64.08% | 0/280 (0%) | 82.04% | 0.9733 |

균형 정확도와 ROC AUC 열은 참고 test 결과다. test는 앞선 실험에서 이미 확인했으므로 새로운 미관측 평가가 아니다. 이번 실행에서는 test 점수를 후보 선택에 사용하지 않았으며 선택 파일을 test 평가 전에 저장했다. 정상 280창은 두 기록에서 나온 서로 상관된 구간이므로 오탐 0개를 현장 오탐률 0%로 해석하지 않는다.

RF는 이상 6,300창 중 4,037창 탐지, 2,263창 미탐이다. 기존 대비 탐지율 +7.84%p이지만 **운영 채택은 보류**한다. 특히 bearing_outer_h 21.07%, bearing_ball_h 13.57%, bearing_outer_l 14.29%, winding_h 15.71% 등 낮은 탐지율이 남아 있다. 모든 고장 유형이 개선된 것은 아니다. 높은 AUC만으로 현재 임계값의 미탐 문제가 해결된 것으로 보고하지 않는다.

## 고정 실험 계약

- train 188기록/26,320창: 정상 1,120, 이상 25,200. validation/test는 각각 47기록/6,580창.
- 새로운 원시 전처리나 특징 추가 없음. RPM·파일명·토크 조건·run ID는 모델 입력에 사용하지 않음.
- 정상/이상 전체 가중치를 동일하게 하고, 각 클래스 안에서는 기록별 전체 가중치를 동일하게 설정.
- HGB: 200 iterations, learning_rate 0.05, max_leaf_nodes 15, min_samples_leaf 20, L2 1.0, early_stopping=False.
- RF: 300 trees, max_depth 12, min_samples_leaf 3, max_features sqrt. 두 후보 seed 42.
- 각 후보 threshold: validation 정상 점수의 99% 분위수(higher). score > threshold만 이상. 정상 validation 280창 중 오탐은 각각 2개.
- 후보 선택: validation balanced accuracy, recall, 낮은 FPR, 이름 순의 결정적 비교. 추가 하이퍼파라미터 탐색 없음.
- 검증 데이터는 임계값과 후보 선택 양쪽에 사용했으므로 검증 성능은 선택 편향이 있다. 교차검증·독립 현장 평가 완료를 주장하지 않음.
- 모델 출력은 분류 점수이며 실제 고장 확률로 교정되지 않음. 임계값 0.9266을 고장 확률 92.66%로 해석하지 않음.

## 산출물 및 안전

실제 출력은 저장소 밖 ../output/mcc5-thu/comparison-v1/에 보관한다.

- comparison_report.json: 학습 계약, 고장별/기록별 결과, 소스·데이터·모델 해시 및 환경.
- selection.json: test 평가 전 확정한 선택.
- random_forest.joblib / hist_gradient_boosting.joblib: 로컬 연구용 저장 모델.
- 각 모델의 *-golden.json 및 *-scores.npz: 원시 구간 수치 재현과 평가 점수.

RF SHA256: 88f97678fe22975187d77b46953036e99095c01bbfce61e0aa5864b82277fd23

HGB SHA256: 60d341de18b9aaf50dc41a9502c79ad87344059cd35bc94a69ea1e62e7143116

joblib은 pickle 기반이므로 신뢰하지 않는 파일을 로드하지 않는다. 코드에서는 같은 프로세스에서 직접 생성한 파일만 해시 확인 후 재로딩하여 validation 전체 출력이 일치하는지 검사했다. 이 형식은 기존 PyTorch .pt 로더와 호환되지 않으며 운영 서버에 복사해 활성화할 수 없다.

기존 preprocessing.json의 primaryCandidate=dense_autoencoder는 이전 기준 모델 메타데이터다. 입력 수치 계약을 유지하기 위해 바꾸지 않았으며 실제 비교 후보 종류는 각 저장 payload의 modelType과 report의 candidate 이름이 권위 값이다.

## 재현

저장소 루트, 기존 NumPy/SciPy/PyTorch 환경에 scikit-learn 1.9.0 및 joblib이 필요하다. 기존 운영 의존성은 변경하지 않았다.

    python -m ai.ai1.mcc5_training.compare --prepared <prepared-v1> --output <new-output> --baseline-report <model-v1>/training_report.json
    python -m unittest discover -s tests -p "test_mcc5*.py" -q

기존 출력 디렉터리는 덮어쓰지 않는다. 소스 ZIP을 다시 다운로드하거나 재전처리하지 않고 검증된 캐시를 사용한다.

## 다음 검증

이번 코드 검증: 관련 테스트 19개 통과, 전체 테스트 486개 실행/18개 제외/실패 0개(468개 통과). 두 실제 모델의 저장 전후 validation 전체 출력 일치와 golden 원시 구간 특징 재현, 코드·모델 SHA256 일치를 확인했다. 보드·현장 시험은 미실행이다.

우선 train/validation 내부의 운전 조건별 분리 평가에서 특징과 임계값 안정성을 확인하고, 세분화한 대역·상대 에너지 등 후보를 하나씩 비교한다. 기존 test는 개발 참고로만 남긴다. 실제 ADXL345 정상/이상 및 독립 기록으로 확인하기 전에는 운영 정확도를 주장하지 않는다. 현재 네 특징 전송이나 센서 단위·축을 임의 변경하지 않았으며 펌웨어·서버·알림·커밋·푸시·배포를 수행하지 않았다.
