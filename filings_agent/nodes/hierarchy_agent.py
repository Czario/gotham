"""Unified, agent-owned hierarchy node.

This node replaces resolve_hierarchy + hierarchy_review + concept_resolve with a
single tool-calling loop.  The model pulls the stored tree and the filing tree,
decides merges, proposes the full tree (line items AND dimensional members),
lints its own plan, and finalizes.  A thin executor then only *materializes*
path strings from the model's parent/position choices and annotates the
bundles — it makes no decisions and never overrides the agent.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Optional

from ..agent.hierarchy_prompts import AGENTIC_HIERARCHY_SYSTEM_PROMPT
from ..agent.hierarchy_tools import build_unified_hierarchy_tools
from ..agent.loop import run_agent_loop
from ..hooks import report_call

logger = logging.getLogger(__name__)

_ORDER_CHARS = "abcdefghijklmnopqrstuvwxyz"


def _order_key(position: int) -> str:
    position = max(0, int(position))
    if position < 26:
        return _ORDER_CHARS[position]
    position -= 26
    return _ORDER_CHARS[position // 26 - 1] + _ORDER_CHARS[position % 26]


def _materialize(rows: list[dict], dims: list[dict], stored_paths: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Encode the model's STRUCTURE decisions (parent/position/hide) into path
    strings.  The model's own ``path``/``order_key`` fields are ignored except
    that ``hide`` (or an explicit path "555") means the supplementary bucket —
    so a model can never emit a malformed materialised path."""
    rows = [r for r in rows if isinstance(r, dict) and r.get("concept")]
    by_parent: dict[Optional[str], list[dict]] = defaultdict(list)
    for r in rows:
        by_parent[r.get("parent")].append(r)

    def _pos(r: dict, idx: int) -> int:
        try:
            return int(r.get("position", idx))
        except (TypeError, ValueError):
            return idx

    def _is_555(r: dict) -> bool:
        return bool(r.get("hide")) or str(r.get("path") or "") == "555"

    path_by_concept: dict[str, str] = {}
    used: set[str] = set()

    def _base(parent: Optional[str]) -> str:
        if not parent:
            return ""
        if parent in path_by_concept:
            return path_by_concept[parent]
        if parent in stored_paths:
            return stored_paths[parent]
        return ""

    def _walk(parent: Optional[str]) -> None:
        base = _base(parent)
        children = sorted(
            by_parent.get(parent, []),
            key=lambda r: (_pos(r, 0), str(r.get("concept", ""))),
        )
        for idx, r in enumerate(children):
            if _is_555(r):
                r["path"] = "555"
                r["order_key"] = ""  # filled after all non-555 rows
                r["level"] = 0
            else:
                n = 1
                while True:
                    cand = f"{base}.{n:03d}" if base else f"{n:03d}"
                    if cand not in used:
                        break
                    n += 1
                r["path"] = cand
                r["order_key"] = _order_key(idx)
                r["level"] = len(cand.split(".")) - 1
                used.add(cand)
            path_by_concept.setdefault(r["concept"], str(r["path"]))
            _walk(r["concept"])

    _walk(None)
    # rows whose parent was never proposed/stored become roots
    for r in rows:
        if not r.get("path"):
            n = 1
            while f"{n:03d}" in used:
                n += 1
            r["path"] = f"{n:03d}"
            r["order_key"] = _order_key(0)
            r["level"] = 0
            used.add(r["path"])
            path_by_concept[r["concept"]] = r["path"]

    # supplementary bucket order keys (distinct, deterministic order)
    supp = [r for r in rows if str(r.get("path") or "") == "555"]
    for i, r in enumerate(supp):
        r["order_key"] = _order_key(i)

    def _dim_base(d: dict) -> str:
        # The extracted ``parent_concept`` is authoritative for WHICH line item a
        # member hangs under (the same member exists under Revenue AND
        # CostOfRevenue).  A model-supplied ``parent_header`` only groups members
        # WITHIN that parent, so it is used only when it is consistent (i.e. it
        # is not itself under a different parent).  This prevents the
        # "Revenue's header swallows CostOfRevenue members" defect.
        pc = d.get("parent_concept")
        pc_path = path_by_concept.get(pc) if pc else None
        hdr = d.get("parent_header")
        hdr_path = path_by_concept.get(hdr) if hdr else None
        if hdr_path and pc_path:
            # accept the header only if it is inside the parent's subtree
            if hdr_path == pc_path or hdr_path.startswith(str(pc_path) + "."):
                return hdr_path
            return pc_path
        if pc_path:
            return pc_path
        if hdr_path:
            return hdr_path
        if pc and pc in stored_paths:
            return stored_paths[pc]
        return ""

    used_under: dict[str, int] = defaultdict(int)
    for idx, d in enumerate(dims):
        if not (isinstance(d, dict) and d.get("concept")):
            continue
        base = _dim_base(d)
        n = 1
        while True:
            cand = f"{base}.{n:03d}" if base else f"{n:03d}"
            if cand not in used:
                break
            n += 1
        d["path"] = cand
        d["order_key"] = _order_key(n - 1)
        d["level"] = len(cand.split(".")) - 1
        used.add(cand)
    return rows, dims


def _stored_rows_for(norm_service: Any, bundle: Any) -> tuple[list[dict], dict[str, dict]]:
    """Fetch all stored rows (main + dims) for this cik+statement."""
    resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(resolver):
        return [], {}
    repo = resolver(getattr(bundle, "form_type", None))
    try:
        main = list(repo.collection.find({
            "cik": getattr(bundle, "company_cik", None),
            "statement_type": getattr(bundle, "statement_type", None),
            "dimension_concept": False,
        }))
    except Exception:
        main = []
    try:
        dims = list(repo.collection.find({
            "cik": getattr(bundle, "company_cik", None),
            "statement_type": getattr(bundle, "statement_type", None),
            "dimension_concept": True,
        }))
    except Exception:
        dims = []
    stored = main + dims
    path_by_concept: dict[str, str] = {}
    for s in main:
        if s.get("concept") and s.get("path"):
            path_by_concept[s["concept"]] = str(s["path"])
    return stored, path_by_concept


def _filing_rows_for(bundle: Any) -> list[dict]:
    concrete = list(getattr(bundle, "concepts", None) or [])
    abstracts = list(getattr(bundle, "abstract_concepts", None) or [])

    # path -> concept, so each row's filing parent is visible to the model
    path_to_concept: dict[str, str] = {}
    for item in concrete + abstracts:
        if isinstance(item, dict) and item.get("path"):
            path_to_concept[str(item["path"])] = item.get("concept", "")

    def _parent_of(item: dict) -> Optional[str]:
        path = str(item.get("path") or "")
        if "." not in path:
            return None
        return path_to_concept.get(path.rsplit(".", 1)[0])

    rows = []
    for item in concrete + abstracts:
        if isinstance(item, dict) and item.get("concept"):
            rows.append({
                "concept": item["concept"],
                "label": item.get("label"),
                "abstract": bool(item.get("abstract")),
                "value": item.get("value"),
                "path": item.get("path"),            # filing reference only
                "order_key": item.get("order_key"),
                "parent_concept": _parent_of(item),
                "dimension_concept": False,
            })
    for dim in getattr(bundle, "dimensional_concepts", None) or []:
        if isinstance(dim, dict) and dim.get("concept"):
            rows.append({
                "concept": dim["concept"],
                "label": dim.get("label"),
                "parent_concept": dim.get("parent_concept"),
                "segment_type": dim.get("segment_type"),
                "dimension_concept": True,
            })
    return rows


def make_hierarchy_agent_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
) -> Callable[[dict], dict]:
    """Build the unified hierarchy agent node (single loop, full ownership)."""

    def hierarchy_agent_node(state: dict) -> dict:
        bundles = state.get("bundles") or []
        reasoned_about = 0
        total_updates = 0
        seeded: list[str] = []
        all_promotions: list[dict] = []

        for bundle in bundles:
            stored, stored_paths = _stored_rows_for(norm_service, bundle)
            filing_rows = _filing_rows_for(bundle)
            if not filing_rows and not stored:
                continue
            if not stored:
                seeded.append(getattr(bundle, "statement_type", "?"))

            proposal: dict = {"merges": {}, "rows": [], "dims": []}
            promotions: list[dict] = []
            tools = build_unified_hierarchy_tools(stored, filing_rows, proposal)
            task = (
                f"Decide the COMPLETE hierarchy for {getattr(bundle, 'statement_type', '?')} "
                f"for CIK {getattr(bundle, 'company_cik', '?')} "
                f"({getattr(bundle, 'form_type', '?')}). Stored rows: {len(stored)}. "
                f"Filing rows: {len(filing_rows)}. Merges, then the full tree, then lint, "
                f"then finalize."
            )
            try:
                result = run_agent_loop(
                    AGENTIC_HIERARCHY_SYSTEM_PROMPT,
                    task,
                    tools,
                    ticker=str(state.get("ticker") or state.get("cik") or "?"),
                    finalize_name="finalize_hierarchy",
                    finalize_description="Call when the full hierarchy is decided. Pass a JSON object {\"confirmed\": true, \"notes\": \"...\"}.",
                    chat_llm=chat_llm,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hierarchy agent failed for %s/%s: %s",
                               bundle.company_cik, bundle.statement_type, exc)
                return {**state, "status": "failed", "error": f"hierarchy agent error: {exc}"}

            if not proposal.get("rows"):
                report_call(f"  [hierarchy agent]  ✗ {bundle.statement_type}: no tree proposed")
                return {**state, "status": "failed", "error": "hierarchy agent proposed no tree"}

            rows, dims = _materialize(proposal["rows"], proposal["dims"], stored_paths)

            # (parent_concept, concept) -> (path, order) for dims
            dim_plan = {
                (d.get("parent_concept"), d.get("concept")): (d.get("path"), d.get("order_key"))
                for d in dims if d.get("concept") and d.get("path")
            }

            concrete = list(getattr(bundle, "concepts", None) or [])
            abstracts = getattr(bundle, "abstract_concepts", None)
            if abstracts is None:
                abstracts = []
                bundle.abstract_concepts = abstracts
            plan_by_concept = {r.get("concept"): r for r in rows if r.get("concept")}

            for item in list(concrete) + list(abstracts):
                if not isinstance(item, dict) or not item.get("concept"):
                    continue
                planned = plan_by_concept.get(item["concept"])
                if planned and planned.get("path"):
                    item["path"] = planned["path"]
                    item["order_key"] = planned["order_key"]
                    item["hierarchy_level"] = planned.get("level", 0)
                    item["level"] = planned.get("level", 0)
                    item["_hierarchy_resolved"] = True
                    item["hierarchy_source"] = "agent"

            # Persist any NEW custom grouping headers the agent proposed (the
            # executor adds rows the model chose; it invents nothing).
            import re as _re
            existing_names = {
                i.get("concept") for i in list(concrete) + list(abstracts) if isinstance(i, dict)
            }
            for r in rows:
                c = r.get("concept")
                if not c or c in existing_names or not str(c).startswith("custom:"):
                    continue
                label = _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", c.split(":")[-1])
                abstracts.append({
                    "concept": c,
                    "label": label,
                    "value": None,
                    "period": None,
                    "abstract": True,
                    "path": r.get("path"),
                    "order_key": r.get("order_key"),
                    "hierarchy_level": r.get("level", 0),
                    "level": r.get("level", 0),
                    "_hierarchy_resolved": True,
                    "hierarchy_source": "agent",
                })

            for dim in getattr(bundle, "dimensional_concepts", None) or []:
                if not isinstance(dim, dict):
                    continue
                key = (dim.get("parent_concept"), dim.get("concept"))
                if key in dim_plan:
                    dim["path"], dim["order_key"] = dim_plan[key]
                    dim["hierarchy_level"] = len(str(dim["path"]).split(".")) - 1
                    dim["level"] = dim["hierarchy_level"]
                    dim["_hierarchy_resolved"] = True

            # merges -> attach (stored name wins) or promotion (incoming name wins).
            for concept, m in (proposal.get("merges") or {}).items():
                if not isinstance(m, dict):
                    continue
                same_as = m.get("same_as")
                if not same_as:
                    continue
                if m.get("keep_tag") == "incoming":
                    promotions.append({
                        "cik": getattr(bundle, "company_cik", None),
                        "statement_type": getattr(bundle, "statement_type", None),
                        "form_type": getattr(bundle, "form_type", None),
                        "from_concept": same_as,
                        "to_concept": concept,
                    })
                else:
                    for item in list(concrete) + list(abstracts):
                        if isinstance(item, dict) and item.get("concept") == concept:
                            item["_concept_target"] = same_as
                            break

            # queue stored main-row path updates that the agent changed
            stored_by_concept: dict[str, dict] = {}
            for s in stored:
                if s.get("concept") and not s.get("dimension_concept"):
                    stored_by_concept[s["concept"]] = s
            existing_updates = []
            for concept, planned in plan_by_concept.items():
                sv = stored_by_concept.get(concept)
                if sv and planned.get("path") and (
                    str(sv.get("path", "")) != str(planned["path"])
                    or str(sv.get("order_key", "")) != str(planned.get("order_key", ""))
                ):
                    existing_updates.append({
                        "_id": sv.get("_id"),
                        "concept": concept,
                        "form_type": getattr(bundle, "form_type", None),
                        "path": planned["path"],
                        "order_key": planned.get("order_key"),
                        "hierarchy_level": planned.get("level", 0),
                        "level": planned.get("level", 0),
                        "reason": "agent_hierarchy:unified",
                    })
            total_updates += len(existing_updates)
            all_promotions.extend(promotions)
            reasoned_about += 1
            bundle._agent_updates = existing_updates

        plan = {
            "decided_by": "agent",
            "seeded_statement_types": seeded,
            "existing_updates": [
                u for b in bundles for u in getattr(b, "_agent_updates", [])
            ],
            "resolved_concepts": sum(
                len([i for i in list(getattr(b, "concepts", None) or []) + list(getattr(b, "abstract_concepts", None) or []) if isinstance(i, dict) and i.get("path")])
                for b in bundles
            ),
        }
        report_call(
            f"  [hierarchy agent]  ✓ {reasoned_about} statement(s), {total_updates} stored row update(s)"
        )
        return {**state, "hierarchy_plan": plan, "concept_promotions": all_promotions, "status": "hierarchy_agent_done"}

    return hierarchy_agent_node
