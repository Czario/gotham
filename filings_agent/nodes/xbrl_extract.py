"""XBRL extraction node — entry point for parsing financial statements.

Calls the XBRL parser and transformer to produce statement documents on state.
Supports fallback extraction and handles empty or failed statements gracefully.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from filings_agent.tools.xbrl_tools import (
    execute_xbrl_extraction,
    validate_statement_classification_logic,
)

logger = logging.getLogger(__name__)


def make_xbrl_extract_node(
    financial_processor: Optional[Any] = None,
    financial_transformer: Optional[Any] = None,
) -> Callable[[dict], dict]:
    """Build the xbrl_extract node for LangGraph."""

    def xbrl_extract_node(state: dict) -> dict:
        # If statements are already provided on state, passthrough
        if state.get("statement_docs"):
            return {**state, "status": "extracted"}

        cik = str(state.get("cik") or "")
        accession_number = state.get("accession_number")
        form_type = state.get("form_type") or "10-K"

        filing_doc = state.get("filing_doc") or {}
        company_doc = state.get("company_doc") or {}

        filing_info = {
            "accessionNumber": accession_number,
            "form": form_type,
            "filingDate": state.get("filing_date"),
            **filing_doc,
        }

        logger.info(
            "xbrl_extract_node: extracting XBRL for CIK %s %s (%s)",
            cik,
            accession_number or "",
            form_type,
        )

        extracted = execute_xbrl_extraction(
            filing_info=filing_info,
            cik=cik,
            company_info=company_doc,
            financial_processor=financial_processor,
        )

        statements = extracted.get("statements") or {}
        if not statements:
            logger.warning(
                "xbrl_extract_node: no statements extracted for %s %s",
                cik,
                accession_number,
            )
            return {
                **state,
                "status": "failed",
                "error": f"XBRL extraction returned no statements for {accession_number}",
            }

        # If financial_transformer is provided, transform statements to statement_docs
        statement_docs = []
        if financial_transformer is not None:
            reporting_period = extracted.get("reporting_period", {})
            for statement_type, statement_data in statements.items():
                if not statement_data:
                    continue
                # Line items extraction and validation
                line_items = []
                if isinstance(statement_data, dict):
                    line_items = statement_data.get("line_items") or []
                elif isinstance(statement_data, list):
                    line_items = statement_data

                # Check classification
                cls_check = validate_statement_classification_logic(statement_type, line_items)
                if not cls_check.get("valid", True):
                    logger.warning("Statement classification check warning: %s", cls_check.get("reason"))

                try:
                    doc = financial_transformer.transform_statement_data(
                        line_items,
                        filing_info.get("id"),
                        cik,
                        statement_type,
                        reporting_period,
                    )
                    if doc:
                        statement_docs.append(doc)
                except Exception as exc:
                    logger.error("Transform error for %s: %s", statement_type, exc)
        else:
            # Fallback: store statements as statement_docs directly if already structured
            for st_type, st_data in statements.items():
                statement_docs.append({"statement_type": st_type, "data": st_data})

        return {
            **state,
            "statement_docs": statement_docs,
            "status": "extracted",
            "extraction_result": {"statement_count": len(statement_docs)},
        }

    return xbrl_extract_node
