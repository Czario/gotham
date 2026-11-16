"""Company-stage Google ADK workflow (P7 + Complete-Stock Audit).

Runs ONCE per company, after the per-filing loop has finished:

    quarterly_deaccumulation
        → companyfacts_reconciliation
        → stock_audit
        → finalize_validation_reports
        → END
"""
from __future__ import annotations

import logging
from typing import Any

from .adk import ADKGraphRunner, ADKSequentialAgent, ADKStepAgent
from .hooks import with_hooks
from .nodes.company import (
    make_companyfacts_node,
    make_finalize_reports_node,
    make_quarterly_node,
)
from .nodes.stock_audit import make_stock_audit_node
from .state import CompanyAgentState

logger = logging.getLogger(__name__)


def build_company_graph(
    *,
    quarterly_service: Any,
    reconciliation_provider: Any = None,
    report_store: Any = None,
    enable_reconciliation: bool = True,
    frequencies: tuple[str, ...] = ("annual", "quarterly"),
    include_edge: bool = True,
    target_db: Any = None,
    audit_chat_llm: Any = None,
) -> ADKGraphRunner:
    """Build and compile the Google ADK company-stage workflow."""
    sub_agents = [
        ADKStepAgent(
            "quarterly_deaccumulation",
            with_hooks(make_quarterly_node(quarterly_service)),
            description="Derive 3-month quarterly statements from YTD statements.",
        ),
        ADKStepAgent(
            "companyfacts_reconciliation",
            with_hooks(
                make_companyfacts_node(
                    reconciliation_provider,
                    enable_reconciliation=enable_reconciliation,
                    frequencies=frequencies,
                    include_edge=include_edge,
                )
            ),
            description="Reconcile missing non-dimensional gaps from SEC companyfacts API.",
        ),
        ADKStepAgent(
            "stock_audit",
            with_hooks(
                make_stock_audit_node(
                    report_store,
                    target_db=target_db,
                    chat_llm=audit_chat_llm,
                )
            ),
            description="Complete-stock audit for hierarchy health, cross-period continuity, and scale consistency.",
        ),
        ADKStepAgent(
            "finalize_validation_reports",
            with_hooks(make_finalize_reports_node(report_store)),
            description="Finalize validation reports for the company run.",
        ),
    ]

    edges = {
        "quarterly_deaccumulation": "companyfacts_reconciliation",
        "companyfacts_reconciliation": "stock_audit",
        "stock_audit": "finalize_validation_reports",
        "finalize_validation_reports": "__end__",
    }

    root_agent = ADKSequentialAgent(
        "company_pipeline_agent",
        sub_agents,
        description="Google ADK company-level post-processing and complete-stock audit workflow.",
        entry_point="quarterly_deaccumulation",
        edges=edges,
    )
    return ADKGraphRunner(root_agent, app_name="filings_company_agent")
