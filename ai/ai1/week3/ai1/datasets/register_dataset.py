"""
기존 데이터셋 등록 매니페스트 빌더 (AI-1, 3주차 DATA_EXPORT_01)

CWRU Bearing Dataset(1주차 인계 경로의 .mat 4개)을 API 명세서 v1.2
`09_보완API상세` 시트의 `POST /api/datasets`(MVP-042) 계약에 맞춰 정규화한
불변 데이터셋 버전 매니페스트로 만든다. 필드 정의와 분할 전략의 근거는
`dataset_manifest_format.md`를 참고한다.

원본 데이터/로더는 1주차 경로(`ai/ai1/week1/ai1/`)의 것을 그대로 재사용하고
복제하지 않는다. 특징값 계산도 week2의 `extract_all_features()`를 그대로
재사용해 로직을 중복 구현하지 않는다.
"""

import os
import sys
import json
import random
import hashlib
import argparse
from datetime import datetime, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK2_FEATURE_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week2", "ai1", "feature_extraction")
)
_DEFAULT_DATA_DIR = os.path.normpath(
    os.path.join(
        _THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru"
    )
)

sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, _WEEK2_FEATURE_DIR)

from load_cwru_vibration import load_cwru_dataset, FILE_LABEL_MAP  # noqa: E402
from extract_features import extract_all_features, FeatureConfig  # noqa: E402

CWRU_SOURCE_URI = "https://engineering.case.edu/bearingdatacenter/welcome"
CWRU_LICENSE_NOTE = (
    "Case Western Reserve University Bearing Data Center — 학술/연구 목적 "
    "공개 데이터셋. 재배포 시 출처 표기가 필요하다 (2026-08-24 확인)."
)

# DATA_EXPORT_01 labelMapping: CWRU 원본 라벨 -> 공통 학습 라벨.
# EVENT_LABEL_01(../event_labels/event_label.py)의 seed_label_from_dataset()이
# 이 값을 그대로 재사용해 리플레이 이벤트의 초기 운영자 라벨을 채운다.
DATASET_LABEL_MAPPING = {
    "NORMAL": "NORMAL",
    "BEARING_FAULT_INNER": "ANOMALY",
    "BEARING_FAULT_BALL": "ANOMALY",
    "BEARING_FAULT_OUTER": "ANOMALY",
}

LABEL_TAXONOMY_VERSION = "CWRU-FAULT-V1"
DEFAULT_SPLIT_RATIOS = {"train": 0.7, "validation": 0.2, "test": 0.1}


def sha256_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    """원본 파일 체크섬(sha256) 계산 — 매니페스트의 source.files에 저장해 원본 추적에 쓴다."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_source_block(data_dir: str) -> dict:
    """실제로 배치된 CWRU .mat 파일만 골라 체크섬과 라벨을 기록한다."""
    files = {}
    for filename, label in FILE_LABEL_MAP.items():
        file_path = os.path.join(data_dir, filename)
        if not os.path.exists(file_path):
            continue
        files[filename] = {"sha256": sha256_of_file(file_path), "label": label}

    if not files:
        raise FileNotFoundError(
            f"{data_dir}에 CWRU .mat 파일이 하나도 없습니다. "
            "ai/ai1/week1/ai1/README.md의 데이터 배치 안내를 참고하세요."
        )

    return {
        "type": "external",
        "uri": CWRU_SOURCE_URI,
        "license": CWRU_LICENSE_NOTE,
        "files": files,
    }


def build_compatibility_block(records: list) -> dict:
    """레코드의 실측 sample_rate/rpm으로 compatibility 블록을 만든다."""
    sample_rate = records[0]["sample_rate"]
    rpms = [r["rpm"] for r in records if r.get("rpm") is not None]
    rpm_range = [min(rpms), max(rpms)] if rpms else None

    return {
        "signalType": ["vibration"],
        "samplingRateHz": sample_rate,
        "units": {"vibration": "g (raw accelerometer output, uncalibrated)"},
        "operatingConditions": {
            "rpmRange": rpm_range,
            "load": "CWRU 모터 시험대, 파일별 부하 조건 상이 (원본 메타데이터 RPM 기준)",
        },
    }


def stratified_split(
    records: list, ratios: dict = None, seed: int = 42
) -> list:
    """라벨(known_label)별로 독립 셔플 후 비율 배분하는 층화 분할.

    CWRU는 라벨당 실제 자산(파일)이 1개뿐이라 설비 단위(group) 분할을 쓰면
    한쪽 split에 특정 라벨이 아예 사라진다. 그래서 윈도우 단위 층화 분할을
    쓴다 (근거: dataset_manifest_format.md "분할 전략").
    반올림 오차는 test 분할이 흡수해 각 라벨 그룹 크기와 정확히 합이 맞는다.

    TODO: 실제 현장 데이터처럼 라벨당 자산이 여러 대가 되면 설비 단위
    group split으로 전환해야 한다 (동일 자산이 train/test에 동시에 들어가면 데이터 누수).
    """
    ratios = ratios or DEFAULT_SPLIT_RATIOS
    by_label: dict = {}
    for idx, rec in enumerate(records):
        by_label.setdefault(rec["label"], []).append(idx)

    split_of_index = {}
    for label, indices in by_label.items():
        rng = random.Random(f"{seed}-{label}")
        shuffled = list(indices)
        rng.shuffle(shuffled)

        n = len(shuffled)
        n_train = round(n * ratios["train"])
        n_train = min(n_train, n)
        n_val = round(n * ratios["validation"])
        n_val = min(n_val, n - n_train)
        n_test = n - n_train - n_val  # 잔여를 test가 흡수 -> 합이 항상 n과 일치

        for i in shuffled[:n_train]:
            split_of_index[i] = "train"
        for i in shuffled[n_train : n_train + n_val]:
            split_of_index[i] = "validation"
        for i in shuffled[n_train + n_val :]:
            split_of_index[i] = "test"

    return [split_of_index[i] for i in range(len(records))]


def build_manifest(
    data_dir: str = _DEFAULT_DATA_DIR,
    window_size: int = 2048,
    hop_size: int = 2048,
    split_ratios: dict = None,
    seed: int = 42,
) -> dict:
    """CWRU 데이터를 로드해 DATA_EXPORT_01 매니페스트(dict)를 만든다."""
    split_ratios = split_ratios or DEFAULT_SPLIT_RATIOS

    records = load_cwru_dataset(data_dir, window_size=window_size, hop_size=hop_size)
    if not records:
        raise FileNotFoundError(f"{data_dir}에서 CWRU 레코드를 하나도 로드하지 못했습니다.")

    source = build_source_block(data_dir)
    compatibility = build_compatibility_block(records)
    splits = stratified_split(records, split_ratios, seed)

    config = FeatureConfig(sample_rate=records[0]["sample_rate"])
    rows = []
    for rec, split in zip(records, splits):
        known_label = rec["label"]
        common_label = DATASET_LABEL_MAPPING[known_label]
        features = extract_all_features(rec["signal"], config)
        row = {
            "sample_id": rec["sample_id"],
            "source_file": rec["source_label"],
            "known_label": known_label,
            "common_label": common_label,
            "split": split,
            "sample_rate_hz": rec["sample_rate"],
            "rpm": rec.get("rpm"),
            **features,
        }
        rows.append(row)

    split_counts = {"train": 0, "validation": 0, "test": 0}
    for split in splits:
        split_counts[split] += 1

    return {
        "id": f"DS-CWRU-VIBRATION-{datetime.now(timezone.utc).strftime('%Y%m%d')}",
        "name": "cwru-bearing-vibration-v1",
        "source": source,
        "compatibility": compatibility,
        "labelTaxonomyVersion": LABEL_TAXONOMY_VERSION,
        "labelMapping": DATASET_LABEL_MAPPING,
        "split": split_ratios,
        "splitStrategy": (
            "stratified_by_label (window-level; group-by-asset not applicable — "
            "CWRU provides a single asset per fault label)"
        ),
        "status": "draft",
        "reason": "3주차 기존 데이터셋 선정 및 정규화 — AI_FREQ_MODEL_01 선행학습 입력 준비",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "rowCount": len(rows),
        "splitCounts": split_counts,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CWRU 데이터셋을 DATA_EXPORT_01 매니페스트로 정규화"
    )
    parser.add_argument("--data-dir", default=_DEFAULT_DATA_DIR)
    parser.add_argument("--window-size", type=int, default=2048)
    parser.add_argument("--hop-size", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output",
        default=os.path.normpath(
            os.path.join(_THIS_DIR, "..", "data", "handoff", "dataset_manifest_full.json")
        ),
    )
    args = parser.parse_args()

    manifest = build_manifest(
        data_dir=args.data_dir,
        window_size=args.window_size,
        hop_size=args.hop_size,
        seed=args.seed,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"데이터셋 {manifest['id']} 매니페스트 생성 완료: {manifest['rowCount']}행")
    print(f"  분할 건수: {manifest['splitCounts']}")
    print(f"  저장 위치: {args.output}")
