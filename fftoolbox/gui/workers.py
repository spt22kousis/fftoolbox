"""背景執行緒：編碼器偵測、佇列執行、以及一般的單次工作（ffprobe、字型檢查、預覽）。"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from .. import encoders
from ..encoders import DetectionReport
from ..job import FFmpegRunner, Job, run_job


class DetectWorker(QThread):
    message = Signal(str)
    done = Signal(object)  # DetectionReport
    failed = Signal(str)

    def __init__(self, force: bool, parent=None) -> None:
        super().__init__(parent)
        self.force = force

    def run(self) -> None:
        try:
            report = encoders.get_report(force=self.force, progress=self.message.emit)
        except Exception as e:  # noqa: BLE001 — 任何錯誤都要回報到介面
            self.failed.emit(str(e))
            return
        self.done.emit(report)


class FuncWorker(QThread):
    """在背景跑 fn()，完成後發出 done(結果) 或 failed(錯誤訊息)。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
            return
        self.done.emit(result)


class QueueWorker(QThread):
    job_started = Signal(int, str)  # 項目 id, 完整指令
    job_progress = Signal(int, object)  # 項目 id, Progress
    job_log = Signal(str)
    job_finished = Signal(int, object)  # 項目 id, JobResult

    def __init__(self, jobs: list[tuple[int, Job]], report: DetectionReport | None, parent=None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.report = report
        self._stop = False
        self._skipped: set[int] = set()
        self._current: int | None = None
        self._runner: FFmpegRunner | None = None
        self._lock = threading.Lock()

    def run(self) -> None:
        for item_id, job in self.jobs:
            with self._lock:
                if self._stop:
                    break
                if item_id in self._skipped:
                    continue
                self._current = item_id
            result = run_job(
                job, self.report,
                on_progress=lambda p, i=item_id: self.job_progress.emit(i, p),
                on_log=self.job_log.emit,
                on_command=lambda c, i=item_id: self.job_started.emit(i, c.text),
                on_runner=self._set_runner,
            )
            with self._lock:
                self._runner = None
                self._current = None
            self.job_finished.emit(item_id, result)

    def _set_runner(self, runner: FFmpegRunner) -> None:
        with self._lock:
            self._runner = runner
            if self._stop or self._current in self._skipped:
                runner.cancel()

    def skip(self, item_id: int) -> None:
        """從佇列移除項目：還沒輪到的直接跳過；剛開始準備（還在讀來源資訊）的就取消。"""
        with self._lock:
            self._skipped.add(item_id)
            if item_id == self._current and self._runner:
                self._runner.cancel()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            if self._runner:
                self._runner.cancel()
