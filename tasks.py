"""Background work for the GUI: run a function on the Qt thread pool and get
its result (or error) back on the GUI thread through signals."""
from __future__ import annotations

import logging
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class TaskSignals(QObject):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run `fn(progress_cb)` on the thread pool and report back via signals."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.signals.progress.emit))
        except Exception as exc:
            log.exception("Background task failed")
            self.signals.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


class Runner:
    """Starts tasks and keeps them (and their signal objects) alive until they finish."""

    def __init__(self):
        self.pool = QThreadPool.globalInstance()
        self._live: set[Task] = set()

    def start(self, fn, done=None, failed=None, progress=None) -> Task:
        task = Task(fn)
        if done:
            task.signals.done.connect(done)
        if failed:
            task.signals.failed.connect(failed)
        if progress:
            task.signals.progress.connect(progress)
        self._live.add(task)
        for sig in (task.signals.done, task.signals.failed):
            sig.connect(lambda *_a, t=task: self._live.discard(t))
        self.pool.start(task)
        return task
