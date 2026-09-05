# AI1 음향 학습 경로·음향/RPM 기준선

2026-09-05 추가 구현 목록의 AI1-01/02 대응이다. 기존 WAV 로더, 2주차 특징 추출,
3주차 내보내기, 4주차 동결·Dense/LSTM 후보 학습·기준선 레지스트리를 재사용한다.
백엔드 자동 등록, 승인·배포, 실제 공개 데이터 재학습 및 현장 성능 검증은 포함하지 않는다.

## 준비

저장소 루트에서 프로젝트용 Python 가상환경을 활성화하고 의존성을 설치한다.

```powershell
python -m pip install -r ai/ai1/week4/ai1/freq_baseline/requirements.txt -r ai/ai1/week3/ai1/requirements.txt
```

MIMII pump WAV는 사용자가 이용 조건을 확인해 별도로 준비한다. 이 구현은 데이터를
자동 다운로드하거나 라이선스를 추정하지 않는다. 폴더는 `pump/id_XX/normal/*.wav`,
`pump/id_XX/abnormal/*.wav` 구조다. 동일 데이터셋에는 같은 취득 조건의 녹음을 넣는다.

## 1. 음향 manifest 생성·동결

아래 경로·출처·이용 조건·취득 조건은 실제 입력에 맞게 바꿔야 한다.

```powershell
python ai/ai1/week3/ai1/datasets/register_acoustic_dataset.py --pump-dir D:/datasets/mimii/pump --source-uri "<확인한 원본 출처>" --license-note "<확인한 이용 조건>" --operating-conditions '{"acquisition":"<취득 조건 식별자>"}' --freeze --output outputs/ai1/acoustic-frozen.json
```

- 샘플링률은 WAV 헤더에서 읽고, 첫 마이크 채널만 사용한다. 샘플링률이 섞이면 실패하며 자동 리샘플링하지 않는다.
- 정수 PCM은 정규화하고 8-bit unsigned PCM은 중앙값을 뺀다. 단위는 `normalized_pcm`이며 보정된 dB SPL이나 진동 g가 아니다.
- 기본 윈도우/이동폭은 각각 2,048 samples다. 마지막 불완전 윈도우는 제외하고 윈도우보다 짧은 파일은 실패시킨다.
- 출처 URI·이용 조건·파일 해시, 단위·운전조건, 라벨 기준 문서와 해시를 보존한다.
  행의 `source_ref`는 `id_XX/normal/file.wav#samples=0:2048`처럼 원본 구간을 가리킨다.
- `NORMAL → NORMAL`, `PUMP_ANOMALY → ANOMALY` 이진 대응만 사용한다.
  `known_acoustic_label`은 CSV/XLSX에서 보존하며 상세 고장 라벨이나 현장 검증 라벨을 추정하지 않는다.
- 모든 녹음과 윈도우를 **machine ID 단위**로 train/validation/test에 분리한다.
  정상·이상 라벨마다 3-way 분할이 가능한 독립 machine 그룹이 필요하다.
  부족하면 실패하고 윈도우 랜덤 분할로 전환하지 않는다. 다른 machine에 같은 WAV를 복사한 경우도 거절한다.
- manifest의 `featureNames`가 모델 입력 스키마다. 메타데이터는 모델 입력에 넣지 않는다.
  원본·특징 출력·단위·조건·라벨 기준이 바뀌면 데이터셋 ID가 달라진다.
- 기본 MFCC는 13개다. librosa가 없으면 오류로 중단한다. 의도적으로 제외할 때만
  `--without-mfcc`를 추가한다. MFCC 열을 없애고 별도 스키마·ID를 만들며 가짜 0벡터를 저장하지 않는다.
- `--freeze` 없이 생성하면 draft다. 동결된 inline rows와 메타데이터를 수정하면 학습·기준선 입력 검증에서 거절한다.
  CLI 출력 파일이 이미 있으면 덮어쓰지 않는다.

## 2. 내보내기·후보 학습

```powershell
python ai/ai1/week3/ai1/datasets/export_dataset.py --manifest outputs/ai1/acoustic-frozen.json --output-dir outputs/ai1/handoff
python ai/ai1/week4/ai1/freq_baseline/train_and_evaluate.py --manifest outputs/ai1/acoustic-frozen.json --output outputs/ai1/acoustic-training-report.json --artifact-dir outputs/ai1/models
```

내보내기는 기존 `versions/<dataset id>/`와 `CURRENT` 구조를 사용한다.
CSV, XLSX, JSON의 단위·운전조건·라벨 집계는 같은 manifest에서 나온다.
**학습 입력은 등록 CLI가 생성한 inline rows 포함 JSON**이다. 내보내기의
`dataset_manifest.json`은 rows가 빠진 조회용 요약이므로 학습 입력으로 대체하지 않는다.

기존 Dense/LSTM 후보·scaler·보고서 저장 코드를 사용한다. validation F1로 후보를
선택하고 선택된 후보만 test holdout을 평가한다. LSTM은 같은 녹음의 연속 5개
윈도우씩 묶으며 각 split에 청크가 있어야 한다. 조건 미충족은 학습 전에 오류로 알린다.
보고서에는 음향 샘플링률·단위·취득 조건과 도메인 차이 제한사항이 포함된다.
이 경로를 구현했다는 사실은 실제 MIMII 성능이나 현장 일반화 성능을 보장하지 않는다.

## 3. 음향 기준선 초안

```powershell
python ai/ai1/week4/ai1/dataset_versions/signal_baseline.py --modality acoustic --input outputs/ai1/acoustic-frozen.json --site-id SITE-01 --asset-id ASSET-01 --output outputs/ai1/acoustic-baseline-draft.json
```

동결 음향 manifest의 정상 train 윈도우만 사용한다. validation/test 및 이상 행은 제외한다.
특징별 mean, 모집단 std, min/max, `mean ± sigma × std` 범위(기본 sigma=3)를 만든다.
이는 통계적 초안이며 현장 안전 한계나 승인된 경보 임계값이 아니다. 공개 데이터에서
자산용 초안을 만들었더라도 마이크·설치 위치·소음·운전조건의 현장 적합성은 별도로 확인한다.

## 4. 실측 RPM 입력 매핑·기준선 초안

RPM 입력은 검수한 실측 export를 아래 **타입이 보존된 JSON 배열**로 매핑한 것이다.
기존 CSV를 무검증으로 읽거나 문자열 `"true"`를 승인으로 간주하지 않는다.
`site_id`, `asset_id`, `rpm_unit`, `operating_conditions`, 고유 `source_ref`는
export 메타데이터/원본 측정 식별자와 대조해 공급해야 한다. 장치가 제출한 임의 라벨을
검수 완료로 바꾸거나 정격 RPM을 실측값으로 대체하는 어댑터가 아니다.

다음 수치는 형식 설명용이며 실제 기준선으로 사용하면 안 된다.

```json
[
  {
    "site_id": "SITE-01", "asset_id": "ASSET-01",
    "rpm": 1450, "rpm_unit": "rpm", "operating_conditions": {"load": "rated"},
    "source_ref": "device-01/session-01/sequence-10",
    "split": "train", "label_status": "verified", "target_label": "NORMAL",
    "training_eligible": true, "is_synthetic": false
  },
  {
    "site_id": "SITE-01", "asset_id": "ASSET-01",
    "rpm": 1470, "rpm_unit": "rpm", "operating_conditions": {"load": "rated"},
    "source_ref": "device-01/session-01/sequence-11",
    "split": "train", "label_status": "verified", "target_label": "NORMAL",
    "training_eligible": true, "is_synthetic": false
  }
]
```

```powershell
python ai/ai1/week4/ai1/dataset_versions/signal_baseline.py --modality rpm --input outputs/ai1/rpm-reviewed.json --dataset-id DS-REVIEWED-01 --site-id SITE-01 --asset-id ASSET-01 --operating-conditions '{"load":"rated"}' --output outputs/ai1/rpm-baseline-draft.json
```

검증된 NORMAL·train·학습 가능·비합성 행만 선택한다. 대상 자산/사이트, 단위 또는
운전조건이 섞이면 실패한다. `rpm:null`은 제외하고 유효한 실측 `0`은 보존한다.
음수·문자열·bool·비유한 값, 중복 참조, 유효 측정 2건 미만도 실패한다.

## 등록 경계와 회귀 검사

`register_signal_baseline(registry, baseline)`은 기존 `BaselineVersionRegistry`에
초안으로 등록한다. `signalContext`에 modality·단위·운전조건·표본 참조를 보존하고
등록/승인 digest에 함께 포함한다. CLI는 로컬 초안 JSON만 생성하며, 운영 백엔드에
업로드하거나 승인·활성화하지 않는다. 기존 평가기가 쓰는 `features` 통계 구조를 유지한다.
RPM 초안과 음향 초안은 별도 ID/문맥이며 서로의 단위를 대체하지 않는다.

```powershell
python -m unittest discover -s ai/ai1/week1/ai1/tests
python -m unittest discover -s ai/ai1/week2/ai1/tests
python -m unittest discover -s ai/ai1/week3/ai1/tests
python -m unittest discover -s ai/ai1/week4/ai1/tests
```

주차별로 별도 프로세스에서 실행해 동명의 과거 모듈이 충돌하지 않게 한다.
신규 테스트는 임시 생성 WAV/실측 형식 fixture로 등록→동결→내보내기→1-epoch 후보 학습,
신호별 기준선·초안 등록과 오류 경계를 검사한다. 실제 공개 데이터 파일이 필요한
기존 테스트는 파일이 없으면 skip한다. 생성 WAV, 모델 및 임시 학습 라이브러리는 Git에 넣지 않는다.
