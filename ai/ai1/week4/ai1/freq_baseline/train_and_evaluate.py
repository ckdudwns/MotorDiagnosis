"""
Dense/LSTM Autoencoder 후보 학습·평가·보고서 생성 (AI-1, 4주차 AI_FREQ_MODEL_01)

CWRU 데이터를 features.py로 벡터화하고, models.py의 두 후보 모델을 정상(NORMAL)
데이터만으로 학습한 뒤, 정상범위(mean+3*std, week2/ANOMALY_RULE_01과 동일한
sigma 관례) 밖의 재구성 오차를 이상으로 판정해 평가한다. 출력은
GET /api/training-jobs/{jobId}(FUT-009) 응답 형태를 따른다. 설계 근거는
freq_baseline_format.md 참고.
"""

import os
import sys
from datetime import datetime, timezone

import numpy as np
import torch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEEK1_SCRIPTS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week1", "ai1", "scripts")
)
_WEEK3_DATASETS_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "week3", "ai1", "datasets")
)
sys.path.insert(0, _WEEK1_SCRIPTS_DIR)
sys.path.insert(0, _WEEK3_DATASETS_DIR)
sys.path.insert(0, _THIS_DIR)

from load_cwru_vibration import load_cwru_dataset  # noqa: E402
from register_dataset import DATASET_LABEL_MAPPING  # noqa: E402
from features import build_feature_dict, vectorize, canonical_feature_names  # noqa: E402
from models import (  # noqa: E402
    DenseAutoencoder,
    LstmAutoencoder,
    FeatureScaler,
    train_autoencoder,
    reconstruction_error,
)

SEQ_LEN = 5

# 3주차 매니페스트 row에서 특징이 아닌 컬럼.
_NON_FEATURE_ROW_KEYS = frozenset(
    {
        "sample_id",
        "source_file",
        "known_label",
        "common_label",
        "split",
        "sample_rate_hz",
        "rpm",
    }
)

# 운전 조건(부하/RPM)에 강하게 묶인 특징 — 모델 입력에서 제외한다.
# 리크 없는 group_split은 사실상 부하조건(0/1/2/3 HP)별 분리라, 이 특징은
# train과 val/test 구간의 값 범위가 겹치지 않는다. z-score가 발산해
# 재구성 오차·임계값 보정이 망가진다(제외 시 test AUC 1.0, 포함 시 f1 0).
# 매니페스트에는 27개 그대로 남기고(데이터셋 산출물은 완전해야 하며
# datasetId가 전체 특징을 정직하게 담는다), 모델 입력만 26개로 좁힌다.
_OPERATING_POINT_FEATURES = frozenset({"vibration_peak_hz"})

DENSE_SPLIT_STRATEGY = (
    "3주차 group_split(source_file=자산 단위) 배정을 그대로 재사용하고, 동결 "
    "매니페스트의 inline 특징 컬럼(27개 중 운전 조건 결합 특징을 뺀 26개)을 직접 "
    "입력으로 사용 (재윈도우/재계산 없음 — 학습 입력이 datasetId가 가리키는 "
    "데이터와 정확히 일치)"
)
LSTM_SPLIT_STRATEGY = (
    f"동일 group_split 파일→split 배정 안에서만 길이 {SEQ_LEN} 비중첩 시퀀스를 "
    "구성 (한 원본 파일의 모든 시퀀스는 한 split에만 — 파일 내부 재분할 없음)"
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


def prepare_lstm_chunks(cwru_dir: str, frozen_manifest: dict, names: list):
    """group_split이 배정한 파일→split 안에서만 길이 SEQ_LEN 비중첩 시퀀스를 만든다.

    한 원본 파일은 통째로 하나의 split에만 들어가므로(group_split 불변식),
    파일 내부 청크를 다시 나누지 않는다 — 그 파일의 모든 시퀀스를 파일이
    배정된 split에 그대로 넣는다. 윈도우/홉은 매니페스트 생성 기본값(2048)과
    같아야 시퀀스가 동결 rows와 동일한 신호 구간을 덮는다.
    """
    split_of_file: dict = {}
    for row in frozen_manifest["rows"]:
        prev = split_of_file.setdefault(row["source_file"], row["split"])
        if prev != row["split"]:
            raise ValueError(
                f"{row['source_file']} 가 여러 split({prev}, {row['split']})에 걸쳐 있습니다 "
                "— group_split 불변식 위반."
            )

    records = load_cwru_dataset(cwru_dir)
    sample_rate = records[0]["sample_rate"]

    by_file: dict = {}
    for rec in records:
        by_file.setdefault(rec["source_label"], []).append(rec)

    samples_by_split = {"train": [], "validation": [], "test": []}

    for source_file, file_records in by_file.items():
        split = split_of_file.get(source_file)
        if split not in samples_by_split:
            continue
        vectors = [
            vectorize(build_feature_dict(r["signal"], sample_rate), names)
            for r in file_records
        ]
        for start in range(0, len(vectors) - SEQ_LEN + 1, SEQ_LEN):
            chunk_records = file_records[start : start + SEQ_LEN]
            matrix = np.stack(vectors[start : start + SEQ_LEN])
            samples_by_split[split].append(
                {
                    "sample_id": "+".join(r["sample_id"] for r in chunk_records),
                    "known_label": chunk_records[0]["label"],
                    "common_label": DATASET_LABEL_MAPPING[chunk_records[0]["label"]],
                    "vector": matrix,
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


def _evaluate_candidate(
    name: str,
    split_strategy: str,
    samples_by_split: dict,
    *,
    feature_names: list,
    epochs: int = 150,
    artifact_path: str = None,
) -> tuple:
    model_builder = _MODEL_BUILDERS[name]

    train_normal = [s for s in samples_by_split["train"] if s["common_label"] == "NORMAL"]
    if not train_normal:
        raise ValueError(f"{name}: train split에 NORMAL 샘플이 없습니다.")

    train_matrix, _ = _matrix_and_labels(train_normal)
    val_matrix, val_labels = _matrix_and_labels(samples_by_split["validation"])
    test_matrix, test_labels = _matrix_and_labels(samples_by_split["test"])
    input_dim = train_matrix.shape[-1]

    scaler = FeatureScaler().fit(train_matrix)
    train_tensor = torch.tensor(scaler.transform(train_matrix), dtype=torch.float32)

    model = model_builder(input_dim)
    losses = train_autoencoder(model, train_tensor, epochs=epochs)

    val_tensor = torch.tensor(scaler.transform(val_matrix), dtype=torch.float32)
    val_errors = reconstruction_error(model, val_tensor)
    val_normal_mask = ~np.array(val_labels, dtype=bool)
    val_normal_errors = val_errors[val_normal_mask]
    threshold = float(val_normal_errors.mean() + SIGMA * val_normal_errors.std())

    # 후보 선택용: 검증셋 전체에 대한 지표 (테스트셋은 선택에 쓰지 않는다).
    validation_metrics = compute_metrics(
        val_labels, (val_errors > threshold).tolist()
    )

    test_tensor = torch.tensor(scaler.transform(test_matrix), dtype=torch.float32)
    test_errors = reconstruction_error(model, test_tensor)
    y_pred = (test_errors > threshold).tolist()

    metrics = compute_metrics(test_labels, y_pred)  # 선택된 모델의 최종 성능 보고용
    error_cases = collect_error_cases(
        samples_by_split["test"], test_labels, y_pred, test_errors, threshold
    )

    scaler_mean = scaler.mean_.tolist()
    scaler_std = scaler.std_.tolist()

    if artifact_path:
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

    candidate = {
        "name": name,
        "splitStrategy": split_strategy,
        "featureCount": len(feature_names),
        "trainSampleCount": len(train_normal),
        "validationSampleCount": len(samples_by_split["validation"]),
        "testSampleCount": len(samples_by_split["test"]),
        "threshold": threshold,
        "trainLossFinal": float(losses[-1]),
        "artifactUri": f"file://{artifact_path}" if artifact_path else None,
        "normalization": {
            "featureOrder": list(feature_names),
            "mean": scaler_mean,
            "std": scaler_std,
            "persistedInArtifact": artifact_path is not None,
        },
        "validationMetrics": validation_metrics,
        "metrics": metrics,
    }
    return candidate, error_cases


def score_from_artifact(artifact_path: str, matrix: np.ndarray) -> dict:
    """저장된 아티팩트만으로 재구성 오차·이상 판정을 재현한다.

    학습 코드와 같은 입력이 주어지면 같은 오차/판정이 나와야 한다 (스케일러
    상태와 임계값이 아티팩트에 함께 저장돼 있으므로).
    """
    payload = torch.load(artifact_path, weights_only=False)  # 신뢰된 로컬 아티팩트
    model = _MODEL_BUILDERS[payload["model_type"]](payload["input_dim"])
    model.load_state_dict(payload["state_dict"])

    scaler = FeatureScaler.from_state(payload["scaler_mean"], payload["scaler_std"])
    tensor = torch.tensor(scaler.transform(np.asarray(matrix)), dtype=torch.float32)
    errors = reconstruction_error(model, tensor)
    threshold = payload["threshold"]
    return {
        "errors": errors,
        "verdict": (errors > threshold).tolist(),
        "threshold": threshold,
        "feature_names": payload["feature_names"],
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
                "리크 없는 group_split이 사실상 부하조건별 분리라, 정상 재구성 "
                "임계값이 train에 없던 부하조건에서는 보정되지 않는다. RPM에 강하게 "
                "묶인 vibration_peak_hz는 모델 입력에서 제외했다(매니페스트에는 유지). "
                "현장에서는 운전 조건별로 임계값을 재보정해야 한다."
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


def run_training_job(
    cwru_dir: str,
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
    torch.manual_seed(seed)

    dense_names, dense_splits = prepare_dense_splits(frozen_manifest)
    dense_candidate, dense_errors = _evaluate_candidate(
        "dense_autoencoder",
        DENSE_SPLIT_STRATEGY,
        dense_splits,
        feature_names=dense_names,
        epochs=dense_epochs,
        artifact_path=os.path.join(artifact_dir, "dense_autoencoder.pt") if artifact_dir else None,
    )

    lstm_names = model_feature_names(
        canonical_feature_names(frozen_manifest["rows"][0]["sample_rate_hz"])
    )
    lstm_splits = prepare_lstm_chunks(cwru_dir, frozen_manifest, lstm_names)
    lstm_candidate, lstm_errors = _evaluate_candidate(
        "lstm_autoencoder",
        LSTM_SPLIT_STRATEGY,
        lstm_splits,
        feature_names=lstm_names,
        epochs=lstm_epochs,
        artifact_path=os.path.join(artifact_dir, "lstm_autoencoder.pt") if artifact_dir else None,
    )

    candidates = [dense_candidate, lstm_candidate]
    best = select_best(candidates)

    return {
        "id": f"TJ-CWRU-VIBRATION-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "datasetId": frozen_manifest.get("id"),
        "status": "completed",
        "candidates": candidates,
        "metrics": {
            "bestCandidate": best["name"],
            "selectionCriterion": "validation_f1",
            "validation": best["validationMetrics"],
            **best["metrics"],  # 선택된 모델의 독립적인 최종(test) 지표
        },
        "errorCases": {"dense_autoencoder": dense_errors, "lstm_autoencoder": lstm_errors},
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
    args = parser.parse_args()

    from register_dataset import build_manifest

    sys.path.insert(
        0, os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
    )
    from dataset_version import freeze_dataset_version  # noqa: E402

    # 라벨당 자산 4개(0~3HP)로 기본 3-way group_split이 성립한다.
    manifest = build_manifest(data_dir=args.cwru_dir)
    frozen = freeze_dataset_version(manifest)

    report = run_training_job(
        args.cwru_dir,
        frozen,
        dense_epochs=args.dense_epochs,
        lstm_epochs=args.lstm_epochs,
        artifact_dir=args.artifact_dir,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"학습 완료: {report['id']}  (datasetId={report['datasetId']})")
    for candidate in report["candidates"]:
        vm, tm = candidate["validationMetrics"], candidate["metrics"]
        print(
            f"  {candidate['name']}: 검증 f1={vm['f1']:.3f} / 테스트 f1={tm['f1']:.3f}, "
            f"precision={tm['precision']:.3f}, recall={tm['recall']:.3f}"
        )
    print(
        f"  선택 기준: {report['metrics']['selectionCriterion']} → "
        f"최적 후보: {report['metrics']['bestCandidate']} "
        f"(검증 f1={report['metrics']['validation']['f1']:.3f}, "
        f"최종 테스트 f1={report['metrics']['f1']:.3f})"
    )
    print(f"저장 위치: {args.output}")
