"""Sign-convention subagent node.

Runs on every filing, right after normalization and before validation, so the
values that are validated, decided on and persisted already carry the house
signs.

Shape mirrors the hierarchy agent: a deterministic CRITIC
(``signs.policy``) reports which covered rows violate a declared convention,
and the agent fixes them through the ``set_sign`` tool, re-checking with
``check_signs()`` until clean.  The conventions themselves are declared data,
not code the agent can re-interpret.

Declared conventions (see ``filings_agent/signs/policy.py``):

* income statement  — interest expense is negative
* balance sheet     — accounts receivable is positive
* cash flow         — share-based compensation is positive

Nothing here touches the database (persist stays the only writer) and a failure
of any kind degrades to "no change + a loud report" — it never blocks a filing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..agent.sign_prompts import SIGN_AGENT_SYSTEM_PROMPT
from ..agent.loop import run_agent_loop
from ..hooks import report_call, report_detail
from ..signs import policy
from ..tools.sign_tools import build_sign_tools

logger = logging.getLogger(__name__)

_FINALIZE_DESCRIPTION = (
    "Call when every convention-covered row carries its required sign. "
    'Pass {"status": "done", "fixes": <count>, "notes": [...]}.'
)


def _sync_flattened(bundle: Any, concept: str, value: float) -> None:
    for attr in ("values", "dimensional_values"):
        for entry in (getattr(bundle, attr, None) or []):
            if isinstance(entry, dict) and entry.get("concept") == concept:
                entry["value"] = value


def _enforce_declared_convention(bundles: list[Any], proposal: dict) -> int:
    """Apply the DECLARED convention to anything still wrong.

    This is not a judgement — the required sign is declared data.  It exists so
    a filing can never be persisted with a known-wrong sign even if the model
    was unavailable or gave up.  Every application is recorded with
    ``source="sign_policy"`` so it stays distinguishable from an agent fix.
    """
    applied = 0
    for bundle in bundles:
        statement_type = getattr(bundle, "statement_type", "") or ""
        for item in (getattr(bundle, "concepts", None) or []):
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            value = float(value)
            if value == 0:
                continue
            required = policy.required_sign(statement_type, item.get("concept") or "")
            if required is None or policy.sign_of(value) == required:
                continue
            new = abs(value) if required == policy.POSITIVE else -abs(value)
            item["value"] = new
            item["sign_fix_source"] = "sign_policy"
            item["sign_fix_old_value"] = value
            _sync_flattened(bundle, item["concept"], new)
            proposal.setdefault("fixes", []).append(
                {
                    "statement_type": statement_type,
                    "concept": item["concept"],
                    "old_value": value,
                    "new_value": new,
                    "required_sign": required,
                    "reason": "declared sign convention",
                    "source": "sign_policy",
                }
            )
            applied += 1
    return applied


def make_sign_agent_node(
    *,
    chat_llm: Any = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> Callable[[dict], dict]:
    """Build the sign-convention node."""

    def sign_agent_node(state: dict) -> dict:
        bundles = state.get("bundles") or []
        if not bundles:
            return {**state, "status": "sign_checked", "sign_fixes": []}

        covered = [row for bundle in bundles for row in policy.classify(bundle)]
        pending = [row for row in covered if not row["compliant"]]

        report_call(
            f"  [signs]  • {len(covered)} convention row(s) checked · "
            f"{len(pending)} violation(s)"
        )
        if not covered:
            report_detail("signs: nothing covered by a convention")
            return {**state, "status": "sign_checked", "sign_fixes": []}

        proposal: dict = {"fixes": []}

        # Fast-path: nothing to do → no LLM call.
        if pending:
            initial = "Rows that violate a declared sign convention:\n" + "\n".join(
                f"- {row['statement_type']} | {row['concept']} | {row['label']} | "
                f"value={row['value']:g} | has={row['current_sign']} | "
                f"must={row['required_sign']}"
                for row in pending
            )
            try:
                tools = build_sign_tools(bundles, proposal)
                run_agent_loop(
                    SIGN_AGENT_SYSTEM_PROMPT,
                    initial,
                    tools,
                    ticker=str(state.get("ticker") or state.get("cik") or "?"),
                    finalize_name="finalize_signs",
                    finalize_description=_FINALIZE_DESCRIPTION,
                    chat_llm=chat_llm,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 — never block a filing over signs
                logger.warning(
                    "sign agent unavailable (%s); applying the declared convention", exc
                )

            # Backstop: enforce the declared convention on whatever is left.
            backstop = _enforce_declared_convention(bundles, proposal)
            if backstop:
                logger.info("sign agent: %d row(s) fixed by the declared convention", backstop)

        fixes = proposal["fixes"]
        remaining = policy.violations(bundles)
        if remaining:
            logger.error(
                "signs: %d violation(s) could not be fixed: %s",
                len(remaining),
                [r["concept"] for r in remaining][:10],
            )

        if fixes:
            for fix in fixes:
                report_call(
                    f"  [signs]  ⇄ {fix['statement_type']} {fix['concept']}: "
                    f"{fix['old_value']:g} → {fix['new_value']:g} ({fix['required_sign']})"
                )
            report_call(f"  [signs]  ✓ {len(fixes)} sign(s) fixed")
        else:
            report_call("  [signs]  ✓ all convention rows already correct")

        return {
            **state,
            "status": "sign_checked",
            "sign_fixes": fixes,
            "sign_fix_unresolved": remaining,
        }

    return sign_agent_node
