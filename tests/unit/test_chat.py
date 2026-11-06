"""Chat-first Syntera: request parsing, plant documents, the agent loop and its guards."""

from __future__ import annotations

import json

import pytest

from syntera.chat.models import BasicChatModel, ModelReply, ScriptedChatModel, parse_plant_request
from syntera.chat.session import UNCHECKED_NOTE, ChatSession, run_turn
from syntera.chat.tools import (
    ChatToolError,
    ChatWorkspace,
    DesignPlant,
    chat_tool_schemas,
    file_kind,
    plant_document,
    safe_filename,
)


def test_simple_sentences_become_equipment_and_pipe_runs():
    request = parse_plant_request(
        "Pump water from tank T-1 to tank T-2 with pump P-1, DN50. It has to fit in a "
        "12 m x 10 m room."
    )
    assert request["equipment"] == [
        {"tag": "T-1", "type": "tank"}, {"tag": "T-2", "type": "tank"},
        {"tag": "P-1", "type": "pump"},
    ]  # fmt: skip
    assert request["lines"] == [
        {"from_tag": "T-1", "to_tag": "P-1", "dn": 50},
        {"from_tag": "P-1", "to_tag": "T-2", "dn": 50},
    ]
    assert request["room"] == {"length_m": 12.0, "width_m": 10.0}
    feed = parse_plant_request("pipe from outside to tank T-5; from T-5 to vessel V-1 DN65")
    assert feed["lines"][0]["from_tag"] == "outside"
    assert parse_plant_request("hello, what can you do?") is None


def test_plant_document_gives_each_line_end_its_own_nozzle_and_keeps_sizes():
    spec = DesignPlant.model_validate({
        "equipment": [{"tag": "T-1", "type": "tank", "diameter_mm": 2000, "height_mm": 3000},
                      {"tag": "P-1", "type": "pump"}],
        "lines": [{"from_tag": "outside", "to_tag": "T-1", "dn": 80},
                  {"from_tag": "T-1", "to_tag": "P-1"}, {"from_tag": "P-1", "to_tag": "outside"}],
    })  # fmt: skip
    document = plant_document(spec)
    tank = next(e for e in document.equipment if e.tag == "T-1")
    assert [n.id for n in tank.nozzles] == ["T-1-N1", "T-1-N2"]
    assert tank.attributes == {"OutsideDiameter": 2000, "Height": 3000, "CylinderLength": 3000}
    kinds = [(c.start.kind, c.end.kind) for c in document.connections]
    assert kinds == [("battery_limit", "nozzle"), ("nozzle", "nozzle"), ("nozzle", "battery_limit")]
    assert document.connections[0].start.inbound and not document.connections[2].end.inbound
    assert document.source_sha256 == plant_document(spec).source_sha256  # deterministic


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"lines": [{"from_tag": "T-1", "to_tag": "X-9"}]}, "not equipment"),
        ({"lines": [{"from_tag": "T-1", "to_tag": "T-1"}]}, "starts and ends"),
        ({"lines": [{"from_tag": "outside", "to_tag": "outside"}]}, "at least one"),
        ({"equipment": [{"tag": "T-1", "type": "tank"}, {"tag": "t-1", "type": "pump"}]}, "twice"),
    ],
)
def test_plant_document_rejects_requests_it_cannot_build(change, message):
    spec = {"equipment": [{"tag": "T-1", "type": "tank"}],
            "lines": [{"from_tag": "T-1", "to_tag": "outside"}], **change}  # fmt: skip
    with pytest.raises(ChatToolError, match=message):
        plant_document(DesignPlant.model_validate(spec))


def test_uploads_are_content_addressed_and_typed(tmp_path):
    ws = ChatWorkspace(tmp_path)
    stored = ws.store_file(b"0\nSECTION\n0\nEOF\n", "../../etc/plan (rev A).DXF")
    assert stored.kind == "drawing" and stored.path.parent.parent == ws.files_root
    assert stored.filename == "plan _rev A_.DXF" == safe_filename("../../etc/plan (rev A).DXF")
    assert ws.file(stored.id).path == stored.path
    with pytest.raises(ChatToolError):
        file_kind("notes.pdf")
    with pytest.raises(ChatToolError):
        ws.file("../secret")


def test_tool_schemas_never_take_a_verdict_as_input():
    from syntera.assistant.tools import VERDICT_WORDS, schema_field_names

    for schema in chat_tool_schemas():
        assert not schema_field_names(schema["input_schema"]) & VERDICT_WORDS, schema["name"]


def test_tool_errors_go_back_to_the_model_and_unchecked_verdict_talk_is_flagged(tmp_path):
    model = ScriptedChatModel([
        ModelReply([{"type": "tool_use", "id": "t1", "name": "check_drawing",
                     "input": {"file_id": "f0000000000000000"}}], "tool_use"),
        ModelReply([{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "Your design passes."}]),
    ])  # fmt: skip
    session = ChatSession(id="c" + "0" * 20)
    reply = run_turn(session, model, ChatWorkspace(tmp_path), "check it", [])
    result = model.requests[1][-1]["content"][0]
    assert result["is_error"] and "no uploaded file" in result["content"]
    assert reply["text"].endswith(UNCHECKED_NOTE)
    # assistant content (thinking blocks included) is replayed unchanged
    assert session.messages[-1]["content"][0] == {"type": "thinking", "thinking": "",
                                                   "signature": "sig"}  # fmt: skip
    activity = [e for e in session.events if e["type"] == "activity"][0]
    assert activity["status"] == "failed"


def test_basic_mode_explains_itself_when_it_cannot_parse(tmp_path):
    session = ChatSession(id="c" + "1" * 20)
    reply = run_turn(session, BasicChatModel(), ChatWorkspace(tmp_path), "hello", [])
    assert "Claude API key" in reply["text"] and reply["views"] == []


def test_basic_mode_routes_an_upload_to_the_matching_tool():
    model = BasicChatModel()
    note = "[attached file f0123456789abcdef: model.step (step)]"
    reply = model.complete("", [{"role": "user", "content": f"check\n{note}"}], [])
    assert reply.content[0]["name"] == "audit_step"
    assert reply.content[0]["input"] == {"file_id": "f0123456789abcdef"}
    error = {"type": "tool_result", "tool_use_id": "x", "content": "boom", "is_error": True}
    answer = model.complete("", [{"role": "user", "content": [error]}], [])
    assert answer.content[0]["text"] == "I could not do that: boom"
    summary = {"plain_summary": "All 2 pipe runs were routed.", "design_audit": {"problems": []}}
    done = {"type": "tool_result", "tool_use_id": "x", "content": json.dumps(summary)}
    text = model.complete("", [{"role": "user", "content": [done]}], []).content[0]["text"]
    assert "All 2 pipe runs" in text
