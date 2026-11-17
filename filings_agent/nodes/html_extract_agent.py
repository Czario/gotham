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
This filing has no XBRL exhibits (or XBRL failed), so you must extract the financial
statements directly from the rendered SEC .htm tables.

Workflow:
1. Call list_html_tables() (or list_html_tables(statement_hint="income"), etc.) to locate the Income Statement, Balance Sheet, and Cash Flow Statement tables.
2. Call read_html_table(table_id=...) on each financial statement table. Check the table header/title or search_html_text("in thousands") / search_html_text("in millions") to confirm the unit multiplier (1, 1000, or 1000000).
3. Call lookup_existing_concepts(statement_type) for each statement ("income", "balancesheet", "cashflow") to see the company's existing database concepts.
   - Whenever an HTML row matches an existing database concept (by meaning or label), REUSE that exact `concept` tag (e.g. `us-gaap:Revenues`).
   - Only assign a new `us-gaap:<BestStandardTag>` or `custom:<CamelCaseLabel>` when no existing database concept matches that row.
4. For each statement ("income", "balancesheet", "cashflow"), call submit_html_statement(statement_type, unit_multiplier, period_date, items_json).
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
   - Abstract section headers (abstract=true) MUST use a distinct `*Abstract` concept (e.g. `us-gaap:AssetsCurrentAbstract`), never the same concept as a numeric total row (`us-gaap:AssetsCurrent`).
   - If submit_html_statement reports a scale or accounting-identity warning, re-read the table header/values and re-submit if needed.
5. Once all available statements are submitted, call finalize_html_extraction({"status": "done"}).
Never invent numbers — only submit values directly observed in the HTML tables."""


def _normalize_stmt_type(statement_type: str) -> str:
    st = (statement_type or "").strip().lower()
    if st in ("income_statement", "operations", "earnings"):
        return "income"
    if st in ("balance_sheet", "financial_position"):
        return "balancesheet"
    if st in ("cash_flow", "cash_flows"):
        return "cashflow"
    return st


def _load_existing_concepts(
    norm_service: Any,
    cik: Optional[str],
    form_type: Optional[str],
) -> dict[str, list[dict[str, Any]]]:
    """Load existing non-dimensional concepts per statement from normalize_data."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not norm_service or not cik:
        return out
    resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(resolver):
        return out
    try:
        repo = resolver(form_type)
        from normalization.src.data_normalization_service.database.database import (
            _POLLUTED_PATH_RX,
            is_polluted_path,
        )
    except Exception:
        return out

    for st in ("income", "balancesheet", "cashflow"):
        try:
            docs = [
                r for r in repo.collection.find({
                    "cik": str(cik),
                    "statement_type": st,
                    "dimension_concept": False,
                    "path": {"$not": _POLLUTED_PATH_RX},
                })
                if not is_polluted_path(r.get("path"))
            ]
        except Exception:
            docs = []
        docs.sort(key=lambda d: (str(d.get("order_key") or ""), str(d.get("path") or "")))
        out[st] = [
            {
                "concept": d.get("concept"),
                "label": d.get("label"),
                "path": d.get("path"),
                "abstract": bool(d.get("abstract", False)),
            }
            for d in docs
            if d.get("concept")
        ]
    return out


def _check_submitted_sanity(
    st: str,
    mult: float,
    items: list[dict[str, Any]],
) -> list[str]:
    """Lightweight inline scale & accounting-identity checks for HTML submissions."""
    warnings: list[str] = []
    non_eps_vals = [
        abs(float(it["value"]))
        for it in items
        if not it.get("abstract") and it.get("value") is not None and it.get("unit") != "USD/shares"
    ]
    if non_eps_vals:
        med = sorted(non_eps_vals)[len(non_eps_vals) // 2]
        if mult == 1.0 and 0 < med < 10_000:
            warnings.append(
                f"Median monetary value is {med:,.2f} with unit_multiplier=1 — verify the table is not 'in thousands' or 'in millions'."
            )
        if mult >= 1_000_000 and max(non_eps_vals) > 1e14:
            warnings.append(
                f"Max monetary value is {max(non_eps_vals):,.0f} with unit_multiplier={mult:g} — check for double-scaling."
            )

    if st == "balancesheet":
        by_concept = {str(it.get("concept") or ""): it.get("value") for it in items if it.get("value") is not None}
        assets = by_concept.get("us-gaap:Assets")
        liab_eq = by_concept.get("us-gaap:LiabilitiesAndStockholdersEquity")
        if assets is not None and liab_eq is not None and assets != 0:
            diff_pct = abs(assets - liab_eq) / abs(assets)
            if diff_pct > 0.02:
                warnings.append(
                    f"Balance sheet check: Assets ({assets:,.0f}) != LiabilitiesAndStockholdersEquity ({liab_eq:,.0f})."
                )
    return warnings


def extract_statements_from_html_agent(
    *,
    html_text: Optional[str] = None,
    accession_number: Optional[str] = None,
    ticker: Optional[str] = None,
    reporting_period: Optional[dict[str, Any]] = None,
    cik: Optional[str] = None,
    form_type: Optional[str] = None,
    norm_service: Any = None,
    existing_concepts: Optional[dict[str, list[dict[str, Any]]]] = None,
    chat_llm: Any = None,
    on_event: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the ADK HTML Extraction Agent and return ``{stmt_type: [items...]}``."""
    extracted: dict[str, list[dict[str, Any]]] = {}
    concept_catalog = (
        existing_concepts
        if existing_concepts is not None
        else _load_existing_concepts(norm_service, cik, form_type)
    )

    def lookup_existing_concepts(statement_type: str) -> str:
        """Return existing database concepts (concept, label, path, abstract) for a statement."""
        st = _normalize_stmt_type(statement_type)
        if st not in ("income", "balancesheet", "cashflow"):
            return f"Error: statement_type must be 'income', 'balancesheet', or 'cashflow' (got {statement_type!r})."
        rows = concept_catalog.get(st) or []
        if not rows:
            return f"No existing concepts in DB for '{st}'. Assign best us-gaap:<Tag> or custom:<CamelCaseLabel>."
        lines = [f"Existing DB concepts for '{st}' ({len(rows)} total) — reuse exact concept tags when rows match:"]
        for r in rows[:150]:
            tag = r.get("concept") or ""
            lbl = r.get("label") or ""
            abs_flag = " [abstract]" if r.get("abstract") else ""
            lines.append(f"  {tag} | {lbl}{abs_flag}")
        if len(rows) > 150:
            lines.append(f"  ... ({len(rows) - 150} more)")
        return "\n".join(lines)

    def submit_html_statement(
        statement_type: str,
        unit_multiplier: float = 1.0,
        period_date: str = "",
        items_json: str = "[]",
    ) -> str:
        """Record an extracted financial statement from the SEC .htm table."""
        st = _normalize_stmt_type(statement_type)
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
        # Prevent abstract headers from colliding with numeric total rows sharing the same tag.
        numeric_concepts = {
            it["concept"] for it in normalized_items if not it.get("abstract")
        }
        for it in normalized_items:
            if it.get("abstract") and it["concept"] in numeric_concepts:
                if not it["concept"].endswith("Abstract"):
                    it["concept"] = f"{it['concept']}Abstract"
        extracted[st] = normalized_items
        warnings = _check_submitted_sanity(st, mult, normalized_items)
        msg = f"OK — recorded {len(normalized_items)} line item(s) for '{st}' (multiplier={mult})."
        if warnings:
            msg += "\nWARNINGS (verify table header/signs and re-submit if wrong):\n- " + "\n- ".join(warnings)
        return msg

    html_tools = build_html_table_tools(
        html_text=html_text,
        accession_number=accession_number,
        ticker=ticker,
    )
    lookup_tool = StructuredTool.from_function(
        func=lookup_existing_concepts,
        name="lookup_existing_concepts",
        description="Look up existing database concepts (concept, label, path) for 'income', 'balancesheet', or 'cashflow' so matching rows reuse exact DB concept tags.",
    )
    submit_tool = StructuredTool.from_function(
        func=submit_html_statement,
        name="submit_html_statement",
        description="Submit extracted line items for 'income', 'balancesheet', or 'cashflow' from an SEC .htm table.",
    )
    tools = list(html_tools) + [lookup_tool, submit_tool]

    db_counts = {st: len(concept_catalog.get(st) or []) for st in ("income", "balancesheet", "cashflow")}
    db_hint = (
        f" Existing DB concepts available: income={db_counts['income']}, "
        f"balancesheet={db_counts['balancesheet']}, cashflow={db_counts['cashflow']}."
        if any(db_counts.values())
        else " No existing DB concepts found for this company yet."
    )
    task = (
        f"Extract all available primary financial statements (income, balancesheet, cashflow) "
        f"from SEC filing {accession_number or '?'} ({ticker or '?'}) using list_html_tables, "
        f"read_html_table, and lookup_existing_concepts.{db_hint}"
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
        max_steps=14,
        role="html_extract",
    )
    return extracted
