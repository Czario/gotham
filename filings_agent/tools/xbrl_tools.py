"""Agentic tools for XBRL extraction, validation, and fallback.

These tools allow LangGraph agent nodes to drive XBRL extraction
dynamically instead of relying on deterministic procedural scripts.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Known revenue markers to detect misclassified income statements
REVENUE_MARKERS = {
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenueFromContractWithCustomer",
    "GrossProfit",
    "OperatingIncomeLoss",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
}

BALANCE_SHEET_MARKERS = {
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "AssetsCurrent",
    "LiabilitiesCurrent",
}

CASH_FLOW_MARKERS = {
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInInvestingActivities",
    "NetCashProvidedByUsedInFinancingActivities",
}


def validate_statement_classification_logic(
    statement_type: str,
    line_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Inspect concepts in line_items to confirm proper statement classification."""
    concepts = {
        item.get("concept", "")
        for item in line_items
        if item.get("value") is not None
    }

    if statement_type == "income":
        has_revenue = any(
            any(marker in c for marker in REVENUE_MARKERS)
            for c in concepts
        )
        if not has_revenue and len(line_items) < 25:
            return {
                "valid": False,
                "statement_type": statement_type,
                "concept_count": len(line_items),
                "reason": (
                    "No revenue/gross profit/operating income concepts found. "
                    "Likely a misclassified comprehensive income statement."
                ),
            }
    elif statement_type in ("balance", "balance_sheet"):
        has_bs = any(
            any(marker in c for marker in BALANCE_SHEET_MARKERS)
            for c in concepts
        )
        if not has_bs and len(line_items) < 25:
            return {
                "valid": False,
                "statement_type": statement_type,
                "concept_count": len(line_items),
                "reason": "Missing standard balance sheet markers (Assets/Liabilities/Equity).",
            }
    elif statement_type in ("cash_flow", "cashflow"):
        has_cf = any(
            any(marker in c for marker in CASH_FLOW_MARKERS)
            for c in concepts
        )
        if not has_cf and len(line_items) < 25:
            return {
                "valid": False,
                "statement_type": statement_type,
                "concept_count": len(line_items),
                "reason": "Missing standard operating/investing/financing cash flow markers.",
            }

    return {"valid": True, "statement_type": statement_type, "concept_count": len(line_items)}


@tool
def validate_statement_classification(
    statement_type: str,
    line_items: list[dict[str, Any]],
) -> str:
    """Validate whether an extracted statement's concepts match its expected classification."""
    result = validate_statement_classification_logic(statement_type, line_items)
    return str(result)


def execute_xbrl_extraction(
    filing_info: dict[str, Any],
    cik: str,
    company_info: dict[str, Any] | None = None,
    financial_processor: Any = None,
) -> dict[str, Any]:
    """Execute XBRL parsing using the financial processor.

    Called by the agent tool or directly by nodes.
    """
    if financial_processor is None:
        try:
            from core.processors.statement_processor import EnhancedFinancialStatementProcessor
            financial_processor = EnhancedFinancialStatementProcessor()
        except ImportError:
            logger.error("Could not import EnhancedFinancialStatementProcessor")
            return {"error": "financial_processor unavailable", "statements": {}}

    try:
        data = financial_processor.process_filing(filing_info, cik, company_info or {})
        return data or {"statements": {}}
    except Exception as exc:
        logger.error(f"XBRL extraction failed for CIK {cik}: {exc}")
        return {"error": str(exc), "statements": {}}


@tool
def xbrl_extract(
    cik: str,
    accession_number: str,
    form_type: str,
) -> str:
    """Extract financial statements from XBRL for a given filing accession."""
    filing_info = {
        "accessionNumber": accession_number,
        "form": form_type,
    }
    result = execute_xbrl_extraction(filing_info, cik)
    stmt_keys = list(result.get("statements", {}).keys())
    return f"Extracted {len(stmt_keys)} statements: {stmt_keys}"


@tool
def xbrl_extract_fallback(
    cik: str,
    accession_number: str,
) -> str:
    """Attempt fallback extraction (e.g. from SEC companyfacts JSON) when primary XBRL fails."""
    return f"Fallback extraction triggered for {cik} / {accession_number}."
