"""
합성 음향 신호 생성기 (AI-1, 센서 도착 전 임시 대체용)

정상음 / 베어링 이상음 / 마찰음 / 불균형 추정음을 흉내낸 합성 신호를 생성해
특징량 추출·라벨링·데이터셋 파이프라인을 미리 검증하기 위한 스크립트.

실제 센서 데이터가 들어오면 이 스크립트 대신 실 데이터를 사용한다.
"""

import numpy as np
import argparse
import os
import json

SAMPLE_RATE = 16000
DURATION_SEC = 2.0


def _time_axis(sample_rate=SAMPLE_RATE, duration=DURATION_SEC):
    return np.linspace(0, duration, int(sample_rate * duration), endpoint=False)


def generate_normal(
    sample_rate=SAMPLE_RATE, duration=DURATION_SEC, base_freq=120.0, noise_level=0.03
):
    """정상음: 일정한 저주파 험(hum) + 낮은 노이즈."""
    t = _time_axis(sample_rate, duration)
    signal = 0.5 * np.sin(2 * np.pi * base_freq * t)
    signal += noise_level * np.random.randn(len(t))
    return signal


def generate_bearing_fault(
    sample_rate=SAMPLE_RATE,
    duration=DURATION_SEC,
    base_freq=120.0,
    impulse_rate_hz=30.0,
    noise_level=0.05,
):
    """베어링 이상음: 기본 험 + 주기적 고주파 임펄스(클릭성 잡음)."""
    t = _time_axis(sample_rate, duration)
    signal = 0.3 * np.sin(2 * np.pi * base_freq * t)

    impulse_period = int(sample_rate / impulse_rate_hz)
    impulses = np.zeros(len(t))
    for i in range(0, len(t), impulse_period):
        width = min(20, len(t) - i)
        impulses[i : i + width] += np.hanning(width) * 0.8

    signal += impulses
    signal += noise_level * np.random.randn(len(t))
    return signal


def generate_friction(
    sample_rate=SAMPLE_RATE, duration=DURATION_SEC, base_freq=120.0, noise_level=0.25
):
    """마찰음: 넓은 대역의 거친 노이즈가 우세."""
    t = _time_axis(sample_rate, duration)
    signal = 0.2 * np.sin(2 * np.pi * base_freq * t)
    signal += noise_level * np.random.randn(len(t))  # 강한 광대역 노이즈
    return signal


def generate_imbalance(
    sample_rate=SAMPLE_RATE, duration=DURATION_SEC, base_freq=120.0, noise_level=0.03
):
    """불균형 추정음: 회전 주기와 동기화된 진폭 변조(amplitude modulation)."""
    t = _time_axis(sample_rate, duration)
    rotation_freq = base_freq / 60.0 * 10  # 임의 배율, 실제는 RPM 기반 계산 필요
    modulation = 1 + 0.6 * np.sin(2 * np.pi * rotation_freq * t)
    signal = 0.4 * modulation * np.sin(2 * np.pi * base_freq * t)
    signal += noise_level * np.random.randn(len(t))
    return signal


GENERATORS = {
    "NORMAL": generate_normal,
    "BEARING_FAULT": generate_bearing_fault,
    "FRICTION": generate_friction,
    "IMBALANCE": generate_imbalance,
}


def generate_dataset(
    output_dir: str, n_per_label: int = 5, sample_rate: int = SAMPLE_RATE
):
    """라벨별로 n_per_label개씩 합성 신호를 생성해 .npy + 메타데이터(labels.json)로 저장."""
    os.makedirs(output_dir, exist_ok=True)
    metadata = []

    for label, gen_fn in GENERATORS.items():
        for i in range(n_per_label):
            signal = gen_fn(sample_rate=sample_rate)
            sample_id = f"{label}_{i:03d}"
            file_path = os.path.join(output_dir, f"{sample_id}.npy")
            np.save(file_path, signal)
            metadata.append(
                {
                    "sample_id": sample_id,
                    "label": label,
                    "file_path": file_path,
                    "sample_rate": sample_rate,
                    "duration_sec": DURATION_SEC,
                    "source": "synthetic",
                }
            )

    meta_path = os.path.join(output_dir, "labels.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="합성 음향 신호 데이터셋 생성")
    parser.add_argument(
        "--output-dir", default="./synthetic_data", help="출력 디렉토리"
    )
    parser.add_argument("--n-per-label", type=int, default=5, help="라벨당 생성 개수")
    args = parser.parse_args()

    metadata = generate_dataset(args.output_dir, args.n_per_label)
    print(f"{len(metadata)}개 합성 샘플 생성 완료 → {args.output_dir}")
