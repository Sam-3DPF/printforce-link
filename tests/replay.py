"""Feed a collected printer log through the current state owner.

The file shape is ``PrinterLog.export()``. ``source`` and ``note`` are
optional labels and are ignored here. ``replay_into_state`` applies inbound
messages with ``PrinterState.ingest``. ``replay_into_printer`` still drives
``BambuPrinter`` and returns ``snapshot()``.
"""

import json


def load_fixture(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if not isinstance(doc, dict):
        raise ValueError("replay fixture must be a JSON object")
    return doc


def replay_into_state(state, fixture, now=None) -> list:
    """Apply inbound messages through ``PrinterState.ingest``.

    Returns the lifecycle events those messages left queued. Outbound
    messages are commands, not state. ``now`` stamps each message: a callable
    is invoked per message, a number is used as-is, and the default lets the
    state use its own clock.
    """
    for message in fixture.get("messages") or []:
        if not isinstance(message, dict) or message.get("direction") != "in":
            continue
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        if callable(now):
            stamp = now()
        else:
            stamp = now
        if stamp is None:
            state.ingest(payload)
        else:
            state.ingest(payload, stamp)
    return state.pending_events()


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
