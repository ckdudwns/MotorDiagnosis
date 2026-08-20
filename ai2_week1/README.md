# AI2 1주차: 분석·실시간 경로 분리와 리플레이

## AI-1 결합 데이터 인계 후: AI2 경로 분리

AI-1의 `ai1_week1/ai1/data/handoff/ai1_handoff_dataset.csv`를 받았다면, 원본 CWRU 변환 대신
아래 명령을 사용한다. 이 도구는 AI-1의 `raw` 특징량과 라벨·출처를 분석 경로에
보존하고, 실시간 경로에는 고정 장치 매핑·현재 UTC 시각·외부 JSON 키를 적용한다.

```bash
python ai2_week1/prepare_ai1_handoff.py
```

결과는 다음과 같다.

```text
output/ai2_week1/ai1_analysis_dataset.csv
output/ai2_week1/ai1_telemetry_replay.jsonl
output/ai2_week1/ai1_handoff_manifest.json
```

- 분석 경로: 원본 `known_*_label`, `scenario_label`, `source`, `is_synthetic`, raw
  특징량과 AI2 고정 장치 매핑을 모두 보존한다.
- 실시간 경로: `siteId`, `assetId`, `deviceId` 등 외부 JSON 키를 쓰며, 가상 시각이
  아닌 현재 UTC 시각부터 1초 간격으로 재생한다.
- `vibration_rms_mm_s` 및 `acoustic_db`가 공란인 것은 정상이다. raw 값을 mm/s RMS나
  dB SPL로 표시하거나 전송하지 않는다.
- 두 입력 경로는 모두 외부 JSON의 camelCase 키(`scenarioLabel`, `assetId` 등)와 JSON
  `null`을 사용한다. 인계 CSV의 빈 nullable 값은 리플레이에서도 `null`로 보존한다.

## 보조 흐름: CWRU 원본 진동 직접 변환

AI-1 결합 데이터가 없는 경우에만, 프로젝트 루트의 `data/진동`에 있는 CWRU 파일 4개를
2초 구간으로 나누고 통계·주파수 특징량을 계산해 백엔드 수집 API용 JSONL을 만든다.

## 이 폴더의 Python 코드가 하는 일

### `prepare_vibration.py`: 원본 진동 데이터 가공기

연구용 원본 `.mat` 파일 안에는 초당 12,000개 수준의 진동 측정값이 들어 있어 대시보드나 API가 바로 쓰기 어렵다. 이 프로그램은 원본 파형을 2초 창으로 나눈 뒤, 각 창을 센서 텔레메트리 한 건으로 바꾼다.

```text
97.mat / 105.mat / 118.mat / 130.mat
                 ↓
         2초 단위 파형 구간
                 ↓
RMS · 표준편차 · kurtosis · peak · crest factor · 피크 주파수 · RPM 계산
                 ↓
vibration_features.csv + telemetry_replay.jsonl
```

출력 한 행은 '특정 시각에 장치가 측정한 2초간의 요약값'이다. `known_condition`에는 CWRU 원본 라벨을, `scenario_label`에는 MVP 운영용 라벨을 둔다. 음향 데이터가 아직 없으므로 `acoustic_*` 필드는 의도적으로 비워 둔다.

> CWRU 공개 파일의 값은 프로젝트 센서의 보정된 `mm/s RMS`가 아니다. 그래서 변환 결과에는 `vibration_rms_raw`처럼 `raw`를 붙인다. 백엔드의 외부 계약인 `vibration` 또는 `vibration_rms_mm_s`로 보내기 전에는 실제 센서 보정값 또는 팀이 합의한 데모 변환 규칙이 필요하다.

### `replay_telemetry.py`: 가상 센서 송신기

`telemetry_replay.jsonl`을 한 건씩 읽어 센서 단말처럼 보낸다. 기본 실행은 안전한 콘솔 미리보기이며 서버로 전송하지 않는다. `--send`를 붙였을 때만 `/api/telemetry/ingest`에 HTTP POST한다.

```text
변환된 JSONL
      ↓  (--interval-seconds 값마다 1건)
백엔드 수집 API
      ↓
대시보드 차트 → 3주차 이상 점수·이벤트
```

따라서 실제 MPU-6050 단말이 아직 없어도 '정상 신호 → 이상 신호' 흐름을 시연하고 백엔드·대시보드를 개발할 수 있다.

## 현재 데이터 매핑

| 파일 | 원본 라벨 | MVP 라벨 |
| --- | --- | --- |
| `97_Normal_0.mat` | normal | `normal` |
| `105_0.mat` | inner-race fault | `vibration_anomaly` |
| `118_0.mat` | ball fault | `vibration_anomaly` |
| `130@6_0.mat` | outer-race fault | `vibration_anomaly` |

`known_condition`은 원본 라벨을 보존하고, `scenario_label`은 MVP의 운영 라벨이다. 이 데이터는 공개 실험 데이터이므로 실제 발전소 고장 진단 결과로 표현하지 않는다.

## 개발 표준 및 Linux 실행

협업 표준에 따라 **Python 3.12.4, 허용 범위 3.12.x**만 사용한다. 가상환경은 저장소 루트의 `.venv`에 만들고, Black의 기본 88자 줄 길이를 따른다.

프로젝트 루트에서 다음을 실행한다.

```bash
python3.12 --version  # 3.12.x인지 확인
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python ai2_week1/prepare_vibration.py
```

현재 작업 환경처럼 `python3.12`가 시스템 PATH에 없다면, 로컬 도구 캐시의 3.12.4 실행본으로 같은 `.venv`를 만들 수 있다. `.local/`과 `.venv/`는 버전 관리하지 않는다.

```bash
./.local/python-3.12.4/python/bin/python3.12 -m venv .venv
```

포맷과 기본 검사는 다음과 같다.

```bash
python -m black ai2_week1
python -m black --check ai2_week1
python -m compileall ai2_week1
```

결과는 아래에 생성된다.

```text
output/ai2_week1/vibration_features.csv
output/ai2_week1/telemetry_replay.jsonl
output/ai2_week1/dataset_manifest.json
output/ai2_week1/label_mapping.csv
```

`vibration_features.csv`는 한 행이 2초간의 진동 측정 구간인 특징량 표다. `label_mapping.csv`는 원본 CWRU 라벨(`known_condition`)을 MVP 운영 라벨(`scenario_label`)로 어떻게 바꿨는지 정리한 AI-1 전달용 표다.

먼저 3건만 콘솔에서 확인한다. 이 명령은 서버로 전송하지 않는다.

```bash
python ai2_week1/replay_telemetry.py --limit 3
```

백엔드가 실행된 뒤 실제 API 전송은 다음처럼 한다. API의 최종 필드명은 백엔드 담당자와 맞춘다.

```bash
python ai2_week1/replay_telemetry.py --send --limit 10 --interval-seconds 1
```

## 음향 데이터가 내려받아진 뒤

음향 파일의 정상/이상 구간도 동일한 2초 창으로 특징량을 만든다. 그 결과에 `acoustic_rms`, `acoustic_peak_hz`, `acoustic_spectral_centroid_hz`를 채우고, 같은 `timestamp`와 `scenario_label` 기준으로 이 리플레이 데이터에 결합한다.

| 결합 시나리오 | 진동 | 음향 | MVP 라벨 |
| --- | --- | --- | --- |
| 정상 | CWRU 정상 | MIMII 정상 | `normal` |
| 진동 이상 | CWRU 이상 | MIMII 정상 | `vibration_anomaly` |
| 음향 이상 | CWRU 정상 | MIMII 이상 | `acoustic_anomaly` |
| 복합 이상 | CWRU 이상 | MIMII 이상 | `combined_anomaly` |

이 결합 데이터는 `공개 데이터 기반 합성 복합신호`로 표기한다. 실제 같은 설비에서 동시에 취득한 두 센서 데이터는 아니다.
