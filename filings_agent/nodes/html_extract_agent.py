"""ADK HTML Statement Extraction Agent for filings without XBRL.

When an SEC filing (pre-XBRL 2010–2012, 6-K, or HTML-only filing) lacks
XBRL exhibits, this agent inspects the ``.htm`` filing using
``list_html_tables``, ``read_html_table``, and ``search_html_text``,
identifies the Income Statement, Balance Sheet, and Cash Flow Statement,
determines the reporting unit multiplier (thousands/millions/ones), and
submits structured line items via ``submit_html_statement``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.tools import StructuredTool

from ..agent.loop import run_agent_loop
from ..tools.html_table_tools import build_html_table_tools

logger = logging.getLogger(__name__)


_HTML_EXTRACT_SYSTEM = """You are an SEC Financial Statement Extraction Agent.
This filing has no XBRL exhibits, so you must extract the financial statements
directly from the rendered SEC .htm tables.

Workflow:
1. Call list_html_tables() (or list_html_tables(statement_hint="income"), etc.) to locate the Income Statement, Balance Sheet, and Cash Flow Statement tables.
2. Call read_html_table(table_id=...) on each financial statement table. Check the table header/title or search_html_text("in thousands") / search_html_text("in millions") to confirm the unit multiplier (1, 1000, or 1000000).
3. For each statement ("income", "balancesheet", "cashflow"), call submit_html_statement(statement_type, unit_multiplier, period_date, items_json).
   - Extract the PRIMARY (most recent) period column.
   - Each item in items_json must be a dict:
     {"concept": "us-gaap:<BestStandardTag>" or "custom:<CamelCaseLabel>",
      "label": "<exact row label from HTML>",
      "value": <float or null for abstract headers>,
      "abstract": <true/false>,
      "parent_concept": "<parent concept or null>",
      "is_per_share": <true for EPS / per-share rows, else false>}
   - Preserve negative signs: values in parentheses like (1,234) MUST be negative (-1234).
   - Do NOT multiply per-share / EPS values by unit_multiplier (set is_per_share=true).
4. Once all available statements are submitted, call finalize_html_extraction({"status": "done"}).
Never invent numbers — only submit values directly observed in the HTML tables."""


def extract_statements_from_html_agent(
    *,
    html_text: Optional[str] = None,
    accession_number: Optional[str] = None,
    ticker: Optional[str] = None,
    reporting_period: Optional[dict[str, Any]] = None,
    chat_llm: Any = None,
    on_event: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the ADK HTML Extraction Agent and return ``{stmt_type: [items...]}``."""
    extracted: dict[str, list[dict[str, Any]]] = {}

    def submit_html_statement(
        statement_type: str,
        unit_multiplier: float = 1.0,
        period_date: str = "",
        items_json: str = "[]",
    ) -> str:
        """Record an extracted financial statement from the SEC .htm table."""
        st = (statement_type or "").strip().lower()
        if st in ("income_statement", "operations", "earnings"):
            st = "income"
        elif st in ("balance_sheet", "financial_position"):
            st = "balancesheet"
        elif st in ("cash_flow", "cash_flows"):
            st = "cashflow"
        if st not in ("income", "balancesheet", "cashflow"):
            return f"Error: statement_type must be 'income', 'balancesheet', or 'cashflow' (got {statement_type!r})."

        try:
            raw_items = json.loads(items_json)
        except json.JSONDecodeError as exc:
            return f"Invalid items_json: {exc}"
        if not isinstance(raw_items, list) or not raw_items:
            return "Error: items_json must be a non-empty JSON array."

        mult = float(unit_multiplier or 1.0)
        p_date = period_date or str((reporting_period or {}).get("end_date") or "")
        normalized_items: list[dict[str, Any]] = []
        for idx, it in enumerate(raw_items):
            if not isinstance(it, dict) or not it.get("label"):
                continue
            concept = str(it.get("concept") or "").strip()
            if not concept:
                clean = "".join(w.capitalize() for w in str(it["label"]).split() if w.isalnum())
                concept = f"custom:{clean or f'LineItem{idx + 1}'}"
            raw_val = it.get("value")
            is_abs = bool(it.get("abstract", False))
            is_eps = bool(it.get("is_per_share", False))
            val: Optional[float] = None
            if not is_abs and raw_val is not None:
                try:
                    num = float(raw_val)
                    val = num if is_eps else num * mult
                except (TypeError, ValueError):
                    val = None
            normalized_items.append({
                "concept": concept,
                "label": str(it["label"]).strip(),
                "value": val,
                "abstract": is_abs,
                "parent_concept": it.get("parent_concept"),
                "period": p_date,
                "unit": "USD/shares" if is_eps else "USD",
                "reading_order": idx + 1,
                "source": "html_agent_extraction",
            })

        if not normalized_items:
            return "Error: no valid items parsed from items_json."
        extracted[st] = normalized_items
        return f"OK — recorded {len(normalized_items)} line item(s) for '{st}' (multiplier={mult})."

    html_tools = build_html_table_tools(
        html_text=html_text,
        accession_number=accession_number,
        ticker=ticker,
    )
    submit_tool = StructuredTool.from_function(
        func=submit_html_statement,
        name="submit_html_statement",
        description="Submit extracted line items for 'income', 'balancesheet', or 'cashflow' from an SEC .htm table.",
    )
    tools = list(html_tools) + [submit_tool]

    task = (
        f"Extract all available primary financial statements (income, balancesheet, cashflow) "
        f"from SEC filing {accession_number or '?'} ({ticker or '?'}) using list_html_tables and read_html_table."
    )
    run_agent_loop(
        _HTML_EXTRACT_SYSTEM,
        task,
        tools,
        ticker=str(ticker or accession_number or "?"),
        finalize_name="finalize_html_extraction",
        finalize_description="Call once all available statements are submitted.",
        chat_llm=chat_llm,
        on_event=on_event,
        max_steps=10,
    )
    return extracted
