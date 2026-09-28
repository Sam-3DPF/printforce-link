"""The command doorbell (plan U12): wait, publish once, ack."""
import bridge.dpf_client as dpf_mod
from bridge.app import _handle_desired, _run_mailbox_command
from bridge.command_channel import CommandChannel
from bridge.dpf_client import DpfClient


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class _Dpf:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.acks = []

    def wait_commands(self, timeout):
        return self.bodies.pop(0) if self.bodies else {"commands": [], "hints": []}

    def ack_command(self, command_id, state, reason=None, reply=None):
        self.acks.append((command_id, state, reason))
        return {"state": state}


def _cmd(cid="c1", action="pause", ms=90_000, **params):
    return {"id": cid, "bambu_id": "P1", "action": action, "params": params,
            "expires_in_ms": ms}


def _channel(dpf, outcomes, clock=None, **kwargs):
    runs = []

    def run(command):
        runs.append(command["id"])
        return outcomes.pop(0) if outcomes else "published"

    channel = CommandChannel(dpf, run, monotonic=clock or _Clock(), sleep=lambda s: None,
                             **kwargs)
    return channel, runs


def test_a_command_is_published_and_acked():
    dpf = _Dpf({"commands": [_cmd()], "hints": []})
    channel, runs = _channel(dpf, ["published"])
    assert channel.run_once() == "done"
    assert runs == ["c1"]
    assert dpf.acks == [("c1", "published", None)]


def test_one_already_published_through_desired_state_is_acked_not_rerun():
    dpf = _Dpf({"commands": [_cmd()], "hints": []})
    channel, _runs = _channel(dpf, ["already"])
    channel.run_once()
    assert dpf.acks == [("c1", "published", None)]


def test_a_command_past_its_deadline_is_refused_on_link():
    clock = _Clock()

    # c2 is published first and takes a second; c1 had half a second left.
    dpf = _Dpf({"commands": [_cmd("c2", ms=90_000), _cmd("c1", ms=500)], "hints": []})
    runs = []

    def run(command):
        clock.t += 1.0          # the first publish took a second
        runs.append(command["id"])
        return "published"

    channel = CommandChannel(dpf, run, monotonic=clock, sleep=lambda s: None)
    channel.run_once()
    assert runs == ["c2"]
    assert ("c1", "failed", "expired_on_link") in dpf.acks


def test_an_unpublished_command_waits_for_its_retry():
    clock = _Clock()
    body = {"commands": [_cmd()], "hints": []}
    dpf = _Dpf(body, body, body)
    channel, runs = _channel(dpf, [None, "published"], clock=clock, retry_seconds=3)
    assert channel.run_once() == "pending"
    clock.t += 1
    assert channel.run_once() == "pending"      # too soon: not run again
    assert runs == ["c1"]
    clock.t += 3
    assert channel.run_once() == "done"
    assert runs == ["c1", "c1"]
    assert dpf.acks == [("c1", "published", None)]


def test_an_unknown_printer_fails_the_command():
    dpf = _Dpf({"commands": [_cmd()], "hints": []})
    channel, _ = _channel(dpf, ["unknown_printer"])
    channel.run_once()
    assert dpf.acks == [("c1", "failed", "unknown_printer")]


def test_a_send_hint_wakes_the_report_loop():
    hints = []
    dpf = _Dpf({"commands": [], "hints": ["send"]})
    channel, _ = _channel(dpf, [], on_hint=hints.append)
    assert channel.run_once() == "idle"
    assert hints == [["send"]]


def test_an_older_3dpf_is_unsupported_and_an_error_is_an_error():
    class _Old(_Dpf):
        def wait_commands(self, timeout):
            return None

    assert _channel(_Old(), [])[0].run_once() == "unsupported"
    assert _channel(_Dpf({}), [])[0].run_once() == "error"


# --- once, across paths and restarts -----------------------------------------

class _Printer:
    def __init__(self):
        self.calls = []

    def pause_print(self):
        self.calls.append("pause")
        return True

    def stop_print(self):
        self.calls.append("stop")
        return True


class _Fleet:
    def __init__(self, printer):
        self.printer = printer

    def by_id(self, bambu_id):
        return self.printer if bambu_id == "P1" else None

    def apply_control(self, bambu_id, action, params):
        return getattr(self.printer, f"{action}_print")()


def test_mailbox_then_desired_state_publish_the_id_once(tmp_path):
    printer = _Printer()
    fleet = _Fleet(printer)
    applied = set()
    command = _cmd("same-id", "pause")
    assert _run_mailbox_command(fleet, None, command, applied, str(tmp_path), None) == "published"
    _handle_desired([{"bambu_id": "P1", "control": {"id": "same-id", "action": "pause"}}],
                    fleet, applied, str(tmp_path))
    assert printer.calls == ["pause"]


def test_a_restart_after_publishing_stop_does_not_publish_it_again(tmp_path):
    printer = _Printer()
    fleet = _Fleet(printer)
    command = _cmd("stop-1", "stop")
    assert _run_mailbox_command(fleet, None, command, set(), str(tmp_path), None) == "published"
    # Link restarts: a new in-memory set, the same spool directory on disk.
    assert _run_mailbox_command(fleet, None, command, set(), str(tmp_path), None) == "already"
    assert printer.calls == ["stop"]


def test_unknown_actions_and_printers_are_named(tmp_path):
    fleet = _Fleet(_Printer())
    assert _run_mailbox_command(fleet, None, _cmd(action="self_destruct"), set(),
                                str(tmp_path), None) == "unknown_action"
    other = dict(_cmd(), bambu_id="P9")
    assert _run_mailbox_command(fleet, None, other, set(), str(tmp_path), None) == "unknown_printer"


# --- the cloud client ---------------------------------------------------------

class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def test_wait_commands_reads_the_body_and_a_404_means_no_mailbox(monkeypatch):
    seen = {}

    def fake_get(self, url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, timeout=timeout)
        return responses.pop(0)

    responses = [_Resp(200, {"data": {"commands": [], "hints": ["send"]}}), _Resp(404)]
    monkeypatch.setattr(dpf_mod.httpx.Client, "get", fake_get)
    client = DpfClient("https://x", "tok")
    assert client.wait_commands(25) == {"commands": [], "hints": ["send"]}
    assert seen["url"] == "https://x/api/bridge/commands/wait"
    assert seen["params"] == {"timeout": 25}
    assert seen["timeout"] > 25          # the HTTP timeout outlasts the wait
    assert client.wait_commands(25) is None


def test_ack_command_posts_the_state(monkeypatch):
    posts = []

    class _OkResp(_Resp):
        def raise_for_status(self):
            pass

    monkeypatch.setattr(dpf_mod.httpx.Client, "post",
                        lambda self, url, json=None, headers=None: (
                            posts.append((url, json)) or _OkResp(200, {"data": {"ok": True}})))
    client = DpfClient("https://x", "tok")
    client.ack_command("c1", "failed", reason="expired_on_link")
    assert posts == [("https://x/api/bridge/commands/c1/ack",
                      {"state": "failed", "reason": "expired_on_link"})]
