"""Link's side of the command doorbell (plan U12, KTD10 / KTD11).

One daemon thread keeps ``GET /api/bridge/commands/wait`` open. A click on
the website returns that wait in about a second, and the command is
published right away instead of on the next 15 s state post.

Each command:
- has a relative deadline (``expires_in_ms``). Link computes its own and never
  runs a command after it: ``failed: expired_on_link``.
- is published at most once, ever. ``run_command`` is ``app._apply_control``,
  which keeps the published ids on disk and shares them with the older
  desired-state ``control`` path.
- is acked ``published`` once the printer has it. A command that could not be
  published stays on the cloud and comes back on the next wait; it is retried
  no faster than ``retry_seconds``.

Hints: ``send`` means a send was just authorized. ``on_hint`` wakes the
report loop so the send arrives now.

A 3DPF without the mailbox answers 404. The channel then sleeps and tries
again later; controls keep arriving through desired state meanwhile.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

ACK_PUBLISHED = "published"
ACK_APPLIED = "applied"
ACK_REJECTED = "rejected"
ACK_FAILED = "failed"

# run_command outcomes (see app._apply_control).
PUBLISHED = "published"
ALREADY = "already"
UNKNOWN_PRINTER = "unknown_printer"
UNKNOWN_ACTION = "unknown_action"
# Published, but the printer is refusing every command (HMS 0500_0500_0001_0007:
# LAN-only without Developer Mode). Settles as rejected: developer_mode_off.
REJECTED_DEVELOPER_MODE = "rejected_developer_mode_off"


class CommandChannel:
    def __init__(self, dpf, run_command: Callable[[Dict], Optional[str]], *,
                 on_hint: Optional[Callable[[list], None]] = None,
                 wait_seconds: float = 25.0, retry_seconds: float = 3.0,
                 error_backoff: float = 5.0, unsupported_backoff: float = 300.0,
                 monotonic=time.monotonic, sleep=time.sleep):
        self._dpf = dpf
        self._run = run_command
        self._on_hint = on_hint
        self._wait = float(wait_seconds)
        self._retry = float(retry_seconds)
        self._error_backoff = float(error_backoff)
        self._unsupported_backoff = float(unsupported_backoff)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_try: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None
        # Printer replies arrive on paho threads; their acks are posted here.
        self._reply_acks: "queue.Queue" = queue.Queue()
        self._reply_thread: Optional[threading.Thread] = None

    # --- one round --------------------------------------------------------

    def run_once(self) -> str:
        """One wait and its commands.

        Returns ``unsupported`` (404), ``error``, ``idle`` (nothing to do),
        ``done`` (every command acked), or ``pending`` (a command is waiting
        for a retry).
        """
        body = self._dpf.wait_commands(self._wait)
        if body is None:
            return "unsupported"
        if not isinstance(body, dict) or not body:
            return "error"
        received = self._monotonic()
        hints = [h for h in body.get("hints") or [] if isinstance(h, str)]
        if hints and self._on_hint is not None:
            try:
                self._on_hint(hints)
            except Exception:
                logger.exception("command channel: hint handler failed")
        commands = [c for c in body.get("commands") or [] if isinstance(c, dict)]
        if not commands:
            return "idle"
        pending = False
        for command in commands:
            if not self._handle(command, received):
                pending = True
        self._forget_old(received)
        return "pending" if pending else "done"

    def _handle(self, command: Dict, received: float) -> bool:
        """True when the command is settled on Link's side (acked)."""
        command_id = command.get("id")
        if not isinstance(command_id, str) or not command_id:
            return True
        try:
            left_ms = float(command.get("expires_in_ms"))
        except (TypeError, ValueError):
            left_ms = 0.0
        now = self._monotonic()
        if now > received + left_ms / 1000.0:
            self._ack(command_id, ACK_FAILED, "expired_on_link")
            return True
        last = self._last_try.get(command_id)
        if last is not None and now - last < self._retry:
            return False
        self._last_try[command_id] = now
        try:
            outcome = self._run(command)
        except Exception:
            logger.exception("command %s (%s) raised", command_id, command.get("action"))
            outcome = None
        if outcome in (PUBLISHED, ALREADY):
            self._ack(command_id, ACK_PUBLISHED)
            self._last_try.pop(command_id, None)
            return True
        if outcome == REJECTED_DEVELOPER_MODE:
            self._ack(command_id, ACK_REJECTED, "developer_mode_off")
            self._last_try.pop(command_id, None)
            return True
        if outcome in (UNKNOWN_PRINTER, UNKNOWN_ACTION):
            self._ack(command_id, ACK_FAILED, outcome)
            self._last_try.pop(command_id, None)
            return True
        # Not published (printer offline, publish refused): the cloud hands it
        # back on the next wait until it expires.
        return False

    def _ack(self, command_id: str, state: str, reason: Optional[str] = None) -> None:
        try:
            self._dpf.ack_command(command_id, state, reason=reason)
        except Exception:
            logger.exception("command %s: ack %s failed; the next wait retries it",
                             command_id, state)

    def report_reply(self, command_id: str, state: str, reason: Optional[str] = None,
                     body: Optional[Dict] = None) -> None:
        """The printer answered a command (plan U13). Safe from the paho thread."""
        if state not in (ACK_APPLIED, ACK_REJECTED):
            return
        self._reply_acks.put((str(command_id), state, reason, body))

    def drain_replies(self, block: bool = False, timeout: Optional[float] = None) -> int:
        """Post queued reply acks. Returns how many were posted."""
        posted = 0
        while True:
            try:
                item = self._reply_acks.get(block=block and posted == 0, timeout=timeout)
            except queue.Empty:
                return posted
            command_id, state, reason, body = item
            reply = None
            if isinstance(body, dict):
                reply = {k: body.get(k) for k in ("command", "result", "reason", "sequence_id")
                         if k in body}
            try:
                self._dpf.ack_command(command_id, state, reason=reason, reply=reply)
            except Exception:
                logger.exception("command %s: reply ack failed", command_id)
            posted += 1

    def _forget_old(self, now: float) -> None:
        stale = [cid for cid, at in self._last_try.items() if now - at > 600]
        for cid in stale:
            self._last_try.pop(cid, None)

    # --- the thread -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="link-commands", daemon=True)
        self._thread.start()
        self._reply_thread = threading.Thread(
            target=self._reply_loop, name="link-command-replies", daemon=True,
        )
        self._reply_thread.start()

    def _reply_loop(self) -> None:
        while True:
            try:
                self.drain_replies(block=True, timeout=30.0)
            except Exception:
                logger.exception("command reply acks failed")
                self._sleep(self._error_backoff)

    def _loop(self) -> None:
        told_unsupported = False
        while True:
            try:
                outcome = self.run_once()
            except Exception:
                logger.exception("command channel round failed")
                outcome = "error"
            if outcome == "unsupported":
                if not told_unsupported:
                    logger.info("3DPF has no command mailbox yet; commands arrive "
                                "with state reports")
                    told_unsupported = True
                self._sleep(self._unsupported_backoff)
            elif outcome == "error":
                self._sleep(self._error_backoff)
            elif outcome == "pending":
                # A command is waiting for its retry; do not spin on the wait.
                self._sleep(self._retry)
            else:
                told_unsupported = False
