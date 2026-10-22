import copy
import json
from types import SimpleNamespace

import pytest

from asmrcore import explain, report
from asmrcore.pipeline import process
from synth import make

SR = 48000


@pytest.fixture(scope="module")
def rep():
    x = make(N=-61.0, A=-46.7, C=-5.6, dur=12.0, seed=21)
    return report.build(process(x, SR), "TX_like.wav", "0" * 64, "default")


class FakeClient:
    """Thay cho anthropic.Anthropic: trả câu cố định, ghi lại request."""

    def __init__(self, text="", stop_reason="end_turn", error=None):
        self.calls = []
        self._text, self._stop, self._err = text, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._err:
            raise self._err
        return SimpleNamespace(stop_reason=self._stop,
                               content=[SimpleNamespace(type="text", text=self._text)])


def test_facts_have_no_file_identity(rep):
    s = json.dumps(explain.facts(rep), ensure_ascii=False)
    assert "TX_like" not in s and "0" * 64 not in s


@pytest.mark.parametrize("warnings", [[], ["TOO_QUIET"], ["LOW_SNR"], ["TOO_QUIET", "LOW_SNR", "CLIPPED"]])
@pytest.mark.parametrize("all_pass", [True, False])
def test_template_always_passes_own_check(rep, warnings, all_pass):
    r = copy.deepcopy(rep)
    r["warnings"], r["all_pass"] = warnings, all_pass
    fx = explain.facts(r)
    text = explain.template(fx)
    assert explain.check(text, fx) == []
    assert 1 <= len(explain.sentences(text)) <= 3


def test_check_accepts_rounded_and_meaningful_differences(rep):
    fx = explain.facts(rep)
    lufs = fx["do_to_LUFS"]
    up = abs(lufs["sau"] - lufs["truoc"])
    text = (f"File được đẩy lên khoảng {round(lufs['sau'])} LUFS, tức là to hơn khoảng {round(up)} dB. "
            f"Âm nền còn {fx['am_nen_dB']['sau']:.1f} dB.")
    assert explain.check(text, fx) == []


def test_check_rejects_invented_number(rep):
    fx = explain.facts(rep)
    errs = explain.check("File đã được đẩy lên -9.3 LUFS.", fx)
    assert errs and "9.3" in errs[0]


def test_check_rejects_more_than_three_sentences(rep):
    fx = explain.facts(rep)
    assert any("câu" in e for e in explain.check("Một. Hai. Ba. Bốn.", fx))


def test_llm_answer_used_when_it_passes(rep):
    fx = explain.facts(rep)
    good = f"Tiếng click to nhất đã được kéo xuống {fx['click_dinh_dBFS']['sau']:.1f} dBFS."
    c = FakeClient(good)
    out = explain.explain(rep, client=c)
    assert out == {"text": good, "source": "llm", "model": explain.MODEL, "rejected": []}
    req = c.calls[0]
    assert req["model"] == explain.MODEL
    assert json.loads(req["messages"][0]["content"]) == fx  # chỉ gửi số đo, không gửi âm thanh


@pytest.mark.parametrize("client", [
    FakeClient("Âm nền đã xuống -99.9 dB."),                       # bịa số
    FakeClient("Một. Hai. Ba. Bốn."),                              # quá 3 câu
    FakeClient("", stop_reason="refusal"),
    FakeClient(error=ConnectionError("offline")),
])
def test_falls_back_to_template(rep, client):
    out = explain.explain(rep, client=client)
    assert out["source"] == "template" and out["model"] is None
    assert out["text"] == explain.template(explain.facts(rep))
    assert out["rejected"]


def test_no_llm_does_not_call_client(rep):
    c = FakeClient("x")
    out = explain.explain(rep, client=c, use_llm=False)
    assert out["source"] == "template" and out["rejected"] == [] and c.calls == []


def test_explain_does_not_touch_report(rep):
    before = json.dumps(rep, sort_keys=True)
    explain.explain(rep, client=FakeClient("Ổn."))
    assert json.dumps(rep, sort_keys=True) == before
