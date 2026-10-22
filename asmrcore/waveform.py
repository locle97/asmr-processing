"""Ảnh waveform trước/sau (PNG, có vạch −6 dB) và dữ liệu peaks cho wavesurfer.js."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # không cần màn hình
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

CEIL_DBFS = -6.0
INK = "#0f7a6c"
LINE = "#b4366b"


def envelope(x: np.ndarray, buckets: int) -> tuple[np.ndarray, np.ndarray]:
    """Min/max của từng khúc (gộp 2 kênh), đủ để vẽ waveform mà không vẽ hàng triệu điểm."""
    mono_hi = np.max(x, axis=1)
    mono_lo = np.min(x, axis=1)
    n = len(x)
    buckets = max(1, min(buckets, n))
    edges = np.linspace(0, n, buckets + 1).astype(np.int64)
    hi = np.maximum.reduceat(mono_hi, edges[:-1])
    lo = np.minimum.reduceat(mono_lo, edges[:-1])
    return lo, hi


def peaks(x: np.ndarray, sr: int, buckets: int = 4000) -> dict:
    """Peaks theo định dạng wavesurfer.js: mỗi kênh một mảng giá trị đỉnh |x| trong [0, 1]."""
    n = len(x)
    buckets = max(1, min(buckets, n))
    edges = np.linspace(0, n, buckets + 1).astype(np.int64)
    chans = [np.round(np.maximum.reduceat(np.abs(x[:, c]), edges[:-1]).astype(np.float64).clip(0, 1), 4).tolist()
             for c in range(x.shape[1])]
    return {"duration": round(n / sr, 3), "length": buckets, "channels": chans}


def render_png(before: np.ndarray, after: np.ndarray, sr: int, path: str,
               title: str = "", width_px: int = 1600, height_px: int = 600) -> None:
    """Hai panel trên/dưới cùng thang tuyến tính ±1, vạch ±0.501 (= −6 dBFS) trên cả hai."""
    dpi = 100
    fig, axes = plt.subplots(2, 1, figsize=(width_px / dpi, height_px / dpi), dpi=dpi, sharex=True)
    ceil = 10 ** (CEIL_DBFS / 20)
    for ax, sig, label in ((axes[0], before, "Trước"), (axes[1], after, "Sau")):
        lo, hi = envelope(sig, width_px)
        t = np.linspace(0, len(sig) / sr, len(lo))
        ax.fill_between(t, lo, hi, color=INK, linewidth=0)
        for y in (ceil, -ceil):
            ax.axhline(y, color=LINE, linewidth=1, linestyle="--")
        ax.set_ylim(-1, 1)
        ax.set_yticks([-1, -ceil, 0, ceil, 1], ["−1", "−6 dB", "0", "−6 dB", "1"])
        ax.set_ylabel(label)
        ax.margins(x=0)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[1].set_xlabel("Giây")
    if title:
        axes[0].set_title(title, loc="left", fontsize=11)
    fig.tight_layout()
    # metadata cố định để cùng dữ liệu ra cùng file PNG
    fig.savefig(path, format="png", metadata={"Software": None})
    plt.close(fig)
