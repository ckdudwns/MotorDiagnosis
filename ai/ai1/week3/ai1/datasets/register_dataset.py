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
import math
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

# week2 extract_features.py의 특징 추출 로직(계산식/특징 목록)을 식별하는 버전표.
# extract_all_features()의 산출 스키마나 계산식이 바뀌면 반드시 함께 올려야
# 체크섬이 "같은 원본에서 다른 특징값이 나온" 상황을 잡아낼 수 있다.
FEATURE_PIPELINE_VERSION = "week2.extract_all_features.v1"


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


class InsufficientAssetGroupsError(ValueError):
    """라벨의 독립 그룹(source_file/자산) 수가 요청한 분할 수보다 적어,
    그룹을 쪼개지 않고는(=리크 없이는) 분할을 만들 수 없을 때 발생한다."""


_REQUIRED_SPLIT_KEYS = ("train", "validation", "test")


def validate_split_ratios(ratios: dict) -> None:
    """분할 비율의 키/타입/범위/합계를 매니페스트 생성 전에 검증한다.

    예를 들어 {train: 0.8, validation: 0.3, test: 0.1}처럼 합이 1.0이 아니면
    조용히 통과시키지 않고, 전부 0이면(group_split 내부에서 `max() iterable is
    empty`로 불명확하게 죽는 대신) 여기서 먼저 명확한 입력 오류로 거부한다.
    합이 1.0이면 range(0~1) 검증과 합쳐 최소 하나는 항상 양수임이 보장된다.
    """
    if not isinstance(ratios, dict):
        raise ValueError(f"split_ratios는 dict여야 합니다: {ratios!r}")

    missing = [key for key in _REQUIRED_SPLIT_KEYS if key not in ratios]
    if missing:
        raise ValueError(f"split_ratios에 필수 키가 없습니다: {missing}")

    extra = sorted(set(ratios) - set(_REQUIRED_SPLIT_KEYS))
    if extra:
        # holdout처럼 쓰이지 않는 키가 섞여 있으면, 실제 결과(group_split)는
        # 동일한데도 체크섬 payload에 그 키가 포함돼 다른 dataset id가 나온다.
        raise ValueError(
            f"split_ratios에 허용되지 않은 키가 있습니다: {extra} "
            f"(허용 키: {list(_REQUIRED_SPLIT_KEYS)})"
        )

    for key in _REQUIRED_SPLIT_KEYS:
        value = ratios[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"split_ratios[{key!r}]는 숫자여야 합니다: {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"split_ratios[{key!r}]는 유한한 값이어야 합니다: {value!r}")
        if not (0 <= value <= 1):
            raise ValueError(f"split_ratios[{key!r}]는 0~1 범위여야 합니다: {value!r}")

    total = sum(ratios[key] for key in _REQUIRED_SPLIT_KEYS)
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"split_ratios 합계는 1.0이어야 합니다 (현재 {total}): {ratios!r}")


def group_split(
    records: list, ratios: dict = None, seed: int = 42, group_key: str = "source_label"
) -> list:
    """라벨별로 `group_key`(기본: 원본 파일명) 단위 그룹을 통째로 하나의
    split에만 배정하는 그룹 분할.

    동일 그룹(예: 같은 원본 .mat 파일)의 윈도우가 train/validation/test에
    나뉘어 들어가면 모델이 그룹 고유 특성(센서 개체차, 노이즈 지문 등)을
    외워 검증 지표가 부풀려지는 데이터 누수가 생긴다. 그래서 윈도우를
    섞지 않고 그룹 단위로만 분할한다.

    라벨 하나에 그룹이 비율 개수(예: train/validation/test 3개)보다 적으면
    그룹을 쪼개지 않는 한 리크 없이 분할을 만들 수 없다 — 이 경우 조용히
    윈도우 단위로 섞는 대신 `InsufficientAssetGroupsError`를 발생시켜
    "데이터 부족" 상태를 명시적으로 드러낸다 (근거: dataset_manifest_format.md
    "분할 전략"). CWRU는 현재 라벨당 자산이 1개뿐이라 기본 3-way 분할에서는
    이 예외가 발생하는 것이 정상이며, 자산이 늘어나거나 train 전용 등
    분할 비율을 조정해야 해소된다.
    """
    ratios = DEFAULT_SPLIT_RATIOS if ratios is None else ratios
    validate_split_ratios(ratios)
    required_splits = [
        name for name in ("train", "validation", "test") if ratios.get(name, 0) > 0
    ]

    by_label_groups: dict = {}
    for idx, rec in enumerate(records):
        label_groups = by_label_groups.setdefault(rec["label"], {})
        label_groups.setdefault(rec[group_key], []).append(idx)

    split_of_index = {}
    for label, groups in by_label_groups.items():
        if len(groups) < len(required_splits):
            raise InsufficientAssetGroupsError(
                f"라벨 {label!r}: 독립 그룹이 {len(groups)}개({sorted(groups)})뿐이라 "
                f"{required_splits} {len(required_splits)}-way 그룹 분할을 리크 없이 "
                "만들 수 없습니다. 자산을 추가하거나 분할 비율(ratios)을 조정하세요."
            )

        rng = random.Random(f"{seed}-{label}")
        group_ids = list(groups.keys())
        rng.shuffle(group_ids)  # 동일 크기 그룹 간 배정 순서만 흔들어 결정성 유지

        total = sum(len(indices) for indices in groups.values())
        targets = {name: ratios[name] * total for name in required_splits}
        assigned: dict = {name: [] for name in required_splits}
        assigned_counts = {name: 0 for name in required_splits}

        # 1단계: 그룹을 쪼개지 않고도 모든 필수 split이 최소 1개 그룹을 받도록
        # 가장 비율이 작은 split부터 가장 작은 남은 그룹을 배정해 둔다.
        remaining = sorted(group_ids, key=lambda g: len(groups[g]))
        for name in sorted(required_splits, key=lambda n: ratios[n]):
            group_id = remaining.pop(0)
            assigned[name].append(group_id)
            assigned_counts[name] += len(groups[group_id])

        # 2단계: 남은 그룹은 목표 건수 대비 부족분(deficit)이 가장 큰 split에
        # 큰 그룹부터 배정하는 그리디로 비율에 최대한 맞춘다.
        remaining.sort(key=lambda g: -len(groups[g]))
        for group_id in remaining:
            best = max(required_splits, key=lambda n: targets[n] - assigned_counts[n])
            assigned[best].append(group_id)
            assigned_counts[best] += len(groups[group_id])

        for name, group_list in assigned.items():
            for group_id in group_list:
                for idx in groups[group_id]:
                    split_of_index[idx] = name

    return [split_of_index[i] for i in range(len(records))]


def compute_feature_output_fingerprint(rows: list) -> str:
    """실제로 계산된 특징값 산출물 자체의 canonical hash.

    week2 `extract_all_features()`의 MFCC는 librosa가 없으면 조용히 0벡터로
    대체된다(`compute_mfcc()` fallback). 이 차이는 소스 코드/설정 어디에도
    드러나지 않으므로, feature_pipeline_version이나 FeatureConfig만으로는
    같은 원본에서 실제 MFCC 값과 0벡터가 나온 두 실행을 구분할 수 없다.
    그래서 메타데이터가 아니라 **최종 산출된 특징값 자체**를 해시해, 실행
    환경 차이로 산출물이 달라지면 반드시 체크섬도 달라지게 한다.
    """
    payload = [
        {name: row[name] for name in sorted(row)} for row in rows
    ]
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def compute_version_checksum(
    source_files: dict,
    window_size: int,
    hop_size: int,
    split_ratios: dict,
    seed: int,
    label_taxonomy_version: str,
    label_mapping: dict,
    feature_config: FeatureConfig,
    feature_output_fingerprint: str,
    feature_pipeline_version: str = FEATURE_PIPELINE_VERSION,
) -> str:
    """원본 파일·라벨·전처리/분할/특징 추출 설정 + 실제 산출물로 불변 버전 체크섬을 만든다.

    입력 파일 sha256 + **파일별 source label**, window/hop 크기, 분할 비율,
    seed, label taxonomy 버전, label mapping, 특징 추출 설정(FeatureConfig:
    sample_rate/frame_length/hop_length/n_mfcc/band_edges), 파이프라인 버전,
    **실제 계산된 특징값의 fingerprint(compute_feature_output_fingerprint())**
    중 하나라도 달라지면 다른 체크섬이 나와야 한다. feature_output_fingerprint를
    포함해야 librosa 유무처럼 소스 코드/설정에는 드러나지 않는 실행 환경
    차이(MFCC 0벡터 폴백 등)로 산출물이 달라진 경우까지 잡아낼 수 있다.
    """
    payload = {
        "files": {
            name: {"sha256": info["sha256"], "label": info["label"]}
            for name, info in sorted(source_files.items())
        },
        "window_size": window_size,
        "hop_size": hop_size,
        "split_ratios": {name: split_ratios[name] for name in sorted(split_ratios)},
        "seed": seed,
        "label_taxonomy_version": label_taxonomy_version,
        "label_mapping": {name: label_mapping[name] for name in sorted(label_mapping)},
        "feature_pipeline_version": feature_pipeline_version,
        "feature_config": {
            "sample_rate": feature_config.sample_rate,
            "frame_length": feature_config.frame_length,
            "hop_length": feature_config.hop_length,
            "n_mfcc": feature_config.n_mfcc,
            "band_edges": list(feature_config.band_edges),
        },
        "feature_output_fingerprint": feature_output_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_manifest(
    data_dir: str = _DEFAULT_DATA_DIR,
    window_size: int = 2048,
    hop_size: int = 2048,
    split_ratios: dict = None,
    seed: int = 42,
) -> dict:
    """CWRU 데이터를 로드해 DATA_EXPORT_01 매니페스트(dict)를 만든다."""
    split_ratios = DEFAULT_SPLIT_RATIOS if split_ratios is None else split_ratios
    validate_split_ratios(split_ratios)

    records = load_cwru_dataset(data_dir, window_size=window_size, hop_size=hop_size)
    if not records:
        raise FileNotFoundError(f"{data_dir}에서 CWRU 레코드를 하나도 로드하지 못했습니다.")

    source = build_source_block(data_dir)
    config = FeatureConfig(sample_rate=records[0]["sample_rate"])
    compatibility = build_compatibility_block(records)
    splits = group_split(records, split_ratios, seed)

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

    version_checksum = compute_version_checksum(
        source["files"],
        window_size,
        hop_size,
        split_ratios,
        seed,
        LABEL_TAXONOMY_VERSION,
        DATASET_LABEL_MAPPING,
        config,
        compute_feature_output_fingerprint(rows),
    )
    source["checksum"] = version_checksum

    split_counts = {"train": 0, "validation": 0, "test": 0}
    for split in splits:
        split_counts[split] += 1

    version_short_hash = version_checksum.split(":", 1)[1][:12]
    return {
        "id": (
            f"DS-CWRU-VIBRATION-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
            f"-{version_short_hash}"
        ),
        "name": "cwru-bearing-vibration-v1",
        "source": source,
        "compatibility": compatibility,
        "labelTaxonomyVersion": LABEL_TAXONOMY_VERSION,
        "labelMapping": DATASET_LABEL_MAPPING,
        "split": split_ratios,
        "splitStrategy": (
            "group_split_by_source_file (per-label; whole source_label groups are "
            "assigned to a single split — never split at window level to avoid leakage)"
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
        "--train-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["train"]
    )
    parser.add_argument(
        "--validation-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["validation"]
    )
    parser.add_argument(
        "--test-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["test"]
    )
    parser.add_argument(
        "--output",
        default=os.path.normpath(
            os.path.join(_THIS_DIR, "..", "data", "handoff", "dataset_manifest_full.json")
        ),
    )
    args = parser.parse_args()

    # 기본 비율(0.7/0.2/0.1)은 라벨당 자산이 여러 개일 때를 전제로 한다. 지금처럼
    # CWRU가 라벨당 자산 1개뿐이면 group_split이 InsufficientAssetGroupsError를
    # 낸다 — 조용히 window 셔플로 우회하지 않고, 자산을 추가하거나
    # --train-ratio 1 --validation-ratio 0 --test-ratio 0 처럼 명시적으로
    # train 전용 비율을 지정해야 한다.
    manifest = build_manifest(
        data_dir=args.data_dir,
        window_size=args.window_size,
        hop_size=args.hop_size,
        split_ratios={
            "train": args.train_ratio,
            "validation": args.validation_ratio,
            "test": args.test_ratio,
        },
        seed=args.seed,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"데이터셋 {manifest['id']} 매니페스트 생성 완료: {manifest['rowCount']}행")
    print(f"  분할 건수: {manifest['splitCounts']}")
    print(f"  저장 위치: {args.output}")
