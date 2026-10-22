"""asmr process <file> [--preset default|soft|loud] [--out DIR] [--bits float32|24] [--no-llm]
asmr serve [--host 127.0.0.1] [--port 8000] [--data DIR]"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import audio_io, outputs
from .pipeline import process
from .rules import Overrides

LABEL = {"noise": "Âm nền", "action_p95": "Tiếng bút p95", "action_p99": "Tiếng bút p99",
         "peak": "Đỉnh mẫu", "tp": "True peak", "lufs": "Độ to (LUFS)"}


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
    pp.add_argument("--no-llm", action="store_true", help="Không gọi Claude, dùng câu giải thích mẫu")
    sp = sub.add_parser("serve", help="Chạy API (FastAPI) ở localhost")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--data", default=None, help="Thư mục lưu job (mặc định ~/.asmr/jobs hoặc $ASMR_DATA_DIR)")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return serve(a.host, a.port, a.data)

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
    rep = outputs.write(x, res, info, os.path.basename(a.input), outputs.sha256_file(a.input), a.preset,
                        out_dir, a.bits, use_llm=not a.no_llm, t0=t0)
    wav = os.path.join(out_dir, rep["output"]["wav"])

    if rep["warnings"]:
        print("Cảnh báo:", ", ".join(rep["warnings"]))
    for c in rep["checks"]:
        print(f"  {'ĐẠT ' if c['pass'] else 'CHƯA'}  {LABEL[c['key']]:<14} {c['value']:>8.2f}   mục tiêu {c['target']}")
    src = "Claude" if rep["explanation"]["source"] == "llm" else "câu mẫu"
    print(f"Giải thích ({src}): {rep['explanation']['text']}")
    print(f"Xong trong {rep['elapsed_s']} s → {wav}")
    return 0 if rep["all_pass"] else 1


def serve(host: str, port: int, data: str | None) -> int:
    import uvicorn

    from asmrserver.app import create_app

    uvicorn.run(create_app(data_dir=data), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
