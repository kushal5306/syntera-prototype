"""The chat front door end to end: pages, uploads, background turns, results and 3D views."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.chat.models import BasicChatModel, ClaudeChatModel, ModelReply, ScriptedChatModel
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
CONFIG = PROJECT / "examples" / "demo_skid.yaml"


def inline(job):
    job()


def _client(tmp_path, model=None):
    factory = (lambda: model) if model is not None else BasicChatModel
    return TestClient(create_app(CONFIG, tmp_path / "web", chat_model=factory, chat_runner=inline))


def _turn(client, session, text, file_ids=()):
    before = len(client.get(f"/api/chat/sessions/{session}").json()["events"])
    sent = client.post(f"/api/chat/sessions/{session}/messages",
                       json={"text": text, "file_ids": list(file_ids)})  # fmt: skip
    assert sent.status_code == 202, sent.text
    snap = client.get(f"/api/chat/sessions/{session}", params={"since": before}).json()
    assert snap["busy"] is False
    return snap["events"]


def test_chat_is_the_front_page_and_expert_pages_stay_reachable(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app(CONFIG, tmp_path / "web"))
    home = client.get("/").text
    assert 'src="/assets/chat.js"' in home and "Expert view" in home
    for page in ("/expert", "/drawings", "/design", "/optimise"):
        assert client.get(page).status_code == 200
    assert 'href="/"' in client.get("/drawings").text
    status = client.get("/api/chat/status").json()
    assert status["model"] == "basic" and status["ai"] is False
    source = client.get("/assets/chat.js").text
    for forbidden in ("eval(", "innerHTML", "new Function", "overall_pass"):
        assert forbidden not in source


def test_a_plain_sentence_designs_routes_and_checks_a_plant(tmp_path):
    client = _client(tmp_path)
    session = client.post("/api/chat/sessions").json()["id"]
    events = _turn(client, session, "Pump water from tank T-1 to tank T-2 with pump P-1, DN50. "
                   "It has to fit in a 12 m x 10 m room.")  # fmt: skip
    kinds = [event["type"] for event in events]
    assert kinds == ["user", "activity", "assistant"]
    activity, reply = events[1], events[2]
    assert activity["tool"] == "design_plant" and activity["status"] == "done"
    assert "All 2 pipe runs were routed and every check passed" in reply["text"]
    view = reply["views"][0]
    assert view["card"]["verdict"] == "pass"
    assert {s["label"] for s in view["card"]["stats"]} >= {"Pipe length", "Bends", "Fits room"}
    meshes = client.get(f"/api/chat/views/{view['id']}").json()["meshes"]
    assert {m["kind"] for m in meshes} == {"obstacle", "route"}
    step = next(link["url"] for link in view["card"]["links"] if link["url"].endswith(".step"))
    assert client.get(step).status_code == 200
    assert client.get("/api/chat/views/v0000000000000000").status_code == 404


def test_attached_files_are_checked_with_the_matching_tool(tmp_path, plan_layout_dxf, dexpi_xml):
    client = _client(tmp_path)
    session = client.post("/api/chat/sessions").json()["id"]
    upload = client.post("/api/chat/files", params={"filename": plan_layout_dxf.name},
                         content=plan_layout_dxf.read_bytes())  # fmt: skip
    assert upload.status_code == 200 and upload.json()["kind"] == "drawing"
    events = _turn(client, session, "check this", [upload.json()["id"]])
    assert events[1]["tool"] == "check_drawing" and events[1]["status"] == "done"
    card = events[-1]["views"][0]["card"]
    assert card["verdict"] in {"pass", "fail"} and card["links"][0]["url"].startswith("/drawings#")
    # the same drawing again reuses the stored check instead of rerunning it
    again = _turn(client, session, "check it again", [upload.json()["id"]])
    assert again[1]["status"] == "done"
    assert again[-1]["views"][0]["id"] == events[-1]["views"][0]["id"]

    pid = client.post("/api/chat/files", params={"filename": "plant.xml"},
                      content=dexpi_xml.read_bytes()).json()  # fmt: skip
    events = _turn(client, session, "build this in 3D", [pid["id"]])
    assert events[1]["tool"] == "pid_to_3d" and events[1]["status"] == "done"
    assert events[-1]["views"][0]["card"]["stats"][0]["label"] == "Pipe length"
    assert client.post("/api/chat/files", params={"filename": "notes.pdf"},
                       content=b"%PDF").status_code == 422  # fmt: skip


def test_a_model_drives_the_tools_and_sees_compact_results(tmp_path):
    design = {
        "title": "Two tanks",
        "equipment": [{"tag": "T-1", "type": "tank"}, {"tag": "T-2", "type": "tank"}],
        "lines": [{"from_tag": "T-1", "to_tag": "T-2"}],
    }
    model = ScriptedChatModel([
        ModelReply([{"type": "text", "text": "Designing it."},
                    {"type": "tool_use", "id": "t1", "name": "design_plant", "input": design}],
                   "tool_use"),
        ModelReply([{"type": "text", "text": "Done: the pipe is routed and checked."}]),
    ])  # fmt: skip
    client = _client(tmp_path, model)
    session = client.post("/api/chat/sessions").json()["id"]
    events = _turn(client, session, "connect two tanks")
    assert events[-1]["text"] == "Done: the pipe is routed and checked."
    result = json.loads(model.requests[1][-1]["content"][0]["content"])
    assert result["verdict"] == "pass" and "meshes" not in json.dumps(result)
    assert result["totals"]["routed"] == 1
    busy = client.post(f"/api/chat/sessions/{session}/messages", json={"text": ""})
    assert busy.status_code == 422
    assert client.get("/api/chat/sessions/cnot-a-session").status_code == 404


def test_claude_requests_use_adaptive_thinking_caching_and_fallbacks():
    class Block:
        def __init__(self, data):
            self.data = data

        def model_dump(self, **_):
            return self.data

    class Response:
        content = [Block({"type": "text", "text": "hi"})]
        stop_reason = "end_turn"

    class Messages:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return Response()

    class Client:
        class beta:  # noqa: N801 - mirrors the SDK's attribute
            messages = Messages()

    model = ClaudeChatModel("key", client=Client())
    reply = model.complete("system", [{"role": "user", "content": "hi"}], [])
    sent = Client.beta.messages.kwargs
    assert reply.content == [{"type": "text", "text": "hi"}]
    assert sent["model"] == "claude-opus-5-5" and sent["thinking"] == {"type": "adaptive"}
    assert sent["cache_control"] == {"type": "ephemeral"} and sent["fallbacks"] == "default"
    assert sent["output_config"] == {"effort": "medium"}
