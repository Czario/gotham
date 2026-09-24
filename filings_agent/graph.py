"""LangGraph workflow for the filings agent.

Architecture mirrors the earning_agent: a linear ``StateGraph`` whose nodes are
pure ``(state) -> state`` functions wrapped by ``with_hooks`` (structured
logging, timing, exception → ``status="failed"``) with conditional edges that
short-circuit to ``END`` on ``failed`` / ``skipped``.

P2/P3/P4 pipeline:

    normalize_bundle → validate → agent_review? → repair? → validate → persist → END

Later phases insert hierarchy and guidance between the final validation and
persist nodes.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .hooks import with_hooks
from .nodes.decide import make_decide_node
from .nodes.concept_resolve import make_concept_resolve_node
from .nodes.guidance import make_guidance_extract_node, make_guidance_save_node
from .nodes.hierarchy import make_hierarchy_node
from .nodes.hierarchy_review import make_hierarchy_review_node
from .nodes.normalize import make_normalize_node
from .nodes.persist import make_persist_node
from .nodes.repair import make_repair_node
from .nodes.review import make_review_node
from .nodes.validate import make_validate_node
from .state import FilingAgentState

logger = logging.getLogger(__name__)

# Statuses that short-circuit the remaining pipeline.
_SHORT_CIRCUIT = ("failed", "skipped")


def _named(node_fn: Any, name: str) -> Any:
    """Give a node factory's function a distinct name.

    ``with_hooks`` derives the node name from ``__name__``, and the graph uses
    the same factory (``make_validate_node``) for three different stages — so
    without this every validate stage would report as ``validate_node`` in the
    terminal narrative and the audit trail.
    """
    node_fn.__name__ = name
    return node_fn


def _route_after(next_node: str) -> Callable[[dict], str]:
    """Route to *next_node*, or END when the run failed / was skipped."""

    def _route(state: dict) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        return next_node

    return _route


def _needs_agent_review(state: dict) -> str:
    """Route high-severity/needs_decision findings through review + repair."""
    if state.get("status") in _SHORT_CIRCUIT:
        return "__end__"
    if any(bool(f.get("needs_decision")) for f in (state.get("findings") or [])):
        return "agent_review"
    return "resolve_hierarchy"


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
):
    """Build and compile the filings-agent workflow.

    Args:
        norm_service: A ``FinancialNormalizationService`` (or compatible) that
            provides ``normalize_statement_to_bundle`` (pure) and
            ``persist_statement_bundle`` (the only writer).
        enforce_allowed_types: Passed to the persist node.
        report_store: Optional ``ValidationReportStore``; when supplied the
            validate node persists its report (audit trail).
        review_chat_llm: Optional injected tool-calling model for tests or
            provider selection. The review node falls back to deterministic
            identity repair when unavailable.
        repair_mode: Optional AGENT_MODE override for the CorrectionGate.
        guidance_chat_llm: Optional injected model for the guidance pass.
        mda_provider: Optional callable ``state -> str | None`` supplying the
            MD&A text (defaults to the local HTML archive).
        on_event: Optional structured event sink for the audit trail; forwarded
            to the agent loops (review + guidance).

    Returns:
        A compiled LangGraph runnable accepting a :class:`FilingAgentState`.
    """
    graph = StateGraph(FilingAgentState)

    graph.add_node(
        "normalize_bundle",
        with_hooks(_named(make_normalize_node(norm_service), "normalize_bundle_node")),
    )
    graph.add_node("validate", with_hooks(_named(make_validate_node(report_store), "validate_node")))
    graph.add_node(
        "resolve_hierarchy",
        with_hooks(_named(make_hierarchy_node(norm_service), "resolve_hierarchy_node")),
    )
    graph.add_node(
        "hierarchy_review",
        with_hooks(
            _named(
                make_hierarchy_review_node(
                    norm_service, chat_llm=decision_chat_llm, on_event=on_event
                ),
                "hierarchy_review_node",
            )
        ),
    )
    graph.add_node(
        "concept_resolve",
        with_hooks(
            _named(
                make_concept_resolve_node(
                    norm_service, chat_llm=decision_chat_llm, on_event=on_event
                ),
                "concept_resolve_node",
            )
        ),
    )
    graph.add_node(
        "agent_review",
        with_hooks(
            _named(
                make_review_node(chat_llm=review_chat_llm, on_event=on_event),
                "agent_review_node",
            )
        ),
    )
    graph.add_node(
        "repair",
        with_hooks(_named(make_repair_node(mode=repair_mode), "repair_node")),
    )
    graph.add_node(
        "validate_after_repair",
        with_hooks(
            _named(make_validate_node(report_store), "validate_after_repair_node")
        ),
    )
    graph.add_node(
        "validate_final",
        with_hooks(_named(make_validate_node(report_store), "validate_final_node")),
    )
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
    graph.add_node(
        "persist",
        with_hooks(
            _named(
                make_persist_node(
                    norm_service, enforce_allowed_types=enforce_allowed_types
                ),
                "persist_node",
            )
        ),
    )
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
    graph.add_node(
        "save_guidance", with_hooks(_named(make_guidance_save_node(), "save_guidance_node"))
    )

    graph.set_entry_point("normalize_bundle")

    graph.add_conditional_edges(
        "normalize_bundle",
        _route_after("validate"),
        {"validate": "validate", "__end__": END},
    )
    graph.add_conditional_edges(
        "validate",
        _needs_agent_review,
        {
            "agent_review": "agent_review",
            "resolve_hierarchy": "resolve_hierarchy",
            "__end__": END,
        },
    )
    graph.add_edge("agent_review", "repair")
    graph.add_edge("repair", "validate_after_repair")
    graph.add_edge("validate_after_repair", "resolve_hierarchy")
    graph.add_edge("resolve_hierarchy", "hierarchy_review")
    # The agent owns the hierarchy: when hierarchy_review cannot obtain an
    # agent-approved plan and the failure policy is "block", it marks the run
    # failed and this edge short-circuits to END so nothing is persisted with a
    # hierarchy the agent did not decide. (validate_final would otherwise reset
    # the status to "validated" and let the write through.)
    graph.add_conditional_edges(
        "hierarchy_review",
        _route_after("concept_resolve"),
        {"concept_resolve": "concept_resolve", "__end__": END},
    )
    graph.add_conditional_edges(
        "concept_resolve",
        _route_after("validate_final"),
        {"validate_final": "validate_final", "__end__": END},
    )
    graph.add_conditional_edges(
        "validate_final",
        _route_after("decide"),
        {"decide": "decide", "__end__": END},
    )
    # The agent decides what (if anything) is written; persist only executes it.
    graph.add_conditional_edges(
        "decide",
        _route_after("persist"),
        {"persist": "persist", "__end__": END},
    )
    # Post-save guidance (mirrors the 8-K agent): runs only when the filing was
    # actually persisted, so scoring can resolve against the stored actuals.
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

