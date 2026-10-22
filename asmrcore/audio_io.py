"""Đọc/ghi file. Mọi tín hiệu trong lõi là float32 dạng (n_mẫu, n_kênh)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

import numpy as np
import soundfile as sf

AUDIO_EXT = {".wav", ".flac", ".mp3", ".m4a"}
VIDEO_EXT = {".mp4", ".mov"}
MAX_BYTES = 500 * 1024 * 1024
MIN_SEC, MAX_SEC = 5.0, 600.0
MIN_SR, MAX_SR = 44100, 96000


class InputError(Exception):
    """Lỗi đầu vào: file không xử lý được. `code` là mã lỗi cho API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeInfo:
    path: str
    is_video: bool
    sr: int
    channels: int
    duration: float
    subtype: str | None  # ví dụ "FLOAT", "PCM_24" khi đọc được bằng soundfile


def _ffprobe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise InputError("DECODE_FAILED", f"Không đọc được file: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def probe(path: str) -> ProbeInfo:
    ext = os.path.splitext(path)[1].lower()
    if ext not in AUDIO_EXT | VIDEO_EXT:
        raise InputError("UNSUPPORTED_FORMAT",
                         f"Định dạng {ext or '(không có)'} chưa hỗ trợ. App nhận WAV, FLAC, MP3, M4A, MP4, MOV.")
    if os.path.getsize(path) > MAX_BYTES:
        raise InputError("TOO_LARGE", "File lớn hơn 500 MB. Hãy cắt bớt hoặc xuất lại nhỏ hơn.")

    subtype = None
    if ext in {".wav", ".flac"}:
        try:
            info = sf.info(path)
            sr, ch, dur, subtype = info.samplerate, info.channels, info.duration, info.subtype
        except RuntimeError as e:
            raise InputError("DECODE_FAILED", f"Không đọc được file: {e}") from e
    else:
        meta = _ffprobe(path)
        audio = [s for s in meta.get("streams", []) if s.get("codec_type") == "audio"]
        if not audio:
            raise InputError("NO_AUDIO_TRACK", "File không có track âm thanh.")
        a = audio[0]
        sr, ch = int(a["sample_rate"]), int(a["channels"])
        dur = float(a.get("duration") or meta["format"]["duration"])

    if not MIN_SR <= sr <= MAX_SR:
        raise InputError("SAMPLE_RATE_OUT_OF_RANGE", f"Tần số mẫu {sr} Hz nằm ngoài 44.1–96 kHz.")
    if ch not in (1, 2):
        raise InputError("UNSUPPORTED_FORMAT", f"File có {ch} kênh, app chỉ nhận mono hoặc stereo.")
    if dur < MIN_SEC:
        raise InputError("TOO_SHORT", f"File dài {dur:.1f} s, cần ít nhất 5 s để đo âm nền.")
    if dur > MAX_SEC:
        raise InputError("TOO_LONG", f"File dài {dur / 60:.1f} phút, app nhận tối đa 10 phút. Hãy cắt bớt.")
    return ProbeInfo(path, ext in VIDEO_EXT, sr, ch, dur, subtype)


def decode(info: ProbeInfo) -> np.ndarray:
    """Giải mã ở tần số mẫu gốc, không resample. Trả float32 (n, ch)."""
    if info.subtype is not None:
        x, _ = sf.read(info.path, dtype="float32", always_2d=True)
        return x
    cmd = ["ffmpeg", "-v", "error", "-i", info.path, "-map", "0:a:0",
           "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(info.sr), "-ac", str(info.channels), "-"]
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode != 0:
        raise InputError("DECODE_FAILED", out.stderr.decode(errors="replace")[:200])
    return np.frombuffer(out.stdout, dtype="<f4").reshape(-1, info.channels).copy()


def write_wav(path: str, y: np.ndarray, sr: int, bits: str = "float32") -> None:
    """bits = "float32" hoặc "24". Bản 24-bit có dither TPDF với seed cố định theo nội dung."""
    if bits == "float32":
        sf.write(path, y, sr, subtype="FLOAT")
        return
    seed = int.from_bytes(np.frombuffer(y[: min(len(y), 4096)].tobytes(), dtype=np.uint8)[:8].tobytes() or b"\0", "little")
    rng = np.random.default_rng(seed)
    lsb = 1.0 / (2 ** 23)
    d = (rng.random(y.shape) - rng.random(y.shape)) * lsb
    sf.write(path, np.clip(y + d, -1.0, 1.0 - lsb), sr, subtype="PCM_24")
