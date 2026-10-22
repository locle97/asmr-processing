"""Ghi kết quả một lần xử lý: WAV, báo cáo JSON, ảnh waveform, peaks, lời giải thích.

Dùng chung cho lệnh `asmr process` và worker của API, để hai đường cho ra đúng cùng file.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import numpy as np

from . import audio_io, explain, report, waveform
from .pipeline import Result


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write(x: np.ndarray, res: Result, info: audio_io.ProbeInfo, input_name: str, input_sha256: str,
          preset: str, out_dir: str, bits: str | None = None, use_llm: bool = True,
          t0: float | None = None) -> dict:
    """Ghi `<tên>_asmr.{wav,json,png}` và `<tên>_asmr_peaks.json` vào out_dir, trả về báo cáo."""
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(input_name)[0]
    bits = bits or ("float32" if info.subtype == "FLOAT" else "24")
    wav = os.path.join(out_dir, f"{stem}_asmr.wav")
    audio_io.write_wav(wav, res.audio, info.sr, bits)
    rep = report.build(res, input_name, input_sha256, preset)
    png = os.path.join(out_dir, f"{stem}_asmr.png")
    waveform.render_png(x, res.audio, info.sr, png, title=input_name)
    peaks = os.path.join(out_dir, f"{stem}_asmr_peaks.json")
    with open(peaks, "w", encoding="utf-8") as f:
        json.dump({"before": waveform.peaks(x, info.sr), "after": waveform.peaks(res.audio, info.sr)}, f)
    rep["output"] = {"wav": os.path.basename(wav), "bits": bits, "waveform_png": os.path.basename(png),
                     "peaks_json": os.path.basename(peaks)}
    rep["explanation"] = explain.explain(rep, use_llm=use_llm)
    if t0 is not None:
        rep["elapsed_s"] = round(time.perf_counter() - t0, 2)
    with open(os.path.join(out_dir, f"{stem}_asmr.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    return rep
