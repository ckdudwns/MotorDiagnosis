# AI-1 — 신호 전처리 · 특징량 · 라벨 · 데이터셋

Bind Edge AI 프로젝트 1주차, AI-1 담당 영역의 초기 구조입니다.
책임 영역: **신호 전처리·특징량, 라벨, 기준선, 학습·검증 데이터셋**
(관련 기능: `ACOUSTIC_LABEL_01` 주담당 / `AUTH_ROLE_01`, `ASSET_MGMT_01`, `INSTALL_POINT_01` 협업)

센서 장비가 도착하기 전, 공개 데이터셋과 합성 신호로 파이프라인을 미리 검증하는 것이 목표입니다.

## 폴더 구조

```
ai/ai1/week1/ai1/
├── labels/                 # 음향 라벨 기준표 (ACOUSTIC_LABEL_01)
│   └── acoustic_label_criteria.md
├── feature_extraction/     # 신호 전처리 + 특징량 추출 코드
│   ├── extract_features.py
│   └── requirements.txt
├── dataset/                 # 학습·검증 데이터셋 구조 정의
│   └── schema.md
├── scripts/                 # 공개 데이터셋 다운로드 / 합성 신호 생성
│   ├── download_public_dataset.py
│   └── generate_synthetic_signal.py
└── tests/                   # 파이프라인 동작 확인용 더미 테스트
    └── test_pipeline_dummy.py
```

## 이번 주 진행 순서

1. `labels/acoustic_label_criteria.md` — 라벨 카테고리·판정 기준 정의 (전문가 자문 전 초안)
2. `scripts/generate_synthetic_signal.py` — 정상/이상음 모의 신호 생성 (센서 도착 전 대체)
3. `feature_extraction/extract_features.py` — RMS, 스펙트로그램, MFCC 등 특징량 추출
4. `dataset/schema.md` — 라벨-샘플-설비-설치위치 연결 스키마 (백엔드·IoT 팀과 정렬 필요)
5. `tests/test_pipeline_dummy.py` — 합성 신호 → 특징량 → 라벨 저장까지 더미 파이프라인 검증

## 프로토타입 데이터 (진동 + 음향 소수 결합)

센서 도착 전, 아래 두 공개 데이터셋을 소수 결합해 프로토타입 파이프라인을 검증한다.

- **진동**: CWRU Bearing Dataset — **10 물리 베어링(specimen)**, 각 0/1/2/3 HP 부하로
  측정한 4파일 = 총 40개 `.mat`:
  정상 베이스라인 `97~100`(1 specimen), 내륜 `105~108`/`169~172`/`209~212`(0.007/0.014/0.021"),
  볼 `118~121`/`185~188`/`222~225`, 외륜 `130~133`/`197~200`/`234~237`.
  같은 베어링의 부하별 4파일은 하나의 specimen이다 — group split은 specimen 단위로 해야
  같은 베어링이 train/test에 섞이는 누수를 막는다. **건강한 베어링은 1개뿐**이라 NORMAL
  specimen 독립 분할은 불가능하다(3주차 참고). 다운로드: https://engineering.case.edu/bearingdatacenter/welcome
  - `98/99.mat`에는 RPM 키가 없어 파일 번호로 부하 조건을 추정해 채운다.
  - `99.mat`은 `X098_DE_time`(=`98.mat`과 중복)과 `X099_DE_time`을 함께 담고 있어,
    로더가 파일 번호와 일치하는 `X099_DE_time`만 선택한다.
- **음향**: MIMII pump 데이터셋 `0_dB_pump.zip` (`normal`/`abnormal` 라벨)

### 데이터 배치

다운로드한 파일을 아래 경로에 놓는다 (`.gitignore`에 등록되어 레포에는 커밋되지 않음):

```
ai/ai1/week1/ai1/data/external/cwru/97.mat  ...  ai/ai1/week1/ai1/data/external/cwru/237.mat
  (정상 97~100 / 내륜 105~108·169~172·209~212 / 볼 118~121·185~188·222~225 /
   외륜 130~133·197~200·234~237 — 40개. 없는 파일은 로더가 건너뛴다)

ai/ai1/week1/ai1/data/external/mimii/pump/id_00/normal/*.wav
ai/ai1/week1/ai1/data/external/mimii/pump/id_00/abnormal/*.wav
ai/ai1/week1/ai1/data/external/mimii/pump/id_02/...   # 0_dB_pump.zip 압축 해제 후 pump/ 폴더를 이 경로에 이동
```

### 실행

```bash
python ai/ai1/week1/ai1/scripts/build_prototype_dataset.py \
  --cwru-dir ai/ai1/week1/ai1/data/external/cwru \
  --mimii-pump-dir ai/ai1/week1/ai1/data/external/mimii/pump \
  --output-dir ai/ai1/week1/ai1/data/prototype
```

결과: `ai/ai1/week1/ai1/data/prototype/features_with_labels.json` — 두 모달리티(vibration/acoustic) 각각 특징량 + 라벨이 통합 저장됨.

라벨 체계 (프로토타입 한정):

| 모달리티 | source_label | label |
|---|---|---|
| vibration | 97/98/99/100.mat | `NORMAL` |
| vibration | 105/106/107/108.mat | `BEARING_FAULT_INNER` |
| vibration | 118/119/120/121.mat | `BEARING_FAULT_BALL` |
| vibration | 130/131/132/133.mat | `BEARING_FAULT_OUTER` |
| acoustic | normal | `NORMAL` |
| acoustic | abnormal | `PUMP_ANOMALY` |

> 진동(가속도)과 음향(음압)은 물리량이 달라 같은 분류기로 바로 합쳐 학습하는 건 권장하지 않는다.
> `modality` 필드로 구분해 분석/실험하고, 특징량 추출 파이프라인이 두 소스 모두에서 동작하는지 검증하는 것이 이번 프로토타입의 목적.

관련 스크립트:
- `scripts/load_cwru_vibration.py` — .mat → 윈도우 분할 → 라벨링 → RPM 추출
- `scripts/load_mimii_acoustic.py` — .wav → 채널0 추출 → 라벨링
- `scripts/build_prototype_dataset.py` — 위 둘을 결합해 특징량 추출까지 원샷 실행
- `scripts/build_ai1_handoff_dataset.py` — 다른 팀(백엔드/IoT 등) 전달용 표준 스키마로 출력
  (컬럼 정의는 [`dataset/handoff_format.md`](./dataset/handoff_format.md) 참고 — **단위 및 합성 데이터 주의사항 필독**)

## 다음 단계 (센서 도착 후)

- 위 프로토타입 파이프라인의 특징량 추출 로직을 실제 센서 원시 데이터에 연결
- 실제 음향 전문가 자문 반영해 `labels/acoustic_label_criteria.md` 확정 (CWRU/MIMII 라벨은 참고용, 최종 기준 아님)
- `DATA_PIPELINE_01`(AI-2 담당)과 실시간/배치 경로 분리 구조 연동
