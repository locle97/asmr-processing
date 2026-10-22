"""Sinh file thử giống video ASMR tô màu: tiếng xì nền, các nét bút, vài tiếng click.

Mức được chỉnh theo đúng cách đo của bộ phân tích, nên có thể đặt N, A, C mong muốn.
Mọi thứ dùng seed cố định.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

from asmrcore.measure import frame_rms_db


def _rms_db(x):
    return 10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-20)


def make(sr=48000, dur=30.0, N=-62.0, A=-46.7, C=-5.6, n_big=1, n_mid=7, channels=2,
         lr_offset_db=0.0, seed=1, lead_silence=2.0):
    rng = np.random.default_rng(seed)
    n = int(sr * dur)

    # tiếng xì: ồn trắng nghiêng về tần số cao
    hiss = sosfilt(butter(1, 2000, "highpass", fs=sr, output="sos"), rng.standard_normal(n))
    hiss += 0.3 * rng.standard_normal(n)
    hiss *= 10 ** ((N - _rms_db(hiss)) / 20)

    # nét bút: ồn dải 1–9 kHz với đường bao từng nét
    band = sosfilt(butter(4, [1000, 9000], "bandpass", fs=sr, output="sos"), rng.standard_normal(n))
    env = np.zeros(n)
    t = lead_silence
    while t < dur - 0.5:
        L = rng.uniform(0.25, 1.4)
        s, e = int(t * sr), min(n, int((t + L) * sr))
        k = e - s
        shape = np.sin(np.linspace(0, np.pi, k)) ** 0.6
        wob = 1 + 0.35 * np.sin(2 * np.pi * rng.uniform(3, 8) * np.arange(k) / sr)
        env[s:e] = shape * wob * rng.uniform(0.2, 1.0) ** 2  # nét mạnh nhẹ khác nhau nhiều như thật
        t += L + rng.uniform(0.3, 2.5)
    strokes = band * env

    # chỉnh mức nét bút để phân vị 99 của RMS 50 ms ra đúng A
    mono = hiss + strokes
    for _ in range(4):
        cur = np.percentile(frame_rms_db((hiss + strokes)[:, None], sr), 99)
        strokes *= 10 ** ((A - cur) / 20)
    mono = hiss + strokes

    # click: xung ngắn tắt dần, 1 lớn ở mức C và vài cái vừa ở A + 20 dB
    def click(at, peak_db):
        k = int(0.004 * sr)
        c = rng.standard_normal(k) * np.exp(-np.arange(k) / (0.0007 * sr))
        c *= 10 ** (peak_db / 20) / np.max(np.abs(c))
        s = int(at * sr)
        mono[s:s + k] += c

    times = np.linspace(lead_silence + 1.0, dur - 1.0, n_big + n_mid)
    for i, at in enumerate(times):
        click(at, C if i == len(times) // 2 and n_big else A + 22.0)

    if channels == 1:
        return mono[:, None].astype(np.float32)
    g = 10 ** (lr_offset_db / 20)
    right = np.roll(mono, 7) * 0.97 + 0.03 * hiss  # hơi khác trái để giống mic stereo thật
    return np.stack([mono * g, right], axis=1).astype(np.float32)
