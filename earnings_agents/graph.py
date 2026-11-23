"""LangGraph workflow — agent pipeline.

Graph:
    fetch_filing → detect_period → check_period → load_company_concepts
        → agent_document_pipeline → mongodb_save → save_guidance
        → calculate_q4 → END

The reporting period is decided ONLY by the period agent (detect_period); if it
fails the run fails — no deterministic period inference anywhere.

``save_guidance`` (post-save, after a successful income-statement save): persists
the agent's forward-looking guidance records into `guidance_values` (the same
collection the admin backend reads/writes) and scores current guidance whose
covered period has arrived against the stored actuals.  Never fails a run.

``calculate_q4`` is the post-save Q4 derivation (income statement only): after
an ANNUAL filing is saved to ``concept_values_annual`` it derives
Q4 = Annual − (Q1 + Q2 + Q3) and inserts the quarterly values into
``concept_values_quarterly`` (see ``nodes/q4.py`` / ``integrations/q4.py``).
It runs only after a successful save and never fails the run.
"""
from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from earnings_agents.agent.period import detect_period_node
from earnings_agents.agent.pipeline import agent_document_pipeline_node
from earnings_agents.nodes.check import check_period_node
from earnings_agents.nodes.concepts import load_company_concepts_node
from earnings_agents.nodes.fetch import fetch_filing_node
from earnings_agents.nodes.q4 import calculate_q4_node
from earnings_agents.nodes.save import mongodb_save_node
from earnings_agents.nodes.save_guidance import save_guidance_node
from earnings_agents.state import EarningsAgentState
from earnings_agents.hooks import with_hooks

logger = logging.getLogger(__name__)

# Statuses that short-circuit the remaining pipeline.
_SHORT_CIRCUIT = ("failed", "skipped")


def _route_after(next_node: str):
    """Route to *next_node*, or END when the run failed / skipped / is stale."""

    def _route(state: EarningsAgentState) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        return next_node

    return _route


def _route_after_save(state: EarningsAgentState) -> str:
    """Run the post-save nodes only after a successful save."""
    if state.get("status") == "saved":
        return "save_guidance"
    return "__end__"


def _route_after_guidance(state: EarningsAgentState) -> str:
    """Quarterly-only and guidance-only runs never run post-save Q4 derivation."""
    if state.get("quarterly_only") or state.get("guidance_only"):
        return "__end__"
    return "calculate_q4"


def extract_guidance_only_node(state: EarningsAgentState) -> EarningsAgentState:
    """Extract forward-looking guidance only from fetched 8-K/6-K text (no financial statement extraction)."""
    from filings_agent.nodes.guidance import make_guidance_extract_node

    raw_text = state.get("raw_text") or ""
    if not raw_text.strip():
        return {
            **state,
            "guidance_records": [],
            "status": "skipped",
            "error": "No filing text available for guidance extraction.",
        }

    dp = state.get("detected_period") or {}
    reporting_period = {
        "end_date": dp.get("period_end"),
        "period_type": dp.get("period_type"),
        "fiscal_year": dp.get("fiscal_year"),
        "quarter": dp.get("quarter"),
        "period_label": dp.get("period_label"),
    }
    extract_fn = make_guidance_extract_node(force_enabled=True)
    sub_state = {
        "cik": state.get("cik") or "",
        "ticker": state.get("ticker") or "",
        "company_name": state.get("company_name") or "",
        "form_type": state.get("source_form_type") or "8-K",
        "accession_number": state.get("accession_number"),
        "reporting_period": reporting_period,
        "mda_text": raw_text,
        "bundles": [],
    }
    res = extract_fn(sub_state)
    extract_info = res.get("guidance_extract") or {}
    records = res.get("guidance_records") or []
    if extract_info.get("status") in ("error", "no_period"):
        return {
            **state,
            "guidance_records": [],
            "status": "failed",
            "error": extract_info.get("error") or extract_info.get("status"),
        }
    return {
        **state,
        "guidance_records": records,
        "status": "saved",
    }


def build_guidance_only_graph():
    """Compile a guidance-only LangGraph workflow for 8-K / 6-K filings.

    Graph:
        fetch_filing → detect_period → extract_guidance_only → save_guidance → END
    """
    graph = StateGraph(EarningsAgentState)

    graph.add_node("fetch_filing", with_hooks(fetch_filing_node))
    graph.add_node("detect_period", with_hooks(detect_period_node))
    graph.add_node("extract_guidance_only", with_hooks(extract_guidance_only_node))
    graph.add_node("save_guidance", with_hooks(save_guidance_node))

    graph.set_entry_point("fetch_filing")
    for src, dst in [
        ("fetch_filing", "detect_period"),
        ("detect_period", "extract_guidance_only"),
        ("extract_guidance_only", "save_guidance"),
    ]:
        route = _route_after(dst)
        graph.add_conditional_edges(src, route, {dst: dst, "__end__": END})
    graph.add_edge("save_guidance", END)

    return graph.compile()


def build_graph():
    """Compile and return the LangGraph earnings scraping workflow."""
    graph = StateGraph(EarningsAgentState)

    graph.add_node("fetch_filing", with_hooks(fetch_filing_node))
    graph.add_node("detect_period", with_hooks(detect_period_node))
    graph.add_node("check_period", with_hooks(check_period_node))
    graph.add_node("load_company_concepts", with_hooks(load_company_concepts_node))
    graph.add_node("agent_document_pipeline", with_hooks(agent_document_pipeline_node))
    graph.add_node("mongodb_save", with_hooks(mongodb_save_node))
    graph.add_node("save_guidance", with_hooks(save_guidance_node))
    graph.add_node("calculate_q4", with_hooks(calculate_q4_node))

    graph.set_entry_point("fetch_filing")

    for src, dst in [
        ("fetch_filing", "detect_period"),
        ("detect_period", "check_period"),
        ("check_period", "load_company_concepts"),
        ("load_company_concepts", "agent_document_pipeline"),
        ("agent_document_pipeline", "mongodb_save"),
    ]:
        route = _route_after(dst)
        graph.add_conditional_edges(src, route, {dst: dst, "__end__": END})

    # Post-save: guidance persistence/scoring runs first, then Q4 derivation
    # (both only after a successful save — the node no-ops otherwise).
    graph.add_conditional_edges(
        "mongodb_save",
        _route_after_save,
        {"save_guidance": "save_guidance", "__end__": END},
    )

    graph.add_conditional_edges(
        "save_guidance",
        _route_after_guidance,
        {"calculate_q4": "calculate_q4", "__end__": END},
    )
    graph.add_edge("calculate_q4", END)

    return graph.compile()
