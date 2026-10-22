"""Bộ phân tích: RMS cửa sổ 50 ms, các chỉ số N, A, C, S, Q, LUFS và true peak.

Quy ước đo (spec chưa ghi, chốt ở trang thiết kế):
- RMS cửa sổ chữ nhật 50 ms, bước 10 ms.
- Stereo: trung bình công suất 2 kênh, dB = 10·log10(mean(x²)), không cộng +3 dB.
- Đỉnh click: đỉnh khối 5 ms cao hơn A trên 15 dB, gộp đỉnh cách nhau dưới 100 ms;
  "lớn" khi cao hơn A trên 30 dB, còn lại là "vừa".
- Đoạn yên lặng: các khung liên tiếp có RMS ≤ N + 6 dB, dài ≥ 0.5 s.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit, prange
from scipy.signal import firwin, sosfilt

WIN_S, HOP_S = 0.050, 0.010
EPS = 1e-20


def db_power(p):
    return 10.0 * np.log10(np.maximum(p, EPS))


def db_amp(a):
    return 20.0 * np.log10(np.maximum(a, 1e-10))


def frame_rms_db(x: np.ndarray, sr: int, win_s: float = WIN_S, hop_s: float = HOP_S) -> np.ndarray:
    p = np.mean(x.astype(np.float64) ** 2, axis=1)
    c = np.concatenate([[0.0], np.cumsum(p)])
    w, h = int(round(win_s * sr)), int(round(hop_s * sr))
    starts = np.arange(0, len(p) - w + 1, h)
    return db_power((c[starts + w] - c[starts]) / w)


def sample_peak_dbfs(x: np.ndarray) -> float:
    return float(db_amp(np.max(np.abs(x))))


# Bộ lọc nội suy 4x dạng polyphase. 97 hệ số (lẻ, để trễ đúng 12 mẫu gốc và các điểm nội suy rơi
# đúng vào vị trí 1/4, 2/4, 3/4 giữa 2 mẫu), dài hơn bộ 48 hệ số của ITU-R BS.1770-4 để ảnh phổ của
# nội dung gần fs/4 không làm đo lệch ~0.1 dB. Đệm 0 lên 100 hệ số = 4 pha × 25.
_TP_H = np.concatenate([firwin(97, 1.0 / 4, window=("kaiser", 8.0)) * 4, np.zeros(3)]).reshape(-1, 4).T.copy()


@njit(cache=True, parallel=True)
def _tp_kernel(x, h):
    """|x| lớn nhất sau oversample 4x, gán cho từng mẫu gốc (4 điểm nội suy ngay sau mẫu đó)."""
    n, ch = x.shape
    P, K = h.shape
    half = 12  # trễ nhóm của bộ lọc 97 hệ số = 48 mẫu tần số cao = 12 mẫu gốc
    out = np.zeros(n)
    for i in prange(n):  # mỗi mẫu độc lập nên chạy song song vẫn ra đúng từng bit
        m = 0.0
        for c in range(ch):
            for p in range(P):
                acc = 0.0
                for k in range(K):
                    j = i + half - k
                    if 0 <= j < n:
                        acc += h[p, k] * x[j, c]
                a = abs(acc)
                if a > m:
                    m = a
        out[i] = m
    return out


def true_peak_per_sample(x: np.ndarray) -> np.ndarray:
    """Đỉnh true peak gán cho từng mẫu gốc n: max các điểm nội suy giữa n−1 và n+1, và chính mẫu n."""
    blk = _tp_kernel(np.ascontiguousarray(x, dtype=np.float32), _TP_H)
    p = np.maximum(blk, np.max(np.abs(x), axis=1))
    p[1:] = np.maximum(p[1:], blk[:-1])
    return p


def true_peak_dbtp(x: np.ndarray) -> float:
    return float(db_amp(true_peak_per_sample(x).max()))


_K_CACHE: dict[int, np.ndarray] = {}


def _k_weighting(sr: int) -> np.ndarray:
    """Bộ lọc K của ITU-R BS.1770 (shelf + high-pass), hệ số tính lại theo sr như pyloudnorm."""
    if sr not in _K_CACHE:
        def shelf(G, Q, fc):
            A = 10 ** (G / 40); w0 = 2 * np.pi * fc / sr; al = np.sin(w0) / (2 * Q); c = np.cos(w0)
            b = [A * ((A + 1) + (A - 1) * c + 2 * np.sqrt(A) * al), -2 * A * ((A - 1) + (A + 1) * c),
                 A * ((A + 1) + (A - 1) * c - 2 * np.sqrt(A) * al)]
            a = [(A + 1) - (A - 1) * c + 2 * np.sqrt(A) * al, 2 * ((A - 1) - (A + 1) * c),
                 (A + 1) - (A - 1) * c - 2 * np.sqrt(A) * al]
            return np.array(b + a) / a[0]

        def hp(Q, fc):
            w0 = 2 * np.pi * fc / sr; al = np.sin(w0) / (2 * Q); c = np.cos(w0)
            b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
            a = [1 + al, -2 * c, 1 - al]
            return np.array(b + a) / a[0]

        _K_CACHE[sr] = np.stack([shelf(4.0, 1 / np.sqrt(2), 1500.0), hp(0.5, 38.0)])
    return _K_CACHE[sr]


def lufs(x: np.ndarray, sr: int) -> float:
    """Độ to tích hợp theo ITU-R BS.1770-4 (khối 400 ms chồng 75%, cổng −70 LUFS và −10 LU).
    Viết lại dạng vector cho nhanh; test đối chiếu với pyloudnorm."""
    y = sosfilt(_k_weighting(sr), x.astype(np.float64), axis=0)
    c = np.vstack([np.zeros((1, y.shape[1])), np.cumsum(y ** 2, axis=0)])
    T, step = int(round(0.4 * sr)), int(round(0.1 * sr))
    starts = np.arange(0, len(y) - T + 1, step)
    if len(starts) == 0:
        return -120.0
    z = (c[starts + T] - c[starts]) / T          # công suất từng khối, từng kênh (trọng số kênh L/R = 1)
    zs = z.sum(axis=1)
    lk = -0.691 + 10 * np.log10(np.maximum(zs, 1e-30))
    g1 = lk > -70.0
    if not g1.any():
        return -120.0
    rel = -0.691 + 10 * np.log10(zs[g1].mean()) - 10.0
    g2 = g1 & (lk > rel)
    return float(-0.691 + 10 * np.log10(zs[g2].mean()))


@dataclass(frozen=True)
class Analysis:
    sr: int
    channels: int
    duration: float
    N_db: float
    A_db: float
    C_peak_dbfs: float
    C_big: int
    C_mid: int
    S_db: float
    Q: tuple[float, float] | None
    has_silence: bool


def _clicks(x: np.ndarray, sr: int, A_db: float) -> tuple[int, int]:
    blk = max(1, int(round(0.005 * sr)))
    a = np.max(np.abs(x), axis=1)
    m = len(a) // blk
    peaks = db_amp(a[: m * blk].reshape(m, blk).max(axis=1))
    idx = np.flatnonzero(peaks > A_db + 15.0)
    big = mid = 0
    gap = int(round(0.100 / 0.005))
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and idx[j + 1] - idx[j] < gap:
            j += 1
        top = peaks[idx[i:j + 1]].max()
        if top > A_db + 30.0:
            big += 1
        else:
            mid += 1
        i = j + 1
    return big, mid


def _quietest_second(x: np.ndarray, sr: int) -> tuple[float, float]:
    p = np.mean(x.astype(np.float64) ** 2, axis=1)
    c = np.concatenate([[0.0], np.cumsum(p)])
    w, h = sr, int(round(0.25 * sr))
    starts = np.arange(0, len(p) - w + 1, h)
    k = int(np.argmin(c[starts + w] - c[starts]))  # argmin lấy vị trí sớm nhất khi bằng nhau
    return (float(starts[k] / sr), float((starts[k] + w) / sr))


def _has_silence(frames_db: np.ndarray, N_db: float, sr: int) -> bool:
    need = int(np.ceil((0.5 - WIN_S) / HOP_S)) + 1
    run = 0
    for q in frames_db <= N_db + 6.0:
        run = run + 1 if q else 0
        if run >= need:
            return True
    return False


def analyze(x: np.ndarray, sr: int) -> Analysis:
    """Đo trên tín hiệu đã lọc trầm."""
    f = frame_rms_db(x, sr)
    N = float(np.percentile(f, 10))
    A = float(np.percentile(f, 99))
    big, mid = _clicks(x, sr, A)
    silent = _has_silence(f, N, sr)
    return Analysis(
        sr=sr, channels=x.shape[1], duration=len(x) / sr,
        N_db=N, A_db=A, C_peak_dbfs=sample_peak_dbfs(x), C_big=big, C_mid=mid,
        S_db=A - N, Q=_quietest_second(x, sr) if silent else None, has_silence=silent,
    )


def count_clipped(x: np.ndarray) -> int:
    """Số mẫu bị clip: nằm trong chuỗi ≥ 3 mẫu |x| ≥ 0.999, hoặc bất kỳ mẫu > 1.0 (WAV float)."""
    a = np.max(np.abs(x), axis=1)
    over = int(np.count_nonzero(a > 1.0))
    hot = np.concatenate([[False], a >= 0.999, [False]]).astype(np.int8)
    d = np.diff(hot)
    s, e = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    runs = e - s
    return over + int(runs[runs >= 3].sum())


@dataclass(frozen=True)
class Metrics:
    noise_db: float      # p10 RMS
    action_p95: float
    action_p99: float
    peak_dbfs: float
    tp_dbtp: float
    lufs: float


def measure_output(y: np.ndarray, sr: int) -> Metrics:
    f = frame_rms_db(y, sr)
    return Metrics(
        noise_db=float(np.percentile(f, 10)),
        action_p95=float(np.percentile(f, 95)),
        action_p99=float(np.percentile(f, 99)),
        peak_dbfs=sample_peak_dbfs(y),
        tp_dbtp=true_peak_dbtp(y),
        lufs=lufs(y, sr),
    )
