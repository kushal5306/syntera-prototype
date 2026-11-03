"""/api/assistant answers with citations, using a scripted provider (F3)."""

from __future__ import annotations

from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient

from syntera.assistant.agent import ProviderReply, ScriptedProvider, TextBlock, ToolUseBlock
from syntera.drawings.intake import ingest_drawing
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def _use(call_id, name, arguments):
    return ProviderReply(content=[ToolUseBlock(id=call_id, name=name, input=arguments)],
                         stop_reason="tool_use")  # fmt: skip


def _say(text):
    return ProviderReply(content=[TextBlock(text=text)])


def _client(tmp_path, replies):
    provider = ScriptedProvider(replies)
    app = create_app(SIZED, tmp_path / "web", assistant_provider=lambda: provider)
    return TestClient(app), provider


def test_design_question_is_answered_with_rationale_citations(tmp_path):
    client, _ = _client(tmp_path, [
        _use("t1", "explain", {"path": "tube.outer_diameter"}),
        _say("The OD is 20 mm: the smallest standard tube that keeps the flow under 2 m/s."),
    ])  # fmt: skip
    assert client.get("/api/assistant/status").json() == {"available": True,
                                                          "provider": "scripted"}  # fmt: skip
    response = client.post("/api/assistant", json={"question": "Why is the pipe 20 mm?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("The OD is 20 mm")
    assert body["tool_calls"] == ["explain"] and body["checks_run"] is False
    cited = {(c["kind"], c["id"]) for c in body["citations"]}
    assert ("rationale", "lines.main.tube.outer_diameter") in cited
    assert ("rule", "rules.tube_size_from_flow.v1") in cited
    assert ("knowledge", "kb.rule.standard_tube") in cited
    assert (tmp_path / "web" / "assistant" / "transcript.jsonl").is_file()


def test_drawing_question_cites_entity_ids(tmp_path):
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    outline = msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True)
    msp.add_text("PUMP P-101", height=20).set_placement((250, 200))
    path = tmp_path / "plan.dxf"
    doc.saveas(path)
    summary = ingest_drawing(path.read_bytes(), "plan.dxf", tmp_path / "web" / "drawings")
    drawing = summary.drawing_id
    client, _ = _client(tmp_path, [
        _use("t1", "query_drawing", {"drawing": drawing, "tag": "P-101"}),
        _say("P-101 is the rectangle outline (inferred from its label)."),
    ])  # fmt: skip
    body = client.post("/api/assistant", json={"question": "Where is P-101?",
                                               "drawing_id": drawing}).json()  # fmt: skip
    assert {"kind": "drawing", "id": f"drawing:{drawing}:{outline.dxf.handle}"} in body["citations"]
    missing = client.post("/api/assistant", json={"question": "x", "drawing_id": "0" * 16})
    assert missing.status_code == 404
    assert client.post("/api/assistant", json={"question": "x", "drawing_id": "../x"}
                       ).status_code == 422  # fmt: skip


def test_history_reaches_the_provider_and_verdicts_need_checks(tmp_path):
    client, provider = _client(tmp_path, [
        _say("It passes."),
        _use("t1", "run_checks", {}),
        _say("The exact checks report PASS for line main."),
    ])  # fmt: skip
    history = [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}]
    first = client.post("/api/assistant", json={"question": "Does it pass?", "history": history})
    assert "no checks were run" in first.json()["answer"]
    assert [m.role for m in provider.requests[0]] == ["user", "assistant", "user"]
    second = client.post("/api/assistant", json={"question": "Run the checks."}).json()
    assert second["checks_run"] is True and "no checks were run" not in second["answer"]
    assert any(c["kind"] == "check" for c in second["citations"])


def test_without_a_model_the_endpoint_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app(SIZED, tmp_path / "web"))
    assert client.get("/api/assistant/status").json()["available"] is False
    response = client.post("/api/assistant", json={"question": "Why?"})
    assert response.status_code == 503 and "ANTHROPIC_API_KEY" in response.json()["detail"]
