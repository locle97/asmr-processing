"""Chạy 1 lần render của 1 job. Hàm `run_render` là đơn vị công việc cho mọi runner.

Bước 1–2 (đọc file, lọc trầm, đo, giảm ồn) là phần chậm nhất và chỉ phụ thuộc thanh "Giảm ồn",
nên được giữ trong bộ nhớ cho job gần nhất: kéo thanh "Độ to" hay "Âm nền" chỉ chạy lại bước 3–7.
Cache chỉ làm nhanh hơn, không đổi kết quả (có test so từng mẫu).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import asdict

import numpy as np
import soundfile as sf

from asmrcore import audio_io, dsp, outputs, pipeline
from asmrcore.measure import lufs
from asmrcore.report import _r
from asmrcore.rules import Overrides

from .store import JobStore, now as _now

log = logging.getLogger("asmrserver")

PREVIEW_CEIL_DBFS = -1.0  # bản gốc nghe thử được tăng gain ngang bản xử lý, limiter để click không vỡ

# tiến độ: phần pipeline chiếm 0–85%, ghi file và giải thích 85–100%
_STEP_STATUS = {"highpass": "analyzing", "denoise": "denoising", "gain": "rendering"}

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "x": None, "preps": {}}
MAX_PREPS = 2
stats = {"prepare_runs": 0}  # cho test: đếm số lần phải chạy lại bước 1–2


def _prepared(job_key, info, ov: Overrides, say, on_analysis):
    with _cache_lock:
        if _cache["key"] != job_key:
            _cache.update(key=job_key, x=None, preps={})
        x = _cache["x"]
        prep = _cache["preps"].get(ov.denoise)
    if prep is not None:
        on_analysis(prep.pre, prep.warnings)
        return x, prep
    if x is None:
        x = audio_io.decode(info)
    prep = pipeline.prepare(x, info.sr, ov, say, on_analysis)
    stats["prepare_runs"] += 1
    with _cache_lock:
        if _cache["key"] == job_key:
            _cache["x"] = x
            preps = _cache["preps"]
            preps[ov.denoise] = prep
            while len(preps) > MAX_PREPS:
                preps.pop(next(iter(preps)))
    return x, prep


def clear_cache() -> None:
    with _cache_lock:
        _cache.update(key=None, x=None, preps={})


def _write_previews(x: np.ndarray, res: pipeline.Result, out_dir: str) -> None:
    """FLAC 24-bit để nghe A/B. Bản gốc được tăng gain cho độ to (LUFS) ngang bản xử lý.

    Tăng gain thẳng thì click của bản gốc vượt 0 dBFS, nên bản gốc đi qua limiter −1 dBFS. Limiter
    làm mất một phần độ to (file nhiều click), nên gain được chỉnh lặp tối đa 4 lần cho LUFS khớp.
    """
    sr = res.sr
    sf.write(os.path.join(out_dir, "proc.flac"), np.clip(res.audio, -1.0, 1.0), sr, subtype="PCM_24", format="FLAC")
    target = res.metrics_after.lufs
    g_db = target - res.metrics_before.lufs
    if not (math.isfinite(g_db) and math.isfinite(target)):
        g_db = 0.0
    for _ in range(4):
        orig, _ = dsp.limiter(dsp.apply_gain(x, g_db), PREVIEW_CEIL_DBFS, sr, 5.0, 40.0)
        err = target - lufs(orig, sr)
        if not math.isfinite(err) or abs(err) < 0.1:
            break
        g_db += err
    sf.write(os.path.join(out_dir, "orig.flac"), np.clip(orig, -1.0, 1.0), sr, subtype="PCM_24", format="FLAC")


def run_render(root: str, job_id: str, rid: str) -> None:
    """Đơn vị công việc cho runner. Không ném lỗi ra ngoài: lỗi được ghi vào job.json."""
    store = JobStore(root)
    try:
        _run(store, job_id, rid)
    except audio_io.InputError as e:
        store.update_render(job_id, rid, status="failed", error={"code": e.code, "message": str(e)})
    except Exception as e:  # noqa: BLE001 — lỗi lạ vẫn phải hiện ra cho người dùng, không treo job
        log.exception("render %s/%s lỗi", job_id, rid)
        store.update_render(job_id, rid, status="failed",
                            error={"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}"})


def _run(store: JobStore, job_id: str, rid: str) -> None:
    job = store.read(job_id)
    r = job["renders"][rid]
    if r["status"] == "done":
        return
    p = r["params"]
    t0 = time.perf_counter()
    info = audio_io.ProbeInfo(**job["probe"])
    ov = Overrides(p["loudness"], p["denoise"], p["floor"])

    def say(step: str, frac: float) -> None:
        status = "rendering" if step.startswith("render_round") else _STEP_STATUS.get(step)
        if status:
            store.update_render(job_id, rid, status=status, step=step, progress=round(0.85 * frac, 3))

    def on_analysis(pre, warnings) -> None:
        def fn(j):
            j["analysis"] = _r(asdict(pre))
            j["warnings"] = warnings
        store.update(job_id, fn)

    store.update_render(job_id, rid, status="analyzing", step="decode", progress=0.01, started_at=_now())
    sha = job["input"].get("sha256")
    if not sha:
        sha = outputs.sha256_file(info.path)
        store.update(job_id, lambda j: j["input"].update(sha256=sha))

    x, prep = _prepared((store.root, job_id, sha), info, ov, say, on_analysis)
    res = pipeline.finish(prep, p["preset"], ov, say)

    store.update_render(job_id, rid, status="reporting", step="write", progress=0.88)
    out_dir = store.render_dir(job_id, rid)
    rep = outputs.write(x, res, info, job["input"]["name"], sha, p["preset"], out_dir, p["bits"],
                        use_llm=p["llm"], t0=t0)
    _write_previews(x, res, out_dir)
    store.update_render(job_id, rid, status="done", step="done", progress=1.0, error=None,
                        all_pass=rep["all_pass"], elapsed_s=round(time.perf_counter() - t0, 2),
                        files={**rep["output"], "report_json": os.path.splitext(job["input"]["name"])[0] + "_asmr.json"},
                        finished_at=_now())
