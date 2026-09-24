"""P2 — ported generic agent loop.

Exercises the ReAct loop with a stubbed tool-calling chat model (no real LLM /
provider SDK required): tool execution, terminal finalize, error propagation.
"""
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from filings_agent.agent.loop import (
    AgentProviderError,
    parse_json_object,
    run_agent_loop,
)


# ── stub chat model ─────────────────────────────────────────────────────────


class StubChat:
    """Minimal ``bind_tools``/``invoke`` chat model returning canned messages."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.bound_tools = None
        self.invocations = 0

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.invocations += 1
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class ExplodingChat(StubChat):
    def invoke(self, messages):
        raise RuntimeError("DeepSeek 402 Insufficient Balance")


@tool
def ping(x: int) -> str:
    """Return pong for the given value."""
    return f"pong:{x}"


# ── JSON parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('here you go: {"a": 1} — done', {"a": 1}),
        ("not json at all", None),
        ("[1, 2, 3]", None),  # must be an object
        ("", None),
        (None, None),
    ],
)
def test_parse_json_object(raw, expected):
    assert parse_json_object(raw) == expected


# ── loop ────────────────────────────────────────────────────────────────────


def test_loop_executes_tool_then_finalizes():
    chat = StubChat(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "ping", "args": {"x": 1}, "id": "tc1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "finalize_review",
                        "args": {"result_json": '{"ok": true, "decision": "pass"}'},
                        "id": "tc2",
                    }
                ],
            ),
        ]
    )

    result = run_agent_loop(
        "system", "do the thing", [ping], ticker="TEST", chat_llm=chat
    )

    assert result == {"ok": True, "decision": "pass"}
    assert chat.invocations == 2
    # The terminal tool is appended to the caller's tools.
    assert {t.name for t in chat.bound_tools} == {"ping", "finalize_review"}


def test_loop_uses_custom_finalize_name():
    chat = StubChat(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "finalize_guidance",
                        "args": {"result_json": '{"records": []}'},
                        "id": "tc1",
                    }
                ],
            )
        ]
    )

    result = run_agent_loop(
        "system",
        "extract guidance",
        [],
        ticker="TEST",
        finalize_name="finalize_guidance",
        chat_llm=chat,
    )

    assert result == {"records": []}
    assert {t.name for t in chat.bound_tools} == {"finalize_guidance"}


def test_loop_propagates_provider_error():
    with pytest.raises(AgentProviderError) as exc:
        run_agent_loop(
            "system", "hi", [ping], ticker="TEST", chat_llm=ExplodingChat([])
        )
    assert "402" in str(exc.value)


def test_loop_respects_max_steps():
    chat = StubChat(
        [
            AIMessage(content="thinking", tool_calls=[
                {"name": "ping", "args": {"x": 1}, "id": "tc1"}
            ])
            for _ in range(10)
        ]
    )

    result = run_agent_loop(
        "system", "hi", [ping], ticker="TEST", max_steps=2, chat_llm=chat
    )

    assert result is None  # never finalized
    assert chat.invocations == 2
