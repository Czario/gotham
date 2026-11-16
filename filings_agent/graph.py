"""Google ADK workflow for the filings agent (Streamlined Deterministic SEC-First Architecture).

Orchestrates the filing pipeline using Google ADK's ``ADKSequentialAgent``,
``ADKStepAgent``, ``InMemorySessionService``, ``InMemoryArtifactService``,
and ``InMemoryMemoryService``:

    fetch_filing → xbrl_extract → normalize_bundle → historical_backfill →
    hierarchy_agent → validate → (repair / agent_review if needed) →
    decide → persist → (guidance if configured) → END
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .adk import ADKGraphRunner, ADKSequentialAgent, ADKStepAgent
from .hooks import with_hooks
from .recovery import with_recovery
from .nodes.decide import make_decide_node
from .nodes.fetch_filing import make_fetch_filing_node
from .nodes.guidance import make_guidance_extract_node, make_guidance_save_node
from .nodes.hierarchy_agent import make_hierarchy_agent_node
from .nodes.historical_backfill import make_historical_backfill_node
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


def _wrap(node_fn: Any, name: str, recover_llm: Any) -> Any:
    """Named node + logging hooks + the generic self-healing retry wrapper."""
    return with_recovery(with_hooks(_named(node_fn, name)), chat_llm=recover_llm)


def _route_after(next_node: str) -> Callable[[dict], str]:
    """Route to *next_node*, or END when the run failed / was skipped."""

    def _route(state: dict) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        return next_node

    return _route


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
) -> ADKGraphRunner:
    """Build and compile the streamlined Google ADK filings-agent workflow."""

    def _route_needs(state: dict) -> str:
        if state.get("status") in _SHORT_CIRCUIT:
            return "__end__"
        if any(bool(f.get("needs_decision")) for f in (state.get("findings") or [])):
            return "agent_review"
        return "hierarchy_agent"

    def _route_after_save(state: dict) -> str:
        if state.get("status") == "saved":
            return "extract_guidance"
        return "__end__"

    sub_agents = [
        ADKStepAgent(
            "fetch_filing",
            _wrap(
                make_fetch_filing_node(sec_client=sec_client, db_collections=db_collections),
                "fetch_filing_node",
                decision_chat_llm,
            ),
            description="Filing discovery and deduplication check.",
        ),
        ADKStepAgent(
            "xbrl_extract",
            _wrap(
                make_xbrl_extract_node(
                    financial_processor=financial_processor,
                    financial_transformer=financial_transformer,
                ),
                "xbrl_extract_node",
                decision_chat_llm,
            ),
            description="XBRL extraction and SEC presentation linkbase + HTML snapshot capture.",
        ),
        ADKStepAgent(
            "normalize_bundle",
            _wrap(make_normalize_node(norm_service), "normalize_bundle_node", decision_chat_llm),
            description="Normalize extracted statement documents into in-memory bundles.",
        ),
        ADKStepAgent(
            "historical_backfill",
            _wrap(
                make_historical_backfill_node(norm_service, chat_llm=decision_chat_llm, on_event=on_event),
                "historical_backfill_node",
                decision_chat_llm,
            ),
            description="Discover and stage missing comparative historical period facts and breakdowns.",
        ),
        ADKStepAgent(
            "hierarchy_agent",
            _wrap(
                make_hierarchy_agent_node(
                    norm_service, chat_llm=decision_chat_llm, on_event=on_event
                ),
                "hierarchy_agent_node",
                decision_chat_llm,
            ),
            description="Deterministic SEC-first hierarchy compiler with ADK LLM fallback for ambiguous aliases.",
        ),
        ADKStepAgent(
            "validate",
            _wrap(make_validate_node(report_store), "validate_node", decision_chat_llm),
            description="Run deterministic validation suite including SEC presentation diff.",
        ),
        ADKStepAgent(
            "agent_review",
            _wrap(
                make_review_node(chat_llm=review_chat_llm, on_event=on_event),
                "agent_review_node",
                decision_chat_llm,
            ),
            description="ADK review agent for validation findings requiring a decision.",
        ),
        ADKStepAgent(
            "repair",
            _wrap(make_repair_node(mode=repair_mode), "repair_node", decision_chat_llm),
            description="Provenance-gated repair execution.",
        ),
        ADKStepAgent(
            "validate_final",
            _wrap(make_validate_node(report_store), "validate_final_node", decision_chat_llm),
            description="Post-repair validation.",
        ),
        ADKStepAgent(
            "decide",
            _wrap(
                make_decide_node(
                    chat_llm=decision_chat_llm,
                    on_event=on_event,
                    report_store=report_store,
                ),
                "decide_node",
                decision_chat_llm,
            ),
            description="Final write decision policy.",
        ),
        ADKStepAgent(
            "persist",
            _wrap(
                make_persist_node(norm_service, enforce_allowed_types=enforce_allowed_types),
                "persist_node",
                decision_chat_llm,
            ),
            description="Database persistence for primary bundles and missing historical backfill.",
        ),
        ADKStepAgent(
            "extract_guidance",
            _wrap(
                make_guidance_extract_node(
                    chat_llm=guidance_chat_llm,
                    mda_provider=mda_provider,
                    on_event=on_event,
                ),
                "extract_guidance_node",
                decision_chat_llm,
            ),
            description="MD&A forward-looking guidance extraction.",
        ),
        ADKStepAgent(
            "save_guidance",
            _wrap(make_guidance_save_node(), "save_guidance_node", decision_chat_llm),
            description="Guidance persistence.",
        ),
    ]

    edges = {
        "fetch_filing": _route_after("xbrl_extract"),
        "xbrl_extract": _route_after("normalize_bundle"),
        "normalize_bundle": _route_after("historical_backfill"),
        "historical_backfill": _route_after("validate"),
        "validate": _route_needs,
        "agent_review": "repair",
        "repair": "hierarchy_agent",
        "hierarchy_agent": _route_after("validate_final"),
        "validate_final": _route_after("decide"),
        "decide": _route_after("persist"),
        "persist": _route_after_save,
        "extract_guidance": "save_guidance",
        "save_guidance": "__end__",
    }

    root_agent = ADKSequentialAgent(
        "filing_pipeline_agent",
        sub_agents,
        description="Google ADK SEC filing extraction, hierarchy, validation, and persistence pipeline.",
        entry_point="fetch_filing",
        edges=edges,
    )
    return ADKGraphRunner(root_agent, app_name="filings_agent")


# Convenience alias used by the CLI / entry point.
build_graph = build_filing_graph
