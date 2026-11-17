"""In-place ADK Hierarchy Agent repair for already-stored company hierarchies.

Triggered via ``--fix-hierarchy`` / ``--fix-hi`` on the CLI. Rebuilds
``path``, ``order_key``, ``parent_concept``, and ``parent_concept_id`` on
existing ``normalized_concepts_quarterly`` / ``normalized_concepts_annual``
documents using the ADK Hierarchy Agent + SEC ``.htm`` table tools, without
deleting or re-scraping any values in ``concept_values_*``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.tools import StructuredTool

from ..agent.hierarchy_prompts import AGENTIC_HIERARCHY_SYSTEM_PROMPT
from ..agent.loop import run_agent_loop
from ..tools.html_table_tools import build_html_table_tools
from .hierarchy_agent import _materialize

logger = logging.getLogger(__name__)


def _latest_accession_for(db: Any, cadence: str, cik: str, statement_type: str) -> Optional[str]:
    vcol = db[f"concept_values_{cadence}"]
    doc = vcol.find_one(
        {
            "cik": cik,
            "statement_type": statement_type,
            "accession_number": {"$exists": True, "$ne": None},
        },
        sort=[("reporting_period.fiscal_year", -1), ("reporting_period.quarter", -1)],
        projection={"accession_number": 1},
    )
    return doc.get("accession_number") if doc else None


def fix_company_statement_hierarchy(
    db: Any,
    cik: str,
    ticker: str,
    cadence: str,
    statement_type: str,
    *,
    html_dir: Optional[str] = None,
    chat_llm: Any = None,
) -> dict[str, Any]:
    """Re-run the ADK Hierarchy Agent in place for ``(cik, cadence, statement_type)``."""
    ccol = db[f"normalized_concepts_{cadence}"]
    vcol = db[f"concept_values_{cadence}"]

    stored = list(ccol.find({"cik": cik, "statement_type": statement_type}))
    if not stored:
        return {"updated": 0, "merged": 0, "skipped": True}

    stored.sort(key=lambda r: (str(r.get("order_key") or "z"), str(r.get("path") or "")))
    main_docs = [d for d in stored if not d.get("dimension_concept")]
    dim_docs = [d for d in stored if d.get("dimension_concept")]
    by_concept_main = {d.get("concept"): d for d in main_docs if d.get("concept")}

    proposal: dict[str, Any] = {"rows": None, "dims": None, "merges": {}}

    def propose_hierarchy(rows_json: str, dims_json: str = "[]") -> str:
        """Record the full corrected hierarchy for all stored main rows and dims."""
        try:
            rows = json.loads(rows_json)
            dims = json.loads(dims_json) if dims_json else []
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        if not isinstance(rows, list) or not rows:
            return "Error: rows_json must be a non-empty JSON array."
        proposal["rows"] = rows
        proposal["dims"] = dims if isinstance(dims, list) else []
        return f"OK — recorded {len(rows)} row(s) and {len(proposal['dims'])} dim(s). Call finalize_hierarchy() now."

    def decide_mapping(concept_json: str) -> str:
        """Merge a duplicate stored concept into another stored concept."""
        try:
            parsed = json.loads(concept_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        src = parsed.get("concept")
        tgt = parsed.get("same_as")
        if not src or not tgt or src == tgt:
            return "Error: provide distinct 'concept' and 'same_as' stored concept names."
        if tgt not in by_concept_main:
            return f"Error: '{tgt}' is not in stored main concepts."
        proposal["merges"][src] = tgt
        return f"OK — will merge '{src}' into '{tgt}'."

    accession = _latest_accession_for(db, cadence, cik, statement_type)
    html_tools = build_html_table_tools(
        accession_number=accession,
        ticker=ticker,
        html_dir=html_dir,
    )
    tools = [
        StructuredTool.from_function(
            func=propose_hierarchy,
            name="propose_hierarchy",
            description="Submit the full corrected hierarchy (rows_json, dims_json) with parent and position for every stored concept.",
        ),
        StructuredTool.from_function(
            func=decide_mapping,
            name="decide_mapping",
            description="Optionally merge a duplicate stored concept into another stored concept: {\"concept\": \"...\", \"same_as\": \"...\"}.",
        ),
    ] + list(html_tools)

    main_lines = [
        f"  - {d.get('concept')} | label={d.get('label')!r} | current_parent={d.get('parent_concept')} | abstract={bool(d.get('abstract'))} | order_key={d.get('order_key')}"
        for d in main_docs
    ]
    dim_lines = [
        f"  - {d.get('concept')} | label={d.get('label')!r} | parent_concept={d.get('parent_concept')} | parent_header={d.get('parent_header')} | segment_type={d.get('segment_type')}"
        for d in dim_docs
    ]

    task = (
        f"FIX STORED HIERARCHY for {statement_type} ({cadence}) of {ticker} (CIK {cik}).\n"
        f"Latest filing accession: {accession or 'unknown'}.\n\n"
        f"Stored main/header concepts ({len(main_docs)}):\n" + "\n".join(main_lines) + "\n\n"
        f"Stored dimensional concepts ({len(dim_docs)}):\n" + ("\n".join(dim_lines) if dim_lines else "  (none)") + "\n\n"
        f"Instructions:\n"
        f"1. Call list_html_tables(statement_hint={statement_type!r}) and read_html_table(...) to inspect the human-visible SEC .htm table.\n"
        f"2. Call propose_hierarchy(rows_json, dims_json) placing EVERY stored concept in exact top-down SEC HTML order with accurate parent-child nesting.\n"
        f"   - Do NOT nest a single child under a redundant parent header (only group when a parent genuinely has 2+ children in the statement, or matches the SEC HTML indentation).\n"
        f"   - On Income Statement, keep Revenue, Cost of goods sold, Gross Profit, SG&A (if alone), Operating Income, EBT, Tax, and Net Income at root level.\n"
        f"3. Call finalize_hierarchy({{\"status\": \"done\"}})."
    )

    run_agent_loop(
        AGENTIC_HIERARCHY_SYSTEM_PROMPT,
        task,
        tools,
        ticker=ticker or cik,
        finalize_name="finalize_hierarchy",
        finalize_description="Call once propose_hierarchy has been recorded. Pass {\"status\": \"done\"}.",
        chat_llm=chat_llm,
        max_steps=6,
        role="hierarchy",
    )

    if not proposal["rows"]:
        return {"updated": 0, "merged": 0, "skipped": False, "error": "no proposal"}

    # Apply merges first if any
    merged_count = 0
    for src_c, tgt_c in proposal["merges"].items():
        src_doc = by_concept_main.get(src_c)
        tgt_doc = by_concept_main.get(tgt_c)
        if not src_doc or not tgt_doc:
            continue
        for sv in vcol.find({"cik": cik, "concept_id": src_doc["_id"]}):
            rp = sv.get("reporting_period") or {}
            dup_q: dict[str, Any] = {
                "cik": cik,
                "concept_id": tgt_doc["_id"],
                "dimension_value": bool(sv.get("dimension_value", False)),
                "reporting_period.fiscal_year": rp.get("fiscal_year"),
            }
            if rp.get("quarter") is not None:
                dup_q["reporting_period.quarter"] = rp.get("quarter")
            if vcol.find_one(dup_q):
                vcol.delete_one({"_id": sv["_id"]})
            else:
                vcol.update_one({"_id": sv["_id"]}, {"$set": {"concept_id": tgt_doc["_id"]}})
        aliases = list(dict.fromkeys((tgt_doc.get("aliases") or []) + [src_c]))
        ccol.update_one({"_id": tgt_doc["_id"]}, {"$set": {"aliases": aliases}})
        ccol.delete_one({"_id": src_doc["_id"]})
        merged_count += 1

    mat_rows, mat_dims = _materialize(proposal["rows"], proposal["dims"] or [], {})
    id_by_concept = {d.get("concept"): d["_id"] for d in main_docs if d.get("concept") not in proposal["merges"]}

    updated_count = 0
    for mr in mat_rows:
        c_name = mr.get("concept")
        cid = id_by_concept.get(c_name)
        if not cid:
            continue
        p_name = mr.get("parent")
        p_id = id_by_concept.get(p_name) if p_name else None
        ccol.update_one(
            {"_id": cid},
            {
                "$set": {
                    "path": mr.get("path"),
                    "order_key": mr.get("order_key"),
                    "parent_concept": p_name,
                    "parent_concept_id": p_id,
                    "level": mr.get("level", 0),
                    "hierarchy_level": mr.get("level", 0),
                }
            },
        )
        updated_count += 1

    # Also update dimensional member paths/parents in place
    dim_by_key = {(d.get("parent_concept"), d.get("concept")): d for d in dim_docs}
    for md in mat_dims:
        c_name = md.get("concept")
        p_name = md.get("parent_concept") or md.get("parent")
        d_doc = dim_by_key.get((p_name, c_name))
        if not d_doc:
            continue
        ccol.update_one(
            {"_id": d_doc["_id"]},
            {
                "$set": {
                    "path": md.get("path"),
                    "order_key": md.get("order_key"),
                    "parent_concept": p_name,
                    "parent_concept_id": id_by_concept.get(p_name),
                    "parent_header": md.get("parent_header"),
                    "level": md.get("level", 1),
                    "hierarchy_level": md.get("level", 1),
                }
            },
        )
        updated_count += 1

    return {"updated": updated_count, "merged": merged_count, "skipped": False}
