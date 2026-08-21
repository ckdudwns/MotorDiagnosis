"""
정상(NORMAL) 기준선(baseline) 산출 스크립트 (AI-1, 2주차 EDGE_FEATURE_01)

CWRU 진동 데이터 중 NORMAL 라벨(97.mat)만 골라 윈도우 단위로 특징량을 추출하고,
특징값별 평균/표준편차와 "정상 범위"(mean ± N*std)를 계산해 baseline.json으로 저장한다.

이 기준선은:
- AI-2의 DASH_SIGNAL_01(실시간 신호 차트)에서 정상 범위 참조선으로 사용 가능
- 백엔드 TELEMETRY_VALIDATE_01(검증·중복 제거)이 이상치 후보 판정 참고자료로 사용 가능
- 같은 폴더의 `feature_extraction/validate_features.py`가 이상치 검증에 그대로 사용

CWRU 원시 데이터는 `ai/ai1/week1/ai1/data/external/cwru/`(1주차 인계 경로)에 있으며,
2주차 코드는 이를 복제하지 않고 1주차 로더(`load_cwru_vibration.py`)를 그대로 재사용한다.

사용법:
    python compute_baseline.py
    python compute_baseline.py \
        --data-dir ../../../week1/ai1/data/external/cwru --sigma 3
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# 1주차 CWRU 로더 재사용 (데이터/로더 모두 1주차 경로가 원본)
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_DEFAULT_DATA_DIR = os.path.normpath(
    os.path.join(
        _THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru"
    )
)
_DEFAULT_OUTPUT = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "dataset", "baseline.json")
)

sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "feature_extraction"))

from load_cwru_vibration import load_cwru_dataset  # noqa: E402
from extract_features import extract_all_features, FeatureConfig  # noqa: E402

NORMAL_LABEL = "NORMAL"
DEFAULT_SIGMA_MULTIPLIER = 3.0


def compute_feature_baseline(
    data_dir: str,
    window_size: int = 2048,
    hop_size: int = 2048,
    sigma_multiplier: float = DEFAULT_SIGMA_MULTIPLIER,
) -> dict:
    """NORMAL 라벨 윈도우들의 특징량을 모아 평균/표준편차/정상범위를 계산."""
    records = load_cwru_dataset(data_dir, window_size=window_size, hop_size=hop_size)
    normal_records = [r for r in records if r["label"] == NORMAL_LABEL]

    if not normal_records:
        raise ValueError(
            f"NORMAL 라벨 윈도우를 찾을 수 없습니다 (data_dir={data_dir}). "
            "97.mat이 해당 경로에 있는지 확인하세요."
        )

    sample_rate = normal_records[0]["sample_rate"]
    config = FeatureConfig(sample_rate=sample_rate)

    per_feature_values = {}
    for rec in normal_records:
        features = extract_all_features(rec["signal"], config)
        for name, value in features.items():
            per_feature_values.setdefault(name, []).append(value)

    feature_baseline = {}
    for name, values in per_feature_values.items():
        arr = np.asarray(values, dtype=np.float64)
        mean = float(np.mean(arr))
        std = float(np.std(arr))
        feature_baseline[name] = {
            "mean": mean,
            "std": std,
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "normal_range": [
                mean - sigma_multiplier * std,
                mean + sigma_multiplier * std,
            ],
        }

    baseline = {
        "meta": {
            "label": NORMAL_LABEL,
            "source": "CWRU 97.mat (Drive-End, NORMAL)",
            "modality": "vibration",
            "n_windows": len(normal_records),
            "window_size": window_size,
            "hop_size": hop_size,
            "sample_rate": sample_rate,
            "sigma_multiplier": sigma_multiplier,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "features": feature_baseline,
    }
    return baseline


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CWRU NORMAL 데이터 기반 특징값 정상 기준선(baseline) 산출"
    )
    parser.add_argument(
        "--data-dir", default=_DEFAULT_DATA_DIR, help="97.mat 등이 있는 폴더"
    )
    parser.add_argument("--window-size", type=int, default=2048)
    parser.add_argument("--hop-size", type=int, default=2048)
    parser.add_argument(
        "--sigma",
        type=float,
        default=DEFAULT_SIGMA_MULTIPLIER,
        help="정상범위 = mean ± sigma*std",
    )
    parser.add_argument("--output", default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    baseline = compute_feature_baseline(
        data_dir=args.data_dir,
        window_size=args.window_size,
        hop_size=args.hop_size,
        sigma_multiplier=args.sigma,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)

    print(f"NORMAL 윈도우 {baseline['meta']['n_windows']}개로 기준선 산출 완료")
    print(f"저장 위치: {args.output}")
    print("\n특징값별 정상 범위 (mean ± {}*std):".format(args.sigma))
    for name, stats in baseline["features"].items():
        low, high = stats["normal_range"]
        print(
            f"  {name}: mean={stats['mean']:.5f}, std={stats['std']:.5f}, "
            f"range=[{low:.5f}, {high:.5f}]"
        )
