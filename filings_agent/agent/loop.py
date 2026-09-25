"""Generic tool-calling agent loop (ReAct-style).

Ported from the earning_agent extraction loop, with the 8-K-specific number
parsing removed.  The filings agent's terminal tool returns *decisions*
(validation resolution, repair proposals, hierarchy placement, guidance
records) rather than raw extracted numbers, so the loop only needs to:

    system prompt → model with bound tools → execute tool calls →
    feed results back → … → terminal finalize tool.

The loop is OPEN-ENDED by default: it runs until the agent calls the terminal
finalize tool (or a provider error occurs).  Pass ``max_steps`` for a hard cap.
"""
from __future__ import annotations

import json
import logging
import re
import time
from itertools import count
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from filings_agent.agent.prompts import FINALIZE_DESCRIPTION
from filings_agent.config import LLM_PROVIDER
from filings_agent.hooks import report_call, report_detail
from filings_agent.llm import build_chat_llm

logger = logging.getLogger(__name__)

# Per-tool result caps.  Document-navigation tools (used by the guidance pass)
# get larger caps so one call can return a whole MD&A section.
_TOOL_RESULT_CAPS: dict[str, int] = {
    "read_lines": 60_000,
    "search": 16_000,
}
_DEFAULT_TOOL_RESULT_CAP = 8_000


class AgentProviderError(RuntimeError):
    """A provider/API failure that must remain visible to callers."""


class _NetworkDiagnosticHandler(logging.Handler):
    """Intercepts transient HTTP/API connection retries and surfaces them to terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if "Retrying request in" in msg:
                report_call(f"  [network]  ⚠ API connection issue, {msg.lower()}")
                report_detail("⚠ retrying API connection...")
            elif "timed out" in msg.lower() or "connecttimeout" in msg.lower():
                report_call(f"  [network]  ⚠ connection delay/timeout: {msg[:100]}")
                report_detail("⚠ connection timeout...")
        except Exception:
            pass


def _summarize_tool_result(tool_name: str, result: Any, dur: float) -> str:
    """Generate a clean, single-line semantic summary of a tool execution."""
    dur_str = f"{dur:.2f}s" if dur < 1.0 else f"{dur:.1f}s"
    if isinstance(result, str):
        if result.startswith("Tool error:"):
            return f"✗ {result[:100]} ({dur_str})"
        if tool_name == "query_hierarchy_diff":
            first_line = result.strip().split("\n")[0]
            if first_line.startswith("SUMMARY:"):
                clean = first_line.replace("SUMMARY:", "").strip()
                return f"✓ query_hierarchy_diff in {dur_str} — {clean}"
            return f"✓ query_hierarchy_diff in {dur_str}"
        if tool_name == "query_filing_hierarchy":
            lines = [l for l in result.strip().split("\n") if l.strip()]
            return f"✓ query_filing_hierarchy in {dur_str} ({len(lines)} lines)"
        if tool_name == "decide_mapping":
            first_line = result.strip().split("\n")[0]
            return f"✓ decide_mapping in {dur_str} — {first_line}"
        if tool_name == "propose_hierarchy":
            first_line = result.strip().split("\n")[0]
            lint_msg = ""
            if "Linter passed" in result:
                lint_msg = ", linter passed"
            elif "Linter found" in result:
                lint_msg = ", linter warnings found"
            return f"✓ propose_hierarchy in {dur_str} — {first_line}{lint_msg}"
        if tool_name == "lint_hierarchy":
            first_line = result.strip().split("\n")[0]
            return f"✓ lint_hierarchy in {dur_str} — {first_line}"
    return f"✓ {tool_name} in {dur_str}"


def _emit(on_event: Callable[[dict], None] | None, record: dict) -> None:
    """Fire a structured event; auditing must never break the loop."""
    if on_event is None:
        return
    try:
        on_event(record)
    except Exception:  # noqa: BLE001
        logger.debug("agent event handler failed", exc_info=True)


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Strip markdown fences and parse a JSON object; ``None`` on failure.

    Tolerant of leading/trailing prose around the object (common with smaller
    models) — it slices from the first ``{`` to the last ``}``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]
    end_brace = cleaned.rfind("}")
    if end_brace >= 0:
        cleaned = cleaned[: end_brace + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_finalize_tool(name: str, description: str):
    """Create the terminal tool the agent must call with its JSON result."""
    from langchain_core.tools import tool as _lc_tool

    @_lc_tool
    def finalize_tool(result_json: str) -> str:
        """Call this when done. Pass a JSON string with all results."""
        return result_json

    finalize_tool.name = name
    finalize_tool.description = description
    return finalize_tool


def run_agent_loop(
    system_prompt: str,
    initial_message: str,
    tools: list,
    *,
    ticker: str = "?",
    max_steps: int | None = None,
    finalize_name: str = "finalize_review",
    finalize_description: str = FINALIZE_DESCRIPTION,
    parse_final_result: Callable[[str], dict[str, Any] | None] | None = None,
    recovery_regex: re.Pattern | None = None,
    chat_llm: Any = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """Run the tool-calling agent loop.

    Args:
        system_prompt: Full system prompt.
        initial_message: First human message.
        tools: LangChain tools; the terminal finalize tool is appended.
        ticker: For logging / progress.
        max_steps: Optional hard step cap (``None`` = open-ended).
        finalize_name: Name of the terminal tool.
        finalize_description: Docstring for the terminal tool.
        parse_final_result: Parser for the terminal tool's JSON string.
            Defaults to :func:`parse_json_object`.
        recovery_regex: Pattern used to recover a final JSON blob from the last
            AI message when the agent never called the terminal tool.
        chat_llm: Pre-built tool-calling chat model (tests / dependency
            injection).  When ``None``, ``build_chat_llm()`` is used.
        on_event: Optional structured event sink.  Receives dicts such as
            ``{"event": "tool_call", "step": n, "tool": name, "args": {...}}``
            — used by the audit trail (P7).

    Returns:
        The parsed final result dict, or ``None`` if the agent produced none.
    """
    if parse_final_result is None:
        parse_final_result = parse_json_object
    if recovery_regex is None:
        recovery_regex = re.compile(r"\{.*\}", re.DOTALL)

    all_tools = list(tools) + [_build_finalize_tool(finalize_name, finalize_description)]

    if chat_llm is None:
        try:
            chat_llm = build_chat_llm()
        except ValueError as exc:
            logger.warning("Agent loop unavailable for %s: %s", ticker, exc)
            return None

    llm_with_tools = chat_llm.bind_tools(all_tools)
    messages: list = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=initial_message),
    ]

    final_result: dict[str, Any] | None = None

    steps = count(1) if not max_steps else range(1, max_steps + 1)

    diag_handler = _NetworkDiagnosticHandler()
    watched_loggers = [
        logging.getLogger("openai._base_client"),
        logging.getLogger("openai"),
        logging.getLogger("httpx"),
    ]
    for wl in watched_loggers:
        wl.addHandler(diag_handler)

    try:
        for step in steps:
            provider_str = LLM_PROVIDER or "llm"
            report_call(f"  [llm]  calling {provider_str}...")
            report_detail(f"calling {provider_str}...")
            _emit(on_event, {"event": "llm_step", "step": step, "provider": LLM_PROVIDER})

            try:
                _t0 = time.perf_counter()
                response = llm_with_tools.invoke(messages)
                _elapsed = time.perf_counter() - _t0
            except Exception as exc:
                # Preserve the provider's exact status/body for the operator.
                detail = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "Agent LLM call failed for %s — %s",
                    ticker, detail, exc_info=True,
                )
                report_call(f"  [llm]  ✗ provider error: {detail[:800]}")
                _emit(
                    on_event,
                    {"event": "provider_error", "step": step, "detail": detail[:800]},
                )
                raise AgentProviderError(
                    f"LLM provider failed at agent call: {detail}"
                ) from exc

            report_call(
                f"  [timing]  llm took {_elapsed:.1f}s  ({provider_str})"
            )

            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                content = getattr(response, "content", "") or ""
                if "finalize" in content.lower() or "{" in content:
                    break
                messages.append(
                    HumanMessage(
                        content=(
                            "Use your tools to investigate, then call "
                            f"{finalize_name} when done."
                        )
                    )
                )
                continue

            tool_messages: list[ToolMessage] = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("args", {})
                tool_call_id = tc.get("id", "")

                logger.info(
                    "Agent call for %s → tool: %s(%s)",
                    ticker, tool_name, str(tool_args)[:120],
                )
                if tool_name == finalize_name:
                    report_call(f"  [tool]  {finalize_name}()")
                    report_detail("finalizing...")
                else:
                    args_brief = ", ".join(f"{k}={v!r}" for k, v in tool_args.items())
                    if len(args_brief) > 110:
                        args_brief = args_brief[:110] + "…"
                    report_call(f"  [tool]  {tool_name}({args_brief})")
                    report_detail(f"tool: {tool_name}")
                _emit(
                    on_event,
                    {"event": "tool_call", "step": step, "tool": tool_name, "args": tool_args},
                )

                if tool_name == finalize_name:
                    _t_fin = time.perf_counter()
                    raw_result = tool_args.get("result_json", "")
                    result_str = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
                    logger.info("Agent finalize JSON for %s: %s", ticker, result_str[:1000])

                    parsed = parse_final_result(result_str)
                    _fin_dur = time.perf_counter() - _t_fin
                    if parsed is None:
                        report_call(f"  [tool]  ✗ {finalize_name} — could not parse result JSON")
                    else:
                        final_result = parsed
                        report_call(f"  [tool]  ✓ {finalize_name} in {_fin_dur:.2f}s — status='done'")
                        logger.info(
                            "Agent finalize for %s: %d key(s)",
                            ticker, len(final_result),
                        )
                        _emit(
                            on_event,
                            {
                                "event": "finalize",
                                "step": step,
                                "tool": finalize_name,
                                "keys": list(final_result)[:50],
                            },
                        )
                    tool_messages.append(
                        ToolMessage(
                            content=(
                                "Finalize received."
                                if parsed is not None
                                else "Failed to parse result JSON. Ensure it is a valid JSON object."
                            ),
                            tool_call_id=tool_call_id,
                        )
                    )
                    messages.extend(tool_messages)
                    break

                tool_fn = next((t for t in all_tools if t.name == tool_name), None)
                if tool_fn is not None:
                    _t_tool = time.perf_counter()
                    try:
                        result = tool_fn.invoke(tool_args)
                        _tool_dur = time.perf_counter() - _t_tool
                    except Exception as exc:  # noqa: BLE001
                        _tool_dur = time.perf_counter() - _t_tool
                        result = f"Tool error: {exc}"
                else:
                    _tool_dur = 0.0
                    result = f"Unknown tool: {tool_name}"

                tool_summary = _summarize_tool_result(tool_name, result, _tool_dur)
                report_call(f"  [tool]  {tool_summary}")

                if isinstance(result, str):
                    cap = _TOOL_RESULT_CAPS.get(tool_name, _DEFAULT_TOOL_RESULT_CAP)
                    if len(result) > cap:
                        result = result[:cap] + "\n... (truncated)"
                tool_messages.append(ToolMessage(content=str(result), tool_call_id=tool_call_id))

            messages.extend(tool_messages)
            if final_result is not None:
                break
    finally:
        for wl in watched_loggers:
            wl.removeHandler(diag_handler)

    # Fallback: try to recover JSON from the last AI message.
    if final_result is None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                content = getattr(msg, "content", "") or ""
                json_match = recovery_regex.search(content)
                if json_match:
                    parsed = parse_final_result(json_match.group())
                    if parsed is not None:
                        final_result = parsed
                        logger.info("Agent loop: recovered result from final message")
                        break

    if final_result is None:
        logger.error("Agent for %s: no result produced", ticker)
        _emit(on_event, {"event": "no_result", "ticker": ticker})
        return None

    return final_result
