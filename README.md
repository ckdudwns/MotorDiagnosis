# MotorDiagnosis — Bind Edge AI

도서·섬 지역 발전설비의 모터·펌프 등 회전설비에서 수집한 진동·음향 신호를 기반으로
상태를 모니터링하고 이상 후보를 확인하는 PoC + MVP 프로젝트다.

## 현재 1주차 범위

- [AI-1: 신호 전처리·라벨·데이터셋](./ai1_week1/README.md)
- [AI-2: 분석/실시간 경로 분리·리플레이](./ai2_week1/README.md)

AI-1의 공개 데이터 기반 합성 인계 파일을 AI-2가 분석 경로와 실시간 리플레이 경로로
분리한다. 이 데이터는 실제 한 설비에서 동시에 측정한 데이터가 아니므로, 모델 성능이나
실제 고장 진단의 근거로 사용하지 않는다.

## 개발 환경

- Python 3.12.4 (허용 범위: 3.12.x)
- 저장소 루트 `.venv`
- Black, line length 88

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m black --check .
```

## 저장소 규칙

- Python·CSV 내부 이름은 snake_case, 외부 JSON은 기존 camelCase 계약을 유지한다.
- 원본 `.mat/.wav`, 생성 데이터셋·리플레이 결과, 내부 참고 문서, 가상환경은 커밋하지 않는다.
- `main`에 직접 푸시하지 않는다. `feat/<kebab-description>` 브랜치와 Pull Request를 사용하고,
  최종 병합은 저장소 소유자만 수행한다.
