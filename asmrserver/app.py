"""FastAPI: tạo job, theo dõi tiến độ (SSE), render lại với preset/thanh trượt, tải kết quả.

    GET  /                              giao diện web (asmrserver/web), file tĩnh ở /ui/
    POST /jobs                          upload multipart (trường `file`) hoặc JSON {"path": ...} khi chạy local
    GET  /jobs/{id}                     trạng thái, bước, %, cảnh báo, số đo
    GET  /jobs/{id}/events              SSE: gửi trạng thái mỗi khi job.json đổi, đóng khi render xong/lỗi
    DELETE /jobs/{id}
    POST /jobs/{id}/renders             {"preset", "loudness", "denoise", "floor", "bits", "llm"}
    GET  /jobs/{id}/renders/{rid}       trạng thái + report JSON khi xong
    GET  …/renders/{rid}/audio?v=orig|proc   FLAC nghe thử (hỗ trợ Range); bản gốc đã tăng gain ngang độ to
    GET  …/renders/{rid}/peaks          peaks trước/sau cho wavesurfer.js
    GET  …/renders/{rid}/download?f=wav|json|png|peaks

Lỗi luôn có dạng {"error": {"code": "...", "message": "..."}}; mã lỗi đầu vào giống asmrcore.audio_io.
Render id là hash của thông số, nên gửi lại đúng thông số cũ thì nhận lại kết quả cũ, không chạy lại.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from asmrcore import PIPELINE_VERSION, audio_io

from . import runner as runners
from .store import ACTIVE, TERMINAL, JobStore, NotFound

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
SSE_POLL_S = 0.25
SSE_HEARTBEAT_S = 15.0


class RenderParams(BaseModel):
    """Preset và 3 thanh trượt. Khoảng giá trị theo trang thiết kế."""
    model_config = ConfigDict(extra="forbid")
    preset: Literal["default", "soft", "loud"] = "default"
    loudness: float = Field(0.0, ge=-6, le=3)          # thanh "Độ to": lệch mục tiêu LUFS
    denoise: float | None = Field(None, ge=0, le=12)   # thanh "Giảm ồn": None = theo quy tắc
    floor: float | None = Field(None, ge=0, le=12)     # thanh "Âm nền": mức hạ nền tối đa
    bits: Literal["float32", "24"] | None = None       # None = 32f nếu gốc là 32f, còn lại 24-bit
    llm: bool | None = None                            # None = theo cấu hình server


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "no", "")


def public_job(job: dict) -> dict:
    """job.json bỏ đường dẫn trên đĩa."""
    out = dict(job)
    out["input"] = {k: v for k, v in job["input"].items() if k != "path"}
    out["probe"] = {k: v for k, v in job["probe"].items() if k != "path"}
    return out


def create_app(data_dir: str | None = None, runner: runners.JobRunner | None = None,
               allow_local_paths: bool | None = None, default_llm: bool | None = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("ASMR_DATA_DIR") or os.path.expanduser("~/.asmr/jobs")
    store = JobStore(data_dir)
    runner = runner or runners.from_env(store.root)
    allow_paths = _env_flag("ASMR_ALLOW_LOCAL_PATHS", True) if allow_local_paths is None else allow_local_paths
    llm_default = _env_flag("ASMR_LLM", True) if default_llm is None else default_llm

    app = FastAPI(title="ASMR Auto Master", version=PIPELINE_VERSION)
    app.state.store, app.state.runner = store, runner

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, e: NotFound):
        return _err(404, "NOT_FOUND", "Không tìm thấy job hoặc lần render này.")

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return _err(e.status, e.code, e.message)

    @app.exception_handler(ValidationError)
    async def _invalid(_: Request, e: ValidationError):
        return _err(422, "INVALID_PARAMS", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))

    # ---------------------------------------------------------------- helpers
    def add_render(job_id: str, params: RenderParams) -> tuple[str, bool]:
        p = params.model_dump()
        if p["llm"] is None:
            p["llm"] = llm_default
        rid = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]
        created = False

        def fn(job):
            nonlocal created
            old = job["renders"].get(rid)
            if old is None or old["status"] == "failed":
                created = True
                job["renders"][rid] = {"id": rid, "params": p, "status": "queued", "step": None, "progress": 0.0,
                                       "error": None, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            r = job["renders"][rid]
            job["latest_render"] = rid
            job.update(status=r["status"], step=r["step"], progress=r["progress"], error=r["error"])

        store.update(job_id, fn)
        if created:
            runner.submit(job_id, rid)
        return rid, created

    def done_render(job_id: str, rid: str) -> tuple[dict, str]:
        job = store.read(job_id)
        r = job["renders"].get(rid)
        if r is None:
            raise NotFound(rid)
        if r["status"] != "done":
            raise ApiError(409, "NOT_READY", f"Lần render này chưa xong (trạng thái: {r['status']}).")
        return r, store.render_dir(job_id, rid)

    async def save_upload(up: UploadFile, dst: str) -> int:
        n = 0
        with open(dst, "wb") as f:
            while chunk := await up.read(1 << 20):
                n += len(chunk)
                if n > audio_io.MAX_BYTES:
                    raise audio_io.InputError("TOO_LARGE", "File lớn hơn 500 MB. Hãy cắt bớt hoặc xuất lại nhỏ hơn.")
                f.write(chunk)
        return n

    # ---------------------------------------------------------------- routes
    app.mount("/ui", StaticFiles(directory=WEB_DIR), name="ui")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"), media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    def health():
        return {"ok": True, "pipeline_version": PIPELINE_VERSION}

    @app.post("/jobs", status_code=201)
    async def create_job(request: Request):
        ct = request.headers.get("content-type", "")
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > audio_io.MAX_BYTES + (1 << 20):
            raise ApiError(413, "TOO_LARGE", "File lớn hơn 500 MB. Hãy cắt bớt hoặc xuất lại nhỏ hơn.")

        job_id, jdir = store.new_job_dir()
        try:
            if ct.startswith("multipart/form-data"):
                form = await request.form()
                up = form.get("file")
                if not isinstance(up, UploadFile) or not up.filename:
                    raise ApiError(422, "NO_FILE", "Thiếu file (trường 'file' trong form).")
                params = RenderParams.model_validate({k: v for k, v in form.items() if k != "file" and v != ""})
                name = os.path.basename(up.filename.replace("\\", "/"))
                ext = os.path.splitext(name)[1].lower()
                if ext not in audio_io.AUDIO_EXT | audio_io.VIDEO_EXT:
                    audio_io.probe(os.path.join(jdir, "input" + ext))  # ném UNSUPPORTED_FORMAT trước khi đọc body
                path = os.path.join(jdir, "input" + ext)
                size = await save_upload(up, path)
                uploaded = True
            elif ct.startswith("application/json"):
                body = await request.json()
                if not isinstance(body, dict) or not isinstance(body.get("path"), str):
                    raise ApiError(422, "NO_FILE", 'Cần multipart có trường "file", hoặc JSON {"path": ...}.')
                if not allow_paths:
                    raise ApiError(403, "LOCAL_PATHS_DISABLED", "Server này không cho đọc file theo đường dẫn. Hãy upload file.")
                path = os.path.abspath(os.path.expanduser(body.pop("path")))
                if not os.path.isfile(path):
                    raise ApiError(422, "FILE_NOT_FOUND", f"Không thấy file {path}.")
                params = RenderParams.model_validate(body)
                name, size, uploaded = os.path.basename(path), os.path.getsize(path), False
            else:
                raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Gửi multipart/form-data (upload) hoặc application/json (đường dẫn).")

            info = audio_io.probe(path)
            if info.is_video:
                raise audio_io.InputError("UNSUPPORTED_FORMAT", "Xử lý video sẽ có ở mốc M5. Bản này nhận file âm thanh.")
        except audio_io.InputError as e:
            shutil.rmtree(jdir, ignore_errors=True)
            return _err(422, e.code, str(e))
        except BaseException:
            shutil.rmtree(jdir, ignore_errors=True)
            raise

        store.create(job_id, {
            "input": {"name": name, "path": path, "uploaded": uploaded, "size": size, "sha256": None},
            "probe": asdict(info),
            "status": "queued", "step": None, "progress": 0.0, "error": None,
            "warnings": [], "analysis": None, "renders": {}, "latest_render": None,
        })
        rid, _ = add_render(job_id, params)
        return {"job_id": job_id, "render_id": rid, "job": public_job(store.read(job_id))}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return public_job(store.read(job_id))

    @app.delete("/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        job = store.read(job_id)
        if any(r["status"] in ACTIVE for r in job["renders"].values()):
            raise ApiError(409, "BUSY", "Job đang xử lý, chờ xong rồi xoá.")
        store.delete(job_id)

    @app.get("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        store.read(job_id)  # 404 trước khi mở stream

        async def gen():
            last, beat = None, time.monotonic()
            while not await request.is_disconnected():
                job = public_job(store.read(job_id))
                snap = json.dumps(job, ensure_ascii=False)
                if snap != last:
                    yield f"event: job\ndata: {snap}\n\n"
                    last, beat = snap, time.monotonic()
                elif time.monotonic() - beat > SSE_HEARTBEAT_S:
                    yield ": ping\n\n"
                    beat = time.monotonic()
                if job["status"] in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/jobs/{job_id}/renders")
    def create_render(job_id: str, params: RenderParams):
        store.read(job_id)
        rid, created = add_render(job_id, params)
        r = store.read(job_id)["renders"][rid]
        return JSONResponse({"render_id": rid, "created": created, "render": r}, status_code=202 if created else 200)

    @app.get("/jobs/{job_id}/renders/{rid}")
    def get_render(job_id: str, rid: str):
        r = store.read(job_id)["renders"].get(rid)
        if r is None:
            raise NotFound(rid)
        rep = None
        if r["status"] == "done":
            with open(os.path.join(store.render_dir(job_id, rid), r["files"]["report_json"]), encoding="utf-8") as f:
                rep = json.load(f)
        return {"render": r, "report": rep}

    @app.get("/jobs/{job_id}/renders/{rid}/audio")
    def audio(job_id: str, rid: str, v: Literal["orig", "proc"] = "proc"):
        _, d = done_render(job_id, rid)
        return FileResponse(os.path.join(d, f"{v}.flac"), media_type="audio/flac")

    @app.get("/jobs/{job_id}/renders/{rid}/peaks")
    def peaks(job_id: str, rid: str):
        r, d = done_render(job_id, rid)
        return FileResponse(os.path.join(d, r["files"]["peaks_json"]), media_type="application/json")

    @app.get("/jobs/{job_id}/renders/{rid}/download")
    def download(job_id: str, rid: str, f: Literal["wav", "json", "png", "peaks", "mp4"] = "wav"):
        if f == "mp4":
            raise ApiError(404, "NO_VIDEO", "Xuất video sẽ có ở mốc M5.")
        r, d = done_render(job_id, rid)
        name = r["files"][{"wav": "wav", "json": "report_json", "png": "waveform_png", "peaks": "peaks_json"}[f]]
        media = {"wav": "audio/wav", "json": "application/json", "png": "image/png", "peaks": "application/json"}[f]
        return FileResponse(os.path.join(d, name), media_type=media, filename=name)

    return app
