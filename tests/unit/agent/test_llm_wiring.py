"""LLM client wiring — retry policy and the dev response cache.

The hierarchy agent is authoritative and ``HIERARCHY_AGENT_FAILURE_POLICY=block``
means an unretried provider timeout blocks the whole filing, so the retry count
actually reaching ``ChatOpenAI`` is a correctness property, not a tuning detail.
"""
from __future__ import annotations

import pytest

from filings_agent import config
from filings_agent.llm import build_chat_llm


def _openai_kwargs(monkeypatch, **overrides) -> dict:
    """Capture the kwargs ``build_chat_llm`` hands to ChatOpenAI."""
    for key, value in overrides.items():
        monkeypatch.setattr(config, key, value)

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("filings_agent.llm.LLM_CACHE_ENABLED", False)
    build_chat_llm(provider="deepseek")
    return captured


def test_chat_model_retries_once_by_default(monkeypatch):
    """A transient timeout must not cost the filing (see failure policy)."""
    assert config.LLM_CHAT_MAX_RETRIES >= 1
    assert _openai_kwargs(monkeypatch)["max_retries"] >= 1


def test_chat_model_retries_are_configurable(monkeypatch):
    assert _openai_kwargs(monkeypatch, LLM_CHAT_MAX_RETRIES=0)["max_retries"] == 0
    assert _openai_kwargs(monkeypatch, LLM_CHAT_MAX_RETRIES=2)["max_retries"] == 2


def test_retries_are_read_at_call_time(monkeypatch):
    """Not bound at import: the value must be picked up per call."""
    assert _openai_kwargs(monkeypatch, LLM_CHAT_MAX_RETRIES=0)["max_retries"] == 0
    assert _openai_kwargs(monkeypatch, LLM_CHAT_MAX_RETRIES=3)["max_retries"] == 3


def test_retries_are_not_left_at_the_library_default(monkeypatch):
    """LangChain defaults to 2, which lets a hung call ride out timeout x 3."""
    assert config.LLM_CHAT_MAX_RETRIES != 2
    assert _openai_kwargs(monkeypatch)["max_retries"] != 2


def test_timeout_is_still_bounded(monkeypatch):
    """Worst case must stay bounded: (attempts) x timeout, not unbounded."""
    kwargs = _openai_kwargs(monkeypatch)
    attempts = kwargs["max_retries"] + 1
    assert attempts <= 2, "more than one retry makes a stall too expensive"
    assert kwargs["timeout"] == pytest.approx(120.0)


def test_env_var_feeds_the_default(monkeypatch):
    """The env var is the operator-facing knob for this value."""
    import importlib

    monkeypatch.setenv("LLM_CHAT_MAX_RETRIES", "2")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_CHAT_MAX_RETRIES == 2
    finally:
        monkeypatch.delenv("LLM_CHAT_MAX_RETRIES", raising=False)
        importlib.reload(config)


def test_explicit_max_retries_argument_wins(monkeypatch):
    monkeypatch.setenv("LLM_CHAT_MAX_RETRIES", "1")

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("filings_agent.llm.LLM_CACHE_ENABLED", False)
    build_chat_llm(provider="deepseek", max_retries=0)
    assert captured["max_retries"] == 0
