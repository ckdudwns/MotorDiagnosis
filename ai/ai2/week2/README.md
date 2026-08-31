# AI-2 2주차: 이상 점수 초안·관제 시각화

AI-1 2주차의 정상 기준선(`../../ai1/week2/ai1/dataset/baseline.json`)을 사용해
수집 텔레메트리에 이상 점수를 붙이고, 관제 화면에서 사이트·설비·기간별로 표시한다.

## 포함 기능

- `anomaly_score.py` — `vibrationRmsRaw`를 AI-1의 RMS 정상 범위와 비교해 0~100 점수,
  상태(`normal`, `warning`, `critical`)와 근거를 만든다.
- `GET /api/telemetry` 응답은 저장된 raw 값은 변경하지 않고 `anomalyScore`,
  `anomalyStatus`, `anomalyEvidence`만 조회 시점에 추가한다.
- 대시보드는 사이트 요약 카드, 사이트·설비·1/6/24시간 선택, 5초 자동 갱신과
  진동·음향 raw RMS·RPM·이상 점수 공통 시간축 차트를 제공한다.

## 점수 범위와 한계

이 초안은 고장 유형 분류 모델이 아니다. AI-1 기준선의 정상 범위인 `mean ± 3σ` 안에서는
점수 0이며, 범위를 벗어나면 6σ에서 100이 되도록 선형 증가한다. 현재 음향 기준선이 없으므로
음향 값은 차트에만 표시하고 점수에는 포함하지 않는다. raw 값을 mm/s RMS나 dB로 변환하지
않는다.

## 검사

저장소 루트에서 실행한다.

```bash
python -m unittest discover -s ai/ai2/week2/tests -v
python -m unittest discover -s tests -v
```
