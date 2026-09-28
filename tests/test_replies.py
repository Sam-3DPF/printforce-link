"""The printer's reply to a command becomes its receipt (plan U13)."""
from bridge.app import _run_mailbox_command
from bridge.bambu.replies import ReplyBook, watch
from bridge.command_channel import CommandChannel
from tests.test_telemetry import _printer


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, command_id, state, reason, body):
        self.calls.append((command_id, state, reason))


def _reply(command, sequence, result="success", reason="success"):
    return {"print": {"command": command, "sequence_id": sequence,
                      "result": result, "reason": reason}}


# --- the book ---------------------------------------------------------------

def test_outside_a_watch_nothing_changes():
    book = ReplyBook()
    payload = {"print": {"command": "pause", "sequence_id": "0"}}
    assert book.stamp(payload) is payload
    assert len(book) == 0


def test_a_watched_publish_gets_its_own_id_and_its_reply_settles_it():
    book, seen = ReplyBook(), _Recorder()
    with watch("cmd-1", seen):
        stamped = book.stamp({"print": {"command": "pause", "sequence_id": "0"}})
    sequence = stamped["print"]["sequence_id"]
    assert sequence != "0"
    book.resolve(_reply("pause", "999999"))          # someone else's reply
    book.resolve(_reply("pause", sequence))
    book.resolve(_reply("pause", sequence))          # a duplicate does nothing
    assert seen.calls == [("cmd-1", "applied", None)]


def test_a_failed_reply_is_a_rejection_with_the_printer_reason():
    book, seen = ReplyBook(), _Recorder()
    with watch("cmd-2", seen):
        stamped = book.stamp({"print": {"command": "resume", "sequence_id": "0"}})
    book.resolve(_reply("resume", stamped["print"]["sequence_id"], "fail", "not paused"))
    assert seen.calls == [("cmd-2", "rejected", "not paused")]


def test_a_reply_without_a_result_is_not_a_verdict():
    book, seen = ReplyBook(), _Recorder()
    with watch("cmd-3", seen):
        stamped = book.stamp({"print": {"command": "pause", "sequence_id": "0"}})
    book.resolve({"print": {"command": "pause",
                            "sequence_id": stamped["print"]["sequence_id"]}})
    assert seen.calls == []


def test_a_two_message_command_settles_once():
    book, seen = ReplyBook(), _Recorder()
    with watch("light", seen):
        first = book.stamp({"system": {"command": "ledctrl", "sequence_id": "0",
                                       "led_node": "chamber_light"}})
        second = book.stamp({"system": {"command": "ledctrl", "sequence_id": "0",
                                        "led_node": "chamber_light2"}})
    book.resolve({"system": {"command": "ledctrl", "result": "success",
                             "sequence_id": first["system"]["sequence_id"]}})
    book.resolve({"system": {"command": "ledctrl", "result": "success",
                             "sequence_id": second["system"]["sequence_id"]}})
    assert seen.calls == [("light", "applied", None)]
    assert len(book) == 0


def test_project_file_and_fixed_ids_are_left_alone():
    book = ReplyBook()
    with watch("cmd-4", _Recorder()):
        start = book.stamp({"print": {"command": "project_file", "sequence_id": "20000"}})
        probe = book.stamp({"info": {"command": "get_version", "sequence_id": "7"}})
    assert start["print"]["sequence_id"] == "20000"
    assert probe["info"]["sequence_id"] == "7"


def test_unanswered_commands_are_forgotten_after_a_minute():
    clock = [0.0]
    book = ReplyBook(monotonic=lambda: clock[0], ttl_seconds=60)
    with watch("old", _Recorder()):
        book.stamp({"print": {"command": "pause", "sequence_id": "0"}})
    clock[0] = 61
    with watch("new", _Recorder()):
        book.stamp({"print": {"command": "resume", "sequence_id": "0"}})
    assert len(book) == 1


# --- through the printer ------------------------------------------------------

def test_a_printer_reply_on_the_report_topic_reaches_on_reply():
    printer = _printer([])
    seen = _Recorder()
    with watch("pause-1", seen):
        assert printer.pause_print() is True
    sent = printer._session.published[-1]
    sequence = sent["print"]["sequence_id"]
    assert sequence != "0"
    printer._on_mqtt_report(_reply("pause", sequence))
    assert seen.calls == [("pause-1", "applied", None)]


# --- the receipt ---------------------------------------------------------------

class _Dpf:
    def __init__(self):
        self.acks = []

    def ack_command(self, command_id, state, reason=None, reply=None):
        self.acks.append((command_id, state, reason, reply))


def test_reply_acks_are_posted_off_the_mqtt_thread():
    dpf = _Dpf()
    channel = CommandChannel(dpf, lambda command: "published")
    channel.report_reply("c1", "rejected", "not paused",
                         {"command": "resume", "result": "fail", "reason": "not paused",
                          "sequence_id": "40001", "extra": "x"})
    channel.report_reply("c2", "published")           # not a reply verdict
    assert dpf.acks == []                              # queued, not posted inline
    assert channel.drain_replies() == 1
    assert dpf.acks == [("c1", "rejected", "not paused",
                         {"command": "resume", "result": "fail", "reason": "not paused",
                          "sequence_id": "40001"})]


class _Refusing:
    commands_rejected = True

    def pause_print(self):
        return True


class _Fleet:
    def __init__(self, printer):
        self.printer = printer

    def by_id(self, bambu_id):
        return self.printer

    def apply_control(self, bambu_id, action, params):
        return getattr(self.printer, f"{action}_print")()


def test_a_printer_refusing_commands_settles_as_developer_mode_off(tmp_path):
    """Plan AE8."""
    command = {"id": "p1", "bambu_id": "S1", "action": "pause", "params": {},
               "expires_in_ms": 90_000}
    outcome = _run_mailbox_command(_Fleet(_Refusing()), None, command, set(),
                                   str(tmp_path), None, on_reply=_Recorder())
    assert outcome == "rejected_developer_mode_off"

    class _WaitDpf(_Dpf):
        def wait_commands(self, timeout):
            return {"commands": [command], "hints": []}

    dpf = _WaitDpf()
    CommandChannel(dpf, lambda c: outcome).run_once()
    assert dpf.acks == [("p1", "rejected", "developer_mode_off", None)]
