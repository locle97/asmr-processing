"""Các khối xử lý của pipeline. Tự viết để không có gain bù ẩn ở đâu cả.

Mọi bộ xử lý động tính MỘT đường gain chung (limiter theo max(|L|, |R|), nén/expander theo
công suất trung bình 2 kênh) và nhân cho cả 2 kênh,
nên tỉ lệ trái/phải giữ nguyên. Numba chạy với fastmath=False để lặp lại được từng bit.
"""

from __future__ import annotations

import numpy as np
from numba import njit
from scipy.ndimage import minimum_filter1d
from scipy.signal import butter, fftconvolve, istft, sosfiltfilt, stft

from .measure import true_peak_per_sample


def _coef(ms: float, sr: int) -> float:
    return float(np.exp(-1.0 / (ms * 1e-3 * sr))) if ms > 0 else 0.0


# ---------- 1. Lọc trầm ----------

def highpass(x: np.ndarray, sr: int, hz: float = 80.0) -> np.ndarray:
    """Butterworth bậc 1 lọc 2 chiều: đáp ứng biên độ bậc 2 (−6 dB tại 80 Hz), pha bằng 0."""
    sos = butter(1, hz, "highpass", fs=sr, output="sos")
    return sosfiltfilt(sos, x.astype(np.float64), axis=0).astype(np.float32)


# ---------- 2. Giảm ồn (spectral gating tĩnh) ----------

def _nperseg(sr: int) -> int:
    return int(2 ** round(np.log2(0.0427 * sr)))  # ~43 ms: 2048 ở 48 kHz


def _tri(n: int) -> np.ndarray:
    n = max(1, n)
    w = np.concatenate([np.linspace(0, 1, n + 1, endpoint=False)[1:], np.linspace(1, 0, n + 2)[:-1]])
    return w


def denoise(x: np.ndarray, sr: int, noise_clip: tuple[float, float], reduce_db: float,
            n_std: float = 1.5, block_s: float = 20.0) -> np.ndarray:
    """Giống noisereduce stationary nhưng mặt nạ tính trên kênh mid và dùng chung cho 2 kênh,
    nên không làm lệch trái/phải. Chạy theo khúc 20 s có chồng lấp để không tốn RAM với file 10 phút."""
    if reduce_db <= 0:
        return x.copy()
    nper = _nperseg(sr)
    hop = nper // 4
    kw = dict(fs=sr, nperseg=nper, noverlap=nper - hop, window="hann", boundary="even", padded=True)

    mid = x.mean(axis=1).astype(np.float64)
    a, b = int(noise_clip[0] * sr), int(noise_clip[1] * sr)
    _, _, Zn = stft(mid[a:b], **kw)
    nd = 20 * np.log10(np.abs(Zn) + 1e-12)
    thresh = (nd.mean(axis=1) + n_std * nd.std(axis=1))[:, None]

    kf = _tri(int(round(500.0 / (sr / nper))))
    kt = _tri(int(round(0.050 * sr / hop)))
    kern = np.outer(kf, kt)
    kern /= kern.sum()
    prop = 1.0 - 10 ** (-reduce_db / 20.0)

    def process(seg: np.ndarray) -> np.ndarray:
        n = len(seg)
        _, _, Zm = stft(seg.mean(axis=1).astype(np.float64), **kw)
        mask = (20 * np.log10(np.abs(Zm) + 1e-12) > thresh).astype(np.float64)
        mask = np.clip(fftconvolve(mask, kern, mode="same"), 0.0, 1.0)
        gain = 1.0 - prop * (1.0 - mask)
        out = np.empty((n, seg.shape[1]), dtype=np.float32)
        for c in range(seg.shape[1]):
            _, _, Z = stft(seg[:, c].astype(np.float64), **kw)
            _, y = istft(Z * gain, fs=sr, nperseg=nper, noverlap=nper - hop, window="hann")
            out[:, c] = y[:n]
        return out

    n = len(x)
    blk, ov = int(block_s * sr), int(1.0 * sr)
    if n <= blk + ov:
        return process(x)
    y = np.zeros_like(x)
    fade = np.linspace(0.0, 1.0, ov, dtype=np.float32)[:, None]
    s = 0
    while s < n:
        e = min(n, s + blk + ov)
        seg = process(x[max(0, s - ov):e])
        if s > 0:
            seg = seg[: e - (s - ov)]
            head = seg[:ov] * fade
            y[s - ov:s] = y[s - ov:s] * (1 - fade) + head
            y[s:e] = seg[ov:]
        else:
            y[s:e] = seg
        s += blk
    return y


# ---------- 3. Tăng âm lượng ----------

def apply_gain(x: np.ndarray, db: float) -> np.ndarray:
    return (x * np.float32(10 ** (db / 20.0))).astype(np.float32)


# ---------- bộ bám đường bao dùng chung ----------

@njit(cache=True)
def _smooth_gr(target, a_up, a_down, hold):
    """Làm mượt mức giảm gain (dB, ≥ 0). a_up khi mức giảm tăng, a_down khi giảm.
    hold: số mẫu giữ nguyên trước khi được tăng mức giảm (dùng cho expander)."""
    out = np.empty_like(target)
    g = target[0]
    cnt = 0
    for n in range(len(target)):
        t = target[n]
        if t > g:
            if cnt < hold:
                cnt += 1
            else:
                g = t + (g - t) * a_up
        else:
            cnt = 0
            g = t + (g - t) * a_down
        out[n] = g
    return out


@njit(cache=True)
def _one_pole(p, a):
    out = np.empty_like(p)
    s = p[0]
    for n in range(len(p)):
        s = p[n] + (s - p[n]) * a
        out[n] = s
    return out


def _detector_db(x: np.ndarray, sr: int, rms_ms: float = 10.0) -> np.ndarray:
    """Mức RMS dùng cho nén/expander: trung bình công suất 2 kênh, giống cách bộ phân tích đo,
    để ngưỡng tính từ số đo (ví dụ âm nền + 4 dB) khớp với cái detector thấy."""
    p = np.mean(x.astype(np.float64) ** 2, axis=1)
    return 10 * np.log10(np.maximum(_one_pole(p, _coef(rms_ms, sr)), 1e-20))


# ---------- 4 & 7. Limiter lookahead, KHÔNG gain bù ----------

@njit(cache=True)
def _release(g_att, a_rel):
    out = np.empty_like(g_att)
    g = 1.0
    for n in range(len(g_att)):
        t = g_att[n]
        g = t if t < g else t + (g - t) * a_rel
        out[n] = g
    return out


def limiter_gain(peak: np.ndarray, thr_db: float, sr: int, look_ms: float, rel_ms: float) -> np.ndarray:
    """Đường gain ≤ 1 sao cho peak·gain ≤ ngưỡng ở mọi mẫu.

    Xử lý offline nên lookahead được tính ngay trên trục thời gian gốc (không gây trễ):
    min tiến L mẫu rồi trung bình lùi L+1 mẫu cho ra dốc xuống mượt trong 5 ms và vẫn luôn
    ≤ gain cần thiết tại mỗi mẫu; sau đó release mũ chỉ làm gain hồi lên chậm hơn.
    Tín hiệu luôn dưới ngưỡng cho gain đúng 1.0: không có gain bù."""
    T = 10 ** (thr_db / 20.0)
    L = max(1, int(round(look_ms * 1e-3 * sr)))
    need = np.minimum(1.0, T / np.maximum(peak.astype(np.float64), 1e-12))
    padded = np.concatenate([np.ones(L), need, np.ones(L)])
    # fwd[i] = min(padded[i .. i+L])
    fwd = minimum_filter1d(padded, size=L + 1, mode="nearest", origin=-((L + 1) // 2))
    c = np.concatenate([[0.0], np.cumsum(fwd)])
    # trung bình lùi: avg[i] = mean(fwd[i-L .. i]) cho i ứng với mẫu gốc i-L
    idx = np.arange(L, L + len(need))
    att = (c[idx + 1] - c[idx - L]) / (L + 1)
    g = _release(att, _coef(rel_ms, sr))
    return np.minimum(g, 1.0)


def limiter(x: np.ndarray, thr_db: float, sr: int, look_ms: float = 5.0, rel_ms: float = 40.0):
    """Bước 4: cắt click theo đỉnh mẫu. Trả (tín hiệu, % thời gian đang giảm gain)."""
    g = limiter_gain(np.max(np.abs(x), axis=1), thr_db, sr, look_ms, rel_ms)
    return (x * g[:, None]).astype(np.float32), float(np.mean(g < 0.9999) * 100)


def tp_limiter(x: np.ndarray, thr_db: float, sr: int, look_ms: float = 5.0, rel_ms: float = 40.0,
               max_pass: int = 3) -> np.ndarray:
    """Bước 7: limiter theo true peak (oversample 4x). Đo lại sau mỗi lượt vì gain thay đổi
    giữa các mẫu có thể làm đỉnh nội suy lệch chút ít; tối đa 3 lượt."""
    y = x
    for _ in range(max_pass):
        p = true_peak_per_sample(y)
        if p.max() <= 10 ** (thr_db / 20.0):
            break
        g = limiter_gain(p, thr_db - 0.05, sr, look_ms, rel_ms)
        y = (y * g[:, None]).astype(np.float32)
    return y


# ---------- 5. Nén ----------

def compressor(x: np.ndarray, sr: int, thr_db: float, ratio: float, att_ms: float, rel_ms: float,
               makeup_db: float) -> np.ndarray:
    """Nén 3:1, gain bù là tham số rõ ràng (không tự tính)."""
    lvl = _detector_db(x, sr)
    target = (1.0 - 1.0 / ratio) * np.maximum(0.0, lvl - thr_db)
    gr = _smooth_gr(target, _coef(att_ms, sr), _coef(rel_ms, sr), 0)
    g = 10 ** ((makeup_db - gr) / 20.0)
    return (x * g[:, None]).astype(np.float32)


# ---------- 6. Hạ âm nền (expander) ----------

def expander(x: np.ndarray, sr: int, thr_db: float, ratio: float, att_ms: float, rel_ms: float,
             max_cut_db: float, hold_ms: float = 50.0) -> np.ndarray:
    """Dưới ngưỡng hạ (ratio − 1)·(ngưỡng − mức), tối đa max_cut_db. Attack = mở ra khi có tiếng
    bút, release = đóng lại chậm, hold giữ 50 ms để không cắt tiếng bút nhẹ giữa 2 nét."""
    if max_cut_db <= 0:
        return x.copy()
    lvl = _detector_db(x, sr)
    target = np.minimum(max_cut_db, (ratio - 1.0) * np.maximum(0.0, thr_db - lvl))
    gr = _smooth_gr(target, _coef(rel_ms, sr), _coef(att_ms, sr), int(hold_ms * 1e-3 * sr))
    return (x * (10 ** (-gr / 20.0))[:, None]).astype(np.float32)
