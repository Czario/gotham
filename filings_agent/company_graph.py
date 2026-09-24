"""Company-stage graph (P7).

Runs ONCE per company, after the per-filing loop has finished:

    quarterly_deaccumulation
        → companyfacts_reconciliation
        → finalize_validation_reports
        → END

Kept in its own module (rather than in ``graph.py``) because it shares no state
or services with the filing graph: it operates on ``CompanyAgentState`` and is
driven by the scraper's company loop.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from .hooks import with_hooks
from .nodes.company import (
    make_companyfacts_node,
    make_finalize_reports_node,
    make_quarterly_node,
)
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
):
    """Build and compile the company-stage workflow.

    Args:
        quarterly_service: ``PeriodBasedFinancialCalculationService`` used for
            deaccumulation.
        reconciliation_provider: Zero-arg callable returning the companyfacts
            service (or ``None``).
        report_store: ``ValidationReportStore`` used to finalize the company's
            validation reports.
        enable_reconciliation: Company-level gate for the reconciliation step.
        frequencies: Companyfacts frequencies to reconcile.
        include_edge: Whether to reconcile the latest filing's trailing edge.

    Returns:
        A compiled LangGraph runnable accepting a :class:`CompanyAgentState`.
    """
    graph = StateGraph(CompanyAgentState)

    graph.add_node(
        "quarterly_deaccumulation",
        with_hooks(make_quarterly_node(quarterly_service)),
    )
    graph.add_node(
        "companyfacts_reconciliation",
        with_hooks(
            make_companyfacts_node(
                reconciliation_provider,
                enable_reconciliation=enable_reconciliation,
                frequencies=frequencies,
                include_edge=include_edge,
            )
        ),
    )
    graph.add_node(
        "finalize_validation_reports",
        with_hooks(make_finalize_reports_node(report_store)),
    )

    graph.set_entry_point("quarterly_deaccumulation")
    graph.add_edge("quarterly_deaccumulation", "companyfacts_reconciliation")
    graph.add_edge("companyfacts_reconciliation", "finalize_validation_reports")
    graph.add_edge("finalize_validation_reports", END)
    return graph.compile()
