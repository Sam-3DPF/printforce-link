"""The report loop's self-check.

Link is only useful while the report loop keeps turning. On 2026-10-12 it
stopped for good and the process stayed up, so launchd had nothing to restart
and the shop's printers went dark until someone restarted Link by hand.

The loop calls ``beat`` at the top of every pass. One daemon thread watches
the time since the last beat:

* after ``dump_after`` it logs every thread's stack, once per stalled pass;
* after ``exit_after`` it says so plainly and exits with ``EXIT_STALLED``.
  launchd (KeepAlive) and the Windows task (restart on failure) start a fresh
  Link.

A pass that finishes with a failed report still beats. A cloud outage is not
a stall, and restarting Link would not fix it.

The stacks are read here, in Python, under the interpreter lock. Do not go
back to ``faulthandler.dump_traceback_later``: its timer thread reads other
threads' frames without the lock, and a frame that changes under it is a wild
read. That thread has every signal blocked, and macOS re-runs a faulting
instruction forever when its signal is blocked, so the read spins at 100% CPU
instead of crashing. ``cancel_dump_traceback_later`` then waits for that
thread while holding the interpreter lock, and every Python thread stops.

The backstop covers the case this thread cannot: nothing in Python running at
all. Each check pushes a ``SIGALRM`` further out. Link installs no handler for
it, so if no check runs for ``backstop_seconds`` the kernel ends the process
and the service manager restarts it. There is no log line for that exit; the
process could not write one. Windows has no ``SIGALRM`` and no backstop.
"""
from __future__ import annotations

import datetime
import logging
import os
import signal
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

# A report that times out three times holds a pass for about 35s. That is
# already in the log, so the stacks wait for something longer.
DUMP_AFTER_SECONDS = 60.0
# Far above any real pass. Uploads run on printer workers, not on the loop.
EXIT_AFTER_SECONDS = 600.0
BACKSTOP_SECONDS = 120
_POLL_SECONDS = 5.0
# Not 75: the updater exits with that when it hands off to a new build.
EXIT_STALLED = 70


def _write_stderr(text: str) -> None:
    """Write without the logging lock; a stuck thread may be holding it."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    line = f"{stamp} CRITICAL {__name__}: {text}\n"
    try:
        os.write(2, line.encode("utf-8", "replace"))
    except OSError:
        pass


def format_stacks(skip_ident=None) -> str:
    """Every thread's stack, most recent call last."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks = []
    for ident, frame in sys._current_frames().items():
        if ident == skip_ident:
            continue
        stack = "".join(traceback.format_stack(frame))
        blocks.append(f"Thread {names.get(ident, 'unknown')} ({ident}):\n{stack}")
    return "\n".join(blocks)


class ReportLoopWatchdog:
    def __init__(self, *, dump_after=DUMP_AFTER_SECONDS, exit_after=EXIT_AFTER_SECONDS,
                 poll=_POLL_SECONDS, backstop_seconds=BACKSTOP_SECONDS,
                 monotonic=time.monotonic, exit=os._exit, write=_write_stderr,
                 alarm=getattr(signal, "alarm", None)):
        self._dump_after = float(dump_after)
        self._exit_after = float(exit_after)
        self._poll = float(poll)
        self._backstop = int(backstop_seconds)
        self._monotonic = monotonic
        self._exit = exit
        self._write = write
        self._alarm = alarm
        self._beats = 0
        self._seen_beats = 0
        self._stalled = 0.0
        self._dumped = False
        self._checked_at = monotonic()
        self._thread = None

    def beat(self) -> None:
        """The report loop is starting a pass."""
        self._beats += 1

    def check(self) -> None:
        """One look at the loop. The thread calls this; tests call it directly.

        Only time this thread saw pass is counted. A machine that slept, or a
        process that was paused, does not come back looking stalled.
        """
        now = self._monotonic()
        step = min(max(0.0, now - self._checked_at), self._poll * 2)
        self._checked_at = now
        beats = self._beats
        if beats != self._seen_beats:
            self._seen_beats = beats
            self._stalled = 0.0
            self._dumped = False
            return
        self._stalled += step
        if self._stalled >= self._exit_after:
            self._exit_stalled()
        elif self._stalled >= self._dump_after and not self._dumped:
            self._dumped = True
            logger.warning(
                "report loop: no new pass for %.0fs. Link exits and is restarted "
                "if this reaches %.0fs. Thread stacks:\n%s",
                self._stalled, self._exit_after,
                format_stacks(skip_ident=threading.get_ident()),
            )

    def _exit_stalled(self) -> None:
        self._write(
            f"report loop has not started a pass for {self._stalled:.0f}s "
            f"(limit {self._exit_after:.0f}s). PrintForce Link is exiting with "
            f"status {EXIT_STALLED} so the service manager starts a fresh one."
        )
        try:
            self._write("thread stacks at exit:\n"
                        + format_stacks(skip_ident=threading.get_ident()))
        except Exception:
            pass
        self._exit(EXIT_STALLED)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._checked_at = self._monotonic()
        self._thread = threading.Thread(
            target=self._run, name="link-loop-watchdog", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            if self._alarm is not None and self._backstop > 0:
                try:
                    self._alarm(self._backstop)
                except (OSError, ValueError):
                    self._alarm = None
            try:
                self.check()
            except Exception:
                logger.exception("report loop watchdog check failed; continuing")
            time.sleep(self._poll)
