"""asmr process <file> [--preset default|soft|loud] [--out DIR] [--bits float32|24]"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

from . import audio_io, report
from .pipeline import process
from .rules import Overrides

LABEL = {"noise": "Âm nền", "action_p95": "Tiếng bút p95", "action_p99": "Tiếng bút p99",
         "peak": "Đỉnh mẫu", "tp": "True peak", "lufs": "Độ to (LUFS)"}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="asmr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("process", help="Xử lý 1 file")
    pp.add_argument("input")
    pp.add_argument("--preset", choices=["default", "soft", "loud"], default="default")
    pp.add_argument("--out", default=None, help="Thư mục xuất (mặc định: cạnh file gốc)")
    pp.add_argument("--bits", choices=["float32", "24"], default=None)
    pp.add_argument("--loudness", type=float, default=0.0, help="Lệch mục tiêu LUFS, −6..+3")
    pp.add_argument("--denoise", type=float, default=None, help="Mức giảm ồn 0..12 dB")
    pp.add_argument("--floor", type=float, default=None, help="Mức hạ nền tối đa 0..12 dB")
    a = ap.parse_args(argv)

    try:
        info = audio_io.probe(a.input)
    except audio_io.InputError as e:
        print(f"Lỗi [{e.code}]: {e}", file=sys.stderr)
        return 2
    if info.is_video:
        print("Lỗi: xử lý video sẽ có ở mốc M5. Bản này nhận file âm thanh.", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    x = audio_io.decode(info)
    res = process(x, info.sr, a.preset, Overrides(a.loudness, a.denoise, a.floor),
                  progress=lambda s, f: print(f"  [{f * 100:3.0f}%] {s}", file=sys.stderr))
    out_dir = a.out or os.path.dirname(os.path.abspath(a.input))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.input))[0]
    bits = a.bits or ("float32" if info.subtype == "FLOAT" else "24")
    wav = os.path.join(out_dir, f"{stem}_asmr.wav")
    audio_io.write_wav(wav, res.audio, info.sr, bits)
    rep = report.build(res, os.path.basename(a.input), _sha256(a.input), a.preset)
    rep["output"] = {"wav": os.path.basename(wav), "bits": bits}
    rep["elapsed_s"] = round(time.perf_counter() - t0, 2)
    with open(os.path.join(out_dir, f"{stem}_asmr.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    if rep["warnings"]:
        print("Cảnh báo:", ", ".join(rep["warnings"]))
    for c in rep["checks"]:
        print(f"  {'ĐẠT ' if c['pass'] else 'CHƯA'}  {LABEL[c['key']]:<14} {c['value']:>8.2f}   mục tiêu {c['target']}")
    print(f"Xong trong {rep['elapsed_s']} s → {wav}")
    return 0 if rep["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
