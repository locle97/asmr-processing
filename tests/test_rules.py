import pytest

from asmrcore import rules


@pytest.mark.parametrize("S,want", [(25, 6), (20.0, 6), (19.9, 10), (12.0, 10), (11.9, 12), (3, 12)])
def test_denoise_amount(S, want):
    assert rules.denoise_db(S) == want


@pytest.mark.parametrize("S,want", [(20.0, -16), (19.9, -20), (15.0, -20), (14.9, -24), (12, -24)])
def test_lufs_target(S, want):
    assert rules.lufs_target(S) == want


def test_sample_file_numbers():
    """Số của TX01_MIC021 trong spec: A sau giảm ồn −46.7, N sau giảm ồn −78, gain bù +7."""
    ov = rules.Overrides()
    nr, g = rules.derive_stage_1_3(14.0, (0.75, 1.75), -46.7, ov)
    assert nr == 10 and g == pytest.approx(31.7)
    p = rules.Params()
    rules.derive_dynamics(p, -78.0, g, 7.0, ov)
    assert p.limiter["thr_dbfs"] == pytest.approx(-13.0)
    assert p.limiter["makeup_db"] == 0.0
    assert p.expander["thr_dbfs"] == pytest.approx(-35.3)
    assert p.expander["max_cut_db"] == pytest.approx(8.7)


def test_no_silence_skips_denoise():
    nr, _ = rules.derive_stage_1_3(14.0, None, -46.7, rules.Overrides())
    assert nr == 0


def test_sliders_override_and_clamp():
    ov = rules.Overrides(denoise=20, floor=-3)
    nr, _ = rules.derive_stage_1_3(25.0, (0, 1), -40, ov)
    assert nr == 12
    p = rules.Params()
    rules.derive_dynamics(p, -70, 25, 5, ov)
    assert p.expander["max_cut_db"] == 0
