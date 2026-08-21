"""
MIMII pump 음향 데이터(0_dB_pump.zip) 로더 (AI-1)

압축 해제 시 기대 구조:
    pump/id_00/normal/00000000.wav
    pump/id_00/abnormal/00000000.wav
    pump/id_02/normal/...
    ...

각 wav는 10초, 16kHz, 다채널(마이크 배열) 녹음. 여기서는 채널 0만 사용한다.
normal → NORMAL, abnormal → PUMP_ANOMALY 로 매핑 (프로토타입용 단순 라벨).

배치 방법:
    1. 0_dB_pump.zip 압축 해제
    2. 압축 해제된 'pump' 폴더를 data/external/mimii/pump 에 위치
       (즉 data/external/mimii/pump/id_00/normal/*.wav 형태가 되도록)
"""

import os
import glob
import numpy as np

try:
    from scipy.io import wavfile
except ImportError:
    wavfile = None

LABEL_MAP = {
    "normal": "NORMAL",
    "abnormal": "PUMP_ANOMALY",
}


def load_wav_signal(file_path: str) -> tuple:
    """wav 파일을 로드해 (sample_rate, mono_signal) 반환. 다채널이면 첫 채널만 사용."""
    if wavfile is None:
        raise ImportError(
            "scipy가 필요합니다: pip install scipy --break-system-packages"
        )

    sample_rate, data = wavfile.read(file_path)
    if data.ndim > 1:
        data = data[:, 0]  # 첫 채널(마이크 0)만 사용

    # 정수형 PCM이면 float로 정규화
    if np.issubdtype(data.dtype, np.integer):
        max_val = np.iinfo(data.dtype).max
        data = data.astype(np.float64) / max_val
    else:
        data = data.astype(np.float64)

    return sample_rate, data


def load_mimii_pump_dataset(pump_dir: str, machine_ids: list = None) -> list:
    """
    pump_dir 예: data/external/mimii/pump
    machine_ids 예: ["id_00", "id_02"] (None이면 존재하는 전체 사용)

    반환 레코드 예:
    {
        "sample_id": "id_00_normal_00000000",
        "label": "NORMAL",
        "source_label": "normal",
        "modality": "acoustic",
        "sample_rate": 16000,
        "signal": np.ndarray,
        "machine_id": "id_00",
    }
    """
    records = []

    if machine_ids is None:
        machine_ids = (
            sorted(
                [
                    d
                    for d in os.listdir(pump_dir)
                    if os.path.isdir(os.path.join(pump_dir, d)) and d.startswith("id_")
                ]
            )
            if os.path.exists(pump_dir)
            else []
        )

    if not machine_ids:
        print(f"[경고] machine id 폴더를 찾을 수 없음: {pump_dir}")
        return records

    for machine_id in machine_ids:
        for condition, label in LABEL_MAP.items():
            condition_dir = os.path.join(pump_dir, machine_id, condition)
            if not os.path.isdir(condition_dir):
                continue

            wav_files = sorted(glob.glob(os.path.join(condition_dir, "*.wav")))
            for wav_path in wav_files:
                sample_rate, signal = load_wav_signal(wav_path)
                sample_id = f"{machine_id}_{condition}_{os.path.splitext(os.path.basename(wav_path))[0]}"

                records.append(
                    {
                        "sample_id": sample_id,
                        "label": label,
                        "source_label": condition,
                        "modality": "acoustic",
                        "sample_rate": sample_rate,
                        "signal": signal,
                        "machine_id": machine_id,
                    }
                )

            print(f"{machine_id}/{condition} → {len(wav_files)}개 파일 ({label})")

    return records


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MIMII pump 음향 데이터 로드")
    parser.add_argument(
        "--pump-dir",
        default="./data/external/mimii/pump",
        help="id_00, id_02 등이 있는 pump 폴더",
    )
    parser.add_argument(
        "--machine-ids", nargs="*", default=None, help="예: id_00 id_02 (생략 시 전체)"
    )
    args = parser.parse_args()

    records = load_mimii_pump_dataset(args.pump_dir, args.machine_ids)
    print(f"\n총 {len(records)}개 샘플 로드 완료")
