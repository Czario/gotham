"""Tools for the unified hierarchy agent.

Tools are read/propose-only.  They never write MongoDB and never override the
agent's decisions — ``lint_hierarchy`` is advisory only; the agent chooses
whether to act on it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from langchain_core.tools import tool


def _fmt_row(row: dict) -> str:
    flags = []
    if row.get("abstract"):
        flags.append("abstract")
    if row.get("hide") or str(row.get("path") or "") == "555":
        flags.append("supplementary/555")
    if row.get("dimension_concept"):
        flags.append("dim")
    if row.get("parent_concept"):
        flags.append(f"parent={row['parent_concept']}")
    flag = f"  [{','.join(flags)}]" if flags else ""
    return (
        f"{str(row.get('path') or '-'):<14} {str(row.get('order_key') or '-'):<4} "
        f"{row.get('concept','?')}"
        + (f" ({row.get('label')})" if row.get("label") else "")
        + flag
    )


def build_unified_hierarchy_tools(
    stored_rows: list[dict],
    filing_rows: list[dict],
    proposal: dict,
):
    """Build the hierarchy agent's toolset.

    ``proposal`` is a mutable dict the tools write decisions / rows / dims into.
    """
    stored_names = {r.get("concept") for r in stored_rows if r.get("concept")}
    filing_names = {r.get("concept") for r in filing_rows if r.get("concept")}
    known_names = stored_names | filing_names

    @tool
    def query_stored_hierarchy() -> str:
        """Show the FULL existing stored tree for this company+statement (all
        periods), including dimensional members. Read this first."""
        if not stored_rows:
            return "NO STORED HIERARCHY (this is a fresh seed). Build the whole tree from query_filing_hierarchy()."
        dims = [r for r in stored_rows if r.get("dimension_concept")]
        main = [r for r in stored_rows if not r.get("dimension_concept")]
        main.sort(key=lambda r: (str(r.get("path") or ""), str(r.get("order_key") or "")))
        dims.sort(key=lambda r: (str(r.get("parent_concept") or ""), str(r.get("concept") or "")))
        return (
            f"STORED MAIN ROWS ({len(main)}):\n" + "\n".join(_fmt_row(r) for r in main)
            + f"\n\nSTORED DIMENSIONAL MEMBERS ({len(dims)}):\n"
            + "\n".join(_fmt_row(r) for r in dims)
        )

    @tool
    def query_filing_hierarchy() -> str:
        """Show this filing's extracted line items and dimensional members."""
        main = [r for r in filing_rows if not r.get("dimension_concept")]
        dims = [r for r in filing_rows if r.get("dimension_concept")]
        return (
            f"FILING LINE ITEMS ({len(main)}):\n" + "\n".join(_fmt_row(r) for r in main)
            + f"\n\nFILING DIMENSIONAL MEMBERS ({len(dims)}):\n"
            + "\n".join(_fmt_row(r) for r in dims)
        )

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
        return "CANDIDATES:\n" + "\n".join(_fmt_row(r) for r in hits[:40])

    @tool
    def suggest_hierarchy() -> str:
        """Return a NON-AUTHORITATIVE reference layout.  You are free to ignore
        it — this is only a starting point."""
        if not filing_rows:
            return "Nothing to suggest (no filing rows)."
        return (
            "Reference (primary lines at root in reading order; members nested "
            "under their parent):\n" + "\n".join(_fmt_row(r) for r in filing_rows[:120])
        )

    @tool
    def decide_mapping(concept_json: str) -> str:
        """Record a merge decision for ONE concept.

        Pass a JSON object:
          {"concept": "...new tag...",
           "same_as": "...existing stored concept...",  // or null for a new row
           "keep_tag": "stored" | "incoming",  // which NAME survives (default stored)
           "reason": "..."}

        same_as must be null or an existing STORED concept name.
        keep_tag=stored  -> values attach to the existing concept (no new row).
        keep_tag=incoming -> the new tag becomes the surviving name; the old row's
                             values move onto it and the old row is deleted.
        """
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
        """Submit the full tree.

        rows_json: JSON array of line-item/header rows. Each row:
          {"concept": "...", "parent": null|"...", "position": 0,
           "abstract": false, "path": "...", "order_key": "...", "hide": false}
          - use parent+position OR explicit path/order_key; path "555" = supplementary.
          - abstract true ONLY for custom: grouping headers.
        dims_json: JSON array of dimensional members. Each dim:
          {"concept": "...", "parent_concept": "...", "parent_header": null,
           "position": 0}
        RULES:
          - Every dim carries its own ``parent_concept``. Place it under THAT
            parent. If a member exists under two parents (e.g. ProductMember
            under Revenue and under CostOfRevenue), submit TWO dim entries with
            the two parent_concept values — never reuse Revenue's header for a
            CostOfRevenue member.
          - ``parent_header`` is only for grouping WITHIN the same parent
            (e.g. custom:ProductSegmentation that itself sits under Revenue).
            When in doubt, set parent_header to null and the dim is placed
            directly under parent_concept.
        """
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
            return (
                f"Warning: these concepts are neither stored nor in the filing "
                f"(they may still be custom headers): {sorted(unknown)[:10]}"
            )
        return (
            f"Recorded {len(proposal['rows'])} row(s) and {len(proposal['dims'])} dim(s). "
            "Call lint_hierarchy() to review, fix what you want, then finalize_hierarchy()."
        )

    @tool
    def lint_hierarchy() -> str:
        """Report structural issues in your CURRENT proposal (path in use by
        more than one row, a child whose parent path is missing, a primary line
        not at root, an unknown parent, or a custom: statement wrapper).

        Advisory only — the findings are shown to you; nothing is auto-fixed."""
        rows = [r for r in proposal.get("rows", []) if isinstance(r, dict)]
        problems: list[str] = []

        # map explicit paths we can already see
        by_path: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            p = r.get("path")
            if p:
                by_path[str(p)].append(r.get("concept", "?"))

        # primary line not at root (only when the row has an explicit dotted path)
        for r in rows:
            c = r.get("concept", "")
            p = str(r.get("path") or "")
            if p and p not in ("555",) and "." in p and _looks_primary(c):
                problems.append(f"{c}: primary line seems nested at path '{p}'")

        # parent not in proposal
        proposed = {r.get("concept") for r in rows}
        for r in rows:
            parent = r.get("parent")
            if parent and parent not in proposed and parent not in known_names:
                problems.append(f"{r.get('concept')}: parent '{parent}' is unknown")

        # custom statement wrapper
        for r in rows:
            c = str(r.get("concept", ""))
            if c.startswith("custom:") and ("Statement" in c or "CashFlow" in c):
                problems.append(f"{c}: looks like a whole-statement wrapper")

        # shared explicit paths
        for p, names in by_path.items():
            if len(names) > 1:
                problems.append(f"path {p} is claimed by {names}")

        if not problems:
            return "OK — no structural issues found."
        return "FINDINGS:\n" + "\n".join(f"  - {x}" for x in problems)

    return [
        query_stored_hierarchy,
        query_filing_hierarchy,
        query_concept_candidates,
        suggest_hierarchy,
        decide_mapping,
        propose_hierarchy,
        lint_hierarchy,
    ]


_PRIMARY_PREFIXES = (
    "Revenue", "Sales", "CostOf", "CostsAndExpenses", "GrossProfit",
    "OperatingExpenses", "OperatingIncomeLoss", "NonoperatingIncomeExpense",
    "IncomeLossFromContinuingOperations", "IncomeTaxExpenseBenefit",
    "NetIncomeLoss", "EarningsPerShare", "WeightedAverageNumberOf",
    "Assets", "Liabilities", "StockholdersEquity", "NetCashProvidedByUsedIn",
)


def _looks_primary(concept: str) -> bool:
    local = str(concept or "").split(":")[-1]
    return local.startswith(_PRIMARY_PREFIXES)
