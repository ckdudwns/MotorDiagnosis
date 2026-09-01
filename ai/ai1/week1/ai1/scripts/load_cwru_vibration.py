"""
CWRU 베어링 진동 데이터(.mat) 로더 (AI-1)

**물리 베어링 specimen 단위 매핑.** CWRU 12k Drive-End는 건강한 베어링(베이스라인)
1개와, 내륜(IR)·볼(Ball)·외륜(OR) 결함을 EDM 가공한 베어링을 결함 직경
(0.007"/0.014"/0.021")별로 갖는다. 각 베어링을 0/1/2/3 HP 부하에서 측정한 4개
`.mat` 파일이 **하나의 물리 specimen**이다 — 파일별로 독립 자산이 아니다.

    specimen                파일(0/1/2/3 HP)          label
    CWRU-NORMAL-BASELINE     97/98/99/100             NORMAL          (건강 베어링 1개뿐)
    CWRU-IR-0007             105/106/107/108          BEARING_FAULT_INNER
    CWRU-IR-0014             169/170/171/172          BEARING_FAULT_INNER
    CWRU-IR-0021             209/210/211/212          BEARING_FAULT_INNER
    CWRU-BALL-0007           118/119/120/121          BEARING_FAULT_BALL
    CWRU-BALL-0014           185/186/187/188          BEARING_FAULT_BALL
    CWRU-BALL-0021           222/223/224/225          BEARING_FAULT_BALL
    CWRU-OR-0007             130/131/132/133          BEARING_FAULT_OUTER
    CWRU-OR-0014             197/198/199/200          BEARING_FAULT_OUTER
    CWRU-OR-0021             234/235/236/237          BEARING_FAULT_OUTER

group split은 **specimen 단위**로 해야 같은 베어링이 train/validation/test에 함께
들어가는 누수를 막는다. NORMAL specimen은 CWRU에 1개뿐이라 specimen 독립 3-way
분할은 만들 수 없다(`InsufficientAssetGroupsError`) — 데모용으로는 부하조건 기준
holdout(`operating_condition_holdout`)을 opt-in으로 쓰되 독립 검증이 아님을 명시한다.

원본 .mat 파일은 scipy.io.loadmat으로 읽으며, 키 이름은 'X{번호}_DE_time'
(Drive-End 가속도 신호) 형식이다. 파일명 접두 번호와 일치하는 키를 우선 선택한다
— 99.mat은 X098_DE_time과 X099_DE_time을 함께 담고 있어(X098 쌍은 98.mat과 중복)
접미사만으로 탐색하면 엉뚱한 쌍을 고를 수 있다. 일부 원본(98/99.mat)에는 RPM 키가
없어 부하 조건별 근사 정격 RPM(LOAD_RPM_BY_FILE)으로 채운다.

배치 방법:
    data/external/cwru/97.mat ... 237.mat (위 40개 중 확보된 것)
    (다운로드: https://engineering.case.edu/bearingdatacenter/welcome)
없는 파일은 로더가 건너뛴다. 24파일(169~237)은 추가 배치 예정.
"""

import os
import glob
import numpy as np

try:
    import scipy.io as sio
except ImportError:
    sio = None

DEFAULT_SAMPLE_RATE = 12000  # CWRU 12k Drive-End 기준

# 부하 조건(0/1/2/3 HP)별 근사 정격 RPM (CWRU 데이터센터 문서 표).
_LOAD_HP_RPM = (1797, 1772, 1750, 1730)

# (specimen_id, label, [0HP, 1HP, 2HP, 3HP 파일 번호]) — 같은 베어링의 4개 부하 측정.
_CWRU_SPECIMENS = [
    ("CWRU-NORMAL-BASELINE", "NORMAL", [97, 98, 99, 100]),
    ("CWRU-IR-0007", "BEARING_FAULT_INNER", [105, 106, 107, 108]),
    ("CWRU-IR-0014", "BEARING_FAULT_INNER", [169, 170, 171, 172]),
    ("CWRU-IR-0021", "BEARING_FAULT_INNER", [209, 210, 211, 212]),
    ("CWRU-BALL-0007", "BEARING_FAULT_BALL", [118, 119, 120, 121]),
    ("CWRU-BALL-0014", "BEARING_FAULT_BALL", [185, 186, 187, 188]),
    ("CWRU-BALL-0021", "BEARING_FAULT_BALL", [222, 223, 224, 225]),
    ("CWRU-OR-0007", "BEARING_FAULT_OUTER", [130, 131, 132, 133]),
    ("CWRU-OR-0014", "BEARING_FAULT_OUTER", [197, 198, 199, 200]),
    ("CWRU-OR-0021", "BEARING_FAULT_OUTER", [234, 235, 236, 237]),
]

FILE_LABEL_MAP: dict[str, str] = {}       # "97.mat" -> "NORMAL"
SPECIMEN_BY_FILE: dict[str, str] = {}     # "97.mat" -> "CWRU-NORMAL-BASELINE"
LOAD_HP_BY_FILE: dict[str, int] = {}      # "97.mat" -> 0
LOAD_RPM_BY_FILE: dict[str, int] = {}     # "97" -> 1797 (RPM 키 없는 파일 폴백)
for _specimen_id, _label, _file_nums in _CWRU_SPECIMENS:
    for _hp, _num in enumerate(_file_nums):
        _fname = f"{_num}.mat"
        FILE_LABEL_MAP[_fname] = _label
        SPECIMEN_BY_FILE[_fname] = _specimen_id
        LOAD_HP_BY_FILE[_fname] = _hp
        LOAD_RPM_BY_FILE[str(_num)] = _LOAD_HP_RPM[_hp]


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
        "specimen_id": "CWRU-NORMAL-BASELINE",   # 물리 베어링 식별자 (group split 단위)
        "load_hp": 0,                            # 0/1/2/3 HP 부하 조건
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
                    "specimen_id": SPECIMEN_BY_FILE[filename],
                    "load_hp": LOAD_HP_BY_FILE[filename],
                    "modality": "vibration",
                    "sample_rate": sample_rate,
                    "rpm": rpm,
                    "signal": seg,
                }
            )

        print(f"{filename} → {len(segments)}개 윈도우 ({label}, {SPECIMEN_BY_FILE[filename]})")

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
