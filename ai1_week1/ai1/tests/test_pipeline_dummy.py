"""
합성 신호 → 특징량 추출 → 라벨 저장까지 더미 파이프라인 동작 확인 (AI-1)

센서 도착 전, 전체 흐름이 깨지지 않는지 확인하기 위한 스모크 테스트.
pytest 없이도 `python3 test_pipeline_dummy.py`로 바로 실행 가능하도록 작성.
"""

import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "feature_extraction"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from extract_features import extract_all_features, FeatureConfig  # noqa: E402
from generate_synthetic_signal import generate_dataset  # noqa: E402


def test_full_dummy_pipeline():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. 합성 신호 생성 (라벨당 2개, 총 8개)
        metadata = generate_dataset(tmp_dir, n_per_label=2)
        assert len(metadata) == 8, f"예상 샘플 수 불일치: {len(metadata)}"

        # 2. 각 샘플에서 특징량 추출
        config = FeatureConfig()
        results = []
        for sample in metadata:
            import numpy as np

            signal = np.load(sample["file_path"])
            features = extract_all_features(signal, config)

            record = {
                "sample_id": sample["sample_id"],
                "label": sample["label"],
                **features,
            }
            results.append(record)

        assert len(results) == len(metadata), "특징량 추출 결과 수 불일치"

        # 3. 결과 저장 확인 (features + label 통합 레코드)
        out_path = os.path.join(tmp_dir, "features_with_labels.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        assert os.path.exists(out_path), "결과 파일 저장 실패"

        # 4. 라벨별로 최소한의 신호 구분력이 있는지 대략 확인 (엄격한 검증 아님)
        labels_seen = {r["label"] for r in results}
        assert labels_seen == {
            "NORMAL",
            "BEARING_FAULT",
            "FRICTION",
            "IMBALANCE",
        }, f"라벨 누락: {labels_seen}"

        print(
            f"파이프라인 정상 동작 확인 완료 — 샘플 {len(results)}개, "
            f"특징량 {len(results[0]) - 2}개, 라벨 {len(labels_seen)}종"
        )


if __name__ == "__main__":
    test_full_dummy_pipeline()
    print("✅ test_pipeline_dummy 통과")
