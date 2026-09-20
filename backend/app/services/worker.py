"""后台 worker：推进 pending 提交。

用线程而不是外部队列，保持 demo 的可讲解性与「clone 即跑」。
mock 很快，real 的阻塞 HTTP 调用放在这个线程里也不会卡住 API 请求。
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings
from app.db.engine import session_scope
from app.services.generation_service import run_pending

logger = logging.getLogger("manju.worker")


class Worker:
    def __init__(self, interval: float | None = None, batch: int = 4) -> None:
        self.interval = interval or settings.WORKER_INTERVAL_SECONDS
        self.batch = batch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="manju-worker", daemon=True)
        self._thread.start()
        logger.info("worker started, interval=%.2fs", self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    processed = run_pending(db, limit=self.batch)
                if processed == 0:
                    time.sleep(self.interval)
            except Exception:  # noqa: BLE001 - worker 不能因为单次异常退出
                logger.exception("worker tick failed")
                time.sleep(self.interval)

    def tick_once(self) -> int:
        """测试用：同步推进一轮。"""
        with session_scope() as db:
            return run_pending(db, limit=self.batch)


worker = Worker()
