"""One command thread per printer.

The report loop reads snapshots and must not wait on connect, upload, or MQTT.
A stuck command occupies only this serial's queue. ``stop`` asks the in-flight
job to notice ``cancel_event`` and does not join past half a second, so removing
a printer cannot deadlock behind a transfer that is still exiting.
"""

import queue
import threading
from concurrent.futures import Future

# remove_printer calls stop on the report path's caller. A transfer that has
# already passed a block boundary exits on cancel; anything slower than this
# is left to the daemon thread so membership changes stay prompt.
_STOP_JOIN_SECONDS = 0.5


class WorkerBusy(Exception):
    """The printer's command queue is full. The caller was not blocked."""


class PrinterWorker:
    def __init__(self, serial: str, *, queue_size: int = 8):
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.serial = serial
        self.cancel_event = threading.Event()
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=queue_size)
        self._queue_size = queue_size
        self._lock = threading.Lock()
        # Waiting jobs only. The running job is `_running`, so busy stays true
        # across the handoff from the queue to the thread.
        self._queued = 0
        self._running = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"printer-worker-{serial}",
            daemon=True,
        )
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._queued > 0 or self._running

    def submit(self, fn, *args, **kwargs) -> Future:
        """Queue ``fn``. A full queue returns a future already failed with WorkerBusy."""
        future = Future()
        item = (fn, args, kwargs, future)
        with self._lock:
            if self._stop.is_set():
                future.set_exception(
                    WorkerBusy(f"printer {self.serial} worker is stopped")
                )
                return future
            if self._queued >= self._queue_size:
                future.set_exception(
                    WorkerBusy(f"printer {self.serial} worker queue is full")
                )
                return future
            self._queued += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._queued -= 1
                future.set_exception(
                    WorkerBusy(f"printer {self.serial} worker queue is full")
                )
        return future

    def stop(self) -> None:
        """Cancel queued jobs and ask the running one to exit.

        The join is bounded. ``cancel_event`` is how an in-flight upload notices.
        ``_stop`` is set under the same lock as the queue count so a job that has
        not entered ``fn`` is cancelled instead of started.
        """
        with self._lock:
            self._stop.set()
            self.cancel_event.set()
            drained = []
            while True:
                try:
                    drained.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._queued = max(0, self._queued - len(drained))
        for _fn, _args, _kwargs, future in drained:
            future.cancel()
        self._thread.join(timeout=_STOP_JOIN_SECONDS)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            fn, args, kwargs, future = item
            with self._lock:
                self._queued = max(0, self._queued - 1)
                if self._stop.is_set():
                    start = False
                else:
                    start = True
                    self._running = True
            if not start:
                future.cancel()
                continue
            self._run_one(fn, args, kwargs, future)

    def _run_one(self, fn, args, kwargs, future) -> None:
        try:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
        finally:
            with self._lock:
                self._running = False
