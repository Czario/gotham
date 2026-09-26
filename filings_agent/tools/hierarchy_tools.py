"""Tools for the unified hierarchy agent.

Tools are read/propose-only. They never write MongoDB and never override the
agent's decisions — ``lint_hierarchy`` is advisory only; the agent chooses
whether to act on it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from langchain_core.tools import tool


def fmt_row(row: dict) -> str:
    flags = []
    if row.get("abstract"):
        flags.append("abstract")
    if row.get("hide") or str(row.get("path") or "") == "555":
        flags.append("supplementary/555")
    if row.get("dimension_concept"):
        flags.append("dim")
    if row.get("parent_concept"):
        flags.append(f"parent={row['parent_concept']}")
    if row.get("parent_header"):
        flags.append(f"parent_header={row['parent_header']}")
    if row.get("segment_type"):
        flags.append(f"segment={row['segment_type']}")
    flag = f"  [{','.join(flags)}]" if flags else ""
    return (
        f"{str(row.get('path') or '-'):<14} {str(row.get('order_key') or '-'):<4} "
        f"{row.get('concept','?')}"
        + (f" ({row.get('label')})" if row.get("label") else "")
        + flag
    )


# Alias with underscore for backwards compatibility
_fmt_row = fmt_row


def build_unified_hierarchy_tools(
    stored_rows: list[dict],
    filing_rows: list[dict],
    proposal: dict,
    *,
    statement_type: str = "",
    form_type: str = "",
    company_cik: str = "",
    stored_paths: dict | None = None,
    occupied_paths: set | None = None,
    identity_paths: dict | None = None,
    identity_order_keys: dict | None = None,
    materialize: Any = None,
) -> list[Any]:
    """Build the hierarchy agent's toolset.

    ``proposal`` is a mutable dict the tools write decisions / rows / dims into.
    ``materialize`` (plus the stored-identity maps) lets ``preview_hierarchy``
    encode the proposal into the exact paths/order_keys that would be written,
    so the agent checks the real result before finalizing — nothing is encoded
    after the loop.
    """
    stored_names = {r.get("concept") for r in stored_rows if r.get("concept")}
    filing_names = {r.get("concept") for r in filing_rows if r.get("concept")}
    known_names = stored_names | filing_names

    cadence = "Annual (10-K/20-F)" if form_type in ("10-K", "20-F") else "Quarterly (10-Q)"
    scope_banner = f"[{company_cik or '?'} | {statement_type.upper() or 'STATEMENT'} | {cadence}]"

    @tool
    def query_stored_hierarchy() -> str:
        """Show the FULL existing stored tree for this company+statement (all
        periods), including dimensional members. Read this first."""
        if not stored_rows:
            return f"NO STORED HIERARCHY for {scope_banner} (this is a fresh seed). Build the whole tree from query_filing_hierarchy()."
        dims = [r for r in stored_rows if r.get("dimension_concept")]
        main = [r for r in stored_rows if not r.get("dimension_concept")]

        def _parent_of_path(p: Any) -> str:
            p = str(p or "")
            return p.rsplit(".", 1)[0] if "." in p else ""

        # Sibling order comes from order_key (path numbers may not match the
        # visual order after a mid-tree insertion), so sort by parent + order_key.
        main.sort(key=lambda r: (
            _parent_of_path(r.get("path")),
            str(r.get("order_key") or ""),
            str(r.get("path") or ""),
        ))
        dims.sort(key=lambda r: (str(r.get("parent_concept") or ""), str(r.get("concept") or "")))
        return (
            f"STORED MAIN ROWS {scope_banner} ({len(main)}):\n" + "\n".join(fmt_row(r) for r in main)
            + f"\n\nSTORED DIMENSIONAL MEMBERS ({len(dims)}):\n"
            + "\n".join(fmt_row(r) for r in dims)
        )

    @tool
    def query_filing_hierarchy() -> str:
        """Show this filing's extracted line items and dimensional members."""
        main = [r for r in filing_rows if not r.get("dimension_concept")]
        dims = [r for r in filing_rows if r.get("dimension_concept")]
        return (
            f"FILING LINE ITEMS {scope_banner} ({len(main)}):\n" + "\n".join(fmt_row(r) for r in main)
            + f"\n\nFILING DIMENSIONAL MEMBERS ({len(dims)}):\n"
            + "\n".join(fmt_row(r) for r in dims)
        )

    @tool
    def query_hierarchy_diff() -> str:
        """Compare this filing's concepts against the stored database hierarchy.

        Returns:
          - MATCHED: filing concepts already present in stored DB tree (reuse their parent and placement).
          - NEW INCOMING: concepts in this filing not yet in DB (must either merge via decide_mapping or insert into propose_hierarchy).
          - STORED GROUPING HEADERS: custom headers already in DB (reuse them in parent_header / rows).
          - STORED ABSENT: concepts in DB but not in this specific filing.
        """
        if not stored_rows:
            return f"NO STORED HIERARCHY for {scope_banner} (this is a fresh seed). All filing concepts are new. Build the whole tree from query_filing_hierarchy()."

        stored_main_map = {r.get("concept"): r for r in stored_rows if not r.get("dimension_concept") and r.get("concept")}
        stored_dims_map = {r.get("concept"): r for r in stored_rows if r.get("dimension_concept") and r.get("concept")}
        stored_headers = [r for r in stored_rows if str(r.get("concept", "")).startswith("custom:")]

        filing_main = [r for r in filing_rows if not r.get("dimension_concept") and r.get("concept")]
        filing_dims = [r for r in filing_rows if r.get("dimension_concept") and r.get("concept")]

        matched_main = [r for r in filing_main if r["concept"] in stored_main_map]
        matched_dims = [r for r in filing_dims if r["concept"] in stored_dims_map]
        new_main = [r for r in filing_main if r["concept"] not in stored_main_map]
        new_dims = [r for r in filing_dims if r["concept"] not in stored_dims_map]

        all_filing_concepts = {r.get("concept") for r in filing_rows if r.get("concept")}
        stored_absent = [r for r in stored_rows if r.get("concept") not in all_filing_concepts]

        lines = [
            f"=== HIERARCHY DELTA {scope_banner} ===",
            f"SUMMARY: {len(matched_main) + len(matched_dims)} matched, {len(new_main)} new line item(s), {len(new_dims)} new dim member(s), {len(stored_absent)} stored absent.\n",
        ]

        if stored_headers:
            lines.append("EXISTING STORED GROUPING HEADERS (reuse these exact names):")
            for h in stored_headers:
                lines.append(f"  {fmt_row(h)}")
            lines.append("")

        if new_main or new_dims:
            lines.append(f"NEW INCOMING CONCEPTS ({len(new_main) + len(new_dims)}) — REQUIRE DECISION (merge or insert):")
            for r in new_main:
                c = str(r.get("concept", ""))
                q = c.split(":")[-1].lower()
                cands = [sr.get("concept") for sr in stored_rows if q in str(sr.get("concept", "")).lower()][:3]
                cand_str = f" -> candidates to merge with: {cands}" if cands else " -> new line item to place"
                lines.append(f"  [NEW LINE ITEM] {fmt_row(r)}{cand_str}")
            for r in new_dims:
                parent_c = r.get("parent_concept") or r.get("parent")
                extra = []
                if parent_c:
                    extra.append(f"parent line: {parent_c}")
                    matching_headers = [
                        sh.get("concept") for sh in stored_headers
                        if (sh.get("parent_concept") == parent_c or sh.get("parent") == parent_c)
                    ]
                    if matching_headers:
                        extra.append(f"existing headers: {matching_headers}")
                extra_str = f" ({'; '.join(extra)})" if extra else ""
                lines.append(f"  [NEW DIM MEMBER] {fmt_row(r)}{extra_str}")
            lines.append("")

        lines.append(f"MATCHED CONCEPTS ({len(matched_main) + len(matched_dims)}) — ALREADY IN STORED HIERARCHY:")
        filing_main_names = {r["concept"] for r in filing_main}
        filing_dims_names = {r["concept"] for r in filing_dims}
        # Show matched rows in STORED order (path/order_key), not filing order,
        # so the agent sees the real current tree structure.
        stored_main_sorted = sorted(
            (r for r in stored_rows if not r.get("dimension_concept") and r.get("concept")),
            key=lambda r: (str(r.get("path") or ""), str(r.get("order_key") or "")),
        )
        for stored_r in stored_main_sorted:
            if stored_r["concept"] in filing_main_names:
                lines.append(f"  {fmt_row(stored_r)}")
        stored_dims_sorted = sorted(
            (r for r in stored_rows if r.get("dimension_concept") and r.get("concept")),
            key=lambda r: (str(r.get("parent_concept") or ""), str(r.get("order_key") or "")),
        )
        for stored_d in stored_dims_sorted:
            if stored_d["concept"] in filing_dims_names:
                lines.append(f"  {fmt_row(stored_d)} [dim]")

        if stored_absent:
            lines.append("")
            lines.append(f"STORED CONCEPTS ABSENT FROM THIS FILING ({len(stored_absent)}):")
            for r in stored_absent:
                lines.append(f"  {fmt_row(r)}")

        return "\n".join(lines)


    @tool
    def decide_mapping(concept_json: str) -> str:
        """Record a merge decision for ONE concept."""
        try:
            parsed = json.loads(concept_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        concept = parsed.get("concept")
        same_as = parsed.get("same_as")
        keep_tag = parsed.get("keep_tag", "stored")
        if not isinstance(concept, str) or not concept:
            return "Error: 'concept' must be a non-empty string."
        if same_as is not None:
            if not isinstance(same_as, str):
                return "Error: 'same_as' must be null or a string."
            if same_as not in stored_names:
                return (
                    f"Error: '{same_as}' is not a stored concept. Choose null or one "
                    f"of the stored names (query_stored_hierarchy / query_hierarchy_diff)."
                )
        if keep_tag not in ("stored", "incoming"):
            return "Error: keep_tag must be 'stored' or 'incoming'."
        proposal.setdefault("merges", {})[concept] = {
            "same_as": same_as,
            "keep_tag": keep_tag,
            "reason": parsed.get("reason") or "",
        }
        if same_as is None:
            return f"OK — '{concept}' will be created as its own row."
        if keep_tag == "incoming":
            # The loser's dimensional children must follow the surviving row.
            # Surface them so the AGENT places them (move_row) — persist never
            # computes their paths.
            children = [
                {
                    "concept": s.get("concept"),
                    "path": s.get("path"),
                    "order_key": s.get("order_key"),
                    "segment_type": s.get("segment_type"),
                    "parent_header": s.get("parent_header"),
                }
                for s in stored_rows
                if s.get("dimension_concept")
                and (s.get("parent_concept") or s.get("parent")) == same_as
            ]
            proposal["merges"][concept]["children"] = children
            if children:
                listing = "; ".join(
                    f"{c['concept']} (now {c.get('path')})" for c in children
                )
                return (
                    f"OK — '{concept}' becomes the surviving name; '{same_as}' is retired. "
                    f"Its {len(children)} dimensional child(ren) must follow it: {listing}. "
                    f"Re-parent EACH under '{concept}' with "
                    f"move_row({{\"concept\": \"<child>\", \"parent_concept\": \"{concept}\"}}), "
                    "then run check_hierarchy() before finalizing."
                )
            return (
                f"OK — '{concept}' will become the surviving name; "
                f"'{same_as}' is retired (no dimensional children)."
            )
        return f"OK — '{concept}' merges into existing '{same_as}'."

    def _merge_proposal_with_stored(
        parsed_rows: list[dict], parsed_dims: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        if not stored_rows:
            return parsed_rows, parsed_dims

        # 1. Base main rows from stored
        merged_away = set()
        for c, m in (proposal.get("merges") or {}).items():
            if isinstance(m, dict):
                same_as = m.get("same_as")
                if same_as:
                    if m.get("keep_tag") == "incoming":
                        merged_away.add(same_as)
                    else:
                        merged_away.add(c)
        # Rows the agent explicitly REMOVED (remove_row) must not be merged back
        # from stored.  A spec with only a concept removes every parent; a spec
        # with both concept and parent removes just that one.
        removed = [s for s in (proposal.get("removed") or []) if isinstance(s, dict)]

        def _is_removed(concept: Any, parent: Any) -> bool:
            for spec in removed:
                if spec.get("concept") != concept:
                    continue
                if "parent" not in spec or spec.get("parent") == parent:
                    return True
            return False

        stored_main = [
            {
                "concept": s["concept"],
                "parent": s.get("parent_concept") or s.get("parent"),
                "label": s.get("label"),
                "order": s.get("order"),
                "order_key": s.get("order_key"),
                "level": s.get("level", 0),
                "abstract": s.get("abstract", False),
            }
            for s in stored_rows
            if not s.get("dimension_concept") and s.get("concept") and s["concept"] not in merged_away
            and not _is_removed(s["concept"], s.get("parent_concept"))
        ]
        stored_main_concepts = {s["concept"] for s in stored_main}
        parsed_row_concepts = {
            r.get("concept") for r in parsed_rows if isinstance(r, dict) and r.get("concept")
        }

        # If agent supplied all active stored concepts, treat as full replacement tree
        if stored_main_concepts and stored_main_concepts.issubset(parsed_row_concepts):
            final_rows = parsed_rows
        else:
            # Incremental: retain stored, apply overrides, append new concepts
            overrides = {
                r.get("concept"): r for r in parsed_rows if isinstance(r, dict) and r.get("concept")
            }
            merged = []
            for s in stored_main:
                c = s["concept"]
                if c in overrides:
                    merged_row = dict(s)
                    merged_row.update(overrides[c])
                    merged.append(merged_row)
                else:
                    merged.append(s)
            for r in parsed_rows:
                if isinstance(r, dict) and r.get("concept") and r["concept"] not in stored_main_concepts:
                    merged.append(r)
            # Stored rows keep their stored order: give every row WITHOUT an
            # explicit position a positional rank from the merged list so newly
            # added rows can interleave by the agent's own position numbers.
            for i, r in enumerate(merged):
                if not any(r.get(k) is not None for k in ("position", "order", "index")):
                    r["position"] = i + 1
            final_rows = merged

        # 2. Base dimensional members from stored
        stored_dims = [
            {
                "concept": s.get("concept") or s.get("member"),
                "parent_concept": s.get("parent_concept") or s.get("parent"),
                "label": s.get("label"),
                "segment_type": s.get("segment_type"),
                "parent_header": s.get("parent_header"),
                "order": s.get("order"),
                "order_key": s.get("order_key"),
            }
            for s in stored_rows
            if s.get("dimension_concept") and (s.get("concept") or s.get("member"))
            and not _is_removed(s.get("concept") or s.get("member"), s.get("parent_concept"))
        ]
        stored_dim_concepts = {d["concept"] for d in stored_dims}
        parsed_dim_concepts = {
            d.get("concept") or d.get("member")
            for d in parsed_dims
            if isinstance(d, dict) and (d.get("concept") or d.get("member"))
        }

        if stored_dim_concepts and stored_dim_concepts.issubset(parsed_dim_concepts):
            final_dims = parsed_dims
        else:
            dim_overrides = {
                (d.get("parent_concept") or d.get("parent"), d.get("concept") or d.get("member")): d
                for d in parsed_dims
                if isinstance(d, dict)
            }
            merged_dims = []
            for sd in stored_dims:
                key = (sd.get("parent_concept"), sd["concept"])
                if key in dim_overrides:
                    merged_dim = dict(sd)
                    merged_dim.update(dim_overrides[key])
                    merged_dims.append(merged_dim)
                else:
                    merged_dims.append(sd)
            for pd in parsed_dims:
                c = pd.get("concept") or pd.get("member")
                parent_c = pd.get("parent_concept") or pd.get("parent")
                key = (parent_c, c)
                if key not in {(d.get("parent_concept"), d.get("concept")) for d in merged_dims}:
                    norm_dim = dict(pd)
                    if "member" in norm_dim and "concept" not in norm_dim:
                        norm_dim["concept"] = norm_dim.pop("member")
                    if "parent" in norm_dim and "parent_concept" not in norm_dim:
                        norm_dim["parent_concept"] = norm_dim.pop("parent")
                    merged_dims.append(norm_dim)
            # Preserve stored dim order: assign positional ranks to dims without
            # an explicit position.
            for i, d in enumerate(merged_dims):
                if not any(d.get(k) is not None for k in ("position", "order", "index")):
                    d["position"] = i + 1
            final_dims = merged_dims

        return final_rows, final_dims

    @tool
    def propose_hierarchy(rows_json: str, dims_json: str) -> str:
        """Submit (or revise) the tree proposal.
        - Each call ACCUMULATES: rows are keyed by (parent, concept) and dims by
          (parent_concept, concept), so a later revision only adds/overrides what
          you pass — it never erases rows from an earlier call.
        - FRESH SEED: pass the complete tree per universal blueprint.
        - INCREMENTAL UPDATE: pass the COMPLETE ordered tree (every row/dim with
          an explicit 'position'/'order'); stored rows you omit are merged back."""
        try:
            parsed_rows = json.loads(rows_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON in rows: {exc}"
        if not isinstance(parsed_rows, list):
            return "Error: rows_json must be a JSON array."

        try:
            parsed_dims = json.loads(dims_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON in dims: {exc}"
        if not isinstance(parsed_dims, list):
            return "Error: dims_json must be a JSON array."

        # Accumulate rows across calls (revision-safe).  An explicit removal
        # (remove_row) is honoured here too: re-proposing the same row must not
        # silently resurrect it (that caused remove→propose→remove thrash).
        # Re-adding a removed concept is done deliberately via move_row.
        removed = [s for s in (proposal.get("removed") or []) if isinstance(s, dict)]

        def _is_removed(concept: Any, parent: Any) -> bool:
            for spec in removed:
                if spec.get("concept") != concept:
                    continue
                spec_parent = spec.get("parent", spec.get("parent_concept"))
                if spec_parent is None or spec_parent == parent:
                    return True
            return False

        cur_rows = [dict(r) for r in (proposal.get("rows") or []) if isinstance(r, dict)]
        row_index = {(r.get("parent"), r.get("concept")): i for i, r in enumerate(cur_rows)}
        for r in parsed_rows:
            if not isinstance(r, dict) or not r.get("concept"):
                continue
            if _is_removed(r.get("concept"), r.get("parent")):
                continue
            key = (r.get("parent"), r.get("concept"))
            if key in row_index:
                cur_rows[row_index[key]] = dict(r)
            else:
                row_index[key] = len(cur_rows)
                cur_rows.append(dict(r))
        proposal["rows"] = cur_rows

        # Accumulate dims across calls (revision-safe).
        cur_dims = [dict(d) for d in (proposal.get("dims") or []) if isinstance(d, dict)]
        dim_index = {
            (d.get("parent_concept") or d.get("parent"), d.get("concept") or d.get("member")): i
            for i, d in enumerate(cur_dims)
        }
        for d in parsed_dims:
            if not isinstance(d, dict):
                continue
            c = d.get("concept") or d.get("member")
            if not c:
                continue
            if _is_removed(c, d.get("parent_concept") or d.get("parent")):
                continue
            key = (d.get("parent_concept") or d.get("parent"), c)
            if key in dim_index:
                cur_dims[dim_index[key]] = dict(d)
            else:
                dim_index[key] = len(cur_dims)
                cur_dims.append(dict(d))
        proposal["dims"] = cur_dims

        row_concepts = {r.get("concept") for r in proposal["rows"] if isinstance(r, dict)}
        unknown = [c for c in row_concepts if c not in known_names and not str(c).startswith("custom:")]
        if unknown:
            sorted_unknown = sorted(str(c) for c in unknown if c is not None)
            return (
                f"Warning: these concepts are neither stored nor in the filing "
                f"(they may still be custom headers): {sorted_unknown[:10]}"
            )
        lint_report = _run_linter()
        if lint_report != "OK — no structural issues found.":
            return (
                f"Recorded {len(proposal['rows'])} row(s) and {len(proposal['dims'])} dim(s).\n"
                f"{lint_report}\n"
                "Please fix these findings with a revised propose_hierarchy()."
            )
        return (
            f"OK — hierarchy recorded with 0 issues. "
            f"Recorded {len(proposal['rows'])} row(s) and {len(proposal['dims'])} dim(s). "
            "Now call preview_hierarchy() to check the exact paths/order_keys before finalizing."
        )

    @tool
    def move_row(row_json: str) -> str:
        """Re-parent / re-position ONE row or member — a MOVE, not an add.

        Pass {"concept": "...", "parent": "...", "position": N} (add
        "parent_concept"/"parent_header" for a dimensional member). Any earlier
        proposal entry for this concept is dropped first, so re-parenting never
        leaves a duplicate behind. Use this whenever you change a row's parent.
        """
        try:
            parsed = json.loads(row_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        if not isinstance(parsed, dict):
            return "Error: row_json must be a JSON object."
        concept = parsed.get("concept") or parsed.get("member")
        if not concept:
            return "Error: 'concept' is required."
        parent = parsed.get("parent") or parsed.get("parent_concept")

        def _same(r: dict) -> bool:
            return (r.get("concept") or r.get("member")) == concept

        proposal["rows"] = [r for r in (proposal.get("rows") or []) if not _same(r)]
        proposal["dims"] = [d for d in (proposal.get("dims") or []) if not _same(d)]
        proposal["removed"] = [
            s for s in (proposal.get("removed") or []) if s.get("concept") != concept
        ]
        # If this concept is STORED under a different parent, exclude that stored
        # entry from the merge — this is a MOVE, not an extra copy.
        for s in (stored_rows or []):
            if (s.get("concept") or s.get("member")) != concept:
                continue
            old_parent = s.get("parent_concept") or s.get("parent")
            if old_parent is not None and old_parent != parent:
                proposal.setdefault("removed", []).append(
                    {"concept": concept, "parent": old_parent}
                )

        spec = {k: v for k, v in parsed.items() if k != "member"}
        is_dim = bool(
            parsed.get("dimension") or parsed.get("parent_concept") or parsed.get("parent_header")
        )
        where = "dimensional members" if is_dim else "rows"
        (proposal["dims"] if is_dim else proposal["rows"]).append(spec)
        return (
            f"OK — '{concept}' moved to parent={parent!r} in {where}. "
            "Run check_hierarchy() to verify."
        )

    @tool
    def remove_row(row_json: str) -> str:
        """Remove ONE row or member from the tree (e.g. a spurious wrapper header).

        Pass {"concept": "<name>"} to remove every instance, or
        {"concept": "<name>", "parent": "<parent>"} to remove one instance.
        """
        try:
            parsed = json.loads(row_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        if not isinstance(parsed, dict):
            return "Error: row_json must be a JSON object."
        concept = parsed.get("concept") or parsed.get("member")
        if not concept:
            return "Error: 'concept' is required."
        parent = parsed.get("parent") or parsed.get("parent_concept")

        def _keep(r: dict) -> bool:
            c = r.get("concept") or r.get("member")
            if c != concept:
                return True
            if parent is None:
                return False
            return (r.get("parent") or r.get("parent_concept")) != parent

        before = len(proposal.get("rows") or []) + len(proposal.get("dims") or [])
        proposal["rows"] = [r for r in (proposal.get("rows") or []) if _keep(r)]
        proposal["dims"] = [d for d in (proposal.get("dims") or []) if _keep(d)]
        after = len(proposal.get("rows") or []) + len(proposal.get("dims") or [])
        proposal.setdefault("removed", []).append(
            {"concept": concept} if parent is None else {"concept": concept, "parent": parent}
        )
        return f"OK — removed {before - after} entr(y/ies) for '{concept}'. Run check_hierarchy() to verify."

    def _structural_findings(rows: list[dict], dims: list[dict], materialized: bool = True) -> list[str]:
        """Structural critic.  Path-based checks run only when the rows carry
        materialised paths (``materialized``); path-free checks always run."""
        problems: list[str] = []
        all_items = [("row", r) for r in rows] + [("dim", d) for d in dims]

        # 1. Duplicate paths — rows AND dims (a dim under a line item can
        #    otherwise collide with a custom grouping header).
        by_path: dict[str, list[str]] = defaultdict(list)
        for kind, it in all_items:
            p = str(it.get("path") or "")
            if p and p != "555":
                by_path[p].append(f"{it.get('concept')}[{kind}]")
        for p, names in by_path.items():
            u = sorted(set(names))
            if len(u) > 1:
                problems.append(f"path {p} is shared by {u} — move one of them with move_row()")

        # 2. (path, order_key) collisions within a dimension class.
        by_pok: dict[tuple, list[str]] = defaultdict(list)
        for kind, it in all_items:
            p = str(it.get("path") or "")
            o = str(it.get("order_key") or "")
            if p and p != "555":
                by_pok[(p, o, kind)].append(str(it.get("concept")))
        for (p, o, kind), names in by_pok.items():
            u = sorted(set(names))
            if len(u) > 1:
                problems.append(f"collision path={p} order_key={o} ({kind}): {u}")

        row_names = {r.get("concept") for r in rows if r.get("concept")}

        # 3. Every parent must exist in the tree (known stored names count).
        for r in rows:
            par = r.get("parent")
            if par and par not in row_names and par not in known_names:
                problems.append(f"{r.get('concept')}: parent '{par}' is not in the tree")

        # 4. Whole-statement wrapper headers must not exist.
        for r in rows:
            c = str(r.get("concept") or "")
            if c.startswith("custom:") and any(
                w in c for w in ("ActivitiesSection", "Consolidation", "Statement", "CashFlow")
            ):
                problems.append(f"{c}: whole-statement wrapper header — remove it with remove_row()")

        # 5. A grouping header must hang under a real parent.
        for r in rows:
            c = str(r.get("concept") or "")
            if c.startswith("custom:") and not r.get("parent"):
                problems.append(
                    f"{c}: grouping header has parent=None — attach it under a real line item"
                )

        # 6. The same header name must not sit under several parents.
        hp: dict[str, set] = defaultdict(set)
        for r in rows:
            c = str(r.get("concept") or "")
            if c.startswith("custom:"):
                hp[c].add(str(r.get("parent")))
        for c, parents in hp.items():
            if len(parents) > 1:
                problems.append(
                    f"{c}: exists under multiple parents {sorted(parents)} — "
                    "keep one and remove/move the others"
                )

        # 7. Dims flat under a parent (>=3) without one grouping header.
        dp: dict[str, list[dict]] = defaultdict(list)
        for d in dims:
            pc = str(d.get("parent_concept") or d.get("parent") or "")
            if pc:
                dp[pc].append(d)
        for pc, ds in dp.items():
            if len(ds) >= 3 and not any(d.get("parent_header") for d in ds):
                problems.append(f"{pc}: {len(ds)} dim members are flat — group them under one header")

        # 8. A dim's parent_header must exist as a row.
        for d in dims:
            h = d.get("parent_header")
            if h and h not in row_names and h not in known_names:
                problems.append(f"{d.get('concept')}: parent_header '{h}' is not in the tree")

        # 9. Unplaced rows/dims (only meaningful once paths are materialised).
        if materialized:
            for kind, it in all_items:
                if not it.get("path"):
                    problems.append(f"{it.get('concept')} [{kind}]: has no path (not placed)")

        # 10. Non-sequential order keys per parent.
        def _rk(ok: Any) -> int:
            r = 0
            for ch in str(ok or ""):
                r = r * 26 + (ord(ch) - 96)
            return r - 1
        bp: dict[Any, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("path") and str(r.get("path")) != "555":
                bp[r.get("parent")].append(r)
        for par, kids in bp.items():
            ranks = sorted(_rk(k.get("order_key")) for k in kids)
            if ranks != list(range(len(ranks))):
                problems.append(
                    f"children of {par or 'ROOT'}: order keys are not sequential "
                    f"{[k.get('order_key') for k in kids]}"
                )
        return problems

    def _run_linter() -> str:
        # Materialise (side-effect free) so path checks see real paths/order_keys.
        rows = [r for r in proposal.get("rows") or [] if isinstance(r, dict)]
        dims = [d for d in proposal.get("dims") or [] if isinstance(d, dict)]
        materialized = False
        res = _materialize_tree()
        if res is not None:
            rows, dims = res
            materialized = True
        problems = _structural_findings(rows, dims, materialized)

        # Blueprint/era checks that need no paths.
        for r in rows:
            c = str(r.get("concept") or "")
            p = str(r.get("path") or "")
            # Only income/balance-sheet primaries must be roots; in the cash-flow
            # statement NetIncomeLoss etc. legitimately nest under the activities.
            if statement_type in ("income", "balancesheet") and p and p != "555" and "." in p and looks_primary(c):
                problems.append(f"{c}: primary line seems nested at path '{p}'")
            parent = str(r.get("parent") or "")
            if parent == "us-gaap:NetIncomeLoss" and (
                "Depreciation" in c or "IncreaseDecrease" in c or "Noncash" in c
            ):
                problems.append(
                    f"{c}: cash-flow adjustments belong under "
                    "us-gaap:NetCashProvidedByUsedInOperatingActivities, not NetIncomeLoss"
                )
        stored_headers = {
            str(r.get("concept")) for r in stored_rows
            if str(r.get("concept", "")).startswith("custom:")
        }
        for r in rows:
            c = str(r.get("concept", ""))
            if c.startswith("custom:") and stored_headers and c not in stored_headers:
                q = c.split(":")[-1].lower()
                similar = [sh for sh in stored_headers if q[:5] in sh.lower() or sh.lower()[:5] in q]
                if similar:
                    problems.append(
                        f"{c}: stored hierarchy already has '{similar[0]}'. Reuse existing header name to prevent duplicate grouping rows."
                    )

        if not problems:
            return "OK — no structural issues found."
        return "FINDINGS:\n" + "\n".join(f"  - {x}" for x in problems)

    @tool
    def lint_hierarchy() -> str:
        """Report structural issues in your CURRENT proposal."""
        return _run_linter()

    @tool
    def check_hierarchy() -> str:
        """Run the FULL structural critic over your proposal (materialised).

        Reports: duplicate paths, (path, order_key) collisions, missing parents,
        whole-statement wrapper headers, flat dims, duplicate header names,
        unplaced rows and non-sequential order keys.  Fix EVERY finding with
        move_row() / remove_row() / propose_hierarchy(), re-check, and only call
        finalize_hierarchy() once this returns OK.
        """
        return _run_linter()

    def _build_preview() -> tuple[list, list] | None:
        """Encode the proposal AND freeze it as the agent-checked payload."""
        res = _materialize_tree()
        if res is None:
            return None
        rows, dims = res
        proposal["_preview"] = {"rows": rows, "dims": dims}
        return rows, dims

    def _materialize_tree() -> tuple[list, list] | None:
        """Encode the proposal — side-effect free (does NOT set _preview)."""
        if materialize is None:
            return None
        merge_targets = {
            m.get("same_as")
            for m in (proposal.get("merges") or {}).values()
            if isinstance(m, dict) and m.get("same_as")
        }
        reserved = set(occupied_paths or set())
        for t in merge_targets:
            tp = (stored_paths or {}).get(str(t))
            if tp:
                reserved.discard(str(tp))
        # Merge the agent's accumulated proposal with the stored tree (a no-op
        # on fresh seed), THEN encode — so omitted stored rows are retained and
        # a partial revision can never shrink the tree.
        base_rows, base_dims = _merge_proposal_with_stored(
            proposal.get("rows") or [], proposal.get("dims") or []
        )
        return materialize(
            base_rows,
            base_dims,
            stored_paths or {},
            occupied_paths=reserved,
            identity_paths=identity_paths or {},
            identity_order_keys=identity_order_keys or {},
        )

    @tool
    def preview_hierarchy() -> str:
        """Encode the current proposal into the EXACT paths/order_keys that will be
        persisted, and show them.  Call this after propose_hierarchy() to check
        the real result; revise and re-preview until it is correct, then finalize."""
        res = _build_preview()
        if res is None:
            return "Preview unavailable."
        rows, dims = res
        proposal["_preview_checked"] = True
        problems: list[str] = []
        # The insert guard is (path, order_key) per dimension class, so check
        # BOTH main rows and dimensional members — a dim placed directly under
        # a line item can otherwise collide with a custom grouping header.
        all_items = [("row", r) for r in rows] + [("dim", d) for d in dims]
        seen_paths: dict[str, list[str]] = defaultdict(list)
        seen_pok: dict[tuple, list[str]] = defaultdict(list)
        for kind, item in all_items:
            p = str(item.get("path") or "")
            o = str(item.get("order_key") or "")
            c = str(item.get("concept") or "?")
            if p and p != "555":
                seen_paths[p].append(f"{c}[{kind}]")
                if o:
                    seen_pok[(p, o, kind)].append(c)
        for p, names in seen_paths.items():
            if len(names) > 1:
                problems.append(f"duplicate path {p} claimed by {names}")
        for (p, o, _kind), names in seen_pok.items():
            if len(set(names)) > 1:
                problems.append(f"duplicate (path={p}, order_key={o}) by {sorted(set(names))}")
        lines = [
            f"PREVIEW — {len(rows)} row(s), {len(dims)} dim(s) (exact values that will be persisted):"
        ]
        for r in rows:
            lines.append("  " + fmt_row(r))
        if dims:
            lines.append("  DIMENSIONAL MEMBERS:")
            for d in dims:
                lines.append("  " + fmt_row(d))
        if problems:
            lines.append("\nCHECK FAILURES (fix before finalizing):")
            lines.extend(f"  - {x}" for x in problems)
            lines.append(
                "  → revise propose_hierarchy() to move one of the conflicting rows, then preview again."
            )
        else:
            lines.append("\n✓ no duplicate paths or path/order_key collisions. If this tree is correct, call finalize_hierarchy().")
        return "\n".join(lines)

    if not stored_rows:
        return [
            query_stored_hierarchy,
            query_filing_hierarchy,
            propose_hierarchy,
            move_row,
            remove_row,
            preview_hierarchy,
            check_hierarchy,
            lint_hierarchy,
        ]
    return [
        query_stored_hierarchy,
        query_hierarchy_diff,
        decide_mapping,
        propose_hierarchy,
        move_row,
        remove_row,
        preview_hierarchy,
        check_hierarchy,
        lint_hierarchy,
    ]


PRIMARY_PREFIXES = (
    "Revenue", "Sales", "CostOf", "CostsAndExpenses", "GrossProfit",
    "OperatingExpenses", "OperatingIncomeLoss", "NonoperatingIncomeExpense",
    "IncomeLossFromContinuingOperations", "IncomeTaxExpenseBenefit",
    "NetIncomeLoss", "EarningsPerShare", "WeightedAverageNumberOf",
    "Assets", "Liabilities", "StockholdersEquity", "NetCashProvidedByUsedIn",
)
_PRIMARY_PREFIXES = PRIMARY_PREFIXES


def looks_primary(concept: str) -> bool:
    local = str(concept or "").split(":")[-1]
    return local.startswith(PRIMARY_PREFIXES)


_looks_primary = looks_primary
