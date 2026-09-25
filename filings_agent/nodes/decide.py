"""Decide node (P8) — the agent's explicit final write decision.

Placed between the final validation and ``persist``.  It always produces a
:class:`~filings_agent.decision.WriteDecision` and never writes to MongoDB;
``persist`` then executes exactly what was decided.

The deterministic policy decides by default.  When ``AGENT_DECISION_ENABLED`` is
set, an LLM judge is consulted for the ambiguous middle only — there are
blocking findings *and* something is still permitted — and may narrow the write
or opt into a partial write.  It can never widen the validation ceiling.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from filings_agent.decision import (
    WriteDecision,
    apply_agent_plan,
    needs_agent_decision,
    policy_decision,
)

logger = logging.getLogger(__name__)


def _ceiling_brief(state: dict, policy: WriteDecision) -> str:
    findings = state.get("findings") or []
    lines = [
        f"PERMITTED: {policy.permitted or '[]'}",
        f"BLOCKED: {policy.blocked or '[]'}",
        f"EXCLUDED CONCEPTS: {policy.drop_concepts or '{}'}",
        "FINDINGS:",
    ]
    for finding in findings:
        lines.append(
            f"  - [{finding.get('severity')}] {finding.get('type')} "
            f"({finding.get('statement_type') or 'filing-level'}"
            f"{', ' + str(finding.get('concept')) if finding.get('concept') else ''}): "
            f"{finding.get('message')}"
        )
    return "\n".join(lines)


def _ask_decision_agent(
    state: dict,
    policy: WriteDecision,
    *,
    chat_llm: Any,
    on_event: Any,
) -> Any:
    """Ask the LLM judge for a write plan (returns a dict or None)."""
    from filings_agent.agent.loop import run_agent_loop
    from filings_agent.agent.prompts import DECISION_FINALIZE_DESCRIPTION, decision_system_prompt_TEMPLATE
    from filings_agent.tools.review_tools import build_review_tools

    tools = [
        tool
        for tool in build_review_tools(state.get("bundles") or [], [])
        if getattr(tool, "name", "") != "propose_repair"
    ]
    system_prompt = (
        decision_system_prompt_TEMPLATE
        + "\n\nTHIS FILING\n"
        + _ceiling_brief(state, policy)
        + f"\n\nFORM: {state.get('form_type')}  PERIOD: {state.get('reporting_period')}"
    )
    initial_message = (
        f"Decide what to write for {state.get('company_name') or state.get('cik')} "
        f"({state.get('ticker') or '?'}), filing {state.get('accession_number')}."
    )
    return run_agent_loop(
        system_prompt,
        initial_message,
        tools,
        ticker=str(state.get("ticker") or state.get("cik") or "?"),
        finalize_name="finalize_decision",
        finalize_description=DECISION_FINALIZE_DESCRIPTION,
        chat_llm=chat_llm,
        on_event=on_event,
    )


def make_decide_node(
    *,
    chat_llm: Any = None,
    on_event: Any = None,
    report_store: Any = None,
    strict_accuracy: Optional[bool] = None,
) -> Callable[[dict], dict]:
    """Build the ``decide`` node."""

    def decide_node(state: dict) -> dict:
        from filings_agent import config

        strict = config.STRICT_ACCURACY if strict_accuracy is None else strict_accuracy
        bundles = state.get("bundles") or []
        findings = state.get("findings") or []

        policy = policy_decision(bundles, findings, strict_accuracy=strict)
        decision = policy

        if needs_agent_decision(policy, enabled=config.AGENT_DECISION_ENABLED):
            try:
                from filings_agent.hooks import report_call

                report_call(
                    f"  [decide]  consulting decision judge — "
                    f"{len(policy.permitted)} statement(s) still writable"
                )
                plan = _ask_decision_agent(
                    state, policy, chat_llm=chat_llm, on_event=on_event
                )
                decision = apply_agent_plan(policy, plan)
            except Exception as exc:  # noqa: BLE001 — fall back to the policy
                logger.warning(
                    "decision agent unavailable for %s; keeping policy decision: %s",
                    state.get("cik"), exc,
                )

        payload = decision.to_dict()

        if report_store is not None:
            try:
                report_store.update_decision(
                    state.get("cik"),
                    state.get("accession_number"),
                    state.get("form_type"),
                    payload,
                )
            except Exception as exc:  # noqa: BLE001 — audit is best-effort
                logger.warning("could not record write decision: %s", exc)

        if on_event is not None:
            try:
                on_event(
                    "write_decision",
                    cik=state.get("cik"),
                    accession_number=state.get("accession_number"),
                    **payload,
                )
            except Exception:  # noqa: BLE001
                logger.debug("write_decision event failed", exc_info=True)

        logger.info(
            "decide: %s by %s — writing %s (permitted=%s, blocked=%s)",
            decision.action,
            decision.decided_by,
            decision.statements or "nothing",
            decision.permitted,
            decision.blocked,
        )
        return {
            **state,
            "write_plan": payload,
            "decision": payload,
            "status": "decided",
        }

    return decide_node
