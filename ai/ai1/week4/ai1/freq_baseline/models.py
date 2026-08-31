"""
Autoencoder 후보 모델 (AI-1, 4주차 AI_FREQ_MODEL_01)

Dense/LSTM 두 후보 모두 "정상(NORMAL) 신호만으로 재구성을 학습 -> 결함 신호는
재구성 오차가 커진다"는 전형적인 비지도 오토인코더 이상탐지 방식을 쓴다.
설계 근거는 freq_baseline_format.md 참고.
"""

import numpy as np
import torch
import torch.nn as nn


class DenseAutoencoder(nn.Module):
    """개별 윈도우 특징 벡터(1D) 재구성용 완전연결 오토인코더."""

    def __init__(self, input_dim: int, hidden_dim: int = 8):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 16),
            nn.ReLU(),
            nn.Linear(16, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 16),
            nn.ReLU(),
            nn.Linear(16, input_dim),
        )

    def forward(self, x):
        return self.net(x)


class LstmAutoencoder(nn.Module):
    """연속 윈도우 시퀀스(2D: seq_len x features) 재구성용 LSTM 오토인코더."""

    def __init__(self, input_dim: int, hidden_dim: int = 8):
        super().__init__()
        self.encoder = nn.LSTM(input_dim, hidden_dim, batch_first=True)
        self.decoder = nn.LSTM(hidden_dim, hidden_dim, batch_first=True)
        self.output_layer = nn.Linear(hidden_dim, input_dim)

    def forward(self, x):
        # x: (batch, seq_len, input_dim)
        _, (h, _) = self.encoder(x)
        seq_len = x.size(1)
        z = h[-1].unsqueeze(1).repeat(1, seq_len, 1)
        dec_out, _ = self.decoder(z)
        return self.output_layer(dec_out)


class FeatureScaler:
    """train-NORMAL 데이터로 fit한 평균/표준편차로 z-score 정규화.

    입력 마지막 축을 특징 축으로 취급해 2D(dense: N x F)/3D(lstm: N x seq x F)
    양쪽 모두 동일 코드로 처리한다(broadcasting).
    """

    def __init__(self):
        self.mean_ = None
        self.std_ = None

    @classmethod
    def from_state(cls, mean_, std_) -> "FeatureScaler":
        """저장된 평균/표준편차로 스케일러를 복원한다 (fit 없이 — 아티팩트 재로딩용)."""
        scaler = cls()
        scaler.mean_ = np.asarray(mean_, dtype=np.float64)
        scaler.std_ = np.asarray(std_, dtype=np.float64)
        return scaler

    def fit(self, matrix: np.ndarray) -> "FeatureScaler":
        flat = matrix.reshape(-1, matrix.shape[-1])
        self.mean_ = flat.mean(axis=0)
        self.std_ = flat.std(axis=0)
        self.std_[self.std_ < 1e-8] = 1.0  # 상수 특징값 0-division 방지
        return self

    def transform(self, matrix: np.ndarray) -> np.ndarray:
        return (matrix - self.mean_) / self.std_


def train_autoencoder(model: nn.Module, train_tensor: torch.Tensor, *, epochs: int = 150, lr: float = 1e-3) -> list:
    """train_tensor(정상 데이터만)로 재구성 학습. epoch별 loss 리스트를 반환."""
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    model.train()
    losses = []
    for _ in range(epochs):
        optimizer.zero_grad()
        output = model(train_tensor)
        loss = loss_fn(output, train_tensor)
        loss.backward()
        optimizer.step()
        losses.append(loss.item())
    return losses


def reconstruction_error(model: nn.Module, x_tensor: torch.Tensor) -> np.ndarray:
    """샘플별 평균 재구성 오차(MSE)를 numpy 배열로 반환."""
    model.eval()
    with torch.no_grad():
        output = model(x_tensor)
        squared_error = (output - x_tensor) ** 2
        reduce_dims = tuple(range(1, squared_error.dim()))
        error = squared_error.mean(dim=reduce_dims)
    return error.numpy()
