"""Provider interface and the bounded agent loop, run with a scripted provider (F2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syntera.assistant.agent import (
    UNCHECKED_WARNING,
    AnthropicProvider,
    Message,
    ProviderError,
    ProviderReply,
    ScriptedProvider,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    provider_from_env,
    run_agent,
)
from syntera.assistant.tools import AssistantContext
from syntera.design import design_from_yaml

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def _ctx(tmp_path):
    return AssistantContext(design=design_from_yaml(SIZED), output=tmp_path)


def _use(call_id, name, arguments):
    return ProviderReply(content=[ToolUseBlock(id=call_id, name=name, input=arguments)],
                         stop_reason="tool_use")  # fmt: skip


def _say(text):
    return ProviderReply(content=[TextBlock(text=text)])


def test_loop_runs_requested_tools_and_returns_the_answer(tmp_path):
    provider = ScriptedProvider([
        _use("t1", "explain", {"path": "tube.outer_diameter"}),
        _say("The OD is 20 mm because rules.tube_size_from_flow.v1 picked it."),
    ])  # fmt: skip
    result = run_agent(provider, _ctx(tmp_path), "Why is the pipe 20 mm?",
                       transcript_path=tmp_path / "t.jsonl")  # fmt: skip
    assert result.stopped == "answer" and result.steps == 2
    assert result.tool_calls == ["explain"]
    assert result.answer.startswith("The OD is 20 mm")
    roles = [message.role for message in result.transcript]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = result.transcript[2].content[0]
    assert isinstance(tool_result, ToolResultBlock) and not tool_result.is_error
    assert json.loads(tool_result.content)["rule_id"] == "rules.tube_size_from_flow.v1"
    # the provider saw the tool result before answering
    assert provider.requests[1][-1].content[0].tool_use_id == "t1"
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and json.loads(lines[-1])["summary"]["stopped"] == "answer"


def test_tool_errors_go_back_to_the_model_not_up_the_stack(tmp_path):
    provider = ScriptedProvider([
        _use("t1", "query_design", {"what": "line", "id": "nope"}),
        _use("t2", "made_up_tool", {}),
        _say("I could not find that line."),
    ])  # fmt: skip
    result = run_agent(provider, _ctx(tmp_path), "Show line nope")
    errors = [m.content[0] for m in result.transcript if m.role == "user" and
              isinstance(m.content[0], ToolResultBlock)]  # fmt: skip
    assert [e.is_error for e in errors] == [True, True]
    assert "unknown line" in errors[0].content and "unknown tool" in errors[1].content
    assert result.stopped == "answer"


def test_step_limit_and_provider_errors_stop_cleanly(tmp_path):
    looping = ScriptedProvider([_use(f"t{i}", "query_design", {"what": "summary"})
                                for i in range(10)])  # fmt: skip
    result = run_agent(looping, _ctx(tmp_path), "loop", max_steps=3)
    assert result.stopped == "step_limit" and result.steps == 3
    assert result.answer.startswith("Stopped after 3 steps")
    empty = run_agent(ScriptedProvider([]), _ctx(tmp_path), "hello")
    assert empty.stopped == "provider_error" and "unavailable" in empty.answer


def test_unverified_verdict_talk_is_flagged(tmp_path):
    claim = run_agent(ScriptedProvider([_say("The design passes all checks.")]),
                      _ctx(tmp_path), "Does it pass?")  # fmt: skip
    assert claim.answer.endswith(UNCHECKED_WARNING) and not claim.checks_run
    quiet = run_agent(ScriptedProvider([_say("The OD is 20 mm.")]), _ctx(tmp_path), "OD?")
    assert UNCHECKED_WARNING not in quiet.answer


def test_transcripts_are_deterministic(tmp_path):
    def script():
        return ScriptedProvider([_use("t1", "search", {"query": "bend radius"}),
                                 _say("Long-radius bends.")])  # fmt: skip

    run_agent(script(), _ctx(tmp_path), "bends?", transcript_path=tmp_path / "a.jsonl")
    run_agent(script(), _ctx(tmp_path), "bends?", transcript_path=tmp_path / "b.jsonl")
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_anthropic_provider_is_off_without_a_key_and_speaks_the_api_shape():
    assert provider_from_env({}) is None
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("")
    provider = provider_from_env({"ANTHROPIC_API_KEY": "test-key",
                                  "SYNTERA_ANTHROPIC_MODEL": "some-model"})  # fmt: skip
    assert provider is not None and provider.model == "some-model"
    hello = [Message(role="user", content=[TextBlock(text="hi")])]
    body = provider.request_body("sys", hello, [{"name": "x", "description": "d"}])
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert body["system"] == "sys" and body["tools"][0]["name"] == "x"
    reply = AnthropicProvider.parse({
        "content": [{"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "u1", "name": "search", "input": {"query": "q"}}],
        "stop_reason": "tool_use",
    })  # fmt: skip
    assert reply.stop_reason == "tool_use"
    assert isinstance(reply.content[1], ToolUseBlock) and reply.content[1].input == {"query": "q"}


def test_ask_cli_needs_a_key(monkeypatch, capsys, tmp_path):
    from syntera.cli import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = ["ask", "--design", str(SIZED), "--question", "why 20 mm?", "--output", str(tmp_path)]
    assert main(args) == 2
    assert "set ANTHROPIC_API_KEY" in capsys.readouterr().err
