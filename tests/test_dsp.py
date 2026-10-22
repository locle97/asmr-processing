import numpy as np
import pytest

from asmrcore import dsp
from asmrcore.measure import db_amp, frame_rms_db, true_peak_dbtp

SR = 48000


def sine(freq, db, dur=1.0, sr=SR, phase=0.0, ch=2):
    t = np.arange(int(dur * sr)) / sr
    x = 10 ** (db / 20) * np.sin(2 * np.pi * freq * t + phase)
    return np.repeat(x[:, None], ch, axis=1).astype(np.float32)


def rms_db(x):
    return 10 * np.log10(np.mean(x.astype(np.float64) ** 2))


# ---------- limiter ----------

def test_limiter_forward_min_matches_brute_force():
    rng = np.random.default_rng(3)
    peak = rng.random(500) * 2
    g = dsp.limiter_gain(peak, 0.0, 1000, look_ms=4, rel_ms=0)  # L = 4 mẫu, không release
    need = np.minimum(1, 1 / peak)
    L = 4
    pad = np.concatenate([np.ones(L), need, np.ones(L)])
    fwd = np.array([pad[i:i + L + 1].min() for i in range(len(pad) - L)])
    ref = np.array([fwd[j:j + L + 1].mean() for j in range(len(need))])
    np.testing.assert_allclose(g, ref, rtol=1e-12)


def test_limiter_never_exceeds_threshold():
    rng = np.random.default_rng(0)
    x = (rng.standard_normal((SR * 2, 2)) * 0.05).astype(np.float32)
    for at in (0.3, 0.31, 1.2):  # click sát nhau và một click riêng
        s = int(at * SR)
        x[s:s + 50] += 0.9
    y, active = dsp.limiter(x, -13.0, SR)
    assert db_amp(np.max(np.abs(y))) <= -13.0 + 1e-4
    assert 0 < active < 100


def test_limiter_has_no_makeup_gain():
    """Hồi quy cho lỗi Limiter của pedalboard: tín hiệu dưới ngưỡng phải đi qua nguyên vẹn."""
    x = sine(1000, -30.0)
    y, active = dsp.limiter(x, -13.0, SR)
    assert np.array_equal(x, y)
    assert active == 0.0


def test_limiter_quiet_part_untouched_away_from_click():
    x = sine(1000, -30.0, dur=2.0)
    x[SR // 2:SR // 2 + 30] = 0.95
    y, _ = dsp.limiter(x, -13.0, SR)
    far = slice(SR // 2 + int(0.6 * SR), None)  # 600 ms sau click = 15 hằng số thời gian release
    np.testing.assert_allclose(y[far], x[far], rtol=1e-5)
    early = slice(0, SR // 2 - int(0.006 * SR))  # trước vùng lookahead 5 ms
    assert np.array_equal(y[early], x[early])


def test_limiter_keeps_stereo_ratio():
    x = sine(1000, -3.0)
    x[:, 1] *= 0.5
    y, _ = dsp.limiter(x, -13.0, SR)
    np.testing.assert_allclose(y[:, 1], y[:, 0] * 0.5, rtol=1e-5, atol=1e-9)


def test_true_peak_limiter():
    x = sine(SR / 4, -0.5, phase=np.pi / 4)  # đỉnh mẫu thấp hơn đỉnh thật 3 dB
    y = dsp.tp_limiter(x, -2.0, SR)
    assert true_peak_dbtp(y) <= -2.0 + 0.05


@pytest.mark.parametrize("freq", [1000, 7000, SR / 4, 19000])
def test_true_peak_meter_catches_intersample_peak(freq):
    """Đo phần giữa: chỗ tín hiệu bắt đầu đột ngột có vọng thật (Gibbs) nên đo dư là đúng."""
    from asmrcore.measure import true_peak_per_sample
    x = sine(freq, -6.0, phase=np.pi / 4, ch=1)
    tp = float(db_amp(true_peak_per_sample(x)[200:-200].max()))
    assert tp == pytest.approx(-6.0, abs=0.17)  # 4x: lệch tối đa cos(11.25°) = −0.17 dB ở fs/4
    if freq == SR / 4:
        assert db_amp(np.max(np.abs(x))) == pytest.approx(-9.0, abs=0.1)


# ---------- nén / expander ----------

@pytest.mark.parametrize("level", [-40, -30, -20, -10])
def test_compressor_static_curve(level):
    x = sine(1000, level + 3.01, dur=1.5)  # biên độ để RMS = level
    y = dsp.compressor(x, SR, -26.0, 3.0, 5.0, 300.0, makeup_db=4.0)
    tail = slice(SR, None)
    want = (level if level <= -26 else -26 + (level + 26) / 3) + 4.0
    assert rms_db(y[tail]) == pytest.approx(want, abs=0.3)


@pytest.mark.parametrize("level,cut", [(-30, 0.0), (-38, 3 * 1.5), (-50, 12.0)])
def test_expander_static_curve(level, cut):
    x = sine(1000, level + 3.01, dur=1.5)
    y = dsp.expander(x, SR, -35.0, 2.5, 20.0, 200.0, max_cut_db=12.0)
    tail = slice(SR, None)
    want_cut = 0.0 if level >= -35 else min(12.0, 1.5 * (-35 - level))
    assert rms_db(y[tail]) == pytest.approx(level - want_cut, abs=0.3)


# ---------- lọc trầm, giảm ồn ----------

def test_highpass_response_and_zero_phase():
    assert rms_db(dsp.highpass(sine(80, -20, 2.0), SR)[SR // 2:-SR // 2]) == pytest.approx(-23.01 - 6.02, abs=0.15)
    assert rms_db(dsp.highpass(sine(1000, -20, 2.0), SR)[SR // 2:-SR // 2]) == pytest.approx(-23.01, abs=0.1)
    x = sine(200, -20, 2.0)
    y = dsp.highpass(x, SR)
    mid = slice(SR // 2, SR // 2 + 4800)
    lags = range(-20, 21)
    best = max(lags, key=lambda k: float(np.dot(x[mid, 0], np.roll(y[:, 0], k)[mid])))
    assert best == 0


def test_denoise_lowers_floor_and_keeps_stroke():
    rng = np.random.default_rng(5)
    n = SR * 6
    hiss = rng.standard_normal(n) * 10 ** (-60 / 20)
    from scipy.signal import butter, sosfilt
    tone = np.zeros(n)  # "nét bút": ồn dải 1–9 kHz, như tiếng marker thật
    burst = sosfilt(butter(4, [1000, 9000], "bandpass", fs=SR, output="sos"), rng.standard_normal(2 * SR))
    tone[3 * SR:5 * SR] = burst / np.sqrt(np.mean(burst ** 2)) * 10 ** (-30 / 20)
    x = np.stack([hiss + tone, hiss + tone], axis=1).astype(np.float32)
    y = dsp.denoise(x, SR, (0.5, 1.5), 10.0)
    assert rms_db(y[SR:2 * SR]) == pytest.approx(rms_db(x[SR:2 * SR]) - 10.0, abs=1.0)
    assert rms_db(y[int(3.5 * SR):int(4.5 * SR)]) == pytest.approx(rms_db(x[int(3.5 * SR):int(4.5 * SR)]), abs=0.3)


def test_denoise_blocks_join_smoothly():
    rng = np.random.default_rng(6)
    x = (rng.standard_normal((SR * 50, 1)) * 0.001).astype(np.float32)
    y = dsp.denoise(x, SR, (0.0, 1.0), 10.0)
    f = frame_rms_db(y, SR)
    assert f[100:-100].max() - f[100:-100].min() < 3.0  # không có bậc nhảy ở chỗ nối khúc 20 s


# ---------- đo ----------

@pytest.mark.parametrize("sr", [44100, 48000, 96000])
def test_lufs_matches_pyloudnorm(sr):
    import pyloudnorm
    from synth import make
    from asmrcore.measure import lufs
    x = make(sr=sr, dur=12.0, seed=21) * 30
    ref = pyloudnorm.Meter(sr).integrated_loudness(x.astype(np.float64))
    assert lufs(x, sr) == pytest.approx(ref, abs=0.05)


def test_lufs_reference_sine():
    """Sin 1 kHz −20 dBFS mono ≈ −23.0 LUFS (BS.1770, sai số bộ lọc K ở 1 kHz rất nhỏ)."""
    from asmrcore.measure import lufs
    assert lufs(sine(1000, -20.0, dur=5.0, ch=1), SR) == pytest.approx(-23.0, abs=0.1)
