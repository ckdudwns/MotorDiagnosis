# AI-1 인계 데이터셋 — 컬럼 정의서

산출 스크립트: `scripts/build_ai1_handoff_dataset.py`
산출 파일: `data/handoff/ai1_handoff_dataset.csv` / `.json`

## ⚠️ 반드시 먼저 읽을 것 — 이 데이터셋의 한계

이 데이터셋은 **실제 센서 동시 측정 데이터가 아니다.**

- **진동 데이터**: CWRU Bearing Dataset (모터 베어링, 실측)
- **음향 데이터**: MIMII pump Dataset (수중 펌프, 실측)

두 데이터셋은 **서로 다른 실제 설비**에서 **서로 다른 시점**에 녹음된 것이라, 같은 자산(asset)에서 동시에 잡힌 값이 아니다. 이 스크립트는 진동 샘플 1개와 음향 샘플 1개를 무작위로 짝지어 "가상의 통합 센서 스냅샷"을 만든다 (`is_synthetic = True`, `source` 컬럼에 결합 방식 명시).

→ **파이프라인/스키마/대시보드 개발 검증용으로만 사용.** 실제 이상탐지 모델 성능 평가나 임계값 튜닝의 근거로 쓰면 안 됨.
→ 센서 도착 후에는 이 합성 페어링 로직을 제거하고, 실제 동시 측정 데이터로 교체해야 함.

## 컬럼 정의

| 컬럼명 | 타입 | 설명 |
|---|---|---|
| `timestamp` | ISO8601 string | 합성 타임스탬프 (실제 측정 시각 아님, 순번 기반으로 생성) |
| `device_id` | string | 가상 장치 ID (`SYN-DEV-0001` 형식) |
| `asset_id` | string | 가상 설비 ID, 5개 순환 (`SYN-ASSET-01`~`05`) |
| `rpm` | float \| null | CWRU 원본 메타데이터의 모터 RPM (진동 데이터에서만 확보, 실측값) |
| `vibration_rms_raw` | float | 진동 신호 RMS. **보정 전 가속도계 raw 출력값** (단위: g) |
| `vibration_rms_mm_s` | null | mm/s 보정값. **이번 프로토타입에서는 계산 안 함** (센서 감도 스펙 필요) |
| `vibration_peak_hz` | float | 진동 신호에서 에너지가 가장 큰 주파수 (FFT 피크) |
| `acoustic_rms_raw` | float \| null | 음향 신호 RMS. **[-1,1] 정규화 진폭 기준, dB 보정 안 됨** |
| `acoustic_db` | null | dB SPL 보정값. **이번 프로토타입에서는 계산 안 함** (마이크 감도/기준 음압 필요) |
| `acoustic_peak_hz` | float \| null | 음향 신호에서 에너지가 가장 큰 주파수 (FFT 피크) |
| `known_vibration_label` | string | CWRU 원본 라벨 (`NORMAL` / `BEARING_FAULT_INNER` / `BEARING_FAULT_BALL` / `BEARING_FAULT_OUTER`) |
| `known_acoustic_label` | string \| null | MIMII 원본 라벨 (`NORMAL` / `PUMP_ANOMALY`) |
| `scenario_label` | string | 아래 4종 중 하나 (진동·음향 상태 조합으로 자동 판정) |
| `source` | string | 데이터 출처 및 결합 방식 (`CWRU+MIMII_synthetic_pairing` 또는 `CWRU_only_synthetic`) |
| `is_synthetic` | bool | 항상 `True`. 공개 데이터 기반 합성 데이터임을 명시 |
| `vibration_unit_note` | string | 진동 단위 관련 주석 (raw임을 재확인) |
| `acoustic_unit_note` | string \| null | 음향 단위 관련 주석 (raw임을 재확인) |

## scenario_label 판정 규칙

| known_vibration_label 상태 | known_acoustic_label 상태 | scenario_label |
|---|---|---|
| 정상 | 정상 | `normal` |
| 이상 | 정상 | `vibration_anomaly` |
| 정상 | 이상 | `acoustic_anomaly` |
| 이상 | 이상 | `combined_anomaly` |

(음향 데이터가 없는 경우 진동만으로 `normal` / `vibration_anomaly` 2종만 생성됨)

## 실행 방법

```bash
cd ai1/scripts
python3 build_ai1_handoff_dataset.py \
  --cwru-dir ../data/external/cwru \
  --mimii-pump-dir ../data/external/mimii/pump \
  --output-dir ../data/handoff
```

MIMII 음향 데이터가 아직 없으면 `--mimii-pump-dir`을 생략해도 진동 데이터만으로 실행됨 (경고 메시지 출력).

## TODO (실제 센서 도착 후)

- [ ] 합성 페어링 로직(`build_ai1_handoff_dataset.py`의 랜덤 매칭 부분) 제거
- [ ] 실제 센서 스펙(가속도계 감도, 마이크 기준 음압) 확보 후 `vibration_rms_mm_s`, `acoustic_db` 계산 로직 추가
- [ ] `device_id`/`asset_id`를 `ASSET_MGMT_01`, `INSTALL_POINT_01` 기준정보와 실제 연결
- [ ] `known_*_label`을 실제 정비 이력/전문가 라벨로 교체
