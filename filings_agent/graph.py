"""LangGraph workflow for the filings agent.

Architecture mirrors the earning_agent: a linear ``StateGraph`` whose nodes are
pure ``(state) -> state`` functions wrapped by ``with_hooks`` (structured
logging, timing, exception → ``status="failed"``) with conditional edges that
short-circuit to ``END`` on ``failed`` / ``skipped``.

Complete agentic pipeline:

    fetch_filing → xbrl_extract → normalize_bundle → validate →
    agent_review? → repair? → validate → hierarchy_agent → validate_final →
    decide → persist → extract_guidance → save_guidance → END
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .hooks import with_hooks
from .nodes.decide import make_decide_node
from .nodes.fetch_filing import make_fetch_filing_node
from .nodes.guidance import make_guidance_extract_node, make_guidance_save_node
from .nodes.hierarchy_agent import make_hierarchy_agent_node
from .nodes.normalize import make_normalize_node
from .nodes.persist import make_persist_node
from .nodes.repair import make_repair_node
from .nodes.review import make_review_node
from .nodes.validate import make_validate_node
from .nodes.xbrl_extract import make_xbrl_extract_node
from .state import FilingAgentState

logger = logging.getLogger(__name__)

# Statuses that short-circuit the remaining pipeline.
_SHORT_CIRCUIT = ("failed", "skipped")


def _named(node_fn: Any, name: str) -> Any:
    """Give a node factory's function a distinct name."""
    node_fn.__name__ = name
    return node_fn


def _route_after(next_node: str) -> Callable[[dict], str]:
    """Route to *next_node*, or END when the run failed / was skipped."""

    def _route(state: dict) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        return next_node

    return _route


def _route_after_save(state: dict) -> str:
    """Run the guidance nodes only after a successful save (mirrors 8-K agent)."""
    if state.get("status") == "saved":
        return "extract_guidance"
    return "__end__"


def build_filing_graph(
    norm_service: Any,
    *,
    enforce_allowed_types: bool = True,
    report_store: Any = None,
    review_chat_llm: Any = None,
    repair_mode: str | None = None,
    guidance_chat_llm: Any = None,
    mda_provider: Any = None,
    decision_chat_llm: Any = None,
    on_event: Any = None,
    financial_processor: Any = None,
    financial_transformer: Any = None,
    sec_client: Any = None,
    db_collections: Any = None,
):
    """Build and compile the filings-agent workflow."""
    graph = StateGraph(FilingAgentState)

    # 1. Filing discovery and dedup node
    graph.add_node(
        "fetch_filing",
        with_hooks(
            _named(
                make_fetch_filing_node(sec_client=sec_client, db_collections=db_collections),
                "fetch_filing_node",
            )
        ),
    )

    # 2. Agentic XBRL extraction node
    graph.add_node(
        "xbrl_extract",
        with_hooks(
            _named(
                make_xbrl_extract_node(
                    financial_processor=financial_processor,
                    financial_transformer=financial_transformer,
                ),
                "xbrl_extract_node",
            )
        ),
    )

    # 3. Normalization node
    graph.add_node(
        "normalize_bundle",
        with_hooks(_named(make_normalize_node(norm_service), "normalize_bundle_node")),
    )

    # 4. Deterministic multi-suite validation node
    graph.add_node("validate", with_hooks(_named(make_validate_node(report_store), "validate_node")))

    # 5. Hierarchy agent node
    graph.add_node(
        "hierarchy_agent",
        with_hooks(
            _named(
                make_hierarchy_agent_node(
                    norm_service, chat_llm=decision_chat_llm, on_event=on_event
                ),
                "hierarchy_agent_node",
            )
        ),
    )

    # 6. ReAct review agent node
    graph.add_node(
        "agent_review",
        with_hooks(
            _named(make_review_node(chat_llm=review_chat_llm, on_event=on_event), "agent_review_node")
        ),
    )

    # 7. Correction gate repair node
    graph.add_node("repair", with_hooks(_named(make_repair_node(mode=repair_mode), "repair_node")))

    # 8. Post-repair validation node
    graph.add_node(
        "validate_after_repair",
        with_hooks(_named(make_validate_node(report_store), "validate_after_repair_node")),
    )

    # 9. Final validation node
    graph.add_node(
        "validate_final",
        with_hooks(_named(make_validate_node(report_store), "validate_final_node")),
    )

    # 10. Final decision node
    graph.add_node(
        "decide",
        with_hooks(
            _named(
                make_decide_node(
                    chat_llm=decision_chat_llm,
                    on_event=on_event,
                    report_store=report_store,
                ),
                "decide_node",
            )
        ),
    )

    # 11. Database persist node
    graph.add_node(
        "persist",
        with_hooks(
            _named(
                make_persist_node(norm_service, enforce_allowed_types=enforce_allowed_types),
                "persist_node",
            )
        ),
    )

    # 12. Guidance extraction node
    graph.add_node(
        "extract_guidance",
        with_hooks(
            _named(
                make_guidance_extract_node(
                    chat_llm=guidance_chat_llm,
                    mda_provider=mda_provider,
                    on_event=on_event,
                ),
                "extract_guidance_node",
            )
        ),
    )

    # 13. Guidance save node
    graph.add_node(
        "save_guidance", with_hooks(_named(make_guidance_save_node(), "save_guidance_node"))
    )

    # Entry point is now fetch_filing
    graph.set_entry_point("fetch_filing")

    graph.add_conditional_edges(
        "fetch_filing",
        _route_after("xbrl_extract"),
        {"xbrl_extract": "xbrl_extract", "__end__": END},
    )

    graph.add_conditional_edges(
        "xbrl_extract",
        _route_after("normalize_bundle"),
        {"normalize_bundle": "normalize_bundle", "__end__": END},
    )

    def _route_needs(state: dict) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        if any(bool(f.get("needs_decision")) for f in (state.get("findings") or [])):
            return "agent_review"
        return "hierarchy_agent"

    graph.add_conditional_edges(
        "normalize_bundle",
        _route_after("validate"),
        {"validate": "validate", "__end__": END},
    )
    graph.add_conditional_edges(
        "validate",
        _route_needs,
        {"agent_review": "agent_review", "hierarchy_agent": "hierarchy_agent", "__end__": END},
    )
    graph.add_edge("agent_review", "repair")
    graph.add_edge("repair", "validate_after_repair")
    graph.add_edge("validate_after_repair", "hierarchy_agent")
    graph.add_conditional_edges(
        "hierarchy_agent",
        _route_after("validate_final"),
        {"validate_final": "validate_final", "__end__": END},
    )
    graph.add_conditional_edges(
        "validate_final",
        _route_after("decide"),
        {"decide": "decide", "__end__": END},
    )
    graph.add_conditional_edges(
        "decide",
        _route_after("persist"),
        {"persist": "persist", "__end__": END},
    )
    graph.add_conditional_edges(
        "persist",
        _route_after_save,
        {"extract_guidance": "extract_guidance", "__end__": END},
    )
    graph.add_edge("extract_guidance", "save_guidance")
    graph.add_edge("save_guidance", END)

    return graph.compile()


# Convenience alias used by the CLI / entry point.
build_graph = build_filing_graph
