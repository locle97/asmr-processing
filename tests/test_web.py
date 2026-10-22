"""M4: server phục vụ giao diện web, Range cho trình phát, và test giao diện trên Chromium thật (Playwright)."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
import pytest
import soundfile as sf
import uvicorn
from fastapi.testclient import TestClient

from asmrserver import worker
from asmrserver.app import create_app
from asmrserver.runner import ThreadRunner
from synth import make
from test_api import wait, wav_bytes

SR = 48000
HERE = os.path.dirname(__file__)


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("jobs"))
    worker.clear_cache()
    with TestClient(create_app(data_dir=root, runner=ThreadRunner(root), default_llm=False)) as c:
        yield c


def test_index_and_assets_served(api):
    r = api.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert '<script src="/ui/app.js">' in r.text and "file-input" in r.text
    js = api.get("/ui/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert api.get("/ui/app.css").status_code == 200
    assert api.get("/ui/../app.py").status_code == 404  # không lộ code server
    # trang chủ không nằm trong tài liệu API
    assert "/" not in api.get("/openapi.json").json()["paths"]


def test_audio_supports_range_requests(api):
    """Safari chỉ phát và tua được <audio> khi server trả 206 cho Range."""
    x = make(N=-61.0, A=-46.7, dur=6.0, seed=4)
    res = api.post("/jobs", files={"file": ("r.wav", wav_bytes(x), "audio/wav")}).json()
    r = wait(api, res["job_id"], res["render_id"])
    assert r["render"]["status"] == "done", r["render"]
    url = f"/jobs/{res['job_id']}/renders/{res['render_id']}/audio"
    for v in ("orig", "proc"):
        full = api.get(url, params={"v": v})
        assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
        part = api.get(url, params={"v": v}, headers={"Range": "bytes=0-1"})
        assert part.status_code == 206 and part.content == full.content[:2] == b"fL"
        assert part.headers["content-range"] == f"bytes 0-1/{len(full.content)}"


# ---------------------------------------------------------------- trình duyệt thật
def _node_playwright():
    node = shutil.which("node")
    if not node:
        return None, None
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    env = {**os.environ, "NODE_PATH": os.pathsep.join(filter(None, [root, os.environ.get("NODE_PATH")]))}
    ok = subprocess.run([node, "-e", "require('playwright')"], env=env, capture_output=True).returncode == 0
    return (node, env) if ok else (None, None)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("jobs_live"))
    worker.clear_cache()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(data_dir=root, default_llm=False),
                                           host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ui_end_to_end(live_server, tmp_path, engine):
    node, env = _node_playwright()
    if not node:
        pytest.skip("cần node + playwright (npm i -g playwright)")
    ok = tmp_path / "clip.wav"
    short = tmp_path / "short.wav"
    sf.write(ok, make(N=-61.0, A=-46.7, dur=12.0, seed=7), SR, subtype="FLOAT")
    sf.write(short, np.zeros((SR * 2, 2), dtype=np.float32), SR, subtype="FLOAT")
    env = {**env, "E2E_SHOT_DIR": str(tmp_path)}
    p = subprocess.run([node, os.path.join(HERE, "e2e", "ui.cjs"), live_server, str(ok), str(short), engine],
                       env=env, capture_output=True, text=True, timeout=600)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if not lines and "Executable doesn't exist" in p.stderr:
        pytest.skip(f"chưa cài trình duyệt {engine} cho Playwright")
    assert lines, p.stderr[-2000:]
    out = json.loads(lines[-1])
    assert out["ok"], out
