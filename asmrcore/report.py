"""Báo cáo JSON: số đo trước/sau/mục tiêu, toàn bộ thông số, các vòng kiểm tra."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from . import PIPELINE_VERSION
from .pipeline import Result


def _r(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _r(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_r(x) for x in v]
    return v


def build(res: Result, input_name: str, input_sha256: str, preset: str) -> dict:
    params = _r(asdict(res.params))
    before, after = res.metrics_before, res.metrics_after
    pre = res.analysis_pre
    rep = {
        "pipeline_version": PIPELINE_VERSION,
        "input": {"name": input_name, "sha256": input_sha256, "sr": res.sr,
                  "channels": pre.channels, "duration_s": round(pre.duration, 3)},
        "preset": preset,
        "warnings": res.warnings,
        "analysis_pre": _r(asdict(pre)),
        "analysis_post_denoise": _r(res.analysis_post_nr),
        "params": params,
        "summary": [
            {"key": "noise", "before": round(before.noise_db, 2), "after": round(after.noise_db, 2), "target": [-50, -40]},
            {"key": "action", "before": round(before.action_p99, 2),
             "after": [round(after.action_p95, 2), round(after.action_p99, 2)], "target": [-24, -14]},
            {"key": "peak", "before": round(before.peak_dbfs, 2), "after": round(after.peak_dbfs, 2), "target": [None, -6]},
            {"key": "tp", "before": round(before.tp_dbtp, 2), "after": round(after.tp_dbtp, 2), "target": [None, -1]},
            {"key": "lufs", "before": round(before.lufs, 2), "after": round(after.lufs, 2),
             "target": [round(res.params.lufs_target - 1, 2), round(res.params.lufs_target + 1, 2)]},
        ],
        "checks": res.checks,
        "all_pass": all(c["pass"] for c in res.checks),
        "rounds": [_r(asdict(r)) for r in res.rounds],
        "chosen_round": res.chosen_round,
        "limiter_active_pct": round(res.limiter_active_pct, 2),
    }
    # mục tiêu LUFS ở bảng tóm tắt là mục tiêu gốc (quy tắc + preset + thanh trượt), không phải mục tiêu đã dời
    lufs_check = next(c for c in res.checks if c["key"] == "lufs")
    rep["summary"][-1]["target"] = lufs_check["target"]
    key = json.dumps({"in": input_sha256, "params": params, "v": PIPELINE_VERSION}, sort_keys=True)
    rep["hash"] = hashlib.sha256(key.encode()).hexdigest()
    return rep
