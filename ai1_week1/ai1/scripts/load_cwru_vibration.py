"""
CWRU 베어링 진동 데이터(.mat) 로더 (AI-1)

파일명 → 라벨 매핑 (프로토타입용, 요청받은 4개 파일 기준):
    97.mat  → NORMAL
    105.mat → BEARING_FAULT_INNER  (내륜 이상)
    118.mat → BEARING_FAULT_BALL   (볼 이상)
    130.mat → BEARING_FAULT_OUTER  (외륜 이상)

원본 .mat 파일은 scipy.io.loadmat으로 읽으며, 키 이름은
'X{번호}_DE_time' (Drive-End 가속도 신호) 형식을 사용한다.
파일마다 X번호가 조금씩 다를 수 있어 접미사(_DE_time)로 유연하게 탐색한다.

배치 방법:
    data/external/cwru/97.mat
    data/external/cwru/105.mat
    data/external/cwru/118.mat
    data/external/cwru/130.mat
    (다운로드: https://engineering.case.edu/bearingdatacenter/welcome)
"""

import os
import glob
import numpy as np

try:
    import scipy.io as sio
except ImportError:
    sio = None

# 요청받은 4개 파일 매핑
FILE_LABEL_MAP = {
    "97.mat": "NORMAL",
    "105.mat": "BEARING_FAULT_INNER",
    "118.mat": "BEARING_FAULT_BALL",
    "130.mat": "BEARING_FAULT_OUTER",
}

DEFAULT_SAMPLE_RATE = 12000  # CWRU 12k Drive-End 기준


def _find_de_time_key(mat_dict: dict) -> str:
    """mat 파일 dict에서 '_DE_time'으로 끝나는 키를 찾는다."""
    candidates = [k for k in mat_dict.keys() if k.endswith("_DE_time")]
    if not candidates:
        # DE 채널이 없으면 FE_time이라도 사용 (경고 포함)
        candidates = [k for k in mat_dict.keys() if k.endswith("_FE_time")]
        if candidates:
            print(f"[경고] DE_time 채널 없음, FE_time으로 대체: {candidates[0]}")
    if not candidates:
        raise KeyError(
            f"DE_time/FE_time 키를 찾을 수 없음. 존재하는 키: {list(mat_dict.keys())}"
        )
    return candidates[0]


def load_mat_signal(file_path: str) -> np.ndarray:
    """단일 .mat 파일에서 진동 신호(1차원 배열)를 로드."""
    if sio is None:
        raise ImportError(
            "scipy가 필요합니다: pip install scipy --break-system-packages"
        )

    mat = sio.loadmat(file_path)
    key = _find_de_time_key(mat)
    signal = mat[key].squeeze().astype(np.float64)
    return signal


def load_mat_signal_and_rpm(file_path: str) -> tuple:
    """단일 .mat 파일에서 (진동 신호, RPM)을 로드. RPM 키 없으면 None."""
    if sio is None:
        raise ImportError(
            "scipy가 필요합니다: pip install scipy --break-system-packages"
        )

    mat = sio.loadmat(file_path)
    key = _find_de_time_key(mat)
    signal = mat[key].squeeze().astype(np.float64)

    rpm_keys = [k for k in mat.keys() if k.endswith("RPM")]
    rpm = float(np.asarray(mat[rpm_keys[0]]).squeeze()) if rpm_keys else None

    return signal, rpm


def segment_signal(
    signal: np.ndarray, window_size: int = 2048, hop_size: int = 2048
) -> list:
    """긴 진동 신호를 고정 길이 윈도우로 분할 (특징량 추출 단위)."""
    segments = []
    for start in range(0, len(signal) - window_size + 1, hop_size):
        segments.append(signal[start : start + window_size])
    return segments


def load_cwru_dataset(
    data_dir: str,
    window_size: int = 2048,
    hop_size: int = 2048,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> list:
    """
    data_dir 안의 97.mat/105.mat/118.mat/130.mat 을 모두 읽어
    윈도우 분할 + 라벨을 붙인 레코드 리스트로 반환.

    반환 레코드 예:
    {
        "sample_id": "97_0000",
        "label": "NORMAL",
        "source_label": "97.mat",
        "modality": "vibration",
        "sample_rate": 12000,
        "signal": np.ndarray,
    }
    """
    records = []

    for filename, label in FILE_LABEL_MAP.items():
        file_path = os.path.join(data_dir, filename)
        if not os.path.exists(file_path):
            print(f"[건너뜀] 파일 없음: {file_path}")
            continue

        signal, rpm = load_mat_signal_and_rpm(file_path)
        segments = segment_signal(signal, window_size, hop_size)

        base_id = filename.replace(".mat", "")
        for i, seg in enumerate(segments):
            records.append(
                {
                    "sample_id": f"{base_id}_{i:04d}",
                    "label": label,
                    "source_label": filename,
                    "modality": "vibration",
                    "sample_rate": sample_rate,
                    "rpm": rpm,
                    "signal": seg,
                }
            )

        print(f"{filename} → {len(segments)}개 윈도우 ({label})")

    return records


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="CWRU 진동 데이터 로드 및 세그먼트 분할"
    )
    parser.add_argument(
        "--data-dir", default="./data/external/cwru", help="97.mat 등이 있는 폴더"
    )
    parser.add_argument("--window-size", type=int, default=2048)
    args = parser.parse_args()

    records = load_cwru_dataset(args.data_dir, window_size=args.window_size)
    print(f"\n총 {len(records)}개 샘플 로드 완료")
