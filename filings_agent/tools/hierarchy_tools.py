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
) -> list[Any]:
    """Build the hierarchy agent's toolset.

    ``proposal`` is a mutable dict the tools write decisions / rows / dims into.
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
        main.sort(key=lambda r: (str(r.get("path") or ""), str(r.get("order_key") or "")))
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
        for r in matched_main:
            stored_r = stored_main_map.get(r["concept"])
            if stored_r:
                lines.append(f"  {fmt_row(stored_r)}")
            else:
                lines.append(f"  {r.get('concept')}")
        for r in matched_dims:
            stored_d = stored_dims_map.get(r["concept"])
            if stored_d:
                lines.append(f"  {fmt_row(stored_d)}")
            else:
                lines.append(f"  {r.get('concept')} [dim]")

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
            return f"OK — '{concept}' will become the surviving name; '{same_as}' is retired."
        return f"OK — '{concept}' merges into existing '{same_as}'."

    def _merge_proposal_with_stored(
        parsed_rows: list[dict], parsed_dims: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        if not stored_rows:
            return parsed_rows, parsed_dims

        # 1. Base main rows from stored
        merged_away = {
            m.get("same_as")
            for m in (proposal.get("merges") or {}).values()
            if isinstance(m, dict) and m.get("same_as")
        }
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
            final_dims = merged_dims

        return final_rows, final_dims

    @tool
    def propose_hierarchy(rows_json: str, dims_json: str) -> str:
        """Submit the tree proposal.
        - In an INCREMENTAL UPDATE: pass ONLY the new incoming concepts and any modifications
          in rows_json and dims_json. They will automatically merge with the existing stored tree.
          You may also pass the complete tree if preferred.
        - In a FRESH SEED: pass the complete tree per universal blueprint."""
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

        if stored_rows:
            proposal["rows"], proposal["dims"] = _merge_proposal_with_stored(parsed_rows, parsed_dims)
        else:
            proposal["rows"] = parsed_rows
            proposal["dims"] = parsed_dims

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
                "Please fix these findings with a revised propose_hierarchy(), or call finalize_hierarchy() if intentional."
            )
        return (
            f"OK — hierarchy verified with 0 issues. "
            f"Recorded {len(proposal['rows'])} row(s) and {len(proposal['dims'])} dim(s). "
            "Call finalize_hierarchy() to complete."
        )

    def _run_linter() -> str:
        rows = [r for r in proposal.get("rows", []) if isinstance(r, dict)]
        problems: list[str] = []

        by_path: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            p = r.get("path")
            if p:
                by_path[str(p)].append(r.get("concept", "?"))

        for r in rows:
            c = r.get("concept", "")
            p = str(r.get("path") or "")
            if p and p not in ("555",) and "." in p and looks_primary(c):
                problems.append(f"{c}: primary line seems nested at path '{p}'")

        proposed = {r.get("concept") for r in rows}
        for r in rows:
            parent = r.get("parent")
            if parent and parent not in proposed and parent not in known_names:
                problems.append(f"{r.get('concept')}: parent '{parent}' is unknown")

        for r in rows:
            c = str(r.get("concept", ""))
            parent = r.get("parent")
            if c.startswith("custom:"):
                if not parent:
                    problems.append(f"{c}: custom grouping header has parent=None. Grouping headers must have a parent line item (e.g. us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax).")
                elif "Statement" in c or "CashFlow" in c:
                    problems.append(f"{c}: looks like a whole-statement wrapper")

        # Cash Flow check: adjustments should be children of OperatingActivities, not NetIncomeLoss
        for r in rows:
            c = str(r.get("concept", ""))
            parent = str(r.get("parent") or "")
            if parent == "us-gaap:NetIncomeLoss" and ("Depreciation" in c or "IncreaseDecrease" in c or "Noncash" in c):
                problems.append(f"{c}: adjustments should be children of us-gaap:NetCashProvidedByUsedInOperatingActivities, sibling to NetIncomeLoss.")

        # Dims check: dimensional members should have parent_header if under a multi-segment line item
        dims = [d for d in proposal.get("dims", []) if isinstance(d, dict)]
        dims_by_pc: dict[str, list[dict]] = defaultdict(list)
        for d in dims:
            pc = d.get("parent_concept") or d.get("parent")
            if pc:
                dims_by_pc[str(pc)].append(d)
        for pc, d_list in dims_by_pc.items():
            if len(d_list) >= 3 and not any(d.get("parent_header") for d in d_list):
                problems.append(
                    f"{pc}: {len(d_list)} dimensional members are flat without grouping headers. "
                    f"Define custom:ProductSegmentation or custom:GeographicSegmentation in rows_json "
                    f"(with parent='{pc}') and assign parent_header in dims_json."
                )

        # Check header naming consistency against stored grouping headers
        stored_headers = {str(r.get("concept")) for r in stored_rows if str(r.get("concept", "")).startswith("custom:")}
        for r in rows:
            c = str(r.get("concept", ""))
            if c.startswith("custom:") and stored_headers and c not in stored_headers:
                # check if there is an existing similar header
                q = c.split(":")[-1].lower()
                similar = [sh for sh in stored_headers if q[:5] in sh.lower() or sh.lower()[:5] in q]
                if similar:
                    problems.append(f"{c}: stored hierarchy already has '{similar[0]}'. Reuse existing header name to prevent duplicate grouping rows.")

        for p, names in by_path.items():
            if len(names) > 1:
                problems.append(f"path {p} is claimed by {names}")

        if not problems:
            return "OK — no structural issues found."
        return "FINDINGS:\n" + "\n".join(f"  - {x}" for x in problems)

    @tool
    def lint_hierarchy() -> str:
        """Report structural issues in your CURRENT proposal."""
        return _run_linter()

    if not stored_rows:
        return [
            query_filing_hierarchy,
            propose_hierarchy,
            lint_hierarchy,
        ]
    return [
        query_hierarchy_diff,
        decide_mapping,
        propose_hierarchy,
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
