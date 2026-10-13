"""The report loop's self-check: stacks for a slow pass, an exit for a stopped loop.

The clock is injected and each test calls ``check`` itself. The last two
tests run a real child process, because the point is what the process does.
"""
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading

import pytest

import bridge.app as app_module
from bridge.loop_watchdog import EXIT_STALLED, ReportLoopWatchdog, format_stacks

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Exited(Exception):
    pass


def _watchdog(clock, **kwargs):
    written = []
    exits = []

    def _exit(code):
        exits.append(code)
        raise Exited()

    watchdog = ReportLoopWatchdog(
        dump_after=60, exit_after=600, poll=5, monotonic=clock,
        exit=_exit, write=written.append, alarm=None, **kwargs,
    )
    return watchdog, written, exits


@pytest.fixture
def parked_thread():
    """A thread that is waiting, standing in for the stuck report loop.

    ``check`` leaves its own thread out of the stacks, and here that is the
    test's main thread.
    """
    release = threading.Event()
    thread = threading.Thread(target=release.wait, args=(30,), name="stuck-loop", daemon=True)
    thread.start()
    yield thread
    release.set()
    thread.join(5)


def _tick(watchdog, clock, seconds, step=5.0):
    for _ in range(int(seconds / step)):
        clock.now += step
        watchdog.check()


def test_a_loop_that_keeps_starting_passes_is_left_alone(caplog):
    clock = Clock()
    watchdog, written, exits = _watchdog(clock)
    with caplog.at_level(logging.WARNING, logger="bridge.loop_watchdog"):
        for _ in range(400):
            watchdog.beat()
            _tick(watchdog, clock, 15)
    assert exits == []
    assert written == []
    assert caplog.records == []


def test_a_pass_that_ends_in_a_failed_report_still_counts():
    """About 45 reports an hour failed on 2026-10-12. Each pass still ended,
    and a restart would not have fixed the cloud."""
    clock = Clock()
    watchdog, _written, exits = _watchdog(clock)
    for _ in range(200):
        watchdog.beat()
        _tick(watchdog, clock, 35)   # three read timeouts and their backoff
        _tick(watchdog, clock, 15)   # the wait before the next pass
    assert exits == []


def test_a_slow_pass_logs_every_stack_once(caplog, parked_thread):
    clock = Clock()
    watchdog, _written, exits = _watchdog(clock)
    watchdog.beat()
    with caplog.at_level(logging.WARNING, logger="bridge.loop_watchdog"):
        _tick(watchdog, clock, 55)
        assert caplog.records == []
        _tick(watchdog, clock, 200)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "no new pass for 60s" in message
    assert "Thread stuck-loop" in message
    assert exits == []


def test_the_next_slow_pass_logs_again(caplog):
    clock = Clock()
    watchdog, _written, _exits = _watchdog(clock)
    with caplog.at_level(logging.WARNING, logger="bridge.loop_watchdog"):
        watchdog.beat()
        _tick(watchdog, clock, 90)
        watchdog.beat()
        _tick(watchdog, clock, 90)
    assert len(caplog.records) == 2


def test_a_loop_that_stops_exits_nonzero_and_says_why(parked_thread):
    clock = Clock()
    watchdog, written, exits = _watchdog(clock)
    watchdog.beat()
    _tick(watchdog, clock, 595)
    assert exits == []
    with pytest.raises(Exited):
        _tick(watchdog, clock, 10)
    assert exits == [EXIT_STALLED]
    assert EXIT_STALLED != 0
    assert "report loop has not started a pass for 600s" in written[0]
    assert "exiting with status 70" in written[0]
    assert "Thread stuck-loop" in written[1]


def test_the_exit_message_does_not_wait_on_the_logging_lock():
    """A thread that died holding the handler lock must not block the exit."""
    clock = Clock()
    watchdog, written, exits = _watchdog(clock)
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    held = threading.Event()
    release = threading.Event()

    def _hold():
        handler.acquire()
        held.set()
        release.wait(10)
        handler.release()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    held.wait(5)
    result = []

    def _run():
        # The stack log before the exit would block here, so start past it.
        watchdog._dumped = True
        watchdog._stalled = 595.0
        watchdog._seen_beats = watchdog._beats
        try:
            _tick(watchdog, clock, 10)
        except Exited:
            result.append("exited")

    runner = threading.Thread(target=_run, daemon=True)
    try:
        runner.start()
        runner.join(5)
        assert result == ["exited"]
        assert exits == [EXIT_STALLED]
    finally:
        release.set()
        holder.join(5)
        root.removeHandler(handler)


def test_time_the_watchdog_did_not_see_is_not_counted():
    """A Mac that slept for an hour wakes with the loop mid-wait, not stalled."""
    clock = Clock()
    watchdog, _written, exits = _watchdog(clock)
    watchdog.beat()
    _tick(watchdog, clock, 30)
    clock.now += 3600
    watchdog.check()
    watchdog.beat()
    _tick(watchdog, clock, 30)
    assert exits == []


def test_each_check_pushes_the_backstop_alarm_out(monkeypatch):
    armed = []
    stop = threading.Event()

    class Stop(BaseException):
        pass

    def _sleep(_seconds):
        if len(armed) >= 3:
            stop.set()
            raise Stop()

    monkeypatch.setattr("bridge.loop_watchdog.time.sleep", _sleep)
    watchdog = ReportLoopWatchdog(alarm=armed.append, backstop_seconds=120, exit=lambda code: None)
    with pytest.raises(Stop):
        watchdog._run()
    assert armed == [120, 120, 120]


def test_stacks_name_each_thread_and_can_skip_the_caller():
    text = format_stacks()
    assert "Thread MainThread" in text
    assert "test_stacks_name_each_thread_and_can_skip_the_caller" in text
    assert "Thread MainThread" not in format_stacks(skip_ident=threading.get_ident())


def test_the_report_loop_does_not_use_the_faulthandler_timer():
    """Its dump thread reads frames without the interpreter lock, and the
    cancel waits for that thread while holding it. That was the hang."""
    assert not hasattr(app_module, "faulthandler")
    assert not hasattr(app_module, "arm_report_loop_dump")


def _child(body):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=_REPO, capture_output=True, text=True, timeout=30,
    )


def test_a_real_process_whose_loop_stops_exits_70_and_writes_the_reason():
    result = _child("""
        import time
        from bridge.loop_watchdog import ReportLoopWatchdog
        watchdog = ReportLoopWatchdog(dump_after=0.2, exit_after=0.6, poll=0.1, alarm=None)
        watchdog.start()
        watchdog.beat()
        time.sleep(20)   # the loop never comes back
    """)
    assert result.returncode == EXIT_STALLED
    assert "report loop has not started a pass" in result.stderr
    assert "exiting with status 70" in result.stderr


@pytest.mark.skipif(not hasattr(signal, "alarm"), reason="no SIGALRM on this platform")
def test_a_process_where_no_python_thread_can_run_is_ended_by_the_backstop():
    """One C call that never gives up the interpreter lock, as on 2026-10-12.
    The watchdog thread cannot run either; the alarm it last set ends the process."""
    result = _child("""
        import re, time
        from bridge.loop_watchdog import ReportLoopWatchdog
        watchdog = ReportLoopWatchdog(poll=0.1, backstop_seconds=1)
        watchdog.start()
        watchdog.beat()
        time.sleep(0.5)
        re.match(r"(a+)+$", "a" * 64 + "b")   # holds the lock for longer than the test
    """)
    assert result.returncode == -signal.SIGALRM
