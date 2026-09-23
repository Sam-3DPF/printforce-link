"""Feed a collected printer log through the current state owner.

The file shape is ``PrinterLog.export()``. ``source`` and ``note`` are
optional labels and are ignored here. U4 can point ``replay_into_printer``
at ``state.py`` without rewriting fixtures.
"""

import json


def load_fixture(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if not isinstance(doc, dict):
        raise ValueError("replay fixture must be a JSON object")
    return doc


def replay_into_printer(printer, fixture) -> dict:
    """Apply inbound messages in order, then return ``snapshot()``.

    Outbound messages are commands, not state. The printer must already
    look connected: ``snapshot`` reports OFFLINE while the session is down.
    """
    for message in fixture.get("messages") or []:
        if not isinstance(message, dict) or message.get("direction") != "in":
            continue
        payload = message.get("payload")
        if isinstance(payload, dict):
            printer._on_mqtt_report(payload)
    return printer.snapshot()
