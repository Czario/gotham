"""Period existence check + deferred replace scheduling.

Runs AFTER the period agent (``detect_period``) — the check has a
trustworthy ``(fiscal_year, quarter)`` returned by the period agent.

No accession checks — only exact-period existence using the canonical
period-agent result:

  • same fiscal period stored  → schedule ``_pending_replace`` and CONTINUE —
    the replacement itself is atomic inside ``mongodb_save`` →
    ``upsert_concept_values`` (all values are upserted first, then a stale
    sweep removes only docs not carrying the current save token — there is no
    delete-before-write window)
  • otherwise                  → proceed

Period typing comes from the agent: an ANNUAL period is checked and replaced
in ``concept_values_annual`` only — a quarterly Q4 record for the same fiscal
year is never checked, deleted, or touched.
"""
from __future__ import annotations

import logging

from earnings_agents.integrations.normalize import (
    fiscal_period_exists,
    official_filing_period_exists,
)
from earnings_agents.state import EarningsAgentState
from earnings_agents.agent.period import (
    format_period_label,
    require_detected_period,
)

logger = logging.getLogger(__name__)


def check_period_node(state: EarningsAgentState) -> EarningsAgentState:
    from earnings_agents.hooks import report_call

    cik = state.get("cik")
    try:
        period = require_detected_period(state)
    except Exception as exc:
        return {**state, "status": "failed", "error": f"check_period: {exc}"}

    # QUARTERLY-ONLY mode (backfill / --quarterly-only / worker flag): period
    # classification still comes exclusively from the period agent, but an
    # annual filing is stopped here — before concept loading, extraction, save.
    if state.get("quarterly_only") and period.period_type != "quarterly":
        reason = (
            f"quarterly-only mode: {format_period_label(period)} "
            "is annual; annual filings are not processed by earnings-backfill"
        )
        report_call(f"  [backfill]  skipped — {reason}")
        return {
            **state,
            "status": "skipped",
            "error": reason,
            "_pending_replace": None,
        }

    if not cik or not period.fiscal_year:
        return {
            **state,
            "status": "failed",
            "error": "check_period: missing cik / agent-detected fiscal year",
        }

    # Guard against calendar-vs-fiscal mismatches in backfill: if the caller
    # pinned a target fiscal year/quarter and the period agent detected a
    # different period, skip before spending LLM calls or overwriting another quarter.
    req_fy = state.get("target_fiscal_year")
    req_q = state.get("target_fiscal_quarter")
    if req_fy is not None and period.fiscal_year != int(req_fy):
        reason = (
            f"period mismatch: requested FY{req_fy}"
            + (f" Q{req_q}" if req_q is not None else "")
            + f", but filing is {format_period_label(period)}"
        )
        report_call(f"  [check]  skipped — {reason}")
        return {**state, "status": "skipped", "error": reason, "_pending_replace": None}
    if req_q is not None and period.quarter != int(req_q):
        reason = (
            f"period mismatch: requested FY{req_fy or '?'} Q{req_q}, "
            f"but filing is {format_period_label(period)}"
        )
        report_call(f"  [check]  skipped — {reason}")
        return {**state, "status": "skipped", "error": reason, "_pending_replace": None}

    period_label = period.period_label or ""

    if fiscal_period_exists(cik, period):
        label = format_period_label(period)
        # Never allow an 8-K / 6-K press release to overwrite official 10-K/10-Q/20-F XBRL data
        # unless the caller explicitly passed --replace.
        if not state.get("force_replace") and official_filing_period_exists(cik, period):
            reason = f"period {label} already has official 10-K/10-Q/20-F data — skipping 8-K/6-K overwrite"
            report_call(f"  [check]  skipped — {reason}")
            return {
                **state,
                "status": "skipped",
                "error": reason,
                "_pending_replace": None,
            }
        action_msg = (
            f"period {label} already stored — will delete and replace fresh (--replace)"
            if state.get("force_replace")
            else f"period {label} already stored — will replace after successful save"
        )
        report_call(f"  [check]  {action_msg}")
        return {
            **state,
            "_pending_replace": {"cik": cik},
            "_replace_period_label": label,
        }

    report_call(
        f"  [check]  new period — {period_label or '?'} "
        f"({format_period_label(period)})"
    )
    return {**state, "_pending_replace": None}
