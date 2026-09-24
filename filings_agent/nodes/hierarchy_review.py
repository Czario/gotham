"""Agent-owned hierarchy review (P16).

Runs straight after the deterministic ``resolve_hierarchy`` pass.  The agent
reads the company's stored tree, decides the *correct* tree for this statement
(grouping headers, correct parents, sibling order, legacy rows to hide) and
proposes it.  Every proposal is put through the deterministic planner, so the
model can never emit a malformed tree — paths and order keys are computed in
code, and an invalid or unavailable proposal leaves the resolver's plan intact.

Trigger: a **seed** (company + statement seen for the first time) or a detected
anomaly.  Clean reuses cost no LLM call, which keeps a multi-thousand-company
backfill affordable.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

from ..hierarchy.planner import _parent_path, order_key, plan_hierarchy
from ..hooks import report_call

logger = logging.getLogger(__name__)

# How many stored rows to show the model per statement.
_MAX_STORED_ROWS = 400


def agent_hierarchy_enabled() -> bool:
    return os.getenv("HIERARCHY_AGENT_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def agent_hierarchy_always() -> bool:
    """True when the agent must decide EVERY statement's hierarchy.

    With this on (the default) the agent is the only author of the tree: clean
    reuses no longer skip the LLM, so no statement's hierarchy is decided by the
    deterministic resolver alone.
    """
    return os.getenv("HIERARCHY_AGENT_ALWAYS", "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def hierarchy_failure_policy() -> str:
    """Behaviour when the agent cannot produce a valid hierarchy.

    ``block``    — persist nothing. No hierarchy ships without agent approval.
    ``fallback`` — keep the deterministic resolver plan (legacy behaviour).
    """
    policy = os.getenv("HIERARCHY_AGENT_FAILURE_POLICY", "block").strip().lower()
    return policy if policy in {"block", "fallback"} else "block"


def should_review(plan: dict, statement_type: Optional[str] = None) -> tuple[bool, str]:
    """Return ``(review?, reason)`` for a deterministic resolution plan.

    Reports *why* a statement needs the agent. The node honours this as a
    trigger, and additionally reviews everything when
    :func:`agent_hierarchy_always` is on — so this returning ``False`` no longer
    means "the deterministic plan will be used".
    """
    seeded = plan.get("seeded_statement_types") or []
    if statement_type is not None:
        if statement_type in seeded:
            return True, f"first filing for {statement_type} (seeding the hierarchy)"
        new_concepts = [
            c for c in (plan.get("new_concepts") or [])
            if not isinstance(c, dict) or not c.get("statement_type") or c.get("statement_type") == statement_type
        ]
        if new_concepts:
            return True, f"{len(new_concepts)} new concept(s) introduced in {statement_type}"
        conflicts = [
            c for c in (plan.get("conflicts") or [])
            if not isinstance(c, dict) or not c.get("statement_type") or c.get("statement_type") == statement_type
        ]
        if conflicts:
            return True, f"{len(conflicts)} path conflict(s) resolved in {statement_type}"
        stmt_integrity = (plan.get("statement_integrity") or {}).get(statement_type)
        if stmt_integrity:
            if stmt_integrity.get("duplicate_paths"):
                return True, f"{stmt_integrity['duplicate_paths']} duplicate path(s) stored in {statement_type}"
            if stmt_integrity.get("orphans"):
                return True, f"{stmt_integrity['orphans']} orphan row(s) stored in {statement_type}"
        elif not plan.get("statement_integrity"):
            integrity = plan.get("integrity") or {}
            if integrity.get("duplicate_paths"):
                return True, f"{integrity['duplicate_paths']} duplicate path(s) stored"
            if integrity.get("orphans"):
                return True, f"{integrity['orphans']} orphan row(s) stored"
        return False, ""

    if seeded:
        return True, "first filing for this statement (seeding the hierarchy)"
    new_concepts = plan.get("new_concepts") or []
    if new_concepts:
        return True, f"{len(new_concepts)} new concept(s) introduced"
    integrity = plan.get("integrity") or {}
    if integrity.get("duplicate_paths"):
        return True, f"{integrity['duplicate_paths']} duplicate path(s) stored"
    if integrity.get("orphans"):
        return True, f"{integrity['orphans']} orphan row(s) stored"
    if plan.get("conflicts"):
        return True, f"{len(plan['conflicts'])} path conflict(s) resolved"
    return False, ""


def _stored_rows(norm_service: Any, bundle: Any) -> list[dict]:
    """Stored rows for this (cik, statement_type) — the tree to review.

    Includes custom grouping headers (``abstract=True``, ``custom:`` prefix)
    so the agent can see the full tree and re-use existing grouping nodes.
    """
    try:
        repo = norm_service._get_concept_repo_by_form_type(bundle.form_type)
        cursor = repo.collection.find(
            {
                "cik": bundle.company_cik,
                "statement_type": bundle.statement_type,
                "dimension_concept": False,
                # Include custom grouping headers (abstract=True, custom: prefix)
                # but still exclude XBRL-standard Abstract wrapper concepts.
                "concept": {"$not": {"$regex": "Abstract$"}},
                "$or": [
                    {"abstract": {"$ne": True}},
                    {"concept": {"$regex": "^custom:"}},
                ],
            },
            {
                "_id": 1, "concept": 1, "label": 1, "path": 1, "order_key": 1,
                "abstract": 1, "dimension": 1, "hide": 1,
            },
        )
        try:
            cursor = cursor.limit(_MAX_STORED_ROWS)
        except AttributeError:   # plain list (tests / alternate collections)
            pass
        docs = list(cursor)
    except Exception as exc:  # noqa: BLE001 — review is best-effort
        logger.warning("hierarchy review: could not read stored rows: %s", exc)
        return []
    return docs



def _filing_concepts(bundle: Any) -> list[dict]:
    rows = []
    for item in (getattr(bundle, "concepts", None) or []) + (
        getattr(bundle, "abstract_concepts", None) or []
    ):
        if isinstance(item, dict) and item.get("concept"):
            concept = item["concept"]
            if concept.split(":")[-1].endswith("Abstract"):
                continue
            is_custom = concept.startswith("custom:")
            if item.get("abstract") and not is_custom:
                continue
            rows.append({
                "concept": concept,
                "label": item.get("label"),
                "abstract": bool(item.get("abstract")),
                "value": item.get("value"),
            })
    return rows


def _apply_plan(bundle: Any, plan, *, stored: list[dict], decided_by: str, norm_service: Any = None) -> dict:
    """Write an approved plan onto the bundle (and record stored-row changes).

    Three cases per planned row:
      * the filing reports it        → set its placement on the bundle;
      * it is a NEW grouping header  → add it to the bundle's abstract rows so the
        normal persist path creates it;
      * it only exists in the DB     → queue an update keyed by ``_id`` (needed
        for re-parenting and for hiding legacy rows).
    """
    stored_by_concept = {
        d.get("concept"): d for d in stored if d.get("concept")
    }
    concrete = getattr(bundle, "concepts", None) or []
    # NB: do not use `or []` here — abstract_concepts is often an EMPTY list,
    # and `[] or []` yields a different object, so appends would be lost.
    abstracts = getattr(bundle, "abstract_concepts", None)
    if abstracts is None:
        abstracts = []
        bundle.abstract_concepts = abstracts
    filing_concepts = {
        item.get("concept")
        for item in list(concrete) + list(abstracts)
        if isinstance(item, dict)
    }

    placed: dict[str, dict] = {}
    stored_updates: list[dict] = []
    created_headers: list[str] = []

    for row in plan.rows:
        placement = {
            "path": row.path,
            "order_key": row.order_key,
            "hierarchy_level": len(row.path.split(".")) - 1 if row.path else 0,
            "hierarchy_source": decided_by,
            "_hierarchy_resolved": True,
        }
        placed[row.concept] = placement

        existing = stored_by_concept.get(row.concept)
        if existing is not None:
            # A stored row must be updated when its placement or visibility
            # changes — the persister reuses concept documents as-is.
            changed = (
                existing.get("path") != row.path
                or existing.get("order_key") != row.order_key
                or bool(existing.get("hide")) != bool(row.hide)
                or bool(existing.get("abstract")) != bool(row.abstract)
            )
            if changed:
                # Never allow a custom grouping header to be demoted to 555
                if (row.abstract or existing.get("abstract")) and row.path == "555":
                    continue
                stored_updates.append({
                    "_id": existing.get("_id"),
                    "concept": row.concept,
                    "path": row.path,
                    "order_key": row.order_key,
                    "hierarchy_level": len(row.path.split(".")) - 1 if row.path else 0,
                    "level": len(row.path.split(".")) - 1 if row.path else 0,
                    "hide": True if row.hide else (False if row.path == "555" else None),
                    "abstract": True if row.abstract else None,
                    "reason": f"agent_hierarchy:{decided_by}",
                })
        elif row.abstract and row.concept not in filing_concepts:
            # A grouping header the filing does not repeat: create it.
            # Generate a human-readable label from the concept name:
            # "custom:GeographicSegmentation" → "Geographic Segmentation"
            raw_suffix = row.concept.split(":")[-1]
            import re as _re
            label = _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", raw_suffix)
            abstracts.append({
                "concept": row.concept,
                "label": label,
                "value": None,
                "period": None,
                "abstract": True,
                **placement,
            })
            created_headers.append(row.concept)


    for item in list(concrete) + list(abstracts):
        if not isinstance(item, dict):
            continue
        placement = placed.get(item.get("concept"))
        if placement:
            item.update(placement)
            item["level"] = placement["hierarchy_level"]

    # Synchronize dimensional concepts with grouping headers and parent line items
    hdr_paths = {
        row.concept: row.path
        for row in plan.rows
        if getattr(row, "abstract", False) and getattr(row, "path", None) and getattr(row, "path", None) != "555"
    }
    for item in abstracts:
        if isinstance(item, dict) and item.get("concept") and item.get("path"):
            hdr_paths.setdefault(item["concept"], item["path"])
    for s in stored:
        if isinstance(s, dict) and s.get("concept") and s.get("path") and s.get("abstract"):
            hdr_paths.setdefault(s["concept"], s["path"])

    dim_concepts = getattr(bundle, "dimensional_concepts", None) or []
    from collections import defaultdict as _defaultdict
    child_counts: dict[str, int] = _defaultdict(int)

    # Initialize child counts from already placed items under each parent path
    for item in list(concrete) + list(abstracts):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        p = item["path"]
        p_parent = _parent_path(p)
        if p_parent and p.startswith(f"{p_parent}."):
            tail = p[len(p_parent) + 1:]
            if tail.isdigit() and "." not in tail:
                child_counts[p_parent] = max(child_counts[p_parent], int(tail))

    for dim in dim_concepts:
        if not isinstance(dim, dict):
            continue
        placement = placed.get(dim.get("concept"))
        if placement:
            dim.update(placement)
            dim["level"] = placement["hierarchy_level"]
            continue

        parent_hdr = dim.get("parent_header")
        parent_concept = dim.get("parent_concept")
        base_p = None

        if parent_hdr and parent_hdr in hdr_paths:
            base_p = hdr_paths[parent_hdr]
        elif parent_concept:
            parent_pl = placed.get(parent_concept)
            if parent_pl and parent_pl.get("path"):
                base_p = parent_pl["path"]
            else:
                parent_doc = next((it for it in concrete if it.get("concept") == parent_concept), None)
                if parent_doc and parent_doc.get("path"):
                    base_p = parent_doc["path"]

        if base_p:
            child_counts[base_p] += 1
            idx = child_counts[base_p]
            dim["path"] = f"{base_p}.{idx:03d}"
            dim["order_key"] = order_key(idx - 1)
            dim["hierarchy_level"] = len(dim["path"].split(".")) - 1
            dim["level"] = dim["hierarchy_level"]
            dim["_hierarchy_resolved"] = True

    # Check if any dimensional concepts are already stored in MongoDB with
    # outdated paths and queue stored_updates so apply_hierarchy_updates fixes
    # them.
    #
    # Dimensional rows are PARENT-SCOPED: the same member (e.g.
    # ``us-gaap:ServiceMember``) hangs under BOTH ``Revenues`` and
    # ``CostOfGoodsAndServicesSold`` and means a different economic fact in
    # each.  Matching stored rows to bundle dims by concept name alone
    # collapsed every parent's copy onto one path, which nested Cost-of-Revenue
    # children under Revenue (and vice-versa).  Match on
    # ``(parent_concept, concept)`` first, then on the dimensional slice
    # signature, and only fall back to a concept-only match when it is
    # unambiguous.
    if norm_service is not None:
        resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
        if callable(resolver):
            try:
                from ..hierarchy.identity import dimension_signature

                def _slice_sig(row: dict) -> str:
                    return dimension_signature(
                        row.get("dimensions"), row.get("dimension_details")
                    )

                id_to_concept = {
                    d.get("_id"): d.get("concept")
                    for d in stored
                    if isinstance(d, dict) and d.get("_id") is not None
                }

                dims_by_parent: dict[tuple, list[dict]] = _defaultdict(list)
                dims_by_parent_sig: dict[tuple, dict] = {}
                dims_by_concept: dict[str, list[dict]] = _defaultdict(list)
                for d in dim_concepts:
                    if not (isinstance(d, dict) and d.get("concept")):
                        continue
                    parent_key = (d.get("parent_concept"), d.get("concept"))
                    dims_by_parent[parent_key].append(d)
                    dims_by_parent_sig[(*parent_key, _slice_sig(d))] = d
                    dims_by_concept[d["concept"]].append(d)

                repo = resolver(getattr(bundle, "form_type", None))
                stored_dims = list(repo.collection.find({
                    "cik": getattr(bundle, "company_cik", None),
                    "statement_type": getattr(bundle, "statement_type", None),
                    "dimension_concept": True,
                }))
                for s_dim in stored_dims:
                    c = s_dim.get("concept")
                    s_parent = s_dim.get("parent_concept") or id_to_concept.get(
                        s_dim.get("concept_id")
                    )
                    matching_dim = dims_by_parent_sig.get((s_parent, c, _slice_sig(s_dim)))
                    if matching_dim is None:
                        candidates = dims_by_parent.get((s_parent, c)) or []
                        if len(candidates) == 1:
                            matching_dim = candidates[0]
                    if matching_dim is None and not s_parent:
                        # Parent linkage unavailable (legacy rows): a concept
                        # match is only safe when the member is unique in this
                        # filing.  When the parent IS known but no bundle row
                        # matches it, leave the stored row alone rather than
                        # re-point it under another parent.
                        candidates = dims_by_concept.get(c) or []
                        if len(candidates) == 1:
                            matching_dim = candidates[0]
                    if not (matching_dim and matching_dim.get("path")):
                        continue
                    new_p = matching_dim["path"]
                    new_o = matching_dim.get("order_key") or "a"
                    if s_dim.get("path") != new_p or s_dim.get("order_key") != new_o:
                        stored_updates.append({
                            "_id": s_dim["_id"],
                            "concept": c,
                            "path": new_p,
                            "order_key": new_o,
                            "hierarchy_level": len(new_p.split(".")) - 1,
                            "level": len(new_p.split(".")) - 1,
                            # Shape parity with the other branch: the caller
                            # and apply_hierarchy_updates both read these keys,
                            # and omitting them raised KeyError: 'hide'.
                            "hide": None,
                            "abstract": None,
                            "reason": f"agent_hierarchy:{decided_by}:dim_sync",
                        })
            except Exception as exc:
                logger.debug("Could not query stored dims for updates: %s", exc)

    return {
        "placed": len(placed),
        "hidden": sum(1 for r in plan.rows if r.hide),
        "grouping_headers": sum(1 for r in plan.rows if r.abstract),
        "created_headers": created_headers,
        "max_depth": plan.max_depth,
        "stored_updates": stored_updates,
    }


def make_hierarchy_review_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
    force: bool = False,
) -> Callable[[dict], dict]:
    """Build the ``hierarchy_review`` node (agent-decided hierarchy)."""

    def hierarchy_review_node(state: dict) -> dict:
        from ..agent.loop import run_agent_loop
        from ..agent.prompts import (
            HIERARCHY_FINALIZE_DESCRIPTION,
            HIERARCHY_SYSTEM_PROMPT,
        )
        from ..agent.tools import build_hierarchy_tools

        bundles = state.get("bundles") or []
        plan = dict(state.get("hierarchy_plan") or {})

        if not agent_hierarchy_enabled():
            report_call("  [hierarchy review]  – agent hierarchy disabled (HIERARCHY_AGENT_ENABLED=0)")
            plan["decided_by"] = plan.get("decided_by") or "policy"
            return {**state, "hierarchy_plan": plan, "status": "hierarchy_resolved"}

        always = agent_hierarchy_always()
        policy = hierarchy_failure_policy()

        reviewed = 0
        agent_plans: list[dict] = []
        # Statements the agent was asked to decide but could not.
        unresolved: list[tuple[str, str]] = []

        for bundle in bundles:
            needs_rev, reason = should_review(plan, statement_type=bundle.statement_type)
            if not (needs_rev or force or always):
                report_call(f"  [hierarchy review]  – skipped {bundle.statement_type}: no review needed")
                continue
            if always and not needs_rev:
                reason = "agent always decides (HIERARCHY_AGENT_ALWAYS)"
            stored = _stored_rows(norm_service, bundle)
            if not stored and not getattr(bundle, "concepts", None):
                # Nothing exists to place: there is no hierarchy for the agent to
                # decide, so this is not a failure.
                report_call(f"  [hierarchy review]  – skipped {bundle.statement_type}: no stored or bundle concepts")
                continue
            proposal: dict = {}
            tools = build_hierarchy_tools(stored, _filing_concepts(bundle), proposal)
            stored_concepts = {r.get("concept") for r in stored if r.get("concept")}
            bundle_concepts = _filing_concepts(bundle)
            bundle_new = [c for c in bundle_concepts if c.get("concept") and c["concept"] not in stored_concepts]
            is_seed = len(stored) == 0

            if is_seed:
                concepts_list = "\n".join(
                    f"  * {c.get('concept')}" + (f" ('{c.get('label')}')" if c.get('label') else "")
                    for c in bundle_concepts
                )
                task_message = (
                    f"Decide the COMPLETE hierarchy for {bundle.statement_type} for CIK "
                    f"{bundle.company_cik} ({bundle.form_type}). No existing hierarchy is stored.\n\n"
                    f"You MUST place ALL {len(bundle_concepts)} concepts reported by this filing in your propose_hierarchy() call:\n"
                    f"{concepts_list}\n\n"
                    f"MANDATORY STRUCTURAL RULES:\n"
                    f"1. Every single concept listed above MUST be included in your propose_hierarchy() JSON array.\n"
                    f"2. For multi-dimensional segment breakdowns (e.g. Revenue broken down by BOTH product members and geographic members), "
                    f"you MUST create custom: grouping headers with 'abstract': true (e.g. custom:ProductSegmentation, custom:GeographicSegmentation), "
                    f"and place each segment member under its corresponding custom header.\n"
                    f"3. Submit the proposal with propose_hierarchy(), validate it with validate_hierarchy(), then call finalize_hierarchy()."
                )
            elif bundle_new:
                new_names = ", ".join(c["concept"] for c in bundle_new[:5])
                if len(bundle_new) > 5:
                    new_names += f" and {len(bundle_new) - 5} more"
                task_message = (
                    f"Review the {bundle.statement_type} hierarchy for CIK {bundle.company_cik} ({bundle.form_type}). "
                    f"Existing stored rows: {len(stored)}. "
                    f"This filing introduces {len(bundle_new)} NEW concept(s): {new_names}. "
                    f"Decide their path and order keys (or parent and position) based on the existing hierarchy. "
                    f"Use place_new_concepts() or propose_hierarchy(), validate it, then finalize."
                )
            else:
                task_message = (
                    f"Review the {bundle.statement_type} hierarchy for CIK {bundle.company_cik} ({bundle.form_type}). "
                    f"Stored rows: {len(stored)}; rows this filing reports: {len(bundle_concepts)}. "
                    f"Fix any defects or anomalies in the tree, propose it, validate it, then finalize."
                )

            try:
                result = run_agent_loop(
                    HIERARCHY_SYSTEM_PROMPT,
                    task_message,
                    tools,
                    ticker=str(state.get("ticker") or state.get("cik") or "?"),
                    finalize_name="finalize_hierarchy",
                    finalize_description=HIERARCHY_FINALIZE_DESCRIPTION,
                    chat_llm=chat_llm,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 — the failure policy decides
                report_call(f"  [hierarchy review]  ✗ exception for {bundle.statement_type}: {exc}")
                logger.warning(
                    "hierarchy review failed for %s/%s: %s",
                    bundle.company_cik, bundle.statement_type, exc,
                )
                unresolved.append((bundle.statement_type, f"agent error: {exc}"))
                continue


            if not isinstance(result, dict):
                report_call(f"  [hierarchy review]  – {bundle.statement_type}: no agent result")
                logger.warning(
                    "hierarchy review: no agent result for %s/%s",
                    bundle.company_cik, bundle.statement_type,
                )
                unresolved.append((bundle.statement_type, "agent returned no result"))
                continue

            raw_proposal_rows = proposal.get("rows")
            if not raw_proposal_rows:
                report_call(
                    f"  [hierarchy review]  ✗ {bundle.statement_type}: agent proposed nothing"
                )
                logger.warning(
                    "hierarchy review: agent proposed no rows for %s/%s",
                    bundle.company_cik, bundle.statement_type,
                )
                unresolved.append((bundle.statement_type, "agent proposed no rows"))
                continue
            approved = plan_hierarchy(
                raw_proposal_rows,
                known_concepts=[r.get("concept") for r in stored],
                stored_rows=stored,
            )
            logger.debug(
                "hierarchy review %s: raw_proposal_rows=%d, valid=%s, errors=%s",
                bundle.statement_type, len(raw_proposal_rows or []), approved.valid, approved.errors,
            )
            if not approved.valid:
                report_call(
                    f"  [hierarchy review]  ✗ {bundle.statement_type}: proposal rejected "
                    f"({'; '.join(approved.errors[:2])})"
                )
                logger.warning(
                    "hierarchy review produced an invalid plan for %s/%s: %s",
                    bundle.company_cik, bundle.statement_type,
                    "; ".join(approved.errors[:3]),
                )
                unresolved.append((
                    bundle.statement_type,
                    f"proposal rejected: {'; '.join(approved.errors[:2])}",
                ))
                continue

            summary = _apply_plan(
                bundle, approved, stored=stored, decided_by="agent", norm_service=norm_service
            )
            report_call(
                f"  [hierarchy review]  ✓ {bundle.statement_type}: {summary['placed']} placed, "
                f"{summary['grouping_headers']} grouping header(s) ({', '.join(summary['created_headers']) or 'none new'})"
            )
            plan.setdefault("existing_updates", []).extend(
                {
                    # ``_id`` is what apply_hierarchy_updates targets; dropping
                    # it silently skipped every stored-row change. Optional fields
                    # are read with .get(): the dim-sync updates legitimately carry
                    # no hide/abstract, and indexing them raised KeyError: 'hide'.
                    "_id": u.get("_id"),
                    "concept": u.get("concept"),
                    "path": u.get("path"),
                    "order_key": u.get("order_key"),
                    "form_type": bundle.form_type,
                    "hide": u.get("hide"),
                    "abstract": u.get("abstract"),
                    "reason": u.get("reason", "agent_hierarchy"),
                }
                for u in summary["stored_updates"]
            )
            agent_plans.append({"statement_type": bundle.statement_type, **summary})
            reviewed += 1
            logger.info(
                "hierarchy review: agent planned %s for %s (%d hidden, %d headers, depth %d)",
                bundle.statement_type, bundle.company_cik,
                summary["hidden"], summary["grouping_headers"], summary["max_depth"],
            )
            if on_event is not None:
                try:
                    on_event("hierarchy_agent_plan", cik=bundle.company_cik,
                             statement_type=bundle.statement_type, **summary)
                except Exception:  # noqa: BLE001
                    pass

        if unresolved and policy == "block":
            # The agent owns the hierarchy: nothing is persisted with a tree it
            # did not decide. The graph short-circuits to END on a failed status,
            # so validate_final / decide / persist never run.
            detail = "; ".join(f"{stmt}: {reason}" for stmt, reason in unresolved)
            report_call(
                f"  [hierarchy review]  ✗ BLOCKED — the agent could not decide "
                f"{len(unresolved)} statement(s); nothing will be written"
            )
            for stmt, reason in unresolved:
                report_call(f"      – {stmt}: {reason[:110]}")
            logger.error(
                "hierarchy review blocked the filing for CIK %s %s — %s",
                state.get("cik"), state.get("accession_number") or "", detail,
            )
            return {
                **state,
                "hierarchy_plan": plan,
                "hierarchy_review_failures": [
                    {"statement_type": stmt, "reason": reason}
                    for stmt, reason in unresolved
                ],
                "status": "failed",
                "error": f"hierarchy not decided by the agent: {detail}",
            }

        if unresolved:
            # policy == "fallback": the deterministic resolver plan stands, but
            # the downgrade is recorded rather than silent.
            report_call(
                f"  [hierarchy review]  ⚠ {len(unresolved)} statement(s) kept the "
                f"deterministic plan (HIERARCHY_AGENT_FAILURE_POLICY=fallback)"
            )
            plan["hierarchy_review_failures"] = [
                {"statement_type": stmt, "reason": reason}
                for stmt, reason in unresolved
            ]

        if reviewed:
            plan["decided_by"] = "agent"
            plan["agent_plans"] = agent_plans
        elif unresolved:
            plan["decided_by"] = "policy_fallback"
        else:
            plan["decided_by"] = plan.get("decided_by") or "policy"
        return {**state, "hierarchy_plan": plan, "status": "hierarchy_reviewed"}

    return hierarchy_review_node
