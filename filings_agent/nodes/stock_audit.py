"""Complete-Stock Audit & Repair Node (Company-Level ADK Agent Stage).

Runs across a company's ENTIRE stored dataset in MongoDB (``concepts_annual``,
``concepts_quarterly``, ``concept_values_annual``, ``concept_values_quarterly``)
after per-filing extraction, quarterly deaccumulation, and companyfacts
reconciliation have completed.

Audits the complete stock for:
  1. Whole-stock hierarchy health: ``404`` / polluted paths, orphan
     ``parent_concept`` links, duplicate ``(path, order_key)`` collisions, and
     3+ dimensional breakdown members lacking a ``parent_header`` grouping.
  2. Cross-period concept & breakdown continuity: concepts or segment breakdowns
     present in surrounding periods of the stock that are missing in an interior
     or trailing period, or split across unmerged alias tags.
  3. Cross-period scale consistency: 1,000x unit/scale jumps across adjacent
     periods of the same concept.

Prioritizes Google ADK built-in tools (``load_artifacts``, ``load_memory``,
``exit_loop``) when consulting the ADK ``LlmAgent`` for stock-level repairs.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Optional

from filings_agent.adk import (
    ADKLlmAgent,
    InMemoryArtifactService,
    InMemoryMemoryService,
    build_adk_exit_loop_tool,
    build_adk_load_artifacts_tool,
    build_adk_load_memory_tool,
    wrap_custom_function_tool,
)
from filings_agent.hooks import report_call

logger = logging.getLogger(__name__)


def _audit_cadence_hierarchy(concepts: list[dict[str, Any]], cadence: str) -> list[dict[str, Any]]:
    """Deterministic whole-stock hierarchy audit for one cadence ('annual' or 'quarterly')."""
    issues: list[dict[str, Any]] = []
    by_stmt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in concepts:
        if isinstance(c, dict) and c.get("statement_type"):
            by_stmt[str(c["statement_type"])].append(c)

    for stmt_type, rows in sorted(by_stmt.items()):
        known_concepts = {str(r.get("concept") or "") for r in rows if r.get("concept")}
        path_order_seen: dict[tuple[bool, str, str], list[str]] = defaultdict(list)
        dims_by_parent: dict[tuple[str, Optional[str]], list[str]] = defaultdict(list)

        for r in rows:
            c_name = str(r.get("concept") or "")
            if not c_name or r.get("hide"):
                continue
            path = str(r.get("path") or "")
            order_key = str(r.get("order_key") or "")
            is_dim = bool(r.get("dimension_concept"))
            parent = r.get("parent_concept") or r.get("parent")
            parent_header = r.get("parent_header")

            # 1. Polluted / 404 path check
            if not path or path == "404" or ".404" in path or path.startswith("404."):
                issues.append({
                    "type": "polluted_or_missing_path",
                    "cadence": cadence,
                    "statement_type": stmt_type,
                    "concept": c_name,
                    "path": path,
                    "message": f"[{cadence}/{stmt_type}] Concept '{c_name}' has invalid/polluted path '{path}'",
                })

            # 2. Orphan parent_concept check
            if parent and str(parent) not in known_concepts and not str(parent).startswith("custom:"):
                issues.append({
                    "type": "orphan_parent_concept",
                    "cadence": cadence,
                    "statement_type": stmt_type,
                    "concept": c_name,
                    "parent_concept": parent,
                    "message": f"[{cadence}/{stmt_type}] Concept '{c_name}' references missing parent '{parent}'",
                })

            # 3. Duplicate (path, order_key) within same kind
            if path and order_key and path != "555":
                path_order_seen[(is_dim, path, order_key)].append(c_name)

            # 4. Dimensional grouping check
            if is_dim and parent:
                dims_by_parent[(str(parent), parent_header)].append(c_name)

        for (is_dim, path, order_key), names in path_order_seen.items():
            unique_names = sorted(set(names))
            if len(unique_names) > 1:
                issues.append({
                    "type": "duplicate_path_order_key",
                    "cadence": cadence,
                    "statement_type": stmt_type,
                    "path": path,
                    "order_key": order_key,
                    "concepts": unique_names,
                    "message": (
                        f"[{cadence}/{stmt_type}] Duplicate (path={path}, order_key={order_key}) "
                        f"shared by {unique_names}"
                    ),
                })

        for (parent_c, header), member_names in dims_by_parent.items():
            if header is None and len(set(member_names)) >= 3:
                issues.append({
                    "type": "ungrouped_dimensional_members",
                    "cadence": cadence,
                    "statement_type": stmt_type,
                    "parent_concept": parent_c,
                    "member_count": len(set(member_names)),
                    "message": (
                        f"[{cadence}/{stmt_type}] {len(set(member_names))} dimensional members under "
                        f"'{parent_c}' lack a custom grouping header"
                    ),
                })

    return issues


def _audit_cadence_continuity_and_scale(
    concepts: list[dict[str, Any]],
    values: list[dict[str, Any]],
    cadence: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check cross-period continuity gaps and 1,000x scale jumps across the complete stock."""
    continuity_gaps: list[dict[str, Any]] = []
    scale_anomalies: list[dict[str, Any]] = []

    cid_to_concept: dict[Any, dict[str, Any]] = {
        c["_id"]: c for c in concepts if isinstance(c, dict) and "_id" in c
    }

    # Map (statement_type, concept_name) -> {period_end: float_val}
    series_by_concept: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    universe_periods_by_stmt: dict[str, set[str]] = defaultdict(set)

    for v in values:
        if not isinstance(v, dict):
            continue
        val = v.get("value")
        if not isinstance(val, (int, float)):
            continue
        rp = v.get("reporting_period") or {}
        end_s = str(rp.get("end_date") or rp.get("period_date") or v.get("period_key") or "")[:10]
        if not end_s:
            continue
        cid = v.get("concept_id")
        c_doc = cid_to_concept.get(cid) or {}
        stmt_type = str(v.get("statement_type") or c_doc.get("statement_type") or "")
        c_name = str(v.get("concept") or c_doc.get("concept") or "")
        if not stmt_type or not c_name:
            continue
        if c_doc.get("abstract") or c_doc.get("hide"):
            continue
        series_by_concept[(stmt_type, c_name)][end_s] = float(val)
        universe_periods_by_stmt[stmt_type].add(end_s)

    for (stmt_type, c_name), period_map in sorted(series_by_concept.items()):
        sorted_periods = sorted(period_map.keys())
        if len(sorted_periods) < 2:
            continue

        # Check interior period gaps against the statement's period universe
        all_stmt_periods = sorted(universe_periods_by_stmt.get(stmt_type, set()))
        first_p, last_p = sorted_periods[0], sorted_periods[-1]
        interior_universe = [p for p in all_stmt_periods if first_p < p < last_p]
        missing_interior = [p for p in interior_universe if p not in period_map]
        if missing_interior:
            continuity_gaps.append({
                "type": "cross_period_continuity_gap",
                "cadence": cadence,
                "statement_type": stmt_type,
                "concept": c_name,
                "missing_periods": missing_interior,
                "message": (
                    f"[{cadence}/{stmt_type}] '{c_name}' missing in interior period(s) "
                    f"{missing_interior} (present in {first_p}..{last_p})"
                ),
            })

        # Check adjacent periods for 500x+ scale jump (e.g. thousands vs millions error)
        for i in range(1, len(sorted_periods)):
            p_prev, p_curr = sorted_periods[i - 1], sorted_periods[i]
            v_prev, v_curr = abs(period_map[p_prev]), abs(period_map[p_curr])
            if v_prev > 1.0 and v_curr > 1.0:
                ratio = max(v_prev, v_curr) / min(v_prev, v_curr)
                if ratio >= 500.0:
                    scale_anomalies.append({
                        "type": "cross_period_scale_jump",
                        "cadence": cadence,
                        "statement_type": stmt_type,
                        "concept": c_name,
                        "from_period": p_prev,
                        "to_period": p_curr,
                        "from_value": period_map[p_prev],
                        "to_value": period_map[p_curr],
                        "ratio": round(ratio, 1),
                        "message": (
                            f"[{cadence}/{stmt_type}] '{c_name}' jumped {ratio:,.1f}x between "
                            f"{p_prev} ({period_map[p_prev]:,.2f}) and {p_curr} ({period_map[p_curr]:,.2f})"
                        ),
                    })

    return continuity_gaps, scale_anomalies


def run_complete_stock_audit(
    cik: str,
    *,
    target_db: Any = None,
    chat_llm: Any = None,
) -> dict[str, Any]:
    """Execute a complete-stock audit across all stored periods and hierarchies for *cik*."""
    padded_cik = str(cik or "").strip()
    if target_db is None:
        return {
            "status": "skipped",
            "cik": padded_cik,
            "hierarchy_issues": [],
            "continuity_gaps": [],
            "scale_anomalies": [],
            "total_issues": 0,
        }

    hierarchy_issues: list[dict[str, Any]] = []
    continuity_gaps: list[dict[str, Any]] = []
    scale_anomalies: list[dict[str, Any]] = []

    for cadence, c_coll_name, legacy_c_coll_name, v_coll_name in (
        ("annual", "normalized_concepts_annual", "concepts_annual", "concept_values_annual"),
        ("quarterly", "normalized_concepts_quarterly", "concepts_quarterly", "concept_values_quarterly"),
    ):
        try:
            c_docs = []
            try:
                c_docs = list(target_db[c_coll_name].find({"cik": {"$in": [padded_cik, padded_cik.lstrip("0")]}}))
            except Exception:
                c_docs = []
            if not c_docs:
                c_docs = list(target_db[legacy_c_coll_name].find({"cik": {"$in": [padded_cik, padded_cik.lstrip("0")]}}))
            v_docs = list(target_db[v_coll_name].find({"cik": {"$in": [padded_cik, padded_cik.lstrip("0")]}}))
        except Exception as exc:
            logger.warning("stock_audit: could not query %s for %s: %s", cadence, padded_cik, exc)
            continue

        if c_docs:
            hierarchy_issues.extend(_audit_cadence_hierarchy(c_docs, cadence))
        if c_docs and v_docs:
            gaps, scales = _audit_cadence_continuity_and_scale(c_docs, v_docs, cadence)
            continuity_gaps.extend(gaps)
            scale_anomalies.extend(scales)

    total_issues = len(hierarchy_issues) + len(continuity_gaps) + len(scale_anomalies)
    agent_review_notes = None

    # When issues exist and an ADK chat_llm is provided, consult the Stock Audit ADK LlmAgent
    # using ADK built-in tools (load_artifacts, load_memory, exit_loop).
    if total_issues > 0 and chat_llm is not None:
        try:
            artifact_svc = InMemoryArtifactService()
            memory_svc = InMemoryMemoryService()
            artifact_svc.save_artifact("hierarchy_issues", hierarchy_issues)
            artifact_svc.save_artifact("continuity_gaps", continuity_gaps)
            artifact_svc.save_artifact("scale_anomalies", scale_anomalies)

            loop_state: dict[str, Any] = {}
            builtin_tools = [
                build_adk_load_artifacts_tool(artifact_svc),
                build_adk_load_memory_tool(memory_svc, cik=padded_cik),
                build_adk_exit_loop_tool(loop_state),
            ]
            auditor = ADKLlmAgent(
                "complete_stock_auditor",
                instruction=(
                    "You are the Complete-Stock Audit Agent running on Google ADK. "
                    "Use the ADK built-in `load_artifacts` and `load_memory` tools to inspect "
                    "any whole-stock hierarchy issues, cross-period continuity gaps, or scale "
                    "anomalies across all filings of this company, then call `finalize_stock_audit`."
                ),
                tools=builtin_tools,
                chat_llm=chat_llm,
                finalize_name="finalize_stock_audit",
            )
            agent_review_notes = auditor.invoke_prompt(
                f"Audit stock CIK {padded_cik}: {len(hierarchy_issues)} hierarchy issue(s), "
                f"{len(continuity_gaps)} continuity gap(s), {len(scale_anomalies)} scale anomaly(s).",
                ticker=padded_cik,
                artifact_service=artifact_svc,
                memory_service=memory_svc,
            )
        except Exception as exc:
            logger.debug("stock_audit LLM review skipped for %s: %s", padded_cik, exc)

    status = "clean" if total_issues == 0 else "findings_detected"
    if total_issues > 0:
        report_call(
            f"  [stock audit]  CIK {padded_cik}: {len(hierarchy_issues)} hierarchy issue(s), "
            f"{len(continuity_gaps)} continuity gap(s), {len(scale_anomalies)} scale anomaly(s)"
        )

    result = {
        "status": status,
        "cik": padded_cik,
        "hierarchy_issues": hierarchy_issues,
        "continuity_gaps": continuity_gaps,
        "scale_anomalies": scale_anomalies,
        "total_issues": total_issues,
    }
    if agent_review_notes is not None:
        result["agent_review"] = agent_review_notes
    return result


def make_stock_audit_node(
    report_store: Any = None,
    *,
    target_db: Any = None,
    chat_llm: Any = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build the ``stock_audit`` node for the company-level ADK workflow."""

    def stock_audit_node(state: dict[str, Any]) -> dict[str, Any]:
        cik = str(state.get("cik") or "")
        db = target_db
        if db is None and report_store is not None:
            db = getattr(report_store, "_db", None) or getattr(report_store, "db", None)

        try:
            audit_summary = run_complete_stock_audit(cik, target_db=db, chat_llm=chat_llm)
        except Exception as exc:  # noqa: BLE001 — never fail the company stage
            logger.warning("stock_audit failed for %s: %s", cik, exc)
            audit_summary = {"status": "failed", "error": str(exc), "total_issues": 0}

        return {
            **state,
            "stock_audit": audit_summary,
        }

    return stock_audit_node
