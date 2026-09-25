"""P4 agent-review node.

Only runs when validation put ``needs_decision`` on a finding. It offers the
scoped XBRL tools to the generic ReAct loop and returns repair proposals; if an
LLM/provider is unavailable, deterministic identity repair is used for math
mismatches. No database writes occur here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..agent.loop import run_agent_loop
from ..agent.tools import build_review_tools

logger = logging.getLogger(__name__)


def _needs_review(findings: list[dict]) -> bool:
    return any(bool(f.get("needs_decision")) for f in findings)


def _deterministic_proposals(findings: list[dict]) -> list[dict]:
    """Create safe repair proposals for validator-proven arithmetic identities."""
    proposals = []
    for finding in findings:
        if finding.get("type") != "math_mismatch":
            continue
        evidence = finding.get("evidence") or {}
        target = evidence.get("target_concept") or finding.get("concept")
        old = evidence.get("target_value")
        expected = evidence.get("expected_value")
        expected_abs = evidence.get("expected_abs_subtrahends", expected)
        if target is None or old is None or expected is None:
            continue
        # Prefer the sign interpretation closest to the current target. For a
        # genuine mismatch both are usually far away; stored-sign is the
        # conservative XBRL calculation result.
        candidates = [float(expected), float(expected_abs)]
        new_value = min(candidates, key=lambda x: abs(float(old) - x))
        proposals.append({
            "statement_type": finding.get("statement_type") or "",
            "concept": target,
            "old_value": float(old),
            "new_value": new_value,
            "reason": f"recomputed from {evidence.get('identity', 'validated XBRL identity')}",
            "source": "deterministic_identity",
            "approved": True,
            "confidence": 0.99,
        })
    return proposals


def _review_prompt(findings: list[dict]) -> tuple[str, str]:
    system = """You are reviewing deterministic validation findings for an SEC XBRL filing.
Use the tools to inspect the exact in-memory concepts and verify arithmetic.
You may propose a correction only when the evidence supports it. Never invent
facts, never correct a value merely because it looks unusual, and preserve the
concept/statement identity. Call finalize_review with JSON:
{"repairs": [{"statement_type":"income","concept":"us-gaap:GrossProfit",
"old_value": 1, "new_value": 2, "reason":"...",
"source":"agent_confirmed", "approved": true, "confidence": 0.9}]}
If no safe repair exists, return {"repairs": []}."""
    initial = "Validation findings:\n" + "\n".join(
        f"- {f.get('severity')}: {f.get('type')}: {f.get('message')}"
        for f in findings
    )
    return system, initial


def make_review_node(*, chat_llm: Any = None, on_event: Any = None) -> Callable[[dict], dict]:
    """Build the review node; ``chat_llm`` is injectable for tests."""

    def review_node(state: dict) -> dict:
        findings = state.get("findings") or []
        if not _needs_review(findings):
            return {**state, "status": "reviewed", "repair_decisions": []}

        proposals: list[dict] = []
        from .. import config

        # The deterministic fallback is always available and is used when the
        # provider is unavailable or the agent returns no usable proposal.
        fallback = _deterministic_proposals(findings)

        # Report mode is intentionally deterministic and non-mutating: do not
        # spend an LLM call on a proposal that CorrectionGate will reject.
        if config.AGENT_REVIEW_ENABLED and config.AGENT_MODE != "report":
            try:
                from ..hooks import report_call

                report_call(
                    f"  [review]  consulting review agent "
                    f"({len(findings)} finding(s), mode={config.AGENT_MODE})"
                )
                tools = build_review_tools(state.get("bundles") or [], proposals)
                system, initial = _review_prompt(findings)
                result = run_agent_loop(
                    system,
                    initial,
                    tools,
                    ticker=state.get("ticker", state.get("cik", "?")),
                    finalize_name="finalize_review",
                    chat_llm=chat_llm,
                    on_event=on_event,
                )
                if isinstance(result, dict) and isinstance(result.get("repairs"), list):
                    proposals.extend(x for x in result["repairs"] if isinstance(x, dict))
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent review unavailable; using deterministic fallback: %s", exc)

        if not proposals:
            proposals = fallback

        logger.info(
            "review: %d proposal(s) for CIK %s",
            len(proposals), state.get("cik"),
        )
        return {
            **state,
            "status": "reviewed",
            "repair_decisions": proposals,
        }

    return review_node
