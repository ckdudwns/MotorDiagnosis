"""
AI-1 인계용 프로토타입 데이터셋 빌더

CWRU 진동 데이터 + MIMII pump 음향 데이터를 아래 요청받은 표준 컬럼 형태로 결합해
CSV/JSON으로 출력한다.

컬럼:
    timestamp, device_id, asset_id, rpm,
    vibration_rms_raw, vibration_peak_hz,
    acoustic_rms_raw, acoustic_peak_hz,
    known_vibration_label, known_acoustic_label,
    scenario_label, source, is_synthetic,
    vibration_unit, acoustic_unit

★ 중요 — 반드시 읽을 것 ★

1. 단위: 실제 mm/s, dB로 "보정"된 값이 아니다.
   - vibration_rms_raw: CWRU 가속도계 원시 출력 (단위: g, 교정 전)
   - acoustic_rms_raw: MIMII wav를 [-1, 1]로 정규화한 진폭의 RMS (dB SPL 아님, 마이크 감도 보정 없음)
   - vibration_rms_mm_s / acoustic_db 컬럼은 이번 프로토타입에서는 비워둠(None).
     실제 mm/s, dB 값이 필요하면 센서 스펙(감도, 기준 음압 등) 확보 후 별도 보정 단계가 필요함.

2. scenario_label은 "합성 페어링" 결과다.
   CWRU(모터 베어링 진동)와 MIMII(펌프 음향)는 서로 다른 실제 설비에서 별도로 녹음된 데이터라
   같은 시점에 동시 측정된 것이 아니다. 이 스크립트는 진동 샘플 1개 + 음향 샘플 1개를
   인위적으로 짝지어 하나의 "가상 설비 스냅샷" 행을 만든다.
   → 그래서 모든 행에 source="CWRU+MIMII_synthetic_pairing", is_synthetic=True 를 명시한다.
   → 실제 센서 도착 후에는 이 페어링 로직을 걷어내고 실측 동시 데이터로 교체해야 한다.

3. scenario_label 정의:
   - normal            : 진동 정상 + 음향 정상
   - vibration_anomaly  : 진동 이상 + 음향 정상
   - acoustic_anomaly   : 진동 정상 + 음향 이상
   - combined_anomaly   : 진동 이상 + 음향 이상

사용법:
    python3 build_ai1_handoff_dataset.py \\
        --cwru-dir ../data/external/cwru \\
        --mimii-pump-dir ../data/external/mimii/pump \\
        --output-dir ../data/handoff
"""

import os
import sys
import csv
import json
import random
import argparse
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from load_cwru_vibration import load_cwru_dataset  # noqa: E402
from load_mimii_acoustic import load_mimii_pump_dataset  # noqa: E402


VIBRATION_UNIT_NOTE = "raw accelerometer output (g), not calibrated to mm/s"
ACOUSTIC_UNIT_NOTE = (
    "raw normalized amplitude RMS ([-1,1] float), not calibrated to dB SPL"
)


def compute_peak_frequency(signal: np.ndarray, sample_rate: int) -> float:
    """신호에서 가장 에너지가 큰 주파수(피크 주파수)를 계산."""
    signal = np.asarray(signal, dtype=np.float64)
    fft_vals = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(len(signal), d=1.0 / sample_rate)

    if len(fft_vals) <= 1:
        return 0.0

    # DC 성분(0Hz) 제외하고 최대값 탐색
    peak_idx = np.argmax(fft_vals[1:]) + 1
    return float(freqs[peak_idx])


def compute_rms(signal: np.ndarray) -> float:
    signal = np.asarray(signal, dtype=np.float64)
    return float(np.sqrt(np.mean(signal**2)))


def vibration_status(label: str) -> str:
    return "normal" if label == "NORMAL" else "anomaly"


def acoustic_status(label: str) -> str:
    return "normal" if label == "NORMAL" else "anomaly"


def scenario_from_statuses(vib_status: str, aco_status: str) -> str:
    if vib_status == "normal" and aco_status == "normal":
        return "normal"
    if vib_status == "anomaly" and aco_status == "normal":
        return "vibration_anomaly"
    if vib_status == "normal" and aco_status == "anomaly":
        return "acoustic_anomaly"
    return "combined_anomaly"


def build_handoff_dataset(
    cwru_dir: str,
    mimii_pump_dir: str,
    output_dir: str,
    cwru_window_size: int = 2048,
    mimii_machine_ids: list = None,
    seed: int = 42,
) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    rng = random.Random(seed)

    print("=== CWRU 진동 데이터 로드 ===")
    vibration_records = load_cwru_dataset(cwru_dir, window_size=cwru_window_size)

    print("\n=== MIMII pump 음향 데이터 로드 ===")
    acoustic_records = load_mimii_pump_dataset(mimii_pump_dir, mimii_machine_ids)

    if not acoustic_records:
        print("\n[경고] 음향(MIMII) 데이터가 없어 진동 데이터만으로 행을 생성합니다.")
        print(
            "       acoustic_* 필드는 비워두고, scenario_label은 진동 기준으로만 판정됩니다."
        )

    rows = []
    base_time = datetime(2026, 1, 1, 0, 0, 0)
    asset_counter = {"vibration": 0, "combined": 0}

    if acoustic_records:
        # 진동 샘플과 음향 샘플을 합성 페어링 (셔플 후 순환 매칭)
        acoustic_pool = acoustic_records.copy()
        rng.shuffle(acoustic_pool)

        for i, vib in enumerate(vibration_records):
            aco = acoustic_pool[i % len(acoustic_pool)]

            vib_feat_rms = compute_rms(vib["signal"])
            vib_feat_peak = compute_peak_frequency(vib["signal"], vib["sample_rate"])
            aco_feat_rms = compute_rms(aco["signal"])
            aco_feat_peak = compute_peak_frequency(aco["signal"], aco["sample_rate"])

            v_status = vibration_status(vib["label"])
            a_status = acoustic_status(aco["label"])
            scenario = scenario_from_statuses(v_status, a_status)

            asset_counter["combined"] += 1
            device_id = f"SYN-DEV-{asset_counter['combined']:04d}"
            asset_id = f"SYN-ASSET-{(asset_counter['combined'] % 5) + 1:02d}"  # 가상 설비 5대 순환
            timestamp = (
                base_time + timedelta(seconds=asset_counter["combined"] * 10)
            ).isoformat()

            rows.append(
                {
                    "timestamp": timestamp,
                    "device_id": device_id,
                    "asset_id": asset_id,
                    "rpm": vib.get("rpm"),
                    "vibration_rms_raw": round(vib_feat_rms, 6),
                    "vibration_rms_mm_s": None,
                    "vibration_peak_hz": round(vib_feat_peak, 2),
                    "acoustic_rms_raw": round(aco_feat_rms, 6),
                    "acoustic_db": None,
                    "acoustic_peak_hz": round(aco_feat_peak, 2),
                    "known_vibration_label": vib["label"],
                    "known_acoustic_label": aco["label"],
                    "scenario_label": scenario,
                    "source": "CWRU+MIMII_synthetic_pairing",
                    "is_synthetic": True,
                    "vibration_unit_note": VIBRATION_UNIT_NOTE,
                    "acoustic_unit_note": ACOUSTIC_UNIT_NOTE,
                }
            )
    else:
        # 음향 데이터 없을 때: 진동 데이터만으로 행 생성
        for vib in vibration_records:
            vib_feat_rms = compute_rms(vib["signal"])
            vib_feat_peak = compute_peak_frequency(vib["signal"], vib["sample_rate"])
            v_status = vibration_status(vib["label"])
            scenario = "normal" if v_status == "normal" else "vibration_anomaly"

            asset_counter["vibration"] += 1
            device_id = f"SYN-DEV-{asset_counter['vibration']:04d}"
            asset_id = f"SYN-ASSET-{(asset_counter['vibration'] % 5) + 1:02d}"
            timestamp = (
                base_time + timedelta(seconds=asset_counter["vibration"] * 10)
            ).isoformat()

            rows.append(
                {
                    "timestamp": timestamp,
                    "device_id": device_id,
                    "asset_id": asset_id,
                    "rpm": vib.get("rpm"),
                    "vibration_rms_raw": round(vib_feat_rms, 6),
                    "vibration_rms_mm_s": None,
                    "vibration_peak_hz": round(vib_feat_peak, 2),
                    "acoustic_rms_raw": None,
                    "acoustic_db": None,
                    "acoustic_peak_hz": None,
                    "known_vibration_label": vib["label"],
                    "known_acoustic_label": None,
                    "scenario_label": scenario,
                    "source": "CWRU_only_synthetic",
                    "is_synthetic": True,
                    "vibration_unit_note": VIBRATION_UNIT_NOTE,
                    "acoustic_unit_note": None,
                }
            )

    # 저장: CSV + JSON
    json_path = os.path.join(output_dir, "ai1_handoff_dataset.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(output_dir, "ai1_handoff_dataset.csv")
    if rows:
        fieldnames = list(rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    # 요약
    summary = {}
    for r in rows:
        summary[r["scenario_label"]] = summary.get(r["scenario_label"], 0) + 1

    print(f"\n총 {len(rows)}개 행 생성")
    print(f"  JSON: {json_path}")
    print(f"  CSV : {csv_path}")
    print("scenario_label 분포:")
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}개")

    return {
        "n_rows": len(rows),
        "csv_path": csv_path,
        "json_path": json_path,
        "scenario_distribution": summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI-1 인계용 표준 스키마 데이터셋 빌드"
    )
    parser.add_argument("--cwru-dir", default="./data/external/cwru")
    parser.add_argument("--mimii-pump-dir", default="./data/external/mimii/pump")
    parser.add_argument("--output-dir", default="./data/handoff")
    parser.add_argument("--cwru-window-size", type=int, default=2048)
    parser.add_argument("--mimii-machine-ids", nargs="*", default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    build_handoff_dataset(
        cwru_dir=args.cwru_dir,
        mimii_pump_dir=args.mimii_pump_dir,
        output_dir=args.output_dir,
        cwru_window_size=args.cwru_window_size,
        mimii_machine_ids=args.mimii_machine_ids,
        seed=args.seed,
    )
