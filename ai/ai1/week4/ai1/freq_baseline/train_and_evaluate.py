"""
Dense/LSTM Autoencoder 후보 학습·평가·보고서 생성 (AI-1, 4주차 AI_FREQ_MODEL_01)

CWRU 데이터를 features.py로 벡터화하고, models.py의 두 후보 모델을 정상(NORMAL)
데이터만으로 학습한 뒤, 정상범위(mean+3*std, week2/ANOMALY_RULE_01과 동일한
sigma 관례) 밖의 재구성 오차를 이상으로 판정해 평가한다. 출력은
GET /api/training-jobs/{jobId}(FUT-009) 응답 형태를 따른다. 설계 근거는
freq_baseline_format.md 참고.
"""

import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASET_VERSIONS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
# __main__ 배선에서 register_dataset.build_manifest / dataset_version.freeze를
# 쓸 때만 필요하다. 학습 파이프라인 자체(run_training_job)는 동결 매니페스트
# dict만 받아 원본 CWRU를 다시 읽지 않는다.
_WEEK3_DATASETS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week3", "ai1", "datasets")
)
sys.path.insert(0, _WEEK3_DATASETS_DIR)
sys.path.insert(0, _DATASET_VERSIONS_DIR)
sys.path.insert(0, _THIS_DIR)

from models import (  # noqa: E402
    DenseAutoencoder,
    LstmAutoencoder,
    FeatureScaler,
    train_autoencoder,
    reconstruction_error,
)
from dataset_version import verify_frozen_integrity  # noqa: E402


def _sha256_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"

SEQ_LEN = 5

# 3주차 매니페스트 row에서 특징이 아닌 컬럼.
_NON_FEATURE_ROW_KEYS = frozenset(
    {
        "sample_id",
        "source_file",
        "specimen_id",
        "known_label",
        "common_label",
        "split",
        "sample_rate_hz",
        "rpm",
    }
)

# 운전 조건(부하/RPM)에 강하게 묶인 특징 — 모델 입력에서 제외한다.
#
# 근거(물리 + train/validation 관찰, test 평가 전에 확정·동결한 결정):
#   vibration_peak_hz ≈ 회전 주파수 = RPM/60. CWRU는 부하조건별로 RPM이 고정이고
#   (0HP≈1797 / 1HP≈1772 / 2HP≈1750 / 3HP≈1730), operating_condition_holdout 분할은
#   바로 그 부하조건 기준이라 이 특징의 값 범위가 train(2·3HP)과 validation(1HP)에서
#   이미 겹치지 않는다 — z-score가 발산해 재구성 오차·임계값 보정이 무너진다.
#   특징 선택·후보 비교는 train/validation만으로 했고, test split은 이 결정에 쓰지 않았다.
# 매니페스트에는 27개 그대로 남기고(datasetId가 전체 특징을 정직하게 담는다),
# 모델 입력만 26개로 좁힌다.
_OPERATING_POINT_FEATURES = frozenset({"vibration_peak_hz"})

DENSE_SPLIT_STRATEGY = (
    "동결 매니페스트의 split 배정(operating_condition_holdout — 부하조건 기준, specimen "
    "독립 아님)을 그대로 재사용하고, inline 특징 컬럼(27개 중 운전 조건 결합 특징을 뺀 "
    "26개)을 직접 입력으로 사용 (재윈도우/재계산 없음 — 학습 입력이 datasetId가 가리키는 "
    "데이터와 정확히 일치)"
)
LSTM_SPLIT_STRATEGY = (
    f"동일 split 배정 안에서만 동결 매니페스트 rows(inline 특징)를 윈도우 순번으로 정렬해 "
    f"길이 {SEQ_LEN} 비중첩 시퀀스를 구성 (원본 재로드·재계산 없음, 한 원본 파일의 모든 "
    "시퀀스는 한 split에만). 분할 자체는 operating_condition_holdout이라 specimen 독립 아님"
)


def _matrix_and_labels(samples: list):
    matrix = np.stack([s["vector"] for s in samples])
    labels = [s["common_label"] == "ANOMALY" for s in samples]
    return matrix, labels


def model_feature_names(all_feature_names) -> list:
    """전체 특징 목록에서 모델 입력으로 쓸 것만 고정 순서로 남긴다
    (운전 조건 결합 특징 제외)."""
    return sorted(n for n in all_feature_names if n not in _OPERATING_POINT_FEATURES)


def feature_names_from_manifest(frozen_manifest: dict) -> list:
    """동결 매니페스트 row에서 모델 입력 특징 컬럼을 고정 순서(정렬)로 뽑는다.

    매니페스트 row에는 27개 특징이 있지만 모델 입력은 운전 조건 결합 특징을
    뺀 26개다. 비었거나 일부 row에 누락된 특징이 있으면 예외 — 모델 입력
    차원이 row마다 달라지는 것을 막는다.
    """
    rows = frozen_manifest.get("rows") or []
    if not rows:
        raise ValueError("frozen_manifest에 rows가 없습니다.")
    all_names = [k for k in rows[0] if k not in _NON_FEATURE_ROW_KEYS]
    names = model_feature_names(all_names)
    if not names:
        raise ValueError("매니페스트 row에서 특징 컬럼을 찾지 못했습니다.")
    for row in rows:
        missing = [n for n in names if n not in row]
        if missing:
            raise ValueError(f"{row.get('sample_id')!r} row에 특징이 누락됨: {missing}")
    return names


def prepare_dense_splits(frozen_manifest: dict):
    """동결 매니페스트 rows의 inline 특징값을 그대로 써서 윈도우 단위 샘플을 만든다.

    예전 구현은 split 배정만 읽고 원본 CWRU를 다시 로드해 특징을 재계산했다 —
    윈도우 크기 등 전처리 설정이 동결 시점과 어긋나면 실제 학습 입력이
    datasetId가 가리키는 데이터와 달라졌다(datasetId가 거짓말). 이제 동결된
    특징값 자체를 입력으로 쓴다.
    """
    if frozen_manifest.get("status") != "frozen":
        raise ValueError(
            f"frozen 상태의 매니페스트가 필요합니다 (status={frozen_manifest.get('status')!r})."
        )

    names = feature_names_from_manifest(frozen_manifest)
    samples_by_split = {"train": [], "validation": [], "test": []}

    for row in frozen_manifest["rows"]:
        split = row["split"]
        if split not in samples_by_split:
            continue  # train 전용 등 3-way가 아닌 매니페스트는 해당 split만 사용
        samples_by_split[split].append(
            {
                "sample_id": row["sample_id"],
                "known_label": row["known_label"],
                "common_label": row["common_label"],
                "vector": np.array([row[n] for n in names], dtype=np.float64),
            }
        )

    return names, samples_by_split


def _window_index(sample_id: str) -> int:
    """'97_0007' -> 7 (원본 파일 안에서의 윈도우 순번).

    load_cwru_vibration이 '<파일번호>_<4자리 순번>' 규칙으로 sample_id를 만든다.
    파일 내부에서 이 순번으로 정렬해야 시퀀스가 동결 시점과 같은 신호 구간
    순서를 덮는다.
    """
    try:
        return int(sample_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        raise ValueError(
            f"sample_id에서 윈도우 순번을 읽을 수 없습니다: {sample_id!r} "
            "('<파일번호>_<순번>' 형식이어야 합니다)."
        )


def prepare_lstm_chunks(frozen_manifest: dict, names: list):
    """group_split이 배정한 파일→split 안에서만 길이 SEQ_LEN 비중첩 시퀀스를 만든다.

    동결 매니페스트 rows의 inline 특징값을 그대로 시퀀스로 묶는다 — 원본 CWRU를
    다시 읽지 않는다. 예전 구현은 cwru_dir을 기본 윈도우(2048/2048)로 재분할하고
    특징을 재계산해서, 매니페스트가 다른 window/hop으로 동결됐거나 원본 .mat이
    바뀌면 LSTM 입력이 datasetId가 가리키는 데이터와 어긋났다(윈도우 수·같은
    sample_id의 특징값 모두 불일치). 이제 dense 경로와 똑같이 동결본만 입력으로
    쓴다.

    한 원본 파일은 통째로 하나의 split에만 들어가므로(group_split 불변식) 파일
    내부를 다시 나누지 않는다 — 파일 안에서는 sample_id의 윈도우 순번으로
    정렬해 그 파일의 모든 시퀀스를 배정된 split에 넣는다.
    """
    if frozen_manifest.get("status") != "frozen":
        raise ValueError(
            f"frozen 상태의 매니페스트가 필요합니다 (status={frozen_manifest.get('status')!r})."
        )

    rows_by_file: dict = {}
    for row in frozen_manifest["rows"]:
        rows_by_file.setdefault(row["source_file"], []).append(row)

    samples_by_split = {"train": [], "validation": [], "test": []}

    for source_file, file_rows in rows_by_file.items():
        file_splits = {r["split"] for r in file_rows}
        if len(file_splits) > 1:
            raise ValueError(
                f"{source_file} 가 여러 split({sorted(file_splits)})에 걸쳐 있습니다 "
                "— group_split 불변식 위반."
            )
        split = next(iter(file_splits))
        if split not in samples_by_split:
            continue  # train 전용 등 3-way가 아닌 매니페스트는 해당 split만 사용

        file_rows = sorted(file_rows, key=lambda r: _window_index(r["sample_id"]))
        vectors = [
            np.array([row[n] for n in names], dtype=np.float64) for row in file_rows
        ]
        for start in range(0, len(vectors) - SEQ_LEN + 1, SEQ_LEN):
            chunk = file_rows[start : start + SEQ_LEN]
            samples_by_split[split].append(
                {
                    "sample_id": "+".join(r["sample_id"] for r in chunk),
                    "known_label": chunk[0]["known_label"],
                    "common_label": chunk[0]["common_label"],
                    "vector": np.stack(vectors[start : start + SEQ_LEN]),
                }
            )

    return samples_by_split


def compute_metrics(y_true: list, y_pred: list) -> dict:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred) if not t and p)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t and not p)
    tn = sum(1 for t, p in zip(y_true, y_pred) if not t and not p)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (tp + tn) / len(y_true) if y_true else 0.0

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
    }


def collect_error_cases(samples: list, y_true: list, y_pred: list, errors, threshold: float, limit: int = 10) -> dict:
    false_positives, false_negatives = [], []
    for sample, true_label, pred_label, error in zip(samples, y_true, y_pred, errors):
        case = {
            "sample_id": sample["sample_id"],
            "known_label": sample["known_label"],
            "reconstruction_error": float(error),
            "threshold": float(threshold),
        }
        if not true_label and pred_label and len(false_positives) < limit:
            false_positives.append(case)
        if true_label and not pred_label and len(false_negatives) < limit:
            false_negatives.append(case)
    return {"false_positives": false_positives, "false_negatives": false_negatives}


SIGMA = 3.0  # 정상범위 mean + 3*std (week2/ANOMALY_RULE_01과 동일한 관례)

_MODEL_BUILDERS = {
    "dense_autoencoder": DenseAutoencoder,
    "lstm_autoencoder": LstmAutoencoder,
}


def _train_and_validate_candidate(
    name: str,
    split_strategy: str,
    samples_by_split: dict,
    *,
    feature_names: list,
    epochs: int = 150,
    artifact_path: str = None,
) -> tuple:
    """train+validation만으로 후보를 학습·평가하고 아티팩트를 저장한다.

    [리뷰 P1] test holdout은 여기서 절대 건드리지 않는다 — 예전에는 두 후보
    모두 여기서 test 지표·오류 사례까지 계산한 뒤에 select_best()를 호출해서,
    선택에 쓰이지 않는 test 값이라도 후보 비교 단계에 노출됐다(test holdout이
    더 이상 "본 적 없는" 데이터가 아니게 된다). 최종 test 평가는 select_best()
    이후 선택된 후보 하나에 대해서만 _finalize_test_evaluation()이 수행한다.
    아티팩트 저장은 test 데이터를 쓰지 않으므로(가중치+스케일러+임계값은
    train/validation만으로 정해짐) 두 후보 모두 여기서 저장해도 무방하다.
    """
    model_builder = _MODEL_BUILDERS[name]

    train_normal = [s for s in samples_by_split["train"] if s["common_label"] == "NORMAL"]
    if not train_normal:
        raise ValueError(f"{name}: train split에 NORMAL 샘플이 없습니다.")
    empty_splits = [
        split_name
        for split_name in ("validation", "test")
        if not samples_by_split.get(split_name)
    ]
    if empty_splits:
        raise ValueError(f"{name}: 필수 split이 비어 있습니다: {empty_splits}")

    train_matrix, _ = _matrix_and_labels(train_normal)
    val_matrix, val_labels = _matrix_and_labels(samples_by_split["validation"])
    input_dim = train_matrix.shape[-1]

    scaler = FeatureScaler().fit(train_matrix)
    train_tensor = torch.tensor(scaler.transform(train_matrix), dtype=torch.float32)

    model = model_builder(input_dim)
    losses = train_autoencoder(model, train_tensor, epochs=epochs)

    val_tensor = torch.tensor(scaler.transform(val_matrix), dtype=torch.float32)
    val_errors = reconstruction_error(model, val_tensor)
    val_normal_mask = ~np.array(val_labels, dtype=bool)
    val_normal_errors = val_errors[val_normal_mask]
    # [리뷰 P1] validation에 NORMAL 표본이 없으면 mean/std가 NaN이 되어 임계값이
    # 조용히 NaN으로 저장되고(모든 판정이 False), 최종 JSON 저장도
    # allow_nan=False 때문에 실패한다 — 학습을 계속하는 대신 여기서 명확히 막는다.
    if val_normal_errors.size == 0:
        raise ValueError(
            f"{name}: validation split에 NORMAL 표본이 없어 정상범위 임계값"
            f"(mean + {SIGMA}*std)을 계산할 수 없습니다. 매니페스트 분할을 확인하세요."
        )
    threshold = float(val_normal_errors.mean() + SIGMA * val_normal_errors.std())

    # 후보 선택용: 검증셋 전체에 대한 지표 (테스트셋은 선택에 쓰지 않는다).
    validation_metrics = compute_metrics(
        val_labels, (val_errors > threshold).tolist()
    )

    scaler_mean = scaler.mean_.tolist()
    scaler_std = scaler.std_.tolist()

    artifact_checksum = None
    if artifact_path:
        # 불변 artifact: 이미 존재하면 절대 덮어쓰지 않는다 — 같은 경로가 나중에
        # 만들어진 다른 모델을 가리키면 이전 보고서의 URI가 거짓말이 된다.
        if os.path.exists(artifact_path):
            raise FileExistsError(
                f"artifact 경로가 이미 존재합니다 (덮어쓰기 금지): {artifact_path}. "
                "job_id 기반 경로가 유일해야 합니다."
            )
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        # 가중치만 저장하면 학습 당시 입력 변환(정규화)을 복원할 수 없다 —
        # 스케일러 평균·표준편차·특징 순서·임계값을 함께 저장한다.
        torch.save(
            {
                "model_type": name,
                "state_dict": model.state_dict(),
                "input_dim": input_dim,
                "seq_len": SEQ_LEN if name == "lstm_autoencoder" else None,
                "feature_names": list(feature_names),
                "scaler_mean": scaler_mean,
                "scaler_std": scaler_std,
                "threshold": threshold,
                "sigma": SIGMA,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            artifact_path,
        )
        artifact_checksum = _sha256_of_file(artifact_path)

    candidate = {
        "name": name,
        "splitStrategy": split_strategy,
        "featureCount": len(feature_names),
        "trainSampleCount": len(train_normal),
        "validationSampleCount": len(samples_by_split["validation"]),
        "testSampleCount": len(samples_by_split["test"]),
        "threshold": threshold,
        "trainLossFinal": float(losses[-1]),
        # file://C:\... 같은 비표준 URI는 백엔드 모델 등록에서 400
        # INVALID_ARTIFACT_URI로 거부된다. 표준 파일 URI로 변환한다
        # (Windows: file:///C:/..., POSIX: file:///...).
        "artifactUri": Path(artifact_path).resolve().as_uri() if artifact_path else None,
        "artifactChecksum": artifact_checksum,
        "normalization": {
            "featureOrder": list(feature_names),
            "mean": scaler_mean,
            "std": scaler_std,
            "persistedInArtifact": artifact_path is not None,
        },
        "validationMetrics": validation_metrics,
    }
    state = {"model": model, "scaler": scaler, "threshold": threshold}
    return candidate, state


def _finalize_test_evaluation(name: str, state: dict, samples_by_split: dict) -> tuple:
    """후보 선택이 끝난 뒤, 선택된 후보 하나에 대해서만 test holdout을 한 번 연다."""
    test_matrix, test_labels = _matrix_and_labels(samples_by_split["test"])
    test_tensor = torch.tensor(state["scaler"].transform(test_matrix), dtype=torch.float32)
    test_errors = reconstruction_error(state["model"], test_tensor)
    y_pred = (test_errors > state["threshold"]).tolist()

    metrics = compute_metrics(test_labels, y_pred)
    error_cases = collect_error_cases(
        samples_by_split["test"], test_labels, y_pred, test_errors, state["threshold"]
    )
    return metrics, error_cases


def score_from_artifact(
    artifact_path: str,
    matrix: np.ndarray,
    *,
    input_feature_names: list,
    expected_checksum: str,
) -> dict:
    """저장된 아티팩트만으로 재구성 오차·이상 판정을 재현한다.

    `input_feature_names`(입력 행렬 각 열의 특징 이름)는 **필수**다 — 아티팩트에 저장된
    학습 시점 특징 순서와 대조·재정렬한다. 이름 없이 shape만 맞는 입력을 넘기면
    열 순서가 어긋나도 오류 없이 다른 판정이 나오기 때문이다.

    [리뷰 P1] `expected_checksum`(보고서의 artifactChecksum)도 **필수**다 — 파일을
    읽기 전에 SHA-256을 검증한다. torch.load()는 threshold 등 내용이 변조된
    아티팩트도 아무 오류 없이 그대로 읽어버리므로, checksum 검증 없이는 변조된
    임계값·가중치가 그대로 추론에 쓰인다. 선택적 인자로 두면 호출자가 잊고
    누락하기 쉬우므로 아예 필수로 강제한다.

    - checksum 불일치 → ValueError (load 전에 거부)
    - 특징 집합이 아티팩트와 다르면 → ValueError
    - 순서만 다르면 → 아티팩트 순서로 열 재정렬
    - 열 개수 불일치 / NaN·Inf → ValueError
    """
    actual_checksum = _sha256_of_file(artifact_path)
    if actual_checksum != expected_checksum:
        raise ValueError(
            "artifact checksum이 일치하지 않습니다 — 변조되었을 수 있어 추론을 "
            f"거부합니다 (경로={artifact_path!r}, 기대={expected_checksum!r}, "
            f"실제={actual_checksum!r})."
        )

    payload = torch.load(artifact_path, weights_only=False)  # checksum 검증된 아티팩트
    artifact_names = list(payload["feature_names"])

    input_names = list(input_feature_names)
    if len(input_names) != len(set(input_names)):
        raise ValueError(f"input_feature_names에 중복이 있습니다: {input_names}")
    if set(input_names) != set(artifact_names):
        raise ValueError(
            "입력 특징 집합이 아티팩트와 다릅니다.\n"
            f"  아티팩트: {sorted(artifact_names)}\n  입력: {sorted(input_names)}"
        )

    arr = np.asarray(matrix, dtype=np.float64)
    if arr.shape[-1] != len(artifact_names):
        raise ValueError(
            f"입력 열 개수({arr.shape[-1]})가 아티팩트 특징 수({len(artifact_names)})와 "
            "다릅니다."
        )
    if not np.isfinite(arr).all():
        raise ValueError("입력 행렬에 NaN 또는 Inf가 있습니다.")

    if input_names != artifact_names:
        # 이름 기준으로 아티팩트 열 순서에 맞춰 재정렬 (마지막 축이 특징 축).
        order = [input_names.index(n) for n in artifact_names]
        arr = arr[..., order]

    model = _MODEL_BUILDERS[payload["model_type"]](payload["input_dim"])
    model.load_state_dict(payload["state_dict"])

    scaler = FeatureScaler.from_state(payload["scaler_mean"], payload["scaler_std"])
    tensor = torch.tensor(scaler.transform(arr), dtype=torch.float32)
    errors = reconstruction_error(model, tensor)
    threshold = payload["threshold"]
    return {
        "errors": errors,
        "verdict": (errors > threshold).tolist(),
        "threshold": threshold,
        "feature_names": artifact_names,
    }


def build_domain_gap() -> dict:
    """MVP 기획서(v1.2) "AI 보장 범위"·"8. AI 및 데이터 기획" 문구를 정적으로 반영."""
    return {
        "samplingRateHz": {"source": 12000, "target": None, "note": "대상 설비 샘플링률 미확정"},
        "installPoint": {
            "source": "Drive-End 베어링 하우징 고정식 가속도계",
            "target": None,
            "note": "대상 설비 설치 위치 미확정 (INSTALL_POINT_01 확정 후 갱신)",
        },
        "operatingConditions": {
            "source": "파일별 고정 부하(0~3HP 근사)/고정 RPM",
            "target": "가변 부하·RPM 예상",
            "note": (
                "이 베이스라인 지표는 operating_condition_holdout(부하조건 기준) 평가이며 "
                "specimen 독립 검증이 아니다 — 같은 물리 베어링이 train/validation/test에 "
                "함께 들어간다. CWRU는 건강한 베어링이 1개뿐이라 specimen 독립 holdout "
                "자체가 불가능하다. 정상 재구성 임계값이 train에 없던 부하조건에서는 "
                "보정되지 않고, RPM 프록시인 vibration_peak_hz는 모델 입력에서 제외했다"
                "(매니페스트에는 유지). 현장 데이터 또는 추가 독립 베어링 확보 후 "
                "specimen 독립 평가로 재검증하고, 운전 조건별 임계값을 재보정해야 한다."
            ),
        },
        "labelTaxonomy": {
            "source": ["NORMAL", "BEARING_FAULT_INNER", "BEARING_FAULT_BALL", "BEARING_FAULT_OUTER"],
            "target": "미확정 — 도서발전소 환경 특유의 염분·혼합 소음원으로 인한 신규 이상 유형 가능성",
        },
        "guaranteeScope": (
            "이 베이스라인은 CWRU 공개 데이터 기반이며, 대상 모터의 고장 유형 분류와 "
            "RUL 성능은 현장 라벨·정비 이력이 충분히 확보된 뒤 별도 검증한다."
        ),
    }


def build_field_calibration_plan() -> list:
    """MVP 기획서 "11. 후속 로드맵" 항목을 체크리스트로 정리."""
    return [
        "대상 모터·센서 확정 후 충분한 현장 정상·이상 데이터로 전이학습",
        "현장 데이터 기반으로 임계값·히스테리시스(ANOMALY_RULE_01) 재보정 "
        "(특히 운전 조건별로 정상 재구성 오차 분포가 달라 조건별 임계값 필요)",
        "기존 데이터셋과 현장 데이터의 도메인 차이·오탐 사례 추적",
        "전문가 라벨 축적 후 Autoencoder/LSTM 후보 재검증",
        "충분한 고장·정비 이력 확보 후 RUL·고장 유형 분류를 별도 과제로 진행",
    ]


def select_best(candidates: list) -> dict:
    """검증셋 f1으로 최적 후보를 고른다 (테스트셋은 선택에 관여하지 않는다)."""
    return max(candidates, key=lambda c: c["validationMetrics"]["f1"])


def _actual_independent_holdout(frozen_manifest: dict) -> bool:
    """rows의 specimen_id -> split 관계에서 실제 독립성을 계산한다.

    같은 specimen_id가 여러 split에 걸쳐 있으면 독립 holdout이 아니다.
    """
    splits_by_specimen: dict = {}
    for row in frozen_manifest.get("rows") or []:
        splits_by_specimen.setdefault(row.get("specimen_id"), set()).add(row.get("split"))
    return all(len(splits) <= 1 for splits in splits_by_specimen.values())


def _verify_independent_holdout_claim(frozen_manifest: dict) -> bool:
    """[리뷰 P1] independentHoldout 메타데이터 선언을 그대로 신뢰하지 않는다.

    같은 specimen이 train/validation/test에 모두 걸쳐 있어도 매니페스트에
    independentHoldout=True가 박혀 있으면 이전에는 그대로 "독립 평가"로
    보고됐다. 실제 rows의 specimen_id -> split 관계로 독립성을 재계산하고,
    선언(True)이 실제(False)와 어긋나면 학습을 거부한다. 반환값(실제 계산된
    독립성)을 보고서에 쓴다 — 선언값을 그대로 믿지 않는다.
    """
    declared = bool(frozen_manifest.get("independentHoldout"))
    actual = _actual_independent_holdout(frozen_manifest)
    if declared and not actual:
        raise ValueError(
            "independentHoldout=True로 선언됐지만 실제 rows에서 같은 specimen_id가 "
            "여러 split에 걸쳐 있습니다 (선언과 데이터 불일치) — 독립 평가로 보고할 "
            "수 없습니다."
        )
    return actual


def run_training_job(
    frozen_manifest: dict,
    *,
    seed: int = 42,
    dense_epochs: int = 150,
    lstm_epochs: int = 150,
    artifact_dir: str = None,
) -> dict:
    if frozen_manifest.get("status") != "frozen":
        raise ValueError(
            f"frozen 상태의 매니페스트가 필요합니다 (status={frozen_manifest.get('status')!r})."
        )
    # status만 보면 freeze 이후 row 값이 바뀌어도 같은 datasetId로 학습이 완료된다 —
    # 특징 준비 전에 전체 snapshot 무결성을 재검증한다.
    verify_frozen_integrity(frozen_manifest)
    # 선언된 independentHoldout이 실제 rows와 일치하는지 검증하고, 실제 계산값을
    # 이후 보고서에 쓴다(선언값을 그대로 신뢰하지 않는다).
    independent_holdout = _verify_independent_holdout_claim(frozen_manifest)
    torch.manual_seed(seed)

    # 작업별 유일 job_id. artifact는 이 아래 불변 경로에 저장 — 다음 학습이 이전 모델을
    # 덮어쓰지 않는다(보고서 URI가 나중 모델을 가리키는 문제 방지).
    job_id = (
        "TJ-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    job_dir = os.path.join(artifact_dir, job_id) if artifact_dir else None

    holdout_type = frozen_manifest.get("holdoutType")

    # dense와 lstm 모두 동결 매니페스트 rows의 inline 특징값만 입력으로 쓴다
    # (원본 CWRU 재로드·재계산 없음). 같은 feature_names로 두 경로를 묶는다.
    # train/validation만으로 두 후보를 학습·평가한다 — test holdout은 아직 열지 않는다.
    feature_names, dense_splits = prepare_dense_splits(frozen_manifest)
    dense_candidate, dense_state = _train_and_validate_candidate(
        "dense_autoencoder",
        DENSE_SPLIT_STRATEGY,
        dense_splits,
        feature_names=feature_names,
        epochs=dense_epochs,
        artifact_path=os.path.join(job_dir, "dense_autoencoder.pt") if job_dir else None,
    )

    lstm_splits = prepare_lstm_chunks(frozen_manifest, feature_names)
    lstm_candidate, lstm_state = _train_and_validate_candidate(
        "lstm_autoencoder",
        LSTM_SPLIT_STRATEGY,
        lstm_splits,
        feature_names=feature_names,
        epochs=lstm_epochs,
        artifact_path=os.path.join(job_dir, "lstm_autoencoder.pt") if job_dir else None,
    )

    candidates = [dense_candidate, lstm_candidate]
    best = select_best(candidates)  # 검증 f1만 사용 — 이 시점까지 test holdout 미접근

    # [리뷰 P1] 선택된 후보 하나에 대해서만, 선택이 끝난 뒤 test holdout을 연다.
    states_by_name = {"dense_autoencoder": dense_state, "lstm_autoencoder": lstm_state}
    splits_by_name = {"dense_autoencoder": dense_splits, "lstm_autoencoder": lstm_splits}
    best_metrics, best_error_cases = _finalize_test_evaluation(
        best["name"], states_by_name[best["name"]], splits_by_name[best["name"]]
    )
    best["metrics"] = best_metrics

    evaluation_note = (
        "specimen-independent holdout"
        if independent_holdout
        else "operating_condition_holdout — NOT specimen-independent (운전조건 기준 "
        "in-distribution 평가). CWRU는 건강한 베어링이 1개뿐이라 specimen 독립 검증 불가."
    )

    return {
        "id": job_id,
        "datasetId": frozen_manifest.get("id"),
        "datasetSnapshotDigest": frozen_manifest.get("snapshotDigest"),
        "status": "completed",
        "candidates": candidates,
        "metrics": {
            "bestCandidate": best["name"],
            "selectionCriterion": "validation_f1",
            "independentHoldout": independent_holdout,
            "holdoutType": holdout_type,
            "evaluation": evaluation_note,
            "validation": best["validationMetrics"],
            **best["metrics"],  # 선택된 모델의 test 지표 (독립 검증 아님 — 위 플래그 참고)
        },
        # test holdout은 선택된 후보에 대해서만 평가했으므로, error case도 그
        # 후보 하나만 담긴다 — 낙선한 후보는 test holdout을 아예 열지 않았다.
        "errorCases": {best["name"]: best_error_cases},
        "domainGap": build_domain_gap(),
        "fieldCalibrationPlan": build_field_calibration_plan(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="AI_FREQ_MODEL_01 베이스라인 학습·평가")
    parser.add_argument(
        "--cwru-dir",
        default=os.path.normpath(
            os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "data", "external", "cwru")
        ),
    )
    parser.add_argument(
        "--output",
        default=os.path.normpath(os.path.join(_THIS_DIR, "..", "data", "training_job_report.json")),
    )
    parser.add_argument(
        "--artifact-dir",
        default=os.path.normpath(os.path.join(_THIS_DIR, "..", "data", "models")),
    )
    parser.add_argument("--dense-epochs", type=int, default=150)
    parser.add_argument("--lstm-epochs", type=int, default=150)
    parser.add_argument(
        "--split-strategy",
        choices=("specimen_group", "operating_condition_holdout"),
        default="operating_condition_holdout",
        help=(
            "기본 데모는 operating_condition_holdout(운전조건 기준, independentHoldout=False). "
            "specimen_group은 물리 베어링 단위 독립 분할 — CWRU는 NORMAL specimen이 1개뿐이라 "
            "3-way에서 InsufficientAssetGroupsError."
        ),
    )
    args = parser.parse_args()

    from register_dataset import build_manifest
    from dataset_version import freeze_dataset_version  # noqa: E402

    manifest = build_manifest(
        data_dir=args.cwru_dir, split_strategy=args.split_strategy
    )
    frozen = freeze_dataset_version(manifest)

    report = run_training_job(
        frozen,
        dense_epochs=args.dense_epochs,
        lstm_epochs=args.lstm_epochs,
        artifact_dir=args.artifact_dir,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    m = report["metrics"]
    print(f"학습 완료: {report['id']}  (datasetId={report['datasetId']})")
    if not m["independentHoldout"]:
        print(
            "  [주의] independentHoldout=False: 이 지표는 운전조건 기준 in-distribution "
            "평가이며 specimen 독립 검증이 아닙니다."
        )
    for candidate in report["candidates"]:
        vm = candidate["validationMetrics"]
        line = f"  {candidate['name']}: 검증 f1={vm['f1']:.3f}"
        # test holdout은 선택된 후보에 대해서만 평가한다 — 낙선 후보는 검증
        # 지표까지만 보고한다.
        tm = candidate.get("metrics")
        if tm is not None:
            line += (
                f" / (비독립) 테스트 f1={tm['f1']:.3f}, precision={tm['precision']:.3f}, "
                f"recall={tm['recall']:.3f}"
            )
        line += f"  artifact={candidate['artifactChecksum']}"
        print(line)
    print(
        f"  선택 기준: {m['selectionCriterion']} → 최적 후보: {m['bestCandidate']} "
        f"(검증 f1={m['validation']['f1']:.3f}, 비독립 테스트 f1={m['f1']:.3f})"
    )
    print(f"저장 위치: {args.output}")
