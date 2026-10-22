"""Lời giải thích tiếng Việt, tối đa 3 câu, viết từ báo cáo.

LLM chỉ đọc số đo (không nhận âm thanh, không nhận tên file) và không chỉnh gì trong âm thanh.
Câu trả lời bị loại nếu nhắc một con số không có trong báo cáo, hoặc dài quá 3 câu; khi đó, hoặc
khi không gọi được API, dùng câu mẫu dựng sẵn từ chính các số đo.
"""

from __future__ import annotations

import json
import os
import re

MODEL = os.environ.get("ASMR_LLM_MODEL", "claude-opus-5-5")
MAX_SENTENCES = 3
# mức meter nên đạt khi tô bút, dùng cho câu gợi ý cách thu; đưa vào báo cáo để LLM được phép nhắc
RECORD_METER_DB = [-30, -20]

SYSTEM = (
    "Bạn viết lời giải thích ngắn cho người quay video ASMR tô màu bằng marker, không rành kỹ thuật âm thanh. "
    "Đầu vào là JSON số đo của một file trước và sau khi app tự xử lý. "
    "Viết tiếng Việt, tối đa 3 câu, giọng tự nhiên. Câu cuối là 1 gợi ý cụ thể để lần sau thu tốt hơn. "
    "Chỉ được nhắc những con số có trong JSON (có thể làm tròn, hoặc nói hiệu của 2 số trong JSON). "
    "Không bịa số, không nhắc tên thông số kỹ thuật như ratio, attack, release. "
    "Dùng từ dễ hiểu: âm nền (tiếng xì), tiếng tô bút, tiếng click (bật nắp, gõ bút), độ to. "
    "Chỉ trả về đoạn văn, không tiêu đề, không gạch đầu dòng."
)

_NUM = re.compile(r"(?<![\w.])[-−+]?\d+(?:[.,]\d+)?")


def facts(report: dict) -> dict:
    """Phần báo cáo gửi cho LLM: chỉ số đo và cảnh báo, không có tên file hay đường dẫn."""
    pre = report["analysis_pre"]
    rows = {r["key"]: r for r in report["summary"]}
    return {
        "canh_bao": report["warnings"],
        "am_nen_dB": {"truoc": rows["noise"]["before"], "sau": rows["noise"]["after"], "muc_tieu": rows["noise"]["target"]},
        "tieng_to_but_dB": {"truoc": rows["action"]["before"], "sau_p95_p99": rows["action"]["after"],
                            "muc_tieu": rows["action"]["target"]},
        "click_dinh_dBFS": {"truoc": rows["peak"]["before"], "sau": rows["peak"]["after"], "tran": -6,
                            "so_click_lon": pre["C_big"], "so_click_vua": pre["C_mid"]},
        "do_to_LUFS": {"truoc": rows["lufs"]["before"], "sau": rows["lufs"]["after"], "muc_tieu": rows["lufs"]["target"]},
        "chenh_lech_tieng_but_va_nen_dB": pre["S_db"],
        "giam_on_dB": report["params"]["denoise"]["db"],
        "tat_ca_dat": report["all_pass"],
        "chua_dat": [c["key"] for c in report["checks"] if not c["pass"]],
        "goi_y_muc_meter_khi_thu_dB": RECORD_METER_DB,
    }


def _numbers(obj) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _numbers(x)]
    if isinstance(obj, (list, tuple)):
        return [v for x in obj for v in _numbers(x)]
    return []


def numbers_in(text: str) -> list[float]:
    return [float(m.replace("−", "-").replace(",", ".")) for m in _NUM.findall(text)]


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p for p in parts if p.strip()]


def _pairs(fx: dict) -> list[tuple[float, float]]:
    """Các cặp số mà hiệu của chúng có nghĩa: trước/sau của cùng chỉ số, số đo so với mục tiêu,
    tiếng bút so với âm nền. Không lấy hiệu của mọi cặp, vì như vậy gần như số nào cũng lọt."""
    nen, but, click, lufs = fx["am_nen_dB"], fx["tieng_to_but_dB"], fx["click_dinh_dBFS"], fx["do_to_LUFS"]
    after_but = but["sau_p95_p99"]
    pairs = [(nen["truoc"], nen["sau"]), (click["truoc"], click["sau"]), (click["truoc"], click["tran"]),
             (lufs["truoc"], lufs["sau"]), (but["truoc"], nen["truoc"])]
    pairs += [(but["truoc"], a) for a in after_but] + [(a, nen["sau"]) for a in after_but]
    pairs += [(lufs["truoc"], t) for t in lufs["muc_tieu"]]
    pairs += [(lufs["truoc"], m) for m in fx["goi_y_muc_meter_khi_thu_dB"]]
    pairs += [(but["truoc"], m) for m in fx["goi_y_muc_meter_khi_thu_dB"]]
    return [(a, b) for a, b in pairs if a is not None and b is not None]


def check(text: str, fx: dict) -> list[str]:
    """Trả danh sách lỗi; rỗng nghĩa là câu trả lời dùng được."""
    errs = []
    if not text.strip():
        errs.append("rỗng")
    if len(sentences(text)) > MAX_SENTENCES:
        errs.append(f"dài {len(sentences(text))} câu")
    known = {abs(v) for v in _numbers(fx)}
    diffs = {abs(a - b) for a, b in _pairs(fx)}
    for n in numbers_in(text):
        a = abs(n)
        tol = 0.5 if a == round(a) else 0.06  # số nguyên: làm tròn; số lẻ: đúng tới 1 chữ số
        ok = (any(abs(a - k) <= tol for k in known)
              or any(abs(a - d) <= max(tol, 1.0) for d in diffs))  # hiệu 2 số, thường nói "khoảng"
        if not ok:
            errs.append(f"số {n:g} không có trong báo cáo")
    return errs


def template(fx: dict) -> str:
    """Câu mẫu khi không có LLM. Chỉ dùng số trong fx nên luôn qua được check()."""
    lufs = fx["do_to_LUFS"]
    out = []
    if "TOO_QUIET" in fx["canh_bao"] or "LOW_SNR" in fx["canh_bao"]:
        out.append(f"File thu nhỏ (độ to {lufs['truoc']:.1f} LUFS) và tiếng tô bút chỉ to hơn âm nền "
                   f"{fx['chenh_lech_tieng_but_va_nen_dB']:.1f} dB, nên app chỉ đẩy lên {lufs['sau']:.1f} LUFS "
                   "để tiếng xì không bị lộ.")
    else:
        out.append(f"App đã đưa độ to từ {lufs['truoc']:.1f} lên {lufs['sau']:.1f} LUFS và giữ âm nền ở "
                   f"{fx['am_nen_dB']['sau']:.1f} dB.")
    if fx["click_dinh_dBFS"]["so_click_lon"] or fx["click_dinh_dBFS"]["so_click_vua"]:
        out.append(f"Tiếng click to nhất đã được kéo từ {fx['click_dinh_dBFS']['truoc']:.1f} xuống "
                   f"{fx['click_dinh_dBFS']['sau']:.1f} dBFS.")
    if fx["tat_ca_dat"]:
        lo, hi = fx["goi_y_muc_meter_khi_thu_dB"]
        out.append(f"Lần sau tăng gain trên mic để meter lên {lo} đến {hi} dB khi tô bút thì âm nền sẽ sạch hơn."
                   if "TOO_QUIET" in fx["canh_bao"] else "Lần sau cứ thu ở mức như lần này là ổn.")
    else:
        out.append("Một số chỉ số chưa đạt mục tiêu, bạn nên nghe lại và chỉnh nhanh bằng thanh trượt.")
    return " ".join(out[:MAX_SENTENCES])


def _ask_claude(fx: dict, client) -> str:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": json.dumps(fx, ensure_ascii=False)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("refusal")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def explain(report: dict, client=None, use_llm: bool = True) -> dict:
    """Trả {"text", "source": "llm"|"template", "model", "rejected"}. Không bao giờ ném lỗi."""
    fx = facts(report)
    rejected: list[str] = []
    if use_llm:
        try:
            if client is None:
                import anthropic
                client = anthropic.Anthropic(timeout=30.0, max_retries=1)
            text = _ask_claude(fx, client)
            errs = check(text, fx)
            if not errs:
                return {"text": text, "source": "llm", "model": MODEL, "rejected": []}
            rejected = errs
        except Exception as e:  # mất mạng, chưa có API key, bị từ chối...: vẫn phải có báo cáo
            rejected = [f"{type(e).__name__}: {str(e)[:120]}"]
    return {"text": template(fx), "source": "template", "model": None, "rejected": rejected}
