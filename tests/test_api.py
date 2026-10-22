"""M3: chạy end-to-end qua API (FastAPI + ThreadRunner thật, file tổng hợp ngắn, không gọi Claude)."""

import io
import json
import os
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from asmrcore import audio_io
from asmrcore.measure import lufs
from asmrcore.pipeline import process
from asmrcore.rules import Overrides
from asmrserver import worker
from asmrserver.app import create_app
from asmrserver.runner import ThreadRunner
from synth import make

SR = 48000


class HoldRunner:
    """Nhận job nhưng không chạy, để kiểm tra trạng thái khi chưa xong."""

    def __init__(self):
        self.submitted = []

    def submit(self, job_id, rid):
        self.submitted.append((job_id, rid))


def wav_bytes(x, sr=SR, subtype="FLOAT"):
    b = io.BytesIO()
    sf.write(b, x, sr, subtype=subtype, format="WAV")
    return b.getvalue()


def wait(c, job_id, rid, timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        r = c.get(f"/jobs/{job_id}/renders/{rid}").json()
        if r["render"]["status"] in ("done", "failed"):
            return r
        time.sleep(0.1)
    raise TimeoutError(rid)


@pytest.fixture(scope="module")
def clip():
    return make(N=-61.0, A=-46.7, dur=10.0, seed=21)


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("jobs"))
    worker.clear_cache()
    runner = ThreadRunner(root)
    with TestClient(create_app(data_dir=root, runner=runner, default_llm=False)) as c:
        yield c, root


@pytest.fixture(scope="module")
def first_job(api, clip):
    c, _ = api
    runs = worker.stats["prepare_runs"]
    res = c.post("/jobs", files={"file": ("clip.wav", wav_bytes(clip), "audio/wav")})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["job"]["status"] == "queued"
    assert "path" not in body["job"]["input"] and "path" not in body["job"]["probe"]  # không lộ đường dẫn
    return body["job_id"], body["render_id"], runs


def test_upload_progress_over_sse_until_done(api, first_job):
    c, _ = api
    job_id, rid, _ = first_job
    events, seen = [], []
    with c.stream("GET", f"/jobs/{job_id}/events") as s:
        assert s.headers["content-type"].startswith("text/event-stream")
        ev = None
        for line in s.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
                events.append(ev)
            elif line.startswith("data: ") and ev == "job":
                seen.append(json.loads(line[6:]))
    assert events[-1] == "end"
    statuses = [j["status"] for j in seen]
    assert statuses[-1] == "done"
    assert seen[-1]["progress"] == 1.0
    # tiến độ không bao giờ lùi
    prog = [j["progress"] for j in seen]
    assert prog == sorted(prog)
    # số đo có ngay sau khi phân tích, kèm cảnh báo
    job = c.get(f"/jobs/{job_id}").json()
    assert job["analysis"]["N_db"] < job["analysis"]["A_db"]
    assert job["warnings"] == c.get(f"/jobs/{job_id}/renders/{rid}").json()["report"]["warnings"]
    assert len(job["input"]["sha256"]) == 64


def test_api_output_matches_core_bit_exact(api, first_job, clip):
    c, _ = api
    job_id, rid, _ = first_job
    r = wait(c, job_id, rid)
    assert r["render"]["status"] == "done", r["render"]
    rep = r["report"]
    assert rep["input"]["name"] == "clip.wav"
    assert rep["explanation"]["source"] == "template"
    assert set(rep["params"]) >= {"highpass", "denoise", "gain", "limiter", "compressor", "expander", "final"}

    d = c.get(f"/jobs/{job_id}/renders/{rid}/download", params={"f": "wav"})
    assert d.status_code == 200
    assert 'filename="clip_asmr.wav"' in d.headers["content-disposition"]
    y, sr = sf.read(io.BytesIO(d.content), dtype="float32")
    assert sr == SR
    assert np.array_equal(y, process(clip, SR).audio)  # API ra đúng từng mẫu như gọi lõi trực tiếp


def test_rerender_loudness_reuses_denoise_and_is_exact(api, first_job, clip):
    c, _ = api
    job_id, rid, runs0 = first_job
    wait(c, job_id, rid)
    runs = worker.stats["prepare_runs"]
    assert runs == runs0 + 1

    res = c.post(f"/jobs/{job_id}/renders", json={"loudness": -2, "floor": 6})
    assert res.status_code == 202 and res.json()["created"]
    rid2 = res.json()["render_id"]
    r2 = wait(c, job_id, rid2)
    assert r2["render"]["status"] == "done"
    assert worker.stats["prepare_runs"] == runs  # bước 1–2 lấy từ cache
    assert r2["report"]["params"]["lufs_goal"] == r_goal(c, job_id, rid) - 2
    assert r2["report"]["params"]["expander"]["max_cut_db"] == 6

    y, _ = sf.read(io.BytesIO(c.get(f"/jobs/{job_id}/renders/{rid2}/download").content), dtype="float32")
    assert np.array_equal(y, process(clip, SR, ov=Overrides(-2, None, 6)).audio)

    job = c.get(f"/jobs/{job_id}").json()
    assert job["latest_render"] == rid2 and set(job["renders"]) == {rid, rid2}


def r_goal(c, job_id, rid):
    return c.get(f"/jobs/{job_id}/renders/{rid}").json()["report"]["params"]["lufs_goal"]


def test_same_params_return_same_render(api, first_job):
    c, _ = api
    job_id, rid, _ = first_job
    wait(c, job_id, rid)
    res = c.post(f"/jobs/{job_id}/renders", json={"preset": "default"})
    assert res.status_code == 200
    assert res.json() == {"render_id": rid, "created": False, "render": res.json()["render"]}


def test_denoise_slider_runs_stage_2_again(api, first_job):
    c, _ = api
    job_id, rid, _ = first_job
    wait(c, job_id, rid)
    runs = worker.stats["prepare_runs"]
    rid3 = c.post(f"/jobs/{job_id}/renders", json={"denoise": 4}).json()["render_id"]
    r = wait(c, job_id, rid3)
    assert r["render"]["status"] == "done"
    assert r["report"]["params"]["denoise"]["db"] == 4
    assert worker.stats["prepare_runs"] == runs + 1


def test_preview_audio_range_and_loudness_matched(api, first_job):
    c, _ = api
    job_id, rid, _ = first_job
    wait(c, job_id, rid)
    base = f"/jobs/{job_id}/renders/{rid}/audio"
    part = c.get(base, params={"v": "proc"}, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    proc, sr = sf.read(io.BytesIO(c.get(base, params={"v": "proc"}).content), dtype="float32", always_2d=True)
    orig, _ = sf.read(io.BytesIO(c.get(base, params={"v": "orig"}).content), dtype="float32", always_2d=True)
    assert sr == SR and proc.shape == orig.shape
    assert lufs(orig, sr) == pytest.approx(lufs(proc, sr), abs=1.0)  # A/B nghe ngang độ to
    assert np.max(np.abs(orig)) <= 10 ** (-1 / 20) + 1e-3  # gốc tăng gain mà click không vỡ


def test_peaks_png_json_downloads(api, first_job):
    c, _ = api
    job_id, rid, _ = first_job
    wait(c, job_id, rid)
    pk = c.get(f"/jobs/{job_id}/renders/{rid}/peaks").json()
    assert set(pk) == {"before", "after"}
    png = c.get(f"/jobs/{job_id}/renders/{rid}/download", params={"f": "png"})
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    rep = c.get(f"/jobs/{job_id}/renders/{rid}/download", params={"f": "json"}).json()
    assert rep["output"]["wav"] == "clip_asmr.wav"
    mp4 = c.get(f"/jobs/{job_id}/renders/{rid}/download", params={"f": "mp4"})
    assert mp4.status_code == 404 and mp4.json()["error"]["code"] == "NO_VIDEO"


# ------------------------------------------------------------------ lỗi đầu vào, không cần chạy pipeline

@pytest.fixture
def held(tmp_path):
    r = HoldRunner()
    with TestClient(create_app(data_dir=str(tmp_path), runner=r, default_llm=False)) as c:
        yield c, r, tmp_path


def test_rejects_unsupported_format(held):
    c, r, root = held
    res = c.post("/jobs", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert res.status_code == 422 and res.json()["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert not r.submitted and os.listdir(root) == []  # không để lại thư mục job rác


def test_rejects_too_short(held):
    c, r, _ = held
    res = c.post("/jobs", files={"file": ("s.wav", wav_bytes(make(dur=6.0)[: int(4.9 * SR)]), "audio/wav")})
    assert res.status_code == 422 and res.json()["error"]["code"] == "TOO_SHORT"
    assert "5 s" in res.json()["error"]["message"]


def test_rejects_bad_params(held, clip):
    c, r, _ = held
    res = c.post("/jobs", files={"file": ("a.wav", wav_bytes(clip), "audio/wav")}, data={"loudness": "9"})
    assert res.status_code == 422 and res.json()["error"]["code"] == "INVALID_PARAMS"


def test_not_ready_and_not_found(held, clip):
    c, r, _ = held
    body = c.post("/jobs", files={"file": ("a.wav", wav_bytes(clip), "audio/wav")}, data={"preset": "soft"}).json()
    job_id, rid = body["job_id"], body["render_id"]
    assert r.submitted == [(job_id, rid)]
    assert body["job"]["renders"][rid]["params"]["preset"] == "soft"
    nr = c.get(f"/jobs/{job_id}/renders/{rid}/download")
    assert nr.status_code == 409 and nr.json()["error"]["code"] == "NOT_READY"
    assert c.delete(f"/jobs/{job_id}").json()["error"]["code"] == "BUSY"
    for url in ("/jobs/0123456789abcdef", "/jobs/..%2F..%2Fetc", f"/jobs/{job_id}/renders/ffffffffffff"):
        res = c.get(url)
        assert res.status_code == 404, url


def test_local_path_mode(held, tmp_path_factory, clip):
    c, r, root = held
    src = tmp_path_factory.mktemp("src") / "TX01.flac"
    sf.write(src, clip, SR, subtype="PCM_24")
    body = c.post("/jobs", json={"path": str(src), "preset": "loud"}).json()
    job = c.get(f"/jobs/{body['job_id']}").json()
    assert job["input"]["name"] == "TX01.flac" and job["input"]["uploaded"] is False
    assert not any(f.startswith("input") for f in os.listdir(os.path.join(root, body["job_id"])))  # không copy file
    assert c.post("/jobs", json={"path": str(src) + ".nope"}).json()["error"]["code"] == "FILE_NOT_FOUND"


def test_local_path_mode_can_be_disabled(tmp_path, clip):
    with TestClient(create_app(data_dir=str(tmp_path), runner=HoldRunner(), allow_local_paths=False)) as c:
        res = c.post("/jobs", json={"path": "/etc/passwd"})
    assert res.status_code == 403 and res.json()["error"]["code"] == "LOCAL_PATHS_DISABLED"


def test_worker_failure_is_reported_not_hung(held, clip):
    c, r, root = held
    body = c.post("/jobs", files={"file": ("a.wav", wav_bytes(clip), "audio/wav")}).json()
    job_id, rid = body["job_id"], body["render_id"]
    os.remove(os.path.join(root, job_id, "input.wav"))  # file biến mất trước khi worker chạy
    worker.run_render(str(root), job_id, rid)
    job = c.get(f"/jobs/{job_id}").json()
    assert job["status"] == "failed" and job["error"]["code"]
    with c.stream("GET", f"/jobs/{job_id}/events") as s:
        assert "event: end" in [ln for ln in s.iter_lines()]
    # gửi lại cùng thông số thì được chạy lại
    again = c.post(f"/jobs/{job_id}/renders", json={})
    assert again.status_code == 202 and again.json()["render_id"] == rid
    assert r.submitted[-1] == (job_id, rid)


def test_delete_finished_job(held, clip):
    c, r, root = held
    body = c.post("/jobs", files={"file": ("a.wav", wav_bytes(clip), "audio/wav")}).json()
    os.remove(os.path.join(root, body["job_id"], "input.wav"))
    worker.run_render(str(root), body["job_id"], body["render_id"])  # kết thúc bằng failed
    assert c.delete(f"/jobs/{body['job_id']}").status_code == 204
    assert c.get(f"/jobs/{body['job_id']}").status_code == 404
