"""Chạy 7 bước theo thứ tự cố định, giải gain bù, vòng kiểm tra tối đa 3 lần."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np

from . import dsp, rules
from .measure import Analysis, Metrics, analyze, count_clipped, frame_rms_db, lufs, measure_output

TARGETS = {
    "noise": (-50.0, -40.0),
    "action": (-24.0, -14.0),
    "peak": (None, -6.0),
    "tp": (None, -1.0),
}
PEAK_TOL = 0.01  # dB, sai số làm tròn float32 quanh trần −6 dBFS

Progress = Callable[[str, float], None]


@dataclass
class Round:
    round: int
    lufs_target: float
    makeup_db: float
    score: float
    metrics: dict
    changes: list[str] = field(default_factory=list)


@dataclass
class Result:
    audio: np.ndarray
    sr: int
    warnings: list[str]
    analysis_pre: Analysis
    analysis_post_nr: dict
    params: rules.Params
    metrics_before: Metrics
    metrics_after: Metrics
    checks: list[dict]
    rounds: list[Round]
    chosen_round: int
    limiter_active_pct: float


def _range_dist(v: float, lo, hi) -> float:
    if lo is not None and v < lo:
        return lo - v
    if hi is not None and v > hi:
        return v - hi
    return 0.0


def checks_for(m: Metrics, lufs_goal: float) -> list[dict]:
    rows = [
        ("noise", m.noise_db, TARGETS["noise"]),
        ("action_p95", m.action_p95, TARGETS["action"]),
        ("action_p99", m.action_p99, TARGETS["action"]),
        ("peak", m.peak_dbfs, (None, TARGETS["peak"][1] + PEAK_TOL)),
        ("tp", m.tp_dbtp, TARGETS["tp"]),
        ("lufs", m.lufs, (lufs_goal - 1.0, lufs_goal + 1.0)),
    ]
    return [{"key": k, "value": round(v, 2), "target": list(t), "pass": _range_dist(v, *t) == 0.0,
             "miss": round(_range_dist(v, *t), 2)} for k, v, t in rows]


def score(checks: list[dict]) -> float:
    return round(sum(c["miss"] for c in checks), 3)


def _dynamics(x3: np.ndarray, sr: int, p: rules.Params) -> tuple[np.ndarray, float]:
    """Bước 4–7."""
    lim, cmp_, exp_, fin = p.limiter, p.compressor, p.expander, p.final
    y, active = dsp.limiter(x3, lim["thr_dbfs"], sr, lim["look_ms"], lim["rel_ms"])
    y = dsp.compressor(y, sr, cmp_["thr_dbfs"], cmp_["ratio"], cmp_["att_ms"], cmp_["rel_ms"], cmp_["makeup_db"])
    y = dsp.expander(y, sr, exp_["thr_dbfs"], exp_["ratio"], exp_["att_ms"], exp_["rel_ms"],
                     exp_["max_cut_db"], exp_["hold_ms"])
    y = dsp.tp_limiter(y, fin["thr_dbtp"], sr, fin["look_ms"], fin["rel_ms"])
    return y, active


def solve_makeup(x3, sr, p: rules.Params, N_post, gain_db, ov, target, max_iter=4):
    """Ngưỡng limiter (bước 4) phụ thuộc gain bù (bước 5), gain bù lại phụ thuộc LUFS sau xử lý:
    giải lặp, bắt đầu từ m = 0, mỗi lần dời m đúng bằng phần LUFS còn thiếu."""
    lo, hi = rules.MAKEUP_RANGE
    m = 0.0
    y = active = None
    for _ in range(max_iter):
        rules.derive_dynamics(p, N_post, gain_db, m, ov)
        y, active = _dynamics(x3, sr, p)
        err = target - lufs(y, sr)
        nm = rules.clamp(m + err, lo, hi)
        if abs(err) < 0.1 or nm == m:
            break
        m = nm
    if abs(m - p.compressor["makeup_db"]) > 1e-9:  # vòng cuối đổi m nhưng chưa render lại
        rules.derive_dynamics(p, N_post, gain_db, m, ov)
        y, active = _dynamics(x3, sr, p)
    return y, active


def process(x: np.ndarray, sr: int, preset: str = "default", ov: rules.Overrides | None = None,
            progress: Progress | None = None, max_rounds: int = 3) -> Result:
    ov = ov or rules.Overrides()
    say = progress or (lambda step, frac: None)
    warnings: list[str] = []

    if count_clipped(x) > 0:
        warnings.append("CLIPPED")
    metrics_before = measure_output(x, sr)

    say("highpass", 0.05)
    x1 = dsp.highpass(x, sr)
    pre = analyze(x1, sr)
    if not pre.has_silence:
        warnings.append("NO_SILENCE")
    if metrics_before.lufs < -45.0:
        warnings.append("TOO_QUIET")
    if pre.S_db < 12.0:
        warnings.append("LOW_SNR")

    p = rules.Params()
    nr_db = 0.0 if pre.Q is None else (rules.denoise_db(pre.S_db) if ov.denoise is None else rules.clamp(ov.denoise, 0, 12))
    say("denoise", 0.15)
    x2 = dsp.denoise(x1, sr, pre.Q, nr_db) if nr_db > 0 else x1
    p.denoise = {"db": nr_db, "noise_clip": list(pre.Q) if pre.Q else None, "method": "spectral_gating_stationary",
                 "n_std": 1.5, "shared_stereo_mask": True}

    f2 = frame_rms_db(x2, sr)
    N_post, A_post = float(np.percentile(f2, 10)), float(np.percentile(f2, 99))
    _, gain_db = rules.derive_stage_1_3(pre.S_db, pre.Q, A_post, ov)
    p.gain = {"db": gain_db}
    say("gain", 0.45)
    x3 = dsp.apply_gain(x2, gain_db)

    goal = rules.lufs_target(pre.S_db) + rules.PRESET_OFFSET.get(preset, 0.0) + rules.clamp(ov.loudness, -6, 3)
    target = goal
    rounds: list[Round] = []
    best = None
    for r in range(1, max_rounds + 2):  # vòng đầu + tối đa 3 vòng chạy lại
        say(f"render_round_{r}", 0.5 + 0.1 * r)
        pr = rules.Params(**{k: (dict(v) if isinstance(v, dict) else v) for k, v in asdict(p).items()})
        pr.lufs_goal, pr.lufs_target = goal, target
        y, active = solve_makeup(x3, sr, pr, N_post, gain_db, ov, target)
        m = measure_output(y, sr)
        ch = checks_for(m, goal)
        rd = Round(r, target, pr.compressor["makeup_db"], score(ch), asdict(m))
        rounds.append(rd)
        if best is None or rd.score < best[0].score:  # hoà thì giữ vòng sớm hơn
            best = (rd, y, pr, m, ch, active)
        if rd.score == 0.0 or r > max_rounds:
            break
        # Thứ tự ưu tiên: âm nền trước, rồi tiếng bút; không bao giờ vượt LUFS mục tiêu + 1 LU.
        if m.noise_db > TARGETS["noise"][1]:
            target -= 2.0
            rd.changes.append("noise>-40: lufs_target -2")
        elif min(m.action_p95, m.action_p99) < TARGETS["action"][0] and target + 1.0 <= goal + 1.0:
            target += 1.0
            rd.changes.append("action<-24: lufs_target +1")
        else:
            break

    rd, y, pr, m, ch, active = best
    if preset == "loud" and m.noise_db > TARGETS["noise"][1]:
        warnings.append("LOUD_LIMITED_BY_NOISE")
    say("done", 1.0)
    return Result(
        audio=y, sr=sr, warnings=warnings, analysis_pre=pre,
        analysis_post_nr={"N_db": N_post, "A_db": A_post},
        params=pr, metrics_before=metrics_before, metrics_after=m, checks=ch,
        rounds=rounds, chosen_round=rd.round, limiter_active_pct=active,
    )
