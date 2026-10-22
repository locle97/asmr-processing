"""Bảng quy tắc chọn thông số. Hàm thuần: cùng số đo vào luôn ra cùng thông số.

Biên của quy tắc: biên dưới thuộc nhánh trên, ví dụ S = 20.0 → nhánh "S ≥ 20".
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRESET_OFFSET = {"default": 0.0, "soft": -3.0, "loud": 3.0}

TARGET_ACTION_DB = -15.0      # bước 3 đưa tiếng bút về mức này
COMP_THR_DBFS = -26.0
CLICK_CEIL_DBFS = -6.0
FLOOR_GOAL_DB = -45.0
FLOOR_MARGIN_DB = 3.0         # biên thêm để ra 9 dB như file mẫu; cần xác nhận lại
MAX_FLOOR_CUT_DB = 12.0
MAKEUP_RANGE = (0.0, 10.0)


def denoise_db(S: float) -> float:
    if S >= 20:
        return 6.0
    if S >= 12:
        return 10.0
    return 12.0


def lufs_target(S: float) -> float:
    if S >= 20:
        return -16.0
    if S >= 15:
        return -20.0
    return -24.0


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Overrides:
    """Ba thanh trượt. None = dùng quy tắc."""
    loudness: float = 0.0          # lệch mục tiêu LUFS, −6..+3 LU
    denoise: float | None = None   # mức giảm ồn 0..12 dB
    floor: float | None = None     # mức hạ nền tối đa 0..12 dB


@dataclass
class Params:
    """Toàn bộ thông số 7 bước, đủ để chạy lại ra đúng kết quả hoặc nhập tay vào Audacity."""
    highpass: dict = field(default_factory=lambda: {"hz": 80.0, "order": 2, "zero_phase": True})
    denoise: dict = field(default_factory=dict)
    gain: dict = field(default_factory=dict)
    limiter: dict = field(default_factory=dict)
    compressor: dict = field(default_factory=dict)
    expander: dict = field(default_factory=dict)
    final: dict = field(default_factory=lambda: {"thr_dbtp": -2.0, "oversample": 4, "look_ms": 5.0, "rel_ms": 40.0})
    lufs_goal: float = -24.0     # theo quy tắc + preset + thanh "Độ to"
    lufs_target: float = -24.0   # mục tiêu thật đã dùng, sau khi vòng kiểm tra dời


def derive_stage_1_3(S_pre: float, Q, A_post_nr: float, ov: Overrides) -> tuple[float, float]:
    """Mức giảm ồn và gain bước 3. A_post_nr đo sau giảm ồn."""
    nr = 0.0 if Q is None else (denoise_db(S_pre) if ov.denoise is None else clamp(ov.denoise, 0, 12))
    return nr, TARGET_ACTION_DB - A_post_nr


def derive_dynamics(p: Params, N_post_nr: float, gain_db: float, makeup_db: float, ov: Overrides) -> None:
    """Thông số bước 4–6 khi đã biết gain bù m. Âm nền N₅ tính sau bước 5 (đã gồm gain bù):
    đây là cách duy nhất ra ngưỡng −35 dBFS như file mẫu."""
    p.limiter = {"thr_dbfs": CLICK_CEIL_DBFS - makeup_db, "look_ms": 5.0, "rel_ms": 40.0, "makeup_db": 0.0}
    p.compressor = {"thr_dbfs": COMP_THR_DBFS, "ratio": 3.0, "att_ms": 5.0, "rel_ms": 300.0, "makeup_db": makeup_db}
    n5 = N_post_nr + gain_db + makeup_db
    cut = clamp(n5 - FLOOR_GOAL_DB + FLOOR_MARGIN_DB, 0.0, MAX_FLOOR_CUT_DB) if ov.floor is None else clamp(ov.floor, 0, 12)
    p.expander = {"thr_dbfs": n5 + 4.0, "ratio": 2.5, "att_ms": 20.0, "rel_ms": 200.0, "hold_ms": 50.0,
                  "max_cut_db": cut}
