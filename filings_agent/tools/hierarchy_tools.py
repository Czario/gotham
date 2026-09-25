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
                lines.append(f"  [NEW DIM MEMBER] {fmt_row(r)}")
            lines.append("")

        lines.append(f"MATCHED CONCEPTS ({len(matched_main) + len(matched_dims)}) — ALREADY IN STORED HIERARCHY:")
        for r in matched_main:
            stored_r = stored_main_map.get(r["concept"])
            stored_path = stored_r.get("path") if stored_r else "-"
            stored_parent = stored_r.get("parent_concept") if stored_r else "-"
            lines.append(f"  {str(stored_path):<14} {r.get('concept')} (stored parent={stored_parent})")
        for r in matched_dims:
            stored_d = stored_dims_map.get(r["concept"])
            stored_path = stored_d.get("path") if stored_d else "-"
            stored_parent = stored_d.get("parent_concept") if stored_d else "-"
            lines.append(f"  {str(stored_path):<14} {r.get('concept')} [dim] (stored parent={stored_parent})")

        return "\n".join(lines)

    @tool
    def query_concept_candidates(concept: str) -> str:
        """List stored concepts whose name/label overlaps a given concept — the
        only valid targets for decide_mapping(..., same_as=...)."""
        q = (concept or "").lower().strip()
        if not q:
            return "Usage: pass a concept name to search."
        hits = [
            r for r in stored_rows
            if q in str(r.get("concept", "")).lower() or q in str(r.get("label", "")).lower()
        ]
        if not hits:
            return f"No stored concept overlaps '{concept}'."
        return "CANDIDATES:\n" + "\n".join(fmt_row(r) for r in hits[:40])

    @tool
    def suggest_hierarchy() -> str:
        """Return a NON-AUTHORITATIVE reference layout. You are free to ignore
        it — this is only a starting point."""
        if not filing_rows:
            return "Nothing to suggest (no filing rows)."
        return (
            "Reference (primary lines at root in reading order; members nested "
            "under their parent):\n" + "\n".join(fmt_row(r) for r in filing_rows[:120])
        )

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
                    f"of the stored names (query_stored_hierarchy / query_concept_candidates)."
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

    @tool
    def propose_hierarchy(rows_json: str, dims_json: str) -> str:
        """Submit the full tree."""
        for label, raw in (("rows", rows_json), ("dims", dims_json)):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                return f"Invalid JSON in {label}: {exc}"
            if not isinstance(parsed, list):
                return f"Error: {label}_json must be a JSON array."
            proposal[label] = parsed

        row_concepts = {r.get("concept") for r in proposal["rows"] if isinstance(r, dict)}
        unknown = [c for c in row_concepts if c not in known_names and not str(c).startswith("custom:")]
        if unknown:
            sorted_unknown = sorted(str(c) for c in unknown if c is not None)
            return (
                f"Warning: these concepts are neither stored nor in the filing "
                f"(they may still be custom headers): {sorted_unknown[:10]}"
            )
        return (
            f"Recorded {len(proposal['rows'])} row(s) and {len(proposal['dims'])} dim(s). "
            "Call lint_hierarchy() to review, fix what you want, then finalize_hierarchy()."
        )

    @tool
    def lint_hierarchy() -> str:
        """Report structural issues in your CURRENT proposal."""
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
            if len(d_list) > 3 and not any(d.get("parent_header") for d in d_list):
                problems.append(f"{pc}: {len(d_list)} dimensional members are flat without grouping headers. Define custom:ProductSegmentation / custom:GeographicSegmentation in rows_json and assign parent_header in dims_json.")

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

    return [
        query_stored_hierarchy,
        query_filing_hierarchy,
        query_hierarchy_diff,
        query_concept_candidates,
        suggest_hierarchy,
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
