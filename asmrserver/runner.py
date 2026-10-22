"""Hàng đợi job. Chọn bằng biến môi trường ASMR_RUNNER=thread (mặc định) hoặc rq.

- ThreadRunner: 1 thread trong tiến trình FastAPI, xử lý lần lượt từng render. Cho máy cá nhân.
- RqRunner: đẩy vào Redis + RQ, worker chạy ở tiến trình khác (`rq worker asmr`). Cho server.
  Worker đọc/ghi cùng thư mục job, nên API không cần biết job chạy ở đâu.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Protocol

from . import worker


class JobRunner(Protocol):
    def submit(self, job_id: str, rid: str) -> None: ...


class ThreadRunner:
    def __init__(self, root: str):
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._loop, name="asmr-worker", daemon=True)
        self._t.start()

    def submit(self, job_id: str, rid: str) -> None:
        self._q.put((job_id, rid))

    def _loop(self) -> None:
        while True:
            job_id, rid = self._q.get()
            try:
                worker.run_render(self.root, job_id, rid)
            finally:
                self._q.task_done()

    def join(self) -> None:
        """Chờ hết hàng đợi (dùng trong test)."""
        self._q.join()


class RqRunner:
    def __init__(self, root: str, redis_url: str | None = None, queue_name: str = "asmr"):
        from redis import Redis  # chỉ cần khi chạy chế độ server
        from rq import Queue

        self.root = root
        self._q = Queue(queue_name, connection=Redis.from_url(redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")))

    def submit(self, job_id: str, rid: str) -> None:
        self._q.enqueue(worker.run_render, self.root, job_id, rid, job_timeout=900)


def from_env(root: str) -> JobRunner:
    kind = os.environ.get("ASMR_RUNNER", "thread")
    if kind == "rq":
        return RqRunner(root)
    if kind != "thread":
        raise ValueError(f"ASMR_RUNNER={kind!r} không hợp lệ, dùng thread hoặc rq")
    return ThreadRunner(root)
