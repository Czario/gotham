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

import time

from ..agent.hierarchy_prompts import AGENTIC_HIERARCHY_SYSTEM_PROMPT
from ..tools.hierarchy_tools import build_unified_hierarchy_tools
from ..agent.loop import run_agent_loop
from ..hooks import report_call, report_detail

logger = logging.getLogger(__name__)

_ORDER_CHARS = "abcdefghijklmnopqrstuvwxyz"


def _order_key(position: int) -> str:
    position = max(0, int(position))
    if position < 26:
        return _ORDER_CHARS[position]
    position -= 26
    return _ORDER_CHARS[position // 26 - 1] + _ORDER_CHARS[position % 26]


def _materialize(rows: list[dict], dims: list[dict], stored_paths: dict[str, str],
                 occupied_paths: Optional[set] = None,
                 identity_paths: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Encode the model's STRUCTURE decisions (parent/position/hide) into path
    strings.  The model's own ``path``/``order_key`` fields are ignored except
    that ``hide`` (or an explicit path "555") means the supplementary bucket —
    so a model can never emit a malformed materialised path.

    ``occupied_paths`` is every path already stored for this company+statement
    (main rows, dimensional members and grouping headers).  It is reserved up
    front so a newly-added row can never steal the slot of an existing row the
    agent did not re-propose; existing concepts additionally reuse their own
    stored path so paths stay stable across filings."""
    norm_rows: list[dict] = []
    norm_dims: list[dict] = []

    import re as _re

    # 1. Collect incoming rows first
    for r in rows:
        if not isinstance(r, dict):
            continue
        item = r.copy()
        c = item.get("concept") or item.get("member")
        if not c:
            continue
        item["concept"] = c
        norm_rows.append(item)

    # 2. Any custom: grouping headers or abstract items proposed in dims belong in rows
    # so they receive row paths and are persisted to the bundle's abstract_concepts.
    for d in dims:
        if not isinstance(d, dict):
            continue
        item = d.copy()
        c = item.get("concept") or item.get("member")
        if not c:
            continue
        item["concept"] = c
        p = item.get("parent") or item.get("parent_concept")
        if str(c).startswith("custom:") or item.get("abstract"):
            item["parent"] = p
            item["abstract"] = True
            norm_rows.append(item)
        else:
            norm_dims.append(item)

    # 3. Synthesize any parent_header referenced in dims (or custom parent) if not already declared in rows
    existing_row_keys: set[tuple[Any, Any]] = {(r.get("parent"), r.get("concept")) for r in norm_rows}
    header_counts: dict[Any, int] = defaultdict(int)
    for d in norm_dims:
        ph = d.get("parent_header")
        pc = d.get("parent_concept") or d.get("parent")
        if not ph and d.get("parent") and str(d.get("parent")).startswith("custom:"):
            ph = d.get("parent")
        if ph and str(ph).startswith("custom:"):
            # A member whose parent IS the header needs no synthesis; only
            # synthesize when we know the real (non-header) line item it hangs
            # under.  Otherwise the header would parent itself (infinite walk).
            if not pc or str(pc).startswith("custom:"):
                continue
            key = (pc, ph)
            if key not in existing_row_keys:
                existing_row_keys.add(key)
                header_counts[pc] += 1
                label = _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(ph).split(":")[-1])
                norm_rows.append({
                    "concept": ph,
                    "parent": pc,
                    "abstract": True,
                    "label": label,
                    "order": header_counts[pc],
                })

    # De-duplicate rows by (parent, concept) while preserving order
    seen_row_keys: set[tuple[Any, Any]] = set()
    deduped_rows: list[dict] = []
    for r in norm_rows:
        key = (r.get("parent"), r["concept"])
        if key not in seen_row_keys:
            seen_row_keys.add(key)
            deduped_rows.append(r)
    norm_rows = deduped_rows

    # Map headers to their parents so members know their line item parent
    header_parents: dict[str, str] = {}
    for r in norm_rows:
        if str(r.get("concept", "")).startswith("custom:") and r.get("parent"):
            header_parents[str(r["concept"])] = str(r["parent"])

    for d in norm_dims:
        p = d.get("parent")
        pc = d.get("parent_concept")
        ph = d.get("parent_header")
        if p and str(p).startswith("custom:"):
            d["parent_header"] = p
            d["parent_concept"] = pc or header_parents.get(str(p))
        elif p and not pc:
            d["parent_concept"] = p
        elif ph and not pc and str(ph) in header_parents:
            d["parent_concept"] = header_parents[str(ph)]

    by_parent: dict[Optional[str], list[dict]] = defaultdict(list)
    for r in norm_rows:
        by_parent[r.get("parent")].append(r)

    row_seq = {id(r): idx for idx, r in enumerate(norm_rows)}

    def _pos(r: dict, default_idx: int) -> int:
        for k in ("position", "order", "index"):
            val = r.get(k)
            if val is not None:
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, str):
                    parts = val.split(".")
                    try:
                        return int(parts[-1])
                    except (ValueError, TypeError):
                        pass
        return default_idx

    def _is_555(r: dict) -> bool:
        return bool(r.get("hide")) or str(r.get("path") or "") == "555"

    path_by_concept: dict[str, str] = {}
    path_by_parent_and_concept: dict[tuple[Optional[str], str], str] = {}
    # Reserve every stored path, then let a re-proposed concept reclaim its own
    # (tracked in ``claimed``) so it is not renumbered just because a sibling
    # was inserted before it.
    used: set[str] = {str(p) for p in (occupied_paths or set()) if p}
    claimed: set[str] = set()

    def _base(parent: Optional[str]) -> str:
        if not parent:
            return ""
        if parent in path_by_concept:
            return path_by_concept[parent]
        if parent in stored_paths:
            return stored_paths[parent]
        return ""

    def _walk(parent: Optional[str], ancestors: frozenset = frozenset()) -> None:
        base = _base(parent)
        children = sorted(
            by_parent.get(parent, []),
            key=lambda r: (_pos(r, row_seq.get(id(r), 0)), row_seq.get(id(r), 0)),
        )
        for idx, r in enumerate(children):
            if _is_555(r):
                r["path"] = "555"
                r["order_key"] = ""  # filled after all non-555 rows
                r["level"] = 0
            else:
                stored = stored_paths.get(r["concept"]) if stored_paths else None
                if stored is None and identity_paths:
                    stored = identity_paths.get((r["concept"], r.get("parent"), False))
                if (
                    stored
                    and stored != "555"
                    and stored not in claimed
                    and (not base or str(stored).startswith(f"{base}."))
                ):
                    cand = str(stored)
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
                claimed.add(cand)
            path_by_concept.setdefault(r["concept"], str(r["path"]))
            path_by_parent_and_concept[(r.get("parent"), r["concept"])] = str(r["path"])
            if r["concept"] not in ancestors:
                _walk(r["concept"], ancestors | {r["concept"]})

    _walk(None)
    # rows whose parent was never proposed/stored become roots
    for r in norm_rows:
        if not r.get("path"):
            n = 1
            while f"{n:03d}" in used:
                n += 1
            r["path"] = f"{n:03d}"
            r["order_key"] = _order_key(n - 1)
            r["level"] = 0
            used.add(r["path"])
            path_by_concept[r["concept"]] = r["path"]
            path_by_parent_and_concept[(r.get("parent"), r["concept"])] = r["path"]

    # supplementary bucket order keys (distinct, deterministic order)
    supp = [r for r in norm_rows if str(r.get("path") or "") == "555"]
    for i, r in enumerate(supp):
        r["order_key"] = _order_key(i)

    def _dim_base(d: dict) -> str:
        # The extracted ``parent_concept`` is authoritative for WHICH line item a
        # member hangs under. Try specific (parent_concept, header) path first.
        pc = d.get("parent_concept")
        hdr = d.get("parent_header")
        if pc and hdr and (pc, hdr) in path_by_parent_and_concept:
            return path_by_parent_and_concept[(pc, hdr)]
        pc_path = path_by_concept.get(pc) if pc else None
        hdr_path = path_by_concept.get(hdr) if hdr else None
        if hdr_path and pc_path:
            # accept the header only if it is inside the parent's subtree
            if hdr_path == pc_path or hdr_path.startswith(str(pc_path) + "."):
                return hdr_path
            return pc_path
        if hdr_path:
            return hdr_path
        if pc_path:
            return pc_path
        if pc and pc in stored_paths:
            return stored_paths[pc]
        return ""

    dim_seq = {id(d): idx for idx, d in enumerate(norm_dims)}
    sorted_dims = sorted(
        norm_dims,
        key=lambda d: (_dim_base(d), _pos(d, dim_seq.get(id(d), 0)), dim_seq.get(id(d), 0)),
    )
    dim_counters: dict[str, int] = defaultdict(int)
    for d in sorted_dims:
        base = _dim_base(d)
        # Reuse a stored member path so re-runs do not drift it to a new slot
        # (the path is already in ``used``, so only its own row may reclaim it).
        stored_dim = (identity_paths or {}).get((
            d.get("concept"), d.get("parent_concept"), True, d.get("parent_header")
        ))
        if (
            stored_dim
            and stored_dim != "555"
            and stored_dim not in claimed
            and (not base or str(stored_dim).startswith(f"{base}."))
        ):
            cand = str(stored_dim)
            pos_idx = len(cand.split(".")) - 1
        else:
            dim_counters[base] += 1
            pos_idx = dim_counters[base]
            cand = f"{base}.{pos_idx:03d}" if base else f"{pos_idx:03d}"
            while cand in used:
                dim_counters[base] += 1
                pos_idx = dim_counters[base]
                cand = f"{base}.{pos_idx:03d}" if base else f"{pos_idx:03d}"
        d["path"] = cand
        d["order_key"] = _order_key(pos_idx - 1)
        d["level"] = len(cand.split(".")) - 1
        used.add(cand)
        claimed.add(cand)

    return norm_rows, sorted_dims


def _stored_rows_for(norm_service: Any, bundle: Any) -> tuple[list[dict], dict[str, str], set, dict]:
    """Fetch all stored rows (main + dims) for this cik+statement.

    Returns ``(stored, path_by_concept, occupied_paths, identity_paths)``.
    ``occupied_paths`` is EVERY materialised path already stored so the
    materialiser never hands an existing slot to a newly-added row, and
    ``identity_paths`` maps ``(concept, parent_concept, dimension_concept)`` to
    its stored path so an existing row reclaims its own slot on reprocessing.
    """
    resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(resolver):
        return [], {}, set(), {}
    repo: Any = resolver(getattr(bundle, "form_type", None))
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
            path_by_concept.setdefault(s["concept"], str(s["path"]))
    occupied = {
        str(s["path"]) for s in stored
        if s.get("path") and str(s["path"]) != "555"
    }
    identity_paths: dict[tuple, str] = {}
    for s in stored:
        if not s.get("concept") or not s.get("path"):
            continue
        # Include parent_header in key so dim members routed through a custom grouping
        # header (e.g. custom:ProductSegmentation) are stored separately from the same
        # member placed directly under the parent concept.
        key = (
            s["concept"],
            s.get("parent_concept"),
            bool(s.get("dimension_concept")),
            s.get("parent_header"),  # None for non-header dims
        )
        identity_paths.setdefault(key, str(s["path"]))
    return stored, path_by_concept, occupied, identity_paths


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
            stmt_t0 = time.perf_counter()
            stmt_type = getattr(bundle, "statement_type", "?")
            stored, stored_paths, occupied_paths, identity_paths = _stored_rows_for(norm_service, bundle)
            filing_rows = _filing_rows_for(bundle)
            if not filing_rows and not stored:
                continue
            if not stored:
                seeded.append(stmt_type)

            # ── Fast-path: skip LLM when all filing concepts are already stored ─────
            # Compute the diff deterministically. If there are no new concepts,
            # stored paths are authoritative and we bypass the LLM entirely.
            proposal: dict = {"merges": {}, "rows": [], "dims": []}
            stored_names = {r.get("concept") for r in stored if r.get("concept")}
            filing_names = {r.get("concept") for r in filing_rows if r.get("concept")}
            new_concepts = filing_names - stored_names
            if stored and not new_concepts:
                # All concepts are known — reuse stored paths verbatim.
                stmt_dur = time.perf_counter() - stmt_t0
                report_call(
                    f"  [hierarchy]  • {stmt_type}: ⚡ fast-path (0 new concepts — reusing stored hierarchy)  {stmt_dur:.2f}s"
                )
                report_detail(f"{stmt_type}: fast-path")
                proposal["rows"] = [
                    {
                        "concept": r["concept"],
                        "parent": r.get("parent_concept"),
                        "label": r.get("label"),
                        "abstract": r.get("abstract", False),
                    }
                    for r in stored if not r.get("dimension_concept") and r.get("concept")
                ]
                proposal["dims"] = [
                    {
                        "concept": r["concept"],
                        "parent_concept": r.get("parent_concept"),
                        "label": r.get("label"),
                        "segment_type": r.get("segment_type"),
                        "parent_header": r.get("parent_header"),
                    }
                    for r in stored if r.get("dimension_concept") and r.get("concept")
                ]
            else:
                # New concepts found (or fresh seed) — invoke the LLM agent.
                if not stored:
                    report_call(
                        f"  [hierarchy]  • {stmt_type}: fresh seed (0 stored) → running hierarchy agent"
                    )
                else:
                    report_call(
                        f"  [hierarchy]  • {stmt_type}: {len(new_concepts)} new concept(s) detected ({len(stored)} stored) → running hierarchy agent"
                    )
                report_detail(f"{stmt_type}: running agent...")

                tools = build_unified_hierarchy_tools(
                    stored,
                    filing_rows,
                    proposal,
                    statement_type=getattr(bundle, "statement_type", ""),
                    form_type=getattr(bundle, "form_type", ""),
                    company_cik=getattr(bundle, "company_cik", ""),
                )
                if not stored:
                    task = (
                        f"FRESH SEED for {getattr(bundle, 'statement_type', '?')} "
                        f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                        f"No existing hierarchy is stored in DB. Inspect filing line items via query_filing_hierarchy(), "
                        f"then call propose_hierarchy(rows_json, dims_json) using the universal statement blueprint, "
                        f"and finalize."
                    )
                else:
                    task = (
                        f"INCREMENTAL UPDATE for {getattr(bundle, 'statement_type', '?')} "
                        f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                        f"Stored rows: {len(stored)}, Filing rows: {len(filing_rows)}. "
                        f"Call query_hierarchy_diff() to inspect the delta between filing and stored tree. "
                        f"If any new concept is an alias, call decide_mapping(). "
                        f"Then call propose_hierarchy(rows_json, dims_json) specifying the new concepts to insert "
                        f"(they will automatically merge with stored rows), and finalize."
                    )
                try:
                    _ = run_agent_loop(
                        AGENTIC_HIERARCHY_SYSTEM_PROMPT,
                        task,
                        tools,
                        ticker=str(state.get("ticker") or state.get("cik") or "?"),
                        finalize_name="finalize_hierarchy",
                        finalize_description="Call when the full hierarchy is decided. Pass {\"status\": \"done\"}.",
                        chat_llm=chat_llm,
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hierarchy agent failed for %s/%s: %s",
                                   bundle.company_cik, bundle.statement_type, exc)

            if not proposal.get("rows"):
                report_call(f"  [hierarchy]  ! {bundle.statement_type}: no tree proposed, using fallback")
                logger.warning("hierarchy agent proposed no tree for %s/%s; falling back to filing rows",
                               bundle.company_cik, bundle.statement_type)
                if stored:
                    proposal["rows"] = [
                        {"concept": s["concept"], "parent": s.get("parent_concept"), "label": s.get("label")}
                        for s in stored if not s.get("dimension_concept")
                    ]
                    stored_concepts = {s["concept"] for s in stored}
                    for r in filing_rows:
                        if not r.get("dimension_concept") and r["concept"] not in stored_concepts:
                            proposal["rows"].append({
                                "concept": r["concept"],
                                "parent": r.get("parent_concept"),
                                "label": r.get("label"),
                            })
                    proposal["dims"] = [
                        {"concept": s["concept"], "parent_concept": s.get("parent_concept"), "label": s.get("label"), "segment_type": s.get("segment_type")}
                        for s in stored if s.get("dimension_concept")
                    ]
                    for r in filing_rows:
                        if r.get("dimension_concept") and r["concept"] not in stored_concepts:
                            proposal["dims"].append({
                                "concept": r["concept"],
                                "parent_concept": r.get("parent_concept"),
                                "label": r.get("label"),
                                "segment_type": r.get("segment_type"),
                            })
                else:
                    proposal["rows"] = [
                        {"concept": r["concept"], "parent": r.get("parent_concept"), "label": r.get("label")}
                        for r in filing_rows if not r.get("dimension_concept")
                    ]
                    proposal["dims"] = [
                        {"concept": r["concept"], "parent_concept": r.get("parent_concept"), "label": r.get("label"), "segment_type": r.get("segment_type")}
                        for r in filing_rows if r.get("dimension_concept")
                    ]

            # Reserve every already-stored path so a new row can never steal an
            # existing slot — except the paths of concepts this filing MERGES
            # into, which the incoming tag legitimately takes over.
            merge_targets = {
                m.get("same_as")
                for m in (proposal.get("merges") or {}).values()
                if isinstance(m, dict) and m.get("same_as")
            }
            reserved_paths = set(occupied_paths)
            for target in merge_targets:
                target_path = stored_paths.get(str(target))
                if target_path:
                    reserved_paths.discard(str(target_path))

            rows, dims = _materialize(
                proposal["rows"], proposal["dims"], stored_paths,
                occupied_paths=reserved_paths,
                identity_paths=identity_paths,
            )

            # (parent_concept, concept) -> (path, order, parent_header) for dims
            # Key by (parent_concept, concept) since the incoming filing bundle dims
            # only have this identity. The agent's proposal provides the parent_header.
            dim_plan = {
                (d.get("parent_concept"), d.get("concept")): (
                    d.get("path"), d.get("order_key"), d.get("parent_header")
                )
                for d in dims if d.get("concept") and d.get("path")
            }

            concrete = list(getattr(bundle, "concepts", None) or [])
            abstracts = getattr(bundle, "abstract_concepts", None)
            if abstracts is None:
                abstracts = []
                bundle.abstract_concepts = abstracts
            plan_by_concept = {r.get("concept"): r for r in rows if r.get("concept")}
            # Grouping headers repeat by name across parents, so they must be
            # resolved by (concept, parent) instead of name alone.
            plan_header_by_parent = {
                (r.get("concept"), r.get("parent")): r
                for r in rows if r.get("concept") and str(r["concept"]).startswith("custom:")
            }

            for item in list(concrete) + list(abstracts):
                if not isinstance(item, dict) or not item.get("concept"):
                    continue
                if str(item["concept"]).startswith("custom:"):
                    planned = plan_header_by_parent.get((item["concept"], item.get("parent_concept")))
                else:
                    target = item.get("_concept_target") or item["concept"]
                    planned = plan_by_concept.get(target)
                if planned and planned.get("path"):
                    item["path"] = planned["path"]
                    item["order_key"] = planned["order_key"]
                    item["hierarchy_level"] = planned.get("level", 0)
                    item["level"] = planned.get("level", 0)
                    item["parent_concept"] = planned.get("parent")
                    item["_hierarchy_resolved"] = True
                    item["hierarchy_source"] = "agent"
                else:
                    # Clear original linkbase paths if the concept was dropped by the agent
                    item["path"] = None
                    item["order_key"] = None

            # Persist any NEW custom grouping headers the agent proposed (the
            # executor adds rows the model chose; it invents nothing).
            import re as _re
            concrete_concepts_set = {i.get("concept") for i in list(concrete) if isinstance(i, dict) and i.get("concept")}
            existing_abstract_keys = {
                (i.get("parent_concept"), i.get("concept"), str(i.get("path") or ""))
                for i in list(abstracts) if isinstance(i, dict)
            }
            for r in rows:
                c = r.get("concept")
                if not c or c in concrete_concepts_set:
                    continue
                path = str(r.get("path") or "")
                parent = r.get("parent")
                key = (parent, c, path)
                if key in existing_abstract_keys:
                    continue
                existing_abstract_keys.add(key)
                label = r.get("label") or _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(c).split(":")[-1])
                abstracts.append({
                    "concept": c,
                    "label": label,
                    "value": None,
                    "period": None,
                    "abstract": True,
                    "parent_concept": parent,
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
                
                target = dim.get("_concept_target") or dim.get("concept")
                # The agent might have also changed the parent_concept during the merge,
                # but the simplest is to just look up by target concept. Wait, dim_plan is keyed by (parent, concept).
                # If we don't know the new parent, we might not find it. But we can search for the concept.
                entry = dim_plan.get((dim.get("parent_concept"), target))
                if not entry:
                    # Fallback: search dim_plan for ANY entry matching the target concept
                    for (p, c), e in dim_plan.items():
                        if c == target:
                            entry = e
                            break
                if entry:
                    dim["path"], dim["order_key"], dim["parent_header"] = entry
                    dim["hierarchy_level"] = len(str(dim["path"]).split(".")) - 1
                    dim["level"] = dim["hierarchy_level"]
                    dim["_hierarchy_resolved"] = True
                else:
                    dim["path"] = None
                    dim["order_key"] = None

            # merges -> attach (stored name wins) or promotion (incoming name wins).
            promotions: list[dict] = []
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
            stored_header_by_parent: dict[tuple, dict] = {}
            for s in stored:
                if not s.get("concept") or s.get("dimension_concept"):
                    continue
                if str(s["concept"]).startswith("custom:"):
                    stored_header_by_parent[(s["concept"], s.get("parent_concept"))] = s
                else:
                    stored_by_concept[s["concept"]] = s
            existing_updates = []
            for r in rows:
                concept = r.get("concept")
                if not concept:
                    continue
                if str(concept).startswith("custom:"):
                    sv = stored_header_by_parent.get((concept, r.get("parent")))
                else:
                    sv = stored_by_concept.get(str(concept))
                if sv and r.get("path") and (
                    str(sv.get("path", "")) != str(r["path"])
                    or str(sv.get("order_key", "")) != str(r.get("order_key", ""))
                ):
                    existing_updates.append({
                        "_id": sv.get("_id"),
                        "concept": concept,
                        "form_type": getattr(bundle, "form_type", None),
                        "path": r["path"],
                        "order_key": r.get("order_key"),
                        "hierarchy_level": r.get("level", 0),
                        "level": r.get("level", 0),
                        "parent_concept": r.get("parent"),
                        "reason": "agent_hierarchy:unified",
                    })
            total_updates += len(existing_updates)
            all_promotions.extend(promotions)
            reasoned_about += 1
            bundle._agent_updates = existing_updates
            if not (stored and not new_concepts):
                stmt_dur = time.perf_counter() - stmt_t0
                report_call(
                    f"  [hierarchy]  ✓ {stmt_type} hierarchy resolved in {stmt_dur:.1f}s ({len(rows)} rows, {len(existing_updates)} re-pathed)"
                )
                report_detail(f"{stmt_type}: done")

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
            f"  [hierarchy]  ✓ {reasoned_about} statement(s) processed ({total_updates} stored row update(s))"
        )
        return {**state, "hierarchy_plan": plan, "concept_promotions": all_promotions, "status": "hierarchy_agent_done"}

    return hierarchy_agent_node
