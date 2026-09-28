"""The checked-in contract examples are exactly what Link produces.

3D PrintForce tests its ingest against a copy of
tests/fixtures/contract/examples.json. If this test fails, Link changed the
wire: update docs/references/link-state-contract-v2.md, regenerate the JSON
(`python -m tests.contract_examples > tests/fixtures/contract/examples.json`),
and copy it to 3D-PrintForce backend/tests/fixtures/link_contract/.
"""
import json
import os

from tests.contract_examples import examples

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "contract", "examples.json")

_V2_KEYS = {
    "contract", "state_seq", "connection", "connection_reason", "activity", "stuck_job",
    "stage", "pause_reason", "job", "errors", "commands_rejected", "model",
    "capabilities", "raw",
}
_EVENT_KEYS = {
    "id", "seq", "bambu_id", "type", "origin", "submission_id", "batch_id", "plate",
    "gcode_file", "subtask_name", "at", "observed", "progress", "stage",
}


def test_checked_in_examples_match_link():
    with open(_FIXTURE) as handle:
        assert json.load(handle) == json.loads(json.dumps(examples()))


def test_every_example_has_the_documented_fields():
    for name, example in examples().items():
        assert set(example["v2"]) == _V2_KEYS, name
        for event in example["events"]:
            assert _EVENT_KEYS <= set(event), name


def test_v2_never_uses_farm_words():
    for name, example in examples().items():
        dumped = json.dumps(example["v2"])
        assert "NEEDS_CLEARING" not in dumped, name
        assert example["v2"]["activity"] in (
            "idle", "preparing", "printing", "paused", "ended", "unknown"), name
