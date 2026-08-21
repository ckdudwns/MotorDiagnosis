"""
프로토타입 데이터셋 빌더 (AI-1)

CWRU 진동(.mat) + MIMII pump 음향(.wav) 두 소스를 결합해
공통 특징량 추출 → dataset/schema.md 형식에 맞는 통합 레코드로 저장.

두 모달리티는 물리적으로 다른 신호(가속도 vs 음압)이므로 라벨 체계를 다음과 같이 분리한다:
  - source_label   : 원본 데이터셋의 원래 라벨 (예: 97.mat, normal, abnormal)
  - label          : 이번 프로젝트용 통합 라벨
      NORMAL / BEARING_FAULT_INNER / BEARING_FAULT_BALL / BEARING_FAULT_OUTER (진동)
      NORMAL / PUMP_ANOMALY (음향)
  - modality       : "vibration" | "acoustic"

주의: 두 모달리티를 같은 분류기로 바로 합쳐 학습하는 건 권장하지 않음
(센서 종류·물리량이 다름). 이 스크립트는 "같은 파이프라인으로 처리 가능한지" 검증하고,
분석 시 modality 필드로 구분해서 쓰기 위한 목적.

사용법:
    python3 build_prototype_dataset.py \\
        --cwru-dir ./data/external/cwru \\
        --mimii-pump-dir ./data/external/mimii/pump \\
        --output-dir ./data/prototype
"""

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "feature_extraction"))
sys.path.insert(0, os.path.dirname(__file__))

from extract_features import extract_all_features, FeatureConfig  # noqa: E402
from load_cwru_vibration import load_cwru_dataset  # noqa: E402
from load_mimii_acoustic import load_mimii_pump_dataset  # noqa: E402


def build_dataset(
    cwru_dir: str,
    mimii_pump_dir: str,
    output_dir: str,
    cwru_window_size: int = 2048,
    mimii_machine_ids: list = None,
) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    all_records = []

    # 1. 진동 데이터 (CWRU)
    print("=== CWRU 진동 데이터 로드 ===")
    vibration_records = load_cwru_dataset(cwru_dir, window_size=cwru_window_size)

    # 2. 음향 데이터 (MIMII pump)
    print("\n=== MIMII pump 음향 데이터 로드 ===")
    acoustic_records = load_mimii_pump_dataset(mimii_pump_dir, mimii_machine_ids)

    print(
        f"\n진동 샘플: {len(vibration_records)}개, 음향 샘플: {len(acoustic_records)}개"
    )

    # 3. 특징량 추출 (두 모달리티 동일 파이프라인 사용)
    print("\n=== 특징량 추출 ===")
    for record in vibration_records + acoustic_records:
        config = FeatureConfig(sample_rate=record["sample_rate"])
        features = extract_all_features(record["signal"], config)

        combined = {
            "sample_id": record["sample_id"],
            "modality": record["modality"],
            "label": record["label"],
            "source_label": record["source_label"],
            "sample_rate": record["sample_rate"],
            **features,
        }
        all_records.append(combined)

    # 4. 저장
    out_path = os.path.join(output_dir, "features_with_labels.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    # 요약 통계
    summary = {}
    for r in all_records:
        key = f"{r['modality']}:{r['label']}"
        summary[key] = summary.get(key, 0) + 1

    print(f"\n총 {len(all_records)}개 샘플 → {out_path}")
    print("라벨 분포:")
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}개")

    return {
        "n_records": len(all_records),
        "output_path": out_path,
        "label_distribution": summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CWRU 진동 + MIMII 음향 프로토타입 데이터셋 빌드"
    )
    parser.add_argument("--cwru-dir", default="./data/external/cwru")
    parser.add_argument("--mimii-pump-dir", default="./data/external/mimii/pump")
    parser.add_argument("--output-dir", default="./data/prototype")
    parser.add_argument("--cwru-window-size", type=int, default=2048)
    parser.add_argument("--mimii-machine-ids", nargs="*", default=None)
    args = parser.parse_args()

    build_dataset(
        cwru_dir=args.cwru_dir,
        mimii_pump_dir=args.mimii_pump_dir,
        output_dir=args.output_dir,
        cwru_window_size=args.cwru_window_size,
        mimii_machine_ids=args.mimii_machine_ids,
    )
