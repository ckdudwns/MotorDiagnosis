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
from features import build_feature_dict, feature_names, vectorize  # noqa: E402
from models import (  # noqa: E402
    DenseAutoencoder,
    LstmAutoencoder,
    FeatureScaler,
    train_autoencoder,
    reconstruction_error,
)

SEQ_LEN = 5
DENSE_SPLIT_STRATEGY = "3주차 DATA_EXPORT_01 라벨별 층화 분할 재사용 (window-level)"
LSTM_SPLIT_STRATEGY = (
    f"파일별 시간순 비중첩 청크(길이 {SEQ_LEN}) 분할, 셔플 없음 (sequence-level, "
    "누수 방지를 위해 3주차 window-level 분할과 별도로 계산)"
)


def _matrix_and_labels(samples: list):
    matrix = np.stack([s["vector"] for s in samples])
    labels = [s["common_label"] == "ANOMALY" for s in samples]
    return matrix, labels


def prepare_dense_splits(cwru_dir: str, frozen_manifest: dict):
    """3주차 매니페스트의 split 배정을 그대로 써서 윈도우 단위 특징 벡터를 만든다."""
    records = load_cwru_dataset(cwru_dir)
    split_by_id = {row["sample_id"]: row["split"] for row in frozen_manifest["rows"]}

    sample_rate = records[0]["sample_rate"]
    names = None
    samples_by_split = {"train": [], "validation": [], "test": []}

    for rec in records:
        features = build_feature_dict(rec["signal"], sample_rate)
        if names is None:
            names = feature_names(features)
        vector = vectorize(features, names)
        sample = {
            "sample_id": rec["sample_id"],
            "known_label": rec["label"],
            "common_label": DATASET_LABEL_MAPPING[rec["label"]],
            "vector": vector,
        }
        samples_by_split[split_by_id[rec["sample_id"]]].append(sample)

    return names, samples_by_split


def _contiguous_split_counts(n: int, ratios=(0.7, 0.2, 0.1)) -> tuple:
    n_train = min(round(n * ratios[0]), n)
    n_val = min(round(n * ratios[1]), n - n_train)
    n_test = n - n_train - n_val
    return n_train, n_val, n_test


def prepare_lstm_chunks(cwru_dir: str, names: list):
    """파일별로 시간순 비중첩 청크(길이 SEQ_LEN)를 만들고, 청크 순서를 유지한 채
    앞 70%/다음 20%/뒤 10%로 분할한다 (셔플 없음 — 데이터 누수 방지, 근거는
    freq_baseline_format.md "왜 두 데이터 분할이 서로 다른가" 참고)."""
    records = load_cwru_dataset(cwru_dir)
    sample_rate = records[0]["sample_rate"]

    by_file: dict = {}
    for rec in records:
        by_file.setdefault(rec["source_label"], []).append(rec)

    samples_by_split = {"train": [], "validation": [], "test": []}

    for source_file, file_records in by_file.items():
        vectors = [
            vectorize(build_feature_dict(r["signal"], sample_rate), names) for r in file_records
        ]
        chunks = []
        for start in range(0, len(vectors) - SEQ_LEN + 1, SEQ_LEN):
            chunk_records = file_records[start : start + SEQ_LEN]
            matrix = np.stack(vectors[start : start + SEQ_LEN])
            chunks.append(
                {
                    "sample_id": "+".join(r["sample_id"] for r in chunk_records),
                    "known_label": chunk_records[0]["label"],
                    "common_label": DATASET_LABEL_MAPPING[chunk_records[0]["label"]],
                    "vector": matrix,
                }
            )

        n_train, n_val, n_test = _contiguous_split_counts(len(chunks))
        samples_by_split["train"].extend(chunks[:n_train])
        samples_by_split["validation"].extend(chunks[n_train : n_train + n_val])
        samples_by_split["test"].extend(chunks[n_train + n_val :])

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


def _evaluate_candidate(
    name: str,
    split_strategy: str,
    samples_by_split: dict,
    model_builder,
    *,
    epochs: int = 150,
    artifact_path: str = None,
) -> tuple:
    train_normal = [s for s in samples_by_split["train"] if s["common_label"] == "NORMAL"]
    if not train_normal:
        raise ValueError(f"{name}: train split에 NORMAL 샘플이 없습니다.")

    train_matrix, _ = _matrix_and_labels(train_normal)
    val_matrix, val_labels = _matrix_and_labels(samples_by_split["validation"])
    test_matrix, test_labels = _matrix_and_labels(samples_by_split["test"])

    scaler = FeatureScaler().fit(train_matrix)
    train_tensor = torch.tensor(scaler.transform(train_matrix), dtype=torch.float32)

    model = model_builder(train_matrix.shape[-1])
    losses = train_autoencoder(model, train_tensor, epochs=epochs)

    val_tensor = torch.tensor(scaler.transform(val_matrix), dtype=torch.float32)
    val_errors = reconstruction_error(model, val_tensor)
    val_normal_mask = ~np.array(val_labels, dtype=bool)
    val_normal_errors = val_errors[val_normal_mask]
    threshold = float(val_normal_errors.mean() + 3.0 * val_normal_errors.std())

    test_tensor = torch.tensor(scaler.transform(test_matrix), dtype=torch.float32)
    test_errors = reconstruction_error(model, test_tensor)
    y_pred = (test_errors > threshold).tolist()

    metrics = compute_metrics(test_labels, y_pred)
    error_cases = collect_error_cases(
        samples_by_split["test"], test_labels, y_pred, test_errors, threshold
    )

    if artifact_path:
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        torch.save(model.state_dict(), artifact_path)

    candidate = {
        "name": name,
        "splitStrategy": split_strategy,
        "trainSampleCount": len(train_normal),
        "validationSampleCount": len(samples_by_split["validation"]),
        "testSampleCount": len(samples_by_split["test"]),
        "threshold": threshold,
        "trainLossFinal": float(losses[-1]),
        "artifactUri": f"file://{artifact_path}" if artifact_path else None,
        "metrics": metrics,
    }
    return candidate, error_cases


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
        "현장 데이터 기반으로 임계값·히스테리시스(ANOMALY_RULE_01) 재보정",
        "기존 데이터셋과 현장 데이터의 도메인 차이·오탐 사례 추적",
        "전문가 라벨 축적 후 Autoencoder/LSTM 후보 재검증",
        "충분한 고장·정비 이력 확보 후 RUL·고장 유형 분류를 별도 과제로 진행",
    ]


def run_training_job(
    cwru_dir: str,
    frozen_manifest: dict,
    *,
    seed: int = 42,
    dense_epochs: int = 150,
    lstm_epochs: int = 150,
    artifact_dir: str = None,
) -> dict:
    torch.manual_seed(seed)

    names, dense_splits = prepare_dense_splits(cwru_dir, frozen_manifest)
    dense_candidate, dense_errors = _evaluate_candidate(
        "dense_autoencoder",
        DENSE_SPLIT_STRATEGY,
        dense_splits,
        DenseAutoencoder,
        epochs=dense_epochs,
        artifact_path=os.path.join(artifact_dir, "dense_autoencoder.pt") if artifact_dir else None,
    )

    lstm_splits = prepare_lstm_chunks(cwru_dir, names)
    lstm_candidate, lstm_errors = _evaluate_candidate(
        "lstm_autoencoder",
        LSTM_SPLIT_STRATEGY,
        lstm_splits,
        LstmAutoencoder,
        epochs=lstm_epochs,
        artifact_path=os.path.join(artifact_dir, "lstm_autoencoder.pt") if artifact_dir else None,
    )

    candidates = [dense_candidate, lstm_candidate]
    best = max(candidates, key=lambda c: c["metrics"]["f1"])

    return {
        "id": f"TJ-CWRU-VIBRATION-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "datasetId": frozen_manifest.get("id"),
        "status": "completed",
        "candidates": candidates,
        "metrics": {"bestCandidate": best["name"], **best["metrics"]},
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
    args = parser.parse_args()

    from register_dataset import build_manifest

    sys.path.insert(
        0, os.path.normpath(os.path.join(_THIS_DIR, "..", "dataset_versions"))
    )
    from dataset_version import freeze_dataset_version  # noqa: E402

    manifest = build_manifest(data_dir=args.cwru_dir)
    frozen = freeze_dataset_version(manifest)

    report = run_training_job(args.cwru_dir, frozen, artifact_dir=args.artifact_dir)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"학습 완료: {report['id']}")
    for candidate in report["candidates"]:
        print(f"  {candidate['name']}: f1={candidate['metrics']['f1']:.3f}, "
              f"precision={candidate['metrics']['precision']:.3f}, "
              f"recall={candidate['metrics']['recall']:.3f}")
    print(f"  최적 후보: {report['metrics']['bestCandidate']}")
    print(f"저장 위치: {args.output}")
