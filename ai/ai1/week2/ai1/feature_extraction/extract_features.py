"""
음향/진동 신호 특징량 추출 모듈 (AI-1, 2주차 EDGE_FEATURE_01)

1주차(`ai/ai1/week1/ai1/feature_extraction/extract_features.py`) 버전을 이어받아
첨도(kurtosis) 계산을 추가한 2주차 버전. 나머지 특징량 로직은 1주차와 동일하다.

특징량:
- RMS (신호 에너지)
- Zero Crossing Rate
- Spectral Centroid / Bandwidth / Rolloff
- MFCC (Mel-Frequency Cepstral Coefficients)
- 주파수 대역별 에너지 (Band Energy)
- Kurtosis (첨도, 베어링 결함의 충격성 성분 탐지용) — 2주차 추가
"""

from dataclasses import dataclass, asdict
import numpy as np

try:
    import librosa

    _HAS_LIBROSA = True
except ImportError:
    _HAS_LIBROSA = False


@dataclass
class FeatureConfig:
    sample_rate: int = 16000
    frame_length: int = 2048
    hop_length: int = 512
    n_mfcc: int = 13
    band_edges: tuple = (0, 500, 1000, 2000, 4000, 8000)  # Hz, 대역 에너지 구간


def compute_rms(signal: np.ndarray, config: FeatureConfig) -> np.ndarray:
    """프레임 단위 RMS(에너지) 계산."""
    frame_length, hop_length = config.frame_length, config.hop_length
    n_frames = (
        1 + (len(signal) - frame_length) // hop_length
        if len(signal) >= frame_length
        else 1
    )
    rms = np.zeros(max(n_frames, 1))
    for i in range(len(rms)):
        start = i * hop_length
        frame = signal[start : start + frame_length]
        rms[i] = np.sqrt(np.mean(frame**2)) if len(frame) > 0 else 0.0
    return rms


def compute_zero_crossing_rate(signal: np.ndarray, config: FeatureConfig) -> np.ndarray:
    """프레임 단위 zero-crossing rate 계산."""
    frame_length, hop_length = config.frame_length, config.hop_length
    n_frames = (
        1 + (len(signal) - frame_length) // hop_length
        if len(signal) >= frame_length
        else 1
    )
    zcr = np.zeros(max(n_frames, 1))
    for i in range(len(zcr)):
        start = i * hop_length
        frame = signal[start : start + frame_length]
        if len(frame) > 1:
            zcr[i] = np.mean(np.abs(np.diff(np.sign(frame)))) / 2
    return zcr


def compute_kurtosis(signal: np.ndarray, config: FeatureConfig) -> np.ndarray:
    """프레임 단위 첨도(kurtosis) 계산.

    Fisher 정의(초과 첨도, excess kurtosis)를 사용한다: 정규분포는 0에 가깝고,
    베어링 결함처럼 충격성(impulsive) 파형이 섞이면 값이 커진다.
    라이브러리 의존성을 늘리지 않기 위해 numpy만으로 직접 구현.
    """
    frame_length, hop_length = config.frame_length, config.hop_length
    n_frames = (
        1 + (len(signal) - frame_length) // hop_length
        if len(signal) >= frame_length
        else 1
    )
    kurt = np.zeros(max(n_frames, 1))
    for i in range(len(kurt)):
        start = i * hop_length
        frame = signal[start : start + frame_length]
        if len(frame) > 1:
            mean = np.mean(frame)
            std = np.std(frame)
            if std > 1e-12:
                kurt[i] = np.mean((frame - mean) ** 4) / (std**4) - 3.0
    return kurt


def compute_band_energy(signal: np.ndarray, config: FeatureConfig) -> dict:
    """지정한 주파수 대역별 에너지 비율 계산 (FFT 기반)."""
    fft_vals = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(len(signal), d=1.0 / config.sample_rate)
    total_energy = np.sum(fft_vals**2) + 1e-12

    band_energy = {}
    edges = config.band_edges
    for i in range(len(edges) - 1):
        low, high = edges[i], edges[i + 1]
        mask = (freqs >= low) & (freqs < high)
        energy = np.sum(fft_vals[mask] ** 2)
        band_energy[f"band_{low}_{high}Hz"] = float(energy / total_energy)
    return band_energy


def compute_spectral_features(signal: np.ndarray, config: FeatureConfig) -> dict:
    """스펙트럴 중심(centroid), 대역폭(bandwidth), 롤오프(rolloff) 계산."""
    fft_vals = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(len(signal), d=1.0 / config.sample_rate)
    power = fft_vals**2
    total_power = np.sum(power) + 1e-12

    centroid = float(np.sum(freqs * power) / total_power)
    bandwidth = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * power) / total_power))

    cumulative = np.cumsum(power)
    rolloff_idx = np.searchsorted(cumulative, 0.85 * total_power)
    rolloff = float(freqs[min(rolloff_idx, len(freqs) - 1)])

    return {
        "spectral_centroid": centroid,
        "spectral_bandwidth": bandwidth,
        "spectral_rolloff": rolloff,
    }


def compute_mfcc(signal: np.ndarray, config: FeatureConfig) -> np.ndarray:
    """MFCC 계산. librosa가 있으면 사용, 없으면 0벡터 반환(경고 출력)."""
    if not _HAS_LIBROSA:
        print(
            "[경고] librosa 미설치 — MFCC는 0벡터로 대체됩니다. "
            "pip install librosa 후 재실행하세요."
        )
        return np.zeros(config.n_mfcc)

    mfcc = librosa.feature.mfcc(
        y=signal.astype(np.float32),
        sr=config.sample_rate,
        n_mfcc=config.n_mfcc,
        n_fft=config.frame_length,
        hop_length=config.hop_length,
    )
    return mfcc.mean(axis=1)  # 프레임 평균으로 요약


def extract_all_features(signal: np.ndarray, config: FeatureConfig = None) -> dict:
    """단일 신호에서 전체 특징량을 추출해 dict로 반환."""
    config = config or FeatureConfig()
    signal = np.asarray(signal, dtype=np.float64)

    rms = compute_rms(signal, config)
    zcr = compute_zero_crossing_rate(signal, config)
    kurtosis = compute_kurtosis(signal, config)
    spectral = compute_spectral_features(signal, config)
    band_energy = compute_band_energy(signal, config)
    mfcc = compute_mfcc(signal, config)

    features = {
        "rms_mean": float(np.mean(rms)),
        "rms_std": float(np.std(rms)),
        "zcr_mean": float(np.mean(zcr)),
        "kurtosis_mean": float(np.mean(kurtosis)),
        "kurtosis_std": float(np.std(kurtosis)),
        **spectral,
        **band_energy,
    }
    for i, val in enumerate(mfcc):
        features[f"mfcc_{i+1}"] = float(val)

    return features


if __name__ == "__main__":
    # 간단 동작 확인용 예시 (합성 신호)
    config = FeatureConfig()
    t = np.linspace(0, 2, config.sample_rate * 2, endpoint=False)
    dummy_signal = 0.5 * np.sin(2 * np.pi * 120 * t) + 0.05 * np.random.randn(len(t))

    features = extract_all_features(dummy_signal, config)
    print("추출된 특징량:")
    for k, v in features.items():
        print(f"  {k}: {v:.5f}")
