"""Lưu job trên đĩa.

    <root>/<job_id>/
        input.<ext>              # file upload (không có khi tạo job bằng đường dẫn local)
        job.json                 # trạng thái job và các lần render
        job.lock
        renders/<render_id>/     # <tên>_asmr.wav/.json/.png, <tên>_asmr_peaks.json, orig.flac, proc.flac

job.json được ghi nguyên tử (ghi file tạm rồi os.replace) và mọi lần sửa đều giữ khoá file,
nên API và worker ở tiến trình khác (RQ) có thể cùng đọc/ghi.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows: chỉ khoá trong tiến trình, đủ cho ThreadRunner
    fcntl = None

_ID = re.compile(r"^[0-9a-f]{6,32}$")
_thread_lock = threading.Lock()

# trạng thái của 1 lần render; job.status lấy theo lần render mới nhất
ACTIVE = ("queued", "analyzing", "denoising", "rendering", "reporting")
TERMINAL = ("done", "failed")


class NotFound(Exception):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class JobStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # đường dẫn ---------------------------------------------------------------
    def job_dir(self, job_id: str) -> str:
        if not _ID.match(job_id):  # chặn ../ và ký tự lạ trong id lấy từ URL
            raise NotFound(job_id)
        return os.path.join(self.root, job_id)

    def render_dir(self, job_id: str, rid: str) -> str:
        if not _ID.match(rid):
            raise NotFound(rid)
        return os.path.join(self.job_dir(job_id), "renders", rid)

    # tạo / đọc / sửa ---------------------------------------------------------
    def new_job_dir(self) -> tuple[str, str]:
        job_id = secrets.token_hex(8)
        d = os.path.join(self.root, job_id)
        os.makedirs(os.path.join(d, "renders"))
        return job_id, d

    def create(self, job_id: str, job: dict) -> None:
        job = {"id": job_id, "created_at": now(), "updated_at": now(), **job}
        self._write(job_id, job)

    def read(self, job_id: str) -> dict:
        path = os.path.join(self.job_dir(job_id), "job.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError as e:
            raise NotFound(job_id) from e

    def update(self, job_id: str, fn: Callable[[dict], None]) -> dict:
        """Đọc, sửa bằng fn(job) tại chỗ, ghi lại; tất cả trong khoá."""
        with self._lock(job_id):
            job = self.read(job_id)
            fn(job)
            job["updated_at"] = now()
            self._write(job_id, job)
            return job

    def update_render(self, job_id: str, rid: str, **fields) -> dict:
        def fn(job):
            job["renders"][rid].update(fields)
            if job.get("latest_render") == rid:
                for k in ("status", "step", "progress", "error"):
                    if k in fields:
                        job[k] = fields[k]
        return self.update(job_id, fn)

    def delete(self, job_id: str) -> None:
        d = self.job_dir(job_id)
        if not os.path.isdir(d):
            raise NotFound(job_id)
        shutil.rmtree(d)

    # nội bộ -----------------------------------------------------------------
    def _write(self, job_id: str, job: dict) -> None:
        d = self.job_dir(job_id)
        tmp = os.path.join(d, f".job.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(d, "job.json"))

    @contextmanager
    def _lock(self, job_id: str) -> Iterator[None]:
        with _thread_lock:
            if fcntl is None:
                yield
                return
            with open(os.path.join(self.job_dir(job_id), "job.lock"), "a") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
