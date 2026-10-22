import json

import numpy as np
import soundfile as sf

from asmrcore import cli, explain, waveform
from synth import make

SR = 48000


def test_envelope_bounds_signal():
    x = make(dur=6.0, seed=31)
    lo, hi = waveform.envelope(x, 500)
    assert len(lo) == len(hi) == 500
    assert hi.max() == x.max() and lo.min() == x.min()
    assert np.all(lo <= hi)


def test_peaks_format_for_wavesurfer():
    x = make(dur=6.0, seed=32)
    p = waveform.peaks(x, SR, buckets=800)
    assert p["duration"] == 6.0 and p["length"] == 800
    assert len(p["channels"]) == 2 and all(len(c) == 800 for c in p["channels"])
    assert max(max(c) for c in p["channels"]) == round(float(np.abs(x).max()), 4)
    assert min(min(c) for c in p["channels"]) >= 0


def test_png_is_deterministic(tmp_path):
    x = make(dur=6.0, seed=33)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    waveform.render_png(x, x * 4, SR, str(a), title="thử")
    waveform.render_png(x, x * 4, SR, str(b), title="thử")
    data = a.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data == b.read_bytes()


def test_cli_writes_m2_outputs_and_llm_never_changes_audio(tmp_path, monkeypatch):
    src = tmp_path / "clip.wav"
    sf.write(src, make(N=-61.0, A=-46.7, dur=10.0, seed=34), SR, subtype="FLOAT")

    cli.main(["process", str(src), "--out", str(tmp_path / "a"), "--no-llm"])
    rep = json.loads((tmp_path / "a" / "clip_asmr.json").read_text(encoding="utf-8"))
    assert rep["output"]["waveform_png"] == "clip_asmr.png"
    assert (tmp_path / "a" / "clip_asmr.png").read_bytes()[:4] == b"\x89PNG"
    peaks = json.loads((tmp_path / "a" / "clip_asmr_peaks.json").read_text())
    assert set(peaks) == {"before", "after"}
    assert rep["explanation"]["source"] == "template" and rep["explanation"]["text"]

    # có Claude trả lời: âm thanh và hash giữ nguyên từng byte, chỉ khác phần giải thích
    real = explain.explain
    monkeypatch.setattr(explain, "explain", lambda r, use_llm=True: real(
        r, client=_Fake("Tiếng xì đã nhỏ lại."), use_llm=use_llm))
    cli.main(["process", str(src), "--out", str(tmp_path / "b")])
    rep_b = json.loads((tmp_path / "b" / "clip_asmr.json").read_text(encoding="utf-8"))
    assert rep_b["explanation"]["source"] == "llm"
    # so mẫu chứ không so byte: libsndfile ghi thời điểm vào chunk PEAK của WAV float
    ya, _ = sf.read(tmp_path / "a" / "clip_asmr.wav", dtype="float32")
    yb, _ = sf.read(tmp_path / "b" / "clip_asmr.wav", dtype="float32")
    assert np.array_equal(ya, yb)
    assert rep_b["hash"] == rep["hash"]


class _Fake:
    def __init__(self, text):
        from types import SimpleNamespace as NS
        self.beta = NS(messages=NS(create=lambda **kw: NS(stop_reason="end_turn",
                                                           content=[NS(type="text", text=text)])))
