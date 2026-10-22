import json
import time

import numpy as np
import pytest
import soundfile as sf

from asmrcore import audio_io, cli
from asmrcore.pipeline import process
from synth import make

SR = 48000


@pytest.fixture(scope="module")
def tx01_like():
    """Giống TX01_MIC021: thu nhỏ (A −46.7), S khoảng 14 dB, 1 click lớn −5.6 dBFS + 7 click vừa."""
    x = make(N=-61.0, A=-46.7, C=-5.6, dur=30.0)
    return x, process(x, SR)


def test_tx01_like_branch_and_params(tx01_like):
    _, r = tx01_like
    pre = r.analysis_pre
    assert 12 <= pre.S_db < 15
    assert pre.C_big == 1 and pre.C_mid == 7
    assert r.params.denoise["db"] == 10
    assert r.params.lufs_goal == -24
    assert r.params.gain["db"] == pytest.approx(-15 - r.analysis_post_nr["A_db"])
    assert r.params.limiter["thr_dbfs"] == pytest.approx(-6 - r.params.compressor["makeup_db"])
    assert 0 <= r.params.compressor["makeup_db"] <= 10
    assert {"TOO_QUIET"} <= set(r.warnings)


def test_tx01_like_meets_all_targets(tx01_like):
    _, r = tx01_like
    failed = [c for c in r.checks if not c["pass"]]
    assert not failed, failed
    assert r.metrics_after.peak_dbfs <= -6 + 0.01
    assert r.metrics_after.tp_dbtp <= -1


def test_good_recording_goes_to_minus_16():
    r = process(make(N=-70.0, A=-40.0, C=-10.0, dur=20.0, seed=2), SR)
    assert r.analysis_pre.S_db >= 20
    assert r.params.lufs_goal == -16
    assert r.metrics_after.lufs == pytest.approx(-16, abs=1.0)


def test_deterministic_bit_exact(tx01_like):
    x, r = tx01_like
    assert np.array_equal(process(x, SR).audio, r.audio)


def test_stereo_balance_preserved():
    x = make(N=-61.0, A=-46.7, dur=15.0, lr_offset_db=3.0, seed=4)
    y = process(x, SR).audio

    def lr(a):
        return 10 * np.log10(np.mean(a[:, 0].astype(np.float64) ** 2) / np.mean(a[:, 1].astype(np.float64) ** 2))

    assert lr(y) == pytest.approx(lr(x), abs=0.1)


def test_mono_works():
    r = process(make(dur=10.0, channels=1, seed=7), SR)
    assert r.audio.shape[1] == 1


def test_no_silence_warns_and_skips_denoise():
    x = make(dur=10.0, lead_silence=0.0, seed=8)
    rng = np.random.default_rng(0)
    x += (rng.standard_normal(x.shape) * 10 ** (-48 / 20)).astype(np.float32)  # nền to, không có chỗ lặng
    r = process(x, SR)
    if "NO_SILENCE" in r.warnings:
        assert r.params.denoise["db"] == 0


def test_clipped_input_warns():
    x = make(dur=8.0, seed=9) * 300
    x = np.clip(x, -1, 1)
    r = process(x, SR)
    assert "CLIPPED" in r.warnings
    assert r.metrics_after.tp_dbtp <= -1


def test_too_short_file_is_rejected(tmp_path):
    p = tmp_path / "short.wav"
    sf.write(p, make(dur=10.0)[: int(4.9 * SR)], SR, subtype="FLOAT")
    with pytest.raises(audio_io.InputError) as e:
        audio_io.probe(str(p))
    assert e.value.code == "TOO_SHORT"


def test_unsupported_format(tmp_path):
    p = tmp_path / "a.ogg"
    p.write_bytes(b"x")
    with pytest.raises(audio_io.InputError) as e:
        audio_io.probe(str(p))
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_cli_end_to_end(tmp_path):
    src = tmp_path / "TX_like.wav"
    sf.write(src, make(N=-61.0, A=-46.7, dur=12.0, seed=11), SR, subtype="FLOAT")
    code = cli.main(["process", str(src), "--out", str(tmp_path / "out")])
    rep = json.loads((tmp_path / "out" / "TX_like_asmr.json").read_text(encoding="utf-8"))
    out, sr = sf.read(tmp_path / "out" / "TX_like_asmr.wav", dtype="float32")
    info = sf.info(tmp_path / "out" / "TX_like_asmr.wav")
    assert sr == SR and info.subtype == "FLOAT"
    assert code == (0 if rep["all_pass"] else 1)
    assert set(rep["params"]) >= {"highpass", "denoise", "gain", "limiter", "compressor", "expander", "final"}
    assert len(rep["hash"]) == 64


def test_cli_mp3_decodes_through_ffmpeg(tmp_path):
    import subprocess
    wav = tmp_path / "a.wav"
    sf.write(wav, make(dur=8.0, seed=12), 44100, subtype="PCM_16")
    mp3 = tmp_path / "a.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-b:a", "192k", str(mp3)], check=True)
    info = audio_io.probe(str(mp3))
    assert info.sr == 44100 and info.channels == 2
    assert audio_io.decode(info).shape[1] == 2


def test_one_minute_under_30_seconds():
    x = make(dur=60.0, seed=13)
    process(x[: SR * 6], SR)  # khởi động numba trước khi đo
    t = time.perf_counter()
    process(x, SR)
    assert time.perf_counter() - t < 30
