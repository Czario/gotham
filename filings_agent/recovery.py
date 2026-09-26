"""Generalized self-healing node wrapper (Exception Handling & Recovery).

Every node in the filing graph is wrapped so an operational failure does NOT
immediately kill the filing.  The cycle is:

    1. Detect   — the node raises (operational) or returns ``status="failed"``.
    2. Record   — the issue is appended to ``state["_issues"]``
                  (``{node, error, attempt, kind}``) so it is always traceable.
    3. Decide   — a lightweight recovery JUDGE (LLM) chooses ``retry`` or
                  ``escalate``.  When no LLM is available the default is a
                  bounded retry (transient failures are the common case).
    4. Recover  — ``retry`` re-runs the node with the failure markers cleared;
                  ``escalate`` marks the filing failed with the full issue log
                  (graceful — never an unhandled crash).

Only *operational* failures (a node raised an exception) are retried.  A
semantic ``failed`` the node returns deliberately (e.g. "nothing to write") is
recorded and escalated immediately — retrying it could not help.

This is deliberately generic: it needs no per-node knowledge, so it covers the
complete flow (fetch, extract, normalize, validate, hierarchy, review, repair,
decide, persist, guidance) uniformly.
"""
from __future__ import annotations

import json
import logging
from functools import wraps
from typing import Any, Callable, Optional

from filings_agent.agent.loop import parse_json_object
from filings_agent.hooks import report_call

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 2

# Node failures that are NOT retried (deterministic / data errors) — escalating
# straight away avoids burning an LLM call on something a retry cannot fix.
_NON_RETRYABLE = frozenset({})


def _summarize(state: dict, node: str, error: str) -> dict:
    """A compact, LLM-friendly snapshot of the failure context."""
    return {
        "node": node,
        "error": error,
        "ticker": state.get("ticker"),
        "cik": state.get("cik"),
        "form_type": state.get("form_type"),
        "accession_number": state.get("accession_number"),
        "status": state.get("status"),
        "statement_types": [
            getattr(b, "statement_type", None) for b in (state.get("bundles") or [])
        ],
        "findings": len(state.get("findings") or []),
    }


def _ask_recover_agent(summary: dict, chat_llm: Any) -> Optional[dict]:
    """Ask the recovery judge for ``retry`` vs ``escalate``.

    Returns ``None`` when the judge is unavailable or cannot decide (the caller
    then falls back to the deterministic bounded retry).
    """
    if chat_llm is None:
        return None
    prompt = (
        "You are a pipeline recovery judge. A node in a financial-filing pipeline failed.\n"
        "Decide whether to RETRY (likely transient: timeout, rate limit, connection, "
        "provider hiccup) or ESCALATE (likely permanent: malformed data, programming error).\n"
        "Respond with a single JSON object only:\n"
        '{"action": "retry" | "escalate", "reason": "<one short sentence>"}\n\n'
        f"FAILURE: {json.dumps(summary, default=str)}"
    )
    try:
        raw = chat_llm.invoke(prompt)
        if not isinstance(raw, str):
            raw = str(getattr(raw, "content", "") or raw)
        parsed = parse_json_object(raw)
        if parsed and parsed.get("action") in ("retry", "escalate"):
            return parsed
    except Exception as exc:  # noqa: BLE001 — recovery must never itself break the run
        logger.warning("recovery judge unavailable for %s: %s", summary.get("node"), exc)
    return None


def with_recovery(
    node_fn: Callable[[dict], dict],
    *,
    chat_llm: Any = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Callable[[dict], dict]:
    """Wrap *node_fn* (already ``with_hooks``-wrapped) with bounded self-healing."""

    node_name = getattr(node_fn, "__name__", "node")

    @wraps(node_fn)
    def _wrapper(state: dict) -> dict:
        current = state
        issues = list(state.get("_issues") or [])
        attempt = 0
        while True:
            result = node_fn(current)
            if result is None:
                result = current
            if not isinstance(result, dict):
                result = {
                    **current,
                    "status": "failed",
                    "error": f"{node_name} returned a non-dict result",
                }

            if result.get("status") != "failed":
                if issues:
                    result["_issues"] = issues
                return result

            error = str(result.get("error") or "unknown error")
            # A semantic ``failed`` the node returned on purpose is NOT retryable
            # (retrying cannot change the outcome) — record and escalate now.
            if not bool(result.get("_raised_exception")):
                issues.append({"node": node_name, "error": error, "attempt": 1, "kind": "semantic"})
                result["_issues"] = issues
                return result

            # ── Detect & record (operational exception) ───────────────────
            attempt += 1
            issues.append({"node": node_name, "error": error, "attempt": attempt, "kind": "exception"})
            logger.warning(
                "recovery: %s failed (attempt %d/%d): %s",
                node_name, attempt, max_attempts, error,
            )
            report_call(
                f"  [recovery]  ⚠ {node_name} failed (attempt {attempt}/{max_attempts}) — {error[:140]}"
            )

            # ── Escalate after the retry budget is exhausted ───────────────
            if attempt >= max_attempts:
                result["_issues"] = issues
                return result

            # ── Decide retry vs escalate (LLM judge, or deterministic retry) ──
            decision = None
            if node_name not in _NON_RETRYABLE:
                decision = _ask_recover_agent(_summarize(result, node_name, error), chat_llm)
            action = (decision or {}).get("action")
            reason = (decision or {}).get("reason") or "deterministic retry"
            if action == "escalate":
                result["_issues"] = issues
                logger.info("recovery: %s escalated: %s", node_name, reason)
                return result

            report_call(f"  [recovery]  ↻ retrying {node_name} — {reason[:120]}")
            # Clear transient failure markers so the node can re-run cleanly.
            current = {**result, "error": None, "status": state.get("status") or "validated"}

    return _wrapper
