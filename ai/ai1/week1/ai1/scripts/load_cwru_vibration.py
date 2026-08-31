"""
CWRU 베어링 진동 데이터(.mat) 로더 (AI-1)

파일명 → 라벨 매핑 (라벨당 부하조건 4개 = 0/1/2/3 HP, 총 16개 파일):
    97/98/99/100.mat    → NORMAL
    105/106/107/108.mat → BEARING_FAULT_INNER  (내륜 이상)
    118/119/120/121.mat → BEARING_FAULT_BALL   (볼 이상)
    130/131/132/133.mat → BEARING_FAULT_OUTER  (외륜 이상)

라벨당 파일을 4개로 늘린 것은 3주차 group_split(원본 파일=자산 단위 분할)이
라벨별로 독립 그룹을 확보해 리크 없는 train/validation/test 분할을 만들 수
있게 하기 위함이다 (라벨당 파일 1개면 InsufficientAssetGroupsError).

원본 .mat 파일은 scipy.io.loadmat으로 읽으며, 키 이름은
'X{번호}_DE_time' (Drive-End 가속도 신호) 형식을 사용한다. 파일명 접두
번호와 일치하는 키를 우선 선택한다 — 99.mat은 X098_DE_time과 X099_DE_time을
함께 담고 있고(X098 쌍은 98.mat과 완전 중복) 접미사만으로 탐색하면 엉뚱한
쌍을 고를 수 있기 때문이다.

일부 원본(98/99.mat)에는 RPM 키가 없다 — 파일 번호로 부하 조건을 역추정해
LOAD_RPM_BY_FILE의 근사 정격 RPM으로 채운다.

배치 방법:
    data/external/cwru/97.mat ... data/external/cwru/133.mat (위 16개)
    (다운로드: https://engineering.case.edu/bearingdatacenter/welcome)
"""

import os
import glob
import numpy as np

try:
    import scipy.io as sio
except ImportError:
    sio = None

# 라벨당 부하조건 4개(0/1/2/3 HP) — group split이 라벨별 독립 그룹을 확보하도록 확장
FILE_LABEL_MAP = {
    "97.mat": "NORMAL",
    "98.mat": "NORMAL",
    "99.mat": "NORMAL",
    "100.mat": "NORMAL",
    "105.mat": "BEARING_FAULT_INNER",
    "106.mat": "BEARING_FAULT_INNER",
    "107.mat": "BEARING_FAULT_INNER",
    "108.mat": "BEARING_FAULT_INNER",
    "118.mat": "BEARING_FAULT_BALL",
    "119.mat": "BEARING_FAULT_BALL",
    "120.mat": "BEARING_FAULT_BALL",
    "121.mat": "BEARING_FAULT_BALL",
    "130.mat": "BEARING_FAULT_OUTER",
    "131.mat": "BEARING_FAULT_OUTER",
    "132.mat": "BEARING_FAULT_OUTER",
    "133.mat": "BEARING_FAULT_OUTER",
}

DEFAULT_SAMPLE_RATE = 12000  # CWRU 12k Drive-End 기준

# 부하 조건(HP)별 근사 정격 RPM (CWRU 데이터센터 문서 표).
# 98/99.mat 등 일부 원본에는 X###RPM 키가 없어, 파일 번호로 부하 조건을
# 역추정해 이 값으로 채운다. None으로 두면 compatibility.rpmRange가 파일마다
# 들쭉날쭉해지고 domainGap 기록도 빈다.
LOAD_RPM_BY_FILE = {
    "97": 1797, "105": 1797, "118": 1797, "130": 1797,   # 0 HP
    "98": 1772, "106": 1772, "119": 1772, "131": 1772,   # 1 HP
    "99": 1750, "107": 1750, "120": 1750, "132": 1750,   # 2 HP
    "100": 1730, "108": 1730, "121": 1730, "133": 1730,  # 3 HP
}


def _base_id_from_path(file_path: str) -> str:
    """'.../99.mat' -> '99' (파일명에서 확장자를 뗀 접두 번호)."""
    return os.path.splitext(os.path.basename(file_path))[0]


def _find_de_time_key(mat_dict: dict, base_id: str = None) -> str:
    """mat 파일 dict에서 Drive-End 신호 키를 찾는다.

    99.mat에는 X098_DE_time과 X099_DE_time이 함께 들어있다(X098 쌍은 98.mat과
    완전 중복). base_id가 주어지면 파일명 접두 번호와 일치하는 키
    (예: '99' -> 'X099_DE_time', CWRU 규칙상 3자리 zero-pad)를 우선 선택한다.
    일치 키가 없으면 기존처럼 첫 _DE_time -> _FE_time 순으로 폴백한다.
    """
    if base_id is not None:
        try:
            preferred = f"X{int(base_id):03d}_DE_time"
        except (TypeError, ValueError):
            preferred = None
        if preferred and preferred in mat_dict:
            return preferred

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
    key = _find_de_time_key(mat, _base_id_from_path(file_path))
    signal = mat[key].squeeze().astype(np.float64)
    return signal


def load_mat_signal_and_rpm(file_path: str) -> tuple:
    """단일 .mat 파일에서 (진동 신호, RPM)을 로드.

    RPM 키가 없으면(98/99.mat) 파일 번호로 부하 조건을 역추정해
    LOAD_RPM_BY_FILE의 근사 정격 RPM으로 채운다. 그래도 알 수 없으면 None.
    """
    if sio is None:
        raise ImportError(
            "scipy가 필요합니다: pip install scipy --break-system-packages"
        )

    base = _base_id_from_path(file_path)
    mat = sio.loadmat(file_path)
    key = _find_de_time_key(mat, base)
    signal = mat[key].squeeze().astype(np.float64)

    rpm_keys = [k for k in mat.keys() if k.endswith("RPM")]
    if rpm_keys:
        rpm = float(np.asarray(mat[rpm_keys[0]]).squeeze())
    else:
        fallback = LOAD_RPM_BY_FILE.get(base)  # RPM 키 없는 파일은 부하 조건으로 보정
        rpm = float(fallback) if fallback is not None else None

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
