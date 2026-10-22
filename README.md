# asmr-app · mốc M1–M2: lõi xử lý, waveform, giải thích

Thư viện `asmrcore` và lệnh `asmr`: đọc 1 file âm thanh, đo, chọn thông số theo bảng quy tắc,
chạy 7 bước cố định, đo lại (tối đa 3 vòng), xuất WAV + báo cáo JSON, ảnh waveform trước/sau và
3 câu giải thích tiếng Việt do Claude viết. Chưa có web, video.

## Cài và chạy

Cần Python ≥ 3.11 và `ffmpeg`/`ffprobe` trong PATH (chỉ để đọc MP3/M4A).

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/asmr process TX01_MIC021.wav               # → TX01_MIC021_asmr.wav + _asmr.json cạnh file gốc
.venv/bin/asmr process clip.wav --preset soft         # Êm hơn (−3 LU); loud = To hơn (+3 LU)
.venv/bin/asmr process clip.wav --loudness -2 --denoise 8 --floor 6   # 3 thanh trượt
.venv/bin/asmr process clip.wav --no-llm        # không gọi Claude, dùng câu giải thích mẫu
.venv/bin/python -m pytest -q
```

Mỗi lần chạy ghi cạnh file gốc (hoặc vào `--out`):

| File | Nội dung |
| --- | --- |
| `<tên>_asmr.wav` | Âm thanh đã xử lý |
| `<tên>_asmr.json` | Báo cáo; mục `explanation` là lời giải thích |
| `<tên>_asmr.png` | Waveform trước (trên) và sau (dưới), cùng thang, vạch −6 dB |
| `<tên>_asmr_peaks.json` | Peaks trước/sau (4000 điểm mỗi kênh) cho wavesurfer.js ở M4 |

Giải thích cần `ANTHROPIC_API_KEY` (hoặc đã `ant auth login`). Model mặc định `claude-opus-5-5`, đổi bằng
biến `ASMR_LLM_MODEL`. Không có key hoặc mất mạng thì vẫn chạy bình thường, dùng câu mẫu.

Mã thoát: 0 = đạt đủ chỉ số, 1 = có chỉ số chưa đạt (vẫn xuất file), 2 = lỗi đầu vào.

## Cấu trúc

| File | Việc |
| --- | --- |
| `asmrcore/audio_io.py` | Kiểm tra đầu vào (định dạng, 5 s–10 phút, 500 MB, 44.1–96 kHz), giải mã, ghi WAV 32f/24-bit |
| `asmrcore/measure.py` | RMS 50 ms, N/A/C/S/Q, LUFS (BS.1770-4), true peak 4x |
| `asmrcore/rules.py` | Bảng quy tắc → thông số. Hàm thuần |
| `asmrcore/dsp.py` | Lọc trầm, giảm ồn, gain, limiter không gain bù, nén, expander, limiter true peak |
| `asmrcore/pipeline.py` | Chạy 7 bước, giải gain bù, vòng kiểm tra |
| `asmrcore/report.py` | Report JSON + hash để chạy lại |
| `asmrcore/waveform.py` | PNG trước/sau có vạch −6 dB, peaks cho wavesurfer.js |
| `asmrcore/explain.py` | Gửi số đo cho Claude, kiểm tra số trong câu trả lời, câu mẫu dự phòng |
| `tests/synth.py` | Sinh file thử giống video tô màu (xì nền, nét bút, click) với N, A, C đặt trước |

## Đã chốt khi code (khác hoặc thêm so với spec)

- **Gain bù giải lặp**: m bắt đầu 0, render bước 4–7, đo LUFS, dời m đúng phần thiếu, tối đa 4 lần.
  Ngưỡng limiter = −6 − m cập nhật theo m.
- **Expander** dùng âm nền sau bước 5 (đã gồm gain bù): ngưỡng = N₅ + 4. Mức hạ tối đa
  = clamp(N₅ + 45 + 3, 0, 12), biên 3 dB để ra 9 dB như file mẫu (chờ xác nhận).
- **Vòng kiểm tra**: âm nền > −40 → mục tiêu LUFS −2; nếu không, tiếng bút (p95 hoặc p99) < −24 →
  mục tiêu +1 nhưng không quá mục tiêu gốc +1 LU. Chọn vòng có tổng độ lệch nhỏ nhất, hoà lấy vòng sớm.
- **Giảm ồn** tự viết theo kiểu noisereduce stationary (ngưỡng = trung bình + 1.5 độ lệch chuẩn của
  đoạn Q, làm mượt mặt nạ 500 Hz × 50 ms) nhưng mặt nạ tính trên kênh mid và dùng chung cho 2 kênh.
  Chạy theo khúc 20 s chồng 1 s để file 10 phút không tốn RAM. Không còn phụ thuộc noisereduce.
- **Limiter** xử lý offline nên lookahead 5 ms tính trên trục thời gian gốc, không gây trễ: file ra
  khớp từng mẫu với file vào, không cần bù trễ khi ghép video.
- **Nén/expander** đo mức bằng công suất trung bình 2 kênh (cùng cách bộ phân tích đo) để ngưỡng
  "âm nền + 4 dB" khớp với cái detector thấy. Limiter đo theo max(|L|, |R|).
- **LUFS và true peak** tự viết (nhanh hơn ~5 lần). LUFS khớp pyloudnorm trong ±0.05 LU ở 44.1/48/96 kHz.
  True peak dùng bộ lọc 97 hệ số; ở 4x có thể đo thiếu tối đa 0.17 dB với nội dung sát fs/4.

## Giải thích bằng Claude (M2)

- Claude chỉ nhận một JSON số đo (`explain.facts`): âm nền, tiếng bút, click, LUFS trước/sau/mục tiêu,
  cảnh báo, mức giảm ồn. Không nhận âm thanh, tên file hay hash. Lời giải thích được tạo **sau** khi
  đã ghi WAV và không nằm trong hash, nên không thể làm đổi âm thanh (có test so từng mẫu).
- Câu trả lời bị loại nếu quá 3 câu, rỗng, bị từ chối, hoặc có con số không lấy từ báo cáo. Được phép:
  đúng số (làm tròn 1 chữ số hoặc số nguyên), hoặc hiệu của một cặp có nghĩa (trước/sau cùng chỉ số,
  tiếng bút với âm nền, LUFS với mục tiêu), sai số 1 dB cho cách nói "khoảng". Không cho hiệu của mọi cặp
  vì như thế gần như số nào cũng lọt.
- Bị loại thì dùng câu mẫu dựng từ chính số đo; lý do loại ghi ở `explanation.rejected`.
- Request dùng `effort: low` và bật fallback phía server (`fallbacks: "default"`) khi bị từ chối.

## Kết quả trên file tổng hợp (chưa có TX01_MIC021 thật)

| File | Kết quả |
| --- | --- |
| Giống TX01 (N −61, A −46.7, click −5.6, 30 s) | Đạt đủ 6 chỉ số, −23.1 LUFS, 8 s |
| Thu tốt (S ≥ 20) | −16.05 LUFS, đạt đủ, 4.6 s |
| 10 phút stereo 48 kHz | 118 s (mục tiêu < 180 s); tiếng bút p95 −24.2, lệch 0.2 dB |
| 1 phút | < 15 s (test yêu cầu < 30 s) |

Cùng file chạy 2 lần cho ra giống nhau từng mẫu; stereo lệch 3 dB giữ nguyên trong ±0.1 dB.

## Việc tiếp

- Chạy `asmr process` trên TX01_MIC021 thật và 10 clip thật, so với bảng trong spec.
- Theo dõi `limiter_active_pct` trong report: trên file tổng hợp limiter bước 4 đang hạ gain
  15–28% thời gian, tức là nó chạm cả tiếng bút chứ không chỉ click.
- Chạy thử giải thích với API key thật (trong môi trường build chưa có key, mới test bằng client giả).
- M3: FastAPI + ThreadRunner. M4: web UI. M5: video.
