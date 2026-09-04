"""
기존 데이터셋 등록 매니페스트 빌더 (AI-1, 3주차 DATA_EXPORT_01)

CWRU Bearing Dataset(1주차 인계 경로의 .mat 4개)을 API 명세서 v1.3
`09_보완API상세` 시트의 `POST /api/datasets`(MVP-042) 계약에 맞춰 정규화한
불변 데이터셋 버전 매니페스트로 만든다 — v1.3의 `labelPolicyVersion`/
`snapshotSchemaVersion`과 export 행 라벨 상태까지 반영한다. 필드 정의와 분할
전략의 근거는 `dataset_manifest_format.md`를 참고한다.

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
import importlib.util
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


def _import_module_from_path(module_name: str, file_path: str):
    """모듈 이름이 아니라 파일 경로로 정확히 특정해 import한다.

    week1(`week1/ai1/feature_extraction/`)과 week2(`week2/ai1/feature_extraction/`)
    모두 `extract_features.py`라는 동일한 이름의 모듈을 갖고 있다. 같은
    프로세스에서 week1 쪽이 먼저 평범한 `from extract_features import ...`로
    import되면(예: week1 테스트가 먼저 수집·실행됨) "extract_features"라는
    이름이 sys.modules에 캐시되고, 이후 여기서 week2 디렉터리를 sys.path
    앞쪽에 넣고 같은 이름으로 import해도 그 캐시된 week1 모듈이 조용히
    재사용된다 — sys.path 순서는 sys.modules 캐시에 이미 있는 이름에는
    영향을 주지 못한다. 그 결과 week2 전용 특징(kurtosis 등)이 빠진
    week1 버전이 실제 판정에 쓰이는데도 아무 오류가 나지 않는다. 고유한
    이름으로 파일 경로를 직접 지정해 로드하면 이 충돌을 원천적으로 피한다.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_week2_extract_features = _import_module_from_path(
    "ai1_week3_register_dataset.week2_extract_features",
    os.path.join(_WEEK2_FEATURE_DIR, "extract_features.py"),
)
extract_all_features = _week2_extract_features.extract_all_features
FeatureConfig = _week2_extract_features.FeatureConfig

# week1 build_ai1_handoff_dataset.compute_peak_frequency(피크 주파수)도 같은
# 이유(모듈명 충돌 회피)로 파일 경로로 명시 로드한다. 이 모듈의 top-level
# import(load_cwru_vibration / load_mimii_acoustic)는 모두 guarded라 안전하다.
_week1_build_handoff = _import_module_from_path(
    "ai1_week3_register_dataset.week1_build_handoff",
    os.path.join(_WEEK1_SCRIPTS_DIR, "build_ai1_handoff_dataset.py"),
)
compute_peak_frequency = _week1_build_handoff.compute_peak_frequency

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

# 특징 추출 로직(계산식/특징 목록)을 식별하는 버전표. 산출 스키마나 계산식이
# 바뀌면 반드시 함께 올려야 체크섬이 "같은 원본에서 다른 특징값이 나온"
# 상황을 잡아낸다. v1(week2 extract_all_features 26개) -> peak_hz 추가로 v2
# (week2 26개 + week1 compute_peak_frequency = vibration_peak_hz, 총 27개).
FEATURE_PIPELINE_VERSION = "week2.extract_all_features+week1.peak_hz.v2"

# 매니페스트 rows에 붙이는 피크 주파수 특징 이름 (week4 features.py와 동일).
PEAK_FEATURE_NAME = "vibration_peak_hz"

# API 명세서 v1.3 `05_데이터모델`의 라벨 정책·snapshot 스키마 버전.
# 신규 데이터셋은 이 값을 fingerprint(compute_version_checksum)와 manifest에 포함해
# 동결한다 — 정책이 바뀌면 다른 id가 나온다. 기존 frozen 데이터셋은 재계산하지 않는다.
LABEL_POLICY_VERSION = "LABEL-POLICY-V2"
SNAPSHOT_SCHEMA_VERSION = "2"

# export 행의 라벨 상태 값 (v1.3 DatasetExportRow.label_status).
#   verified  = 신뢰된 라벨 + taxonomy 매핑 성공 → 지도학습 사용
#   weak      = 미검수 이벤트 후보만 존재 → 학습 제외 (CWRU 공개 데이터셋 경로에서는
#               이벤트가 없어 발생하지 않음. parity 위해 값만 정의)
#   unlabeled = 라벨·후보 없음
#   unmapped  = 신뢰 라벨은 있으나 taxonomy 매핑 실패
LABEL_STATUSES = ("verified", "weak", "unlabeled", "unmapped")

# CWRU known_label의 출처: 공개 데이터셋 파일→라벨 맵(신뢰된 import).
# source/isSynthetic이 아니라 이 검증 가능한 출처로만 supervised target을 승격한다.
DATASET_REGISTRATION_LABEL_SOURCE = "dataset_registration"


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


def _validate_finite_features(features: dict, sample_id: str) -> None:
    """추출된 특징값이 전부 유한한 실수인지 행을 만들기 전에 검증한다.

    NaN/Inf가 그대로 rows에 들어가면 CSV에는 문자열 "nan"이, XLSX(openpyxl)는
    NaN/Inf를 쓸 수 없어 빈 셀로 남아 같은 값이 산출물마다 다르게(그리고
    조용히) 표현된다. 저장 직전에 명시적으로 막아 포맷 불일치를 방지한다.
    """
    for name, value in features.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{sample_id}의 특징값 {name!r}이 숫자가 아닙니다: {value!r}"
            )
        if not math.isfinite(value):
            raise ValueError(
                f"{sample_id}의 특징값 {name!r}이 유한하지 않습니다: {value!r}"
            )


def group_split(
    records: list, ratios: dict = None, seed: int = 42, group_key: str = "source_label"
) -> list:
    """`group_key`(기본: 원본 파일명) 단위 그룹을 통째로 하나의 split에만
    배정하는 그룹 분할.

    동일 그룹(예: 같은 원본 .mat 파일)의 윈도우가 train/validation/test에
    나뉘어 들어가면 모델이 그룹 고유 특성(센서 개체차, 노이즈 지문 등)을
    외워 검증 지표가 부풀려지는 데이터 누수가 생긴다. 그래서 윈도우를
    섞지 않고 그룹 단위로만 분할한다.

    그룹은 **라벨과 무관하게** 전역으로 묶는다 — 같은 자산(group_key)이
    라벨이 다른 레코드를 함께 갖고 있어도(예: 한 설비의 NORMAL 구간과
    ANOMALY 구간) 그 자산 전체가 하나의 split에만 배정돼야 하기 때문이다.
    라벨별로 그룹을 따로 만들면 같은 자산이 라벨에 따라 서로 다른 split에
    배정될 수 있어(예: NORMAL은 train, ANOMALY는 test) 위와 동일한 데이터
    누수가 생긴다.

    라벨 하나에 그 라벨을 포함하는 독립 그룹이 비율 개수(예:
    train/validation/test 3개)보다 적으면 그룹을 쪼개지 않는 한 리크 없이
    분할을 만들 수 없다 — 이 경우 조용히 윈도우 단위로 섞는 대신
    `InsufficientAssetGroupsError`를 발생시켜 "데이터 부족" 상태를 명시적으로
    드러낸다 (근거: dataset_manifest_format.md "분할 전략").

    `build_manifest`는 `group_key="specimen_id"`(물리 베어링)로 이 함수를 호출한다.
    CWRU는 IR/Ball/OR 결함마다 specimen 3개(0.007/0.014/0.021")를 확보했지만
    **NORMAL specimen은 1개뿐**이라(건강 베어링 1개) 기본 3-way 분할에서는 이
    예외가 발생하는 것이 정상이다 — 정직한 실패다. 데모/리포트용으로는
    `operating_condition_split()`(부하조건 기준, 독립 검증 아님)을 opt-in으로 쓴다.
    """
    ratios = DEFAULT_SPLIT_RATIOS if ratios is None else ratios
    validate_split_ratios(ratios)
    required_splits = [
        name for name in ("train", "validation", "test") if ratios.get(name, 0) > 0
    ]

    groups: dict = {}  # group_id -> [record idx, ...] (라벨 무관, 전역)
    group_label_counts: dict = {}  # group_id -> {label: count}
    label_group_ids: dict = {}  # label -> {group_id, ...}
    label_totals: dict = {}  # label -> 전체 레코드 수

    for idx, rec in enumerate(records):
        group_id, label = rec[group_key], rec["label"]
        groups.setdefault(group_id, []).append(idx)
        label_counts = group_label_counts.setdefault(group_id, {})
        label_counts[label] = label_counts.get(label, 0) + 1
        label_group_ids.setdefault(label, set()).add(group_id)
        label_totals[label] = label_totals.get(label, 0) + 1

    for label, group_ids in label_group_ids.items():
        if len(group_ids) < len(required_splits):
            raise InsufficientAssetGroupsError(
                f"라벨 {label!r}: 독립 그룹이 {len(group_ids)}개({sorted(group_ids)})뿐이라 "
                f"{required_splits} {len(required_splits)}-way 그룹 분할을 리크 없이 "
                "만들 수 없습니다. 자산을 추가하거나 분할 비율(ratios)을 조정하세요."
            )

    targets = {
        label: {name: ratios[name] * total for name in required_splits}
        for label, total in label_totals.items()
    }
    assigned_counts = {
        label: {name: 0 for name in required_splits} for label in label_totals
    }
    split_of_group: dict = {}

    def assign(group_id: str, split_name: str) -> None:
        split_of_group[group_id] = split_name
        for label, count in group_label_counts[group_id].items():
            assigned_counts[label][split_name] += count

    # 1단계: 그룹을 쪼개지 않고도 모든 (라벨, 필수 split) 조합이 최소 1개
    # 그룹을 받도록 배정한다. coverage_pairs는 비율이 작은 split부터 처리
    # 되도록 정렬한다(이미 배정된 그룹이 해당 라벨을 포함하면(공유 그룹) 그
    # 조합은 이미 충족된 것으로 보고 건너뛴다).
    #
    # 각 pair를 되돌릴 수 없는 그리디로 독립적으로 처리하면, 그룹이 서로
    # 다른 라벨 쌍을 사슬처럼 공유하는 구조에서는 실제로 가능한 배정이
    # 있어도 특정 처리 순서 때문에 막다른 길에 몰릴 수 있다(예: 한 라벨의
    # 그룹 두 개가 각각 다른 라벨의 요구를 채우느라 같은 split에 몰려
    # 배정되면, 정작 그 라벨 자신은 나머지 split을 커버할 그룹이 남지
    # 않는다). 그래서 pair마다 결정적으로 정렬한 후보를 시도하다 막히면
    # 이전 선택으로 되돌아가는 백트래킹을 쓴다 — 가능한 배정이 존재하면
    # 반드시 찾아낸다.
    coverage_pairs = [
        (label, name) for label in label_group_ids for name in required_splits
    ]
    coverage_pairs.sort(key=lambda pair: ratios[pair[1]])

    def _candidate_order(label: str) -> list:
        # 비공유(전용) 그룹, 작은 그룹, gid 순으로 결정적으로 정렬한다.
        # gid를 마지막 tie-break로 넣어야 label_group_ids[label](set)의
        # PYTHONHASHSEED에 따라 달라지는 반복 순서가 결과(따라서 split 배정과
        # dataset fingerprint)에 영향을 주지 않는다 — 동일한 PYTHONHASHSEED
        # 없이도 매번 같은 records/seed에 대해 같은 split이 나와야 한다.
        return sorted(
            label_group_ids[label],
            key=lambda gid: (len(group_label_counts[gid]) > 1, len(groups[gid]), gid),
        )

    def _backtrack(pair_index: int, coverage_assignment: dict) -> bool:
        if pair_index == len(coverage_pairs):
            return True
        label, name = coverage_pairs[pair_index]
        if any(
            coverage_assignment.get(gid) == name for gid in label_group_ids[label]
        ):
            return _backtrack(pair_index + 1, coverage_assignment)
        for gid in _candidate_order(label):
            if gid in coverage_assignment:
                continue
            coverage_assignment[gid] = name
            if _backtrack(pair_index + 1, coverage_assignment):
                return True
            del coverage_assignment[gid]
        return False

    coverage_assignment: dict = {}
    if not _backtrack(0, coverage_assignment):
        # 그룹 수 자체는 라벨별로 충분해도(위 사전 검사 통과), 그룹을
        # 공유하는 구조상 모든 (라벨, split) 조합을 리크 없이 동시에
        # 커버하는 배정이 아예 존재하지 않을 수 있다 — 조용히 건너뛰지
        # 않고 명시적으로 알린다.
        raise InsufficientAssetGroupsError(
            "라벨/그룹 공유 구조상 모든 (라벨, split) 조합을 그룹을 쪼개지 "
            f"않고 리크 없이 커버하는 배정을 찾을 수 없습니다: {coverage_pairs!r}"
        )
    for gid, name in coverage_assignment.items():
        assign(gid, name)

    # 2단계: 남은 그룹은 그 그룹이 걸친 모든 라벨의 목표 건수 대비 부족분
    # 합이 가장 큰 split에 큰 그룹부터 배정하는 그리디로 비율에 최대한
    # 맞춘다.
    rng = random.Random(seed)
    remaining = [gid for gid in groups if gid not in split_of_group]
    rng.shuffle(remaining)  # 동일 크기 그룹 간 배정 순서만 흔들어 결정성 유지
    remaining.sort(key=lambda gid: -len(groups[gid]))

    for group_id in remaining:
        def deficit(split_name: str, group_id: str = group_id) -> float:
            return sum(
                targets[label][split_name] - assigned_counts[label][split_name]
                for label in group_label_counts[group_id]
            )

        best = max(required_splits, key=deficit)
        assign(group_id, best)

    split_of_index = {}
    for group_id, split_name in split_of_group.items():
        for idx in groups[group_id]:
            split_of_index[idx] = split_name

    return [split_of_index[i] for i in range(len(records))]


# 부하조건(0/1/2/3 HP) → split 고정 배정. test는 가장 낮은 부하(0HP), validation은
# 1HP, train은 2·3HP. 결정적(seed 무관)이며 각 부하 tier의 윈도우는 통째로 한 split에만
# 들어간다.
_OPERATING_CONDITION_SPLIT_MAP = {0: "test", 1: "validation", 2: "train", 3: "train"}


def operating_condition_split(records: list, ratios: dict = None) -> list:
    """부하조건(`load_hp`) 기준으로 split을 고정 배정한다 — **specimen 독립 아님**.

    같은 물리 베어링(specimen)이 모든 부하조건에 걸쳐 있으므로 이 분할에서는 같은
    specimen이 train/validation/test에 함께 들어간다(누수). CWRU는 건강한 베어링이
    1개뿐이라 specimen 독립 holdout 자체가 불가능해서, 데모/리포트용으로만 이 분할을
    opt-in으로 쓴다. 매니페스트에 `independentHoldout=False`가 붙는다.

    `ratios`는 시그니처 호환을 위해 받지만 무시한다(부하 tier가 배정을 결정).
    """
    del ratios  # 부하 tier 배정이 우선. 시그니처 호환용으로만 받는다.
    missing = [
        rec.get("sample_id") for rec in records if rec.get("load_hp") is None
    ]
    if missing:
        raise ValueError(
            "operating_condition_split에는 레코드마다 load_hp(0~3)가 필요합니다. "
            f"누락: {missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
    unknown = sorted(
        {rec["load_hp"] for rec in records} - set(_OPERATING_CONDITION_SPLIT_MAP)
    )
    if unknown:
        raise ValueError(f"알 수 없는 load_hp 값: {unknown} (허용: 0~3).")
    return [_OPERATING_CONDITION_SPLIT_MAP[rec["load_hp"]] for rec in records]


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
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
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
    label_policy_version: str = LABEL_POLICY_VERSION,
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION,
    split_strategy: str = "specimen_group",
) -> str:
    """원본 파일·라벨·전처리/분할/특징 추출 설정 + 실제 산출물로 불변 버전 체크섬을 만든다.

    입력 파일 sha256 + **파일별 source label**, window/hop 크기, 분할 비율,
    seed, label taxonomy 버전, label mapping, 특징 추출 설정(FeatureConfig:
    sample_rate/frame_length/hop_length/n_mfcc/band_edges), 파이프라인 버전,
    라벨 정책·snapshot 스키마 버전(API 명세서 v1.3),
    **실제 계산된 특징값의 fingerprint(compute_feature_output_fingerprint())**
    중 하나라도 달라지면 다른 체크섬이 나와야 한다. feature_output_fingerprint를
    포함해야 librosa 유무처럼 소스 코드/설정에는 드러나지 않는 실행 환경
    차이(MFCC 0벡터 폴백 등)로 산출물이 달라진 경우까지 잡아낼 수 있다.
    label_policy_version을 포함해야 라벨 정책이 바뀐 신규 데이터셋이 기존 frozen
    버전과 같은 id를 재사용하지 않는다.
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
        "label_policy_version": label_policy_version,
        "snapshot_schema_version": snapshot_schema_version,
        "split_strategy": split_strategy,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# export 행에 붙는 v1.3 DatasetExportRow 라벨 필드의 고정 순서(CSV/XLSX 컬럼 순서).
DATASET_EXPORT_LABEL_FIELDS = (
    "scenario_label",
    "known_vibration_label",
    "known_acoustic_label",
    "ground_truth_label",
    "ground_truth_source",
    "target_label",
    "target_label_taxonomy_version",
    "label_status",
    "training_eligible",
    "event_reviewed",
)


def dataset_export_label_fields(
    row: dict, label_mapping: dict, taxonomy_version: str
) -> dict:
    """CWRU 매니페스트 행 하나에 대한 v1.3 라벨 필드 dict를 만든다.

    CWRU의 ``known_label``은 공개 데이터셋 파일→라벨 맵에서 온 **신뢰된 외부 라벨**
    (검증 가능한 출처: ``dataset_registration``)이므로, taxonomy 매핑에 성공한 행은
    전부 ``label_status="verified"`` / ``training_eligible=True``다. 미검수 이벤트·모델
    판정 같은 약한 후보는 이 경로에 존재하지 않는다.

    - ``known_label`` ∈ ``label_mapping``  → verified, target_label = 공통 라벨
    - ``known_label`` 있으나 매핑 실패      → unmapped, target_label = None (학습 제외)
    - ``known_label`` 없음/빈값             → unlabeled (학습 제외)
    """
    known_label = str(row.get("known_label") or "").strip() or None
    fields = {
        "scenario_label": None,
        "known_vibration_label": None,
        "known_acoustic_label": None,
        "ground_truth_label": known_label,
        "ground_truth_source": (
            DATASET_REGISTRATION_LABEL_SOURCE if known_label else None
        ),
        "target_label": None,
        "target_label_taxonomy_version": None,
        "label_status": "unlabeled",
        "training_eligible": False,
        "event_reviewed": False,
    }
    if known_label is None:
        return fields
    if known_label in label_mapping:
        fields["target_label"] = label_mapping[known_label]
        fields["target_label_taxonomy_version"] = taxonomy_version
        fields["label_status"] = "verified"
        fields["training_eligible"] = True
    else:
        fields["label_status"] = "unmapped"
    return fields


def summarize_dataset_labels(
    rows: list, label_mapping: dict, taxonomy_version: str
) -> dict:
    """행 전체의 라벨 상태 집계 (v1.3 DatasetExportManifest 필드).

    반환: ``{labelCounts: {verified, weak, unlabeled, unmapped},
    trainingEligibleCount: int, trainingEligibleSplitCounts: {train, validation, test}}``
    """
    label_counts = {status: 0 for status in LABEL_STATUSES}
    eligible_split_counts = {"train": 0, "validation": 0, "test": 0}
    eligible_total = 0
    for row in rows:
        info = dataset_export_label_fields(row, label_mapping, taxonomy_version)
        label_counts[info["label_status"]] += 1
        if info["training_eligible"]:
            eligible_total += 1
            split = row.get("split")
            if split in eligible_split_counts:
                eligible_split_counts[split] += 1
    return {
        "labelCounts": label_counts,
        "trainingEligibleCount": eligible_total,
        "trainingEligibleSplitCounts": eligible_split_counts,
    }


# 분할 전략.
#   specimen_group            : 물리 베어링(specimen) 단위 group split. 라벨별로 독립
#                               specimen이 분할 수보다 적으면 InsufficientAssetGroupsError.
#                               CWRU는 NORMAL specimen이 1개뿐이라 기본 3-way에서 실패한다
#                               (정직한 실패). independentHoldout=True.
#   operating_condition_holdout: 부하조건(0/1/2/3 HP) 기준 고정 배정. 같은 specimen이 여러
#                               split에 등장 → specimen 독립 검증이 아님. 데모/리포트 opt-in.
#                               independentHoldout=False.
SPLIT_STRATEGIES = ("specimen_group", "operating_condition_holdout")

_SPLIT_STRATEGY_TEXT = {
    "specimen_group": (
        "specimen_group: 물리 베어링(결함타입+직경, 부하 무관) 단위로 group split. "
        "같은 specimen의 윈도우는 통째로 한 split에만 — specimen-independent holdout."
    ),
    "operating_condition_holdout": (
        "operating_condition_holdout: 부하조건(test=0HP, validation=1HP, train=2·3HP) "
        "기준 고정 배정. 같은 물리 베어링이 train/validation/test에 함께 들어간다 — "
        "specimen 독립 검증이 아니라 운전조건 기준 in-distribution 평가다 "
        "(independentHoldout=False). split 비율은 부하 tier가 결정한다."
    ),
}


def build_manifest(
    data_dir: str = _DEFAULT_DATA_DIR,
    window_size: int = 2048,
    hop_size: int = 2048,
    split_ratios: dict = None,
    seed: int = 42,
    split_strategy: str = "specimen_group",
) -> dict:
    """CWRU 데이터를 로드해 DATA_EXPORT_01 매니페스트(dict)를 만든다.

    split_strategy 기본값은 `specimen_group`(물리 베어링 단위 독립 분할)이다. CWRU는
    NORMAL specimen이 1개뿐이라 기본 3-way에서 `InsufficientAssetGroupsError`가 나는
    것이 정상이다. 데모/리포트가 필요하면 `operating_condition_holdout`를 명시적으로
    지정한다 — 이 경우 결과에 `independentHoldout=False`가 붙는다.
    """
    if split_strategy not in SPLIT_STRATEGIES:
        raise ValueError(
            f"split_strategy는 {SPLIT_STRATEGIES} 중 하나여야 합니다: {split_strategy!r}"
        )
    split_ratios = DEFAULT_SPLIT_RATIOS if split_ratios is None else split_ratios
    validate_split_ratios(split_ratios)

    records = load_cwru_dataset(data_dir, window_size=window_size, hop_size=hop_size)
    if not records:
        raise FileNotFoundError(f"{data_dir}에서 CWRU 레코드를 하나도 로드하지 못했습니다.")

    source = build_source_block(data_dir)
    config = FeatureConfig(sample_rate=records[0]["sample_rate"])
    compatibility = build_compatibility_block(records)

    if split_strategy == "specimen_group":
        splits = group_split(records, split_ratios, seed, group_key="specimen_id")
        holdout_type = "specimen"
        independent_holdout = True
        recorded_split_ratios = split_ratios
        recorded_seed = seed
    else:  # operating_condition_holdout
        # [리뷰 P1] operating_condition_split은 부하 tier로 배정을 고정하고
        # split_ratios 인자를 완전히 무시한다(시그니처 호환용). 그런데 예전
        # 코드는 이 무시된 입력값을 그대로 매니페스트["split"]/체크섬에 기록해서,
        # 예를 들어 train=1.0/validation=test=0.0을 넘겨도 실제 rows에는
        # validation/test가 들어가는데 매니페스트는 "전부 train"이라고 거짓말했다.
        # 실제 적용된(rows 기준) 비율을 아래에서 계산해 기록한다.
        splits = operating_condition_split(records, split_ratios)
        holdout_type = "operating_condition"
        independent_holdout = False
        recorded_split_ratios = None  # split_counts 계산 후 채운다
        # [리뷰 P2] operating_condition_split은 seed를 전혀 쓰지 않는다(부하 tier로
        # 배정이 고정). 그런데 체크섬 payload에는 호출자의 seed가 그대로 들어가서,
        # 실제 rows/split이 완전히 동일해도 seed만 바꾸면 다른 checksum/id가 나왔다
        # (버전 identity가 결과에 영향 없는 입력에 좌우됨). 이 전략에서는 seed를
        # canonical 값(None)으로 정규화해 checksum 입력에서 사실상 제외한다.
        recorded_seed = None

    rows = []
    for rec, split in zip(records, splits):
        known_label = rec["label"]
        common_label = DATASET_LABEL_MAPPING[known_label]
        features = extract_all_features(rec["signal"], config)
        # 기능정의가 특징에 "피크"를 명시하므로 week1 피크 주파수를 결합한다
        # (계산 재구현 없이 compute_peak_frequency 재사용).
        features[PEAK_FEATURE_NAME] = compute_peak_frequency(
            rec["signal"], rec["sample_rate"]
        )
        _validate_finite_features(features, rec["sample_id"])
        row = {
            "sample_id": rec["sample_id"],
            "source_file": rec["source_label"],
            "specimen_id": rec["specimen_id"],
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

    if recorded_split_ratios is None:
        # operating_condition_holdout: 실제로 rows에 반영된(요청 비율과 무관한
        # 고정 tier 배정 결과의) 비율을 계산해 기록한다 — 매니페스트/체크섬이
        # 무시된 요청값이 아니라 실제 데이터를 정직하게 반영하도록.
        total = len(records)
        recorded_split_ratios = {
            name: split_counts[name] / total for name in ("train", "validation", "test")
        }

    feature_output_fingerprint = compute_feature_output_fingerprint(rows)
    version_checksum = compute_version_checksum(
        source["files"],
        window_size,
        hop_size,
        recorded_split_ratios,
        recorded_seed,
        LABEL_TAXONOMY_VERSION,
        DATASET_LABEL_MAPPING,
        config,
        feature_output_fingerprint,
        label_policy_version=LABEL_POLICY_VERSION,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        split_strategy=split_strategy,
    )
    source["checksum"] = version_checksum

    label_summary = summarize_dataset_labels(
        rows, DATASET_LABEL_MAPPING, LABEL_TAXONOMY_VERSION
    )

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
        "labelPolicyVersion": LABEL_POLICY_VERSION,
        "snapshotSchemaVersion": SNAPSHOT_SCHEMA_VERSION,
        # [리뷰 P1, 2차] rows만으로 재계산 가능한 fingerprint. dataset_version.py의
        # freeze_dataset_version()이 동결 직전에 이 값을 rows에서 다시 계산해
        # 대조한다 — build 이후 rows/라벨이 바뀐 draft가 (재계산 없이 복사된) 이전
        # source.checksum/id로 동결·승인되는 것을 막는다.
        "featureOutputFingerprint": feature_output_fingerprint,
        # [리뷰 P1, 3차] compute_version_checksum() payload를 만드는 데 쓰인 나머지
        # 입력(rows/labelMapping만으로는 재현 불가능한 것들)을 그대로 노출한다.
        # week4 dataset_version.freeze_dataset_version()이 이 필드들로 source.checksum/id를
        # 독립 재계산해, rows만 바꾸고 featureOutputFingerprint만 재계산하거나
        # (fingerprint에는 안 들어가지만 checksum에는 들어가는) labelMapping만 바꿔도
        # 예전 source.checksum/id를 그대로 승계해 동결되는 것을 막는다.
        "checksumInputs": {
            "windowSize": window_size,
            "hopSize": hop_size,
            "seed": recorded_seed,
            "featurePipelineVersion": FEATURE_PIPELINE_VERSION,
            "featureConfig": {
                "sampleRate": config.sample_rate,
                "frameLength": config.frame_length,
                "hopLength": config.hop_length,
                "nMfcc": config.n_mfcc,
                "bandEdges": list(config.band_edges),
            },
            "splitStrategyKey": split_strategy,
        },
        "split": recorded_split_ratios,
        "splitStrategy": _SPLIT_STRATEGY_TEXT[split_strategy],
        "holdoutType": holdout_type,
        "independentHoldout": independent_holdout,
        "status": "draft",
        "reason": "3주차 기존 데이터셋 선정 및 정규화 — AI_FREQ_MODEL_01 선행학습 입력 준비",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "rowCount": len(rows),
        "splitCounts": split_counts,
        "labelCounts": label_summary["labelCounts"],
        "trainingEligibleCount": label_summary["trainingEligibleCount"],
        "trainingEligibleSplitCounts": label_summary["trainingEligibleSplitCounts"],
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
        "--split-strategy",
        choices=SPLIT_STRATEGIES,
        default="specimen_group",
        help=(
            "specimen_group(기본, 물리 베어링 단위 독립 분할 — CWRU는 NORMAL specimen이 "
            "1개뿐이라 3-way에서 InsufficientAssetGroupsError) 또는 "
            "operating_condition_holdout(부하조건 기준, independentHoldout=False)"
        ),
    )
    parser.add_argument(
        "--output",
        default=os.path.normpath(
            os.path.join(_THIS_DIR, "..", "data", "handoff", "dataset_manifest_full.json")
        ),
    )
    args = parser.parse_args()

    # 기본(specimen_group)은 물리 베어링 단위 독립 분할이다. CWRU는 건강한 베어링이
    # 1개뿐(NORMAL specimen 1개)이라 기본 3-way에서 InsufficientAssetGroupsError가 나는
    # 것이 정상이다 — 조용히 우회하지 않는다. 데모/리포트가 필요하면
    # --split-strategy operating_condition_holdout 를 명시한다 (independentHoldout=False).
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
        split_strategy=args.split_strategy,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"데이터셋 {manifest['id']} 매니페스트 생성 완료: {manifest['rowCount']}행")
    print(f"  분할 전략: {args.split_strategy} (independentHoldout={manifest['independentHoldout']})")
    print(f"  분할 건수: {manifest['splitCounts']}")
    print(f"  저장 위치: {args.output}")
