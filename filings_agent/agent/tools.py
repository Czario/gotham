"""XBRL review tools for the P4 agent node.

Tools are deliberately read/propose-only. ``query_*`` and ``verify_math``
inspect the in-memory bundle; ``propose_repair`` appends a proposal to a local
list. None of these tools writes MongoDB. The repair node applies only
CorrectionGate-approved proposals, and the persist node remains the sole DB
writer.
"""
from __future__ import annotations

import ast
import operator
from typing import Any

from langchain_core.tools import tool

from ..hierarchy.planner import plan_hierarchy


def _concept_rows(bundles: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for bundle in bundles:
        for item in getattr(bundle, "concepts", None) or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "statement_type": getattr(bundle, "statement_type", ""),
                "concept": item.get("concept"),
                "label": item.get("label"),
                "value": item.get("value"),
                "period": item.get("period"),
                "path": item.get("path"),
                "order_key": item.get("order_key"),
                "unit": item.get("unit"),
                "fact_id": item.get("fact_id"),
            })
    return rows


def _safe_arithmetic(expression: str) -> float:
    """Evaluate +, -, *, / and parentheses with no names/calls/imports."""
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.USub: operator.neg, ast.UAdd: operator.pos}
    tree = ast.parse(expression.replace(",", ""), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](visit(node.operand))
        raise ValueError("unsupported arithmetic expression")

    return float(visit(tree.body))


def build_review_tools(bundles: list[Any], proposals: list[dict[str, Any]]):
    """Build the scoped tools available to one review turn."""
    rows = _concept_rows(bundles)

    @tool
    def query_concepts(query: str = "") -> str:
        """List in-memory XBRL concepts, values, paths and periods.

        Use this to inspect the exact facts that the validator flagged. The
        query is an optional case-insensitive concept/label filter.
        """
        q = (query or "").lower().strip()
        selected = [r for r in rows if not q or q in str(r).lower()]
        return str(selected[:100])

    @tool
    def query_values(concept: str) -> str:
        """Return all in-memory values for an exact concept name."""
        selected = [r for r in rows if r.get("concept") == concept]
        return str(selected)

    @tool
    def verify_math(expression: str) -> str:
        """Evaluate a simple arithmetic expression exactly and return the result."""
        try:
            return str(_safe_arithmetic(expression))
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"

    @tool
    def propose_repair(
        statement_type: str,
        concept: str,
        old_value: float,
        new_value: float,
        reason: str,
        source: str = "agent_confirmed",
        approved: bool = False,
        confidence: float | None = None,
    ) -> str:
        """Propose a provenance-tagged correction; does not write the DB.

        The CorrectionGate and repair node decide whether this proposal is
        allowed and apply it only in memory before the final validation.
        """
        proposal = {
            "statement_type": statement_type,
            "concept": concept,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "source": source,
            "approved": approved,
            "confidence": confidence,
        }
        proposals.append(proposal)
        return f"Repair proposal recorded (proposal #{len(proposals)}); no database write occurred."

    return [query_concepts, query_values, verify_math, propose_repair]


# ── hierarchy tools (agent-owned hierarchy) ─────────────────────────────────


def _anomalies(stored_rows: list[dict]) -> list[str]:
    """Deterministic defect list for a stored statement hierarchy."""
    from collections import Counter, defaultdict

    problems: list[str] = []
    by_path: dict[str, list[str]] = defaultdict(list)
    for row in stored_rows:
        if row.get("path"):
            by_path[row["path"]].append(row.get("concept", "?"))
    shared = {p: names for p, names in by_path.items() if len(names) > 1 and p != "555"}
    if shared:
        sample = list(shared.items())[:3]
        problems.append(
            f"{len(shared)} path(s) are shared by more than one row "
            f"(a path must identify exactly one row) e.g. {sample}"
        )
    dup_pairs = [k for k, n in Counter(
        (r.get("path"), r.get("order_key")) for r in stored_rows
    ).items() if n > 1]
    if dup_pairs:
        problems.append(f"{len(dup_pairs)} duplicate (path, order_key) pair(s)")

    present = set(by_path)
    orphans = [
        r.get("concept") for r in stored_rows
        if r.get("path") and "." in r["path"]
        and not r["path"].startswith("555")
        and r["path"].rsplit(".", 1)[0] not in present
    ]
    if orphans:
        problems.append(f"{len(orphans)} orphan row(s) whose parent path is missing")

    suspect = [
        r.get("concept") for r in stored_rows
        if any(t in str(r.get("concept", "")) for t in
               ("Segold", "SegOld", "Segrev", "Segcog"))
        # custom:*Segmentation rows are INTENTIONAL grouping headers — not suspect.
    ]
    if suspect:
        problems.append(
            f"{len(suspect)} suspect row(s) that look like legacy-era "
            f"labels rather than statement line items: {suspect[:6]}"
        )
    return problems



def build_hierarchy_tools(
    stored_rows: list[dict],
    filing_concepts: list[dict],
    proposal: dict,
):
    """Build the hierarchy toolset for one statement review.

    ``proposal`` is a single-element dict the tools write into — the node reads
    it after the loop finishes.
    """
    stored_concept_names = {r.get("concept") for r in stored_rows if r.get("concept")}
    new_concepts = [
        c for c in filing_concepts
        if c.get("concept") and c.get("concept") not in stored_concept_names
    ]

    @tool
    def query_hierarchy() -> str:
        """Show the CURRENTLY STORED hierarchy for this statement, new concepts, and defects.

        Read this first.  It lists every stored row as
        ``path | order | concept | flags`` (flags: ``abstract``, ``hidden``,
        ``dimension``), newly introduced concepts in this filing, and a defect summary.
        """
        if not stored_rows:
            return (
                f"NO EXISTING HIERARCHY STORED FOR THIS STATEMENT (SEED FILING).\n"
                f"As the Hierarchy Agent, you must decide the COMPLETE hierarchy tree for this statement.\n\n"
                f"ROWS THIS FILING REPORTS ({len(filing_concepts)}):\n"
                + "\n".join(
                    f"  {c.get('concept', '?')}"
                    + (f" (label: '{c.get('label')}')" if c.get("label") else "")
                    + ("  [abstract]" if c.get("abstract") else "")
                    for c in filing_concepts[:220]
                )
                + ("\n... (truncated)" if len(filing_concepts) > 220 else "")
            )

        lines = []
        for row in sorted(stored_rows, key=lambda r: (r.get("path") or "", r.get("order_key") or "")):
            flags = []
            if row.get("abstract"):
                flags.append("abstract")
            if row.get("hide"):
                flags.append("hidden")
            if row.get("dimension") or row.get("dimension_concept"):
                flags.append("dimension")
            if not row.get("path"):
                flags.append("NO-PATH")
            lines.append(
                f"{row.get('path') or '-':<16} {str(row.get('order_key') or '-'):<5} "
                f"{row.get('concept', '?')}"
                + (f"  [{','.join(flags)}]" if flags else "")
            )
        problems = _anomalies(stored_rows)
        new_section = ""
        if new_concepts:
            new_section = (
                f"\n\nNEW CONCEPTS INTRODUCED IN THIS FILING ({len(new_concepts)}) — DECIDE THEIR PLACEMENT:\n"
                + "\n".join(
                    f"  * {c.get('concept', '?')}"
                    + (f" (label: '{c.get('label')}')" if c.get("label") else "")
                    + (" [abstract]" if c.get("abstract") else "")
                    for c in new_concepts
                )
            )
        else:
            new_section = "\n\nNEW CONCEPTS INTRODUCED IN THIS FILING: None (all concepts exist in stored hierarchy)"

        return (
            f"STORED HIERARCHY ({len(stored_rows)} rows):\n"
            + "\n".join(lines[:220])
            + ("\n... (truncated)" if len(lines) > 220 else "")
            + new_section
            + "\n\nDEFECTS:\n"
            + ("\n".join(f"  - {p}" for p in problems) if problems else "  none detected")
            + f"\n\nALL ROWS THIS FILING REPORTS ({len(filing_concepts)}):\n"
            + "\n".join(
                f"  {c.get('concept', '?')}" + ("  [abstract]" if c.get("abstract") else "")
                for c in filing_concepts[:220]
            )
        )

    @tool
    def place_new_concepts(placements_json: str) -> str:
        """Submit placements for NEW concepts into the existing hierarchy as a JSON array.

        Use this when an existing hierarchy exists and you need to place new
        concepts introduced by this filing.

        Each row can specify:
          {"concept": "us-gaap:NewConcept",       // required concept name
           "parent": "us-gaap:ExistingParent",    // parent concept, or null for root
           "position": 0,                         // optional position among siblings under parent
           "path": "004.003",                     // optional explicit path (e.g. 004.003)
           "order_key": "c",                      // optional explicit order key (e.g. c)
           "abstract": false}                     // true ONLY for new custom: grouping headers

        To create a NEW custom grouping header (e.g. when restructuring segment layout):
          {"concept": "custom:ProductSegmentation", "abstract": true,
           "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "position": 0}
        Existing custom: grouping headers already stored (shown with [abstract] in query_hierarchy)
        can be used directly as a parent — no need to re-propose them.

        If path and order_key are provided, they are validated against the stored
        hierarchy. If omitted, they are automatically computed under the parent.
        Call validate_hierarchy() afterwards to check the proposal, then finalize_hierarchy().
        """

        import json as _json

        try:
            parsed = _json.loads(placements_json)
        except _json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        proposal["rows"] = parsed
        result = plan_hierarchy(
            parsed,
            known_concepts=[r.get("concept") for r in stored_rows],
            stored_rows=stored_rows,
        )
        if not result.valid:
            return "Placement REJECTED:\n" + "\n".join(f"  - {e}" for e in result.errors)
        placed_summary = ", ".join(f"{r.concept} → {r.path} (order {r.order_key})" for r in result.rows)
        return (
            f"Placements accepted: {len(result.rows)} new concept(s) placed: {placed_summary}. "
            f"Call validate_hierarchy() to confirm, then finalize_hierarchy()."
        )

    @tool
    def propose_hierarchy(rows_json: str) -> str:
        """Submit the DESIRED hierarchy as a JSON array of rows.

        Each row:
          {"concept": "us-gaap:Revenues",
           "parent": null,                  // parent concept, or null for a root (001, 002...)
           "position": 0,                   // 0-based order among its siblings
           "path": "001",                   // optional explicit path (use "555" for non-relevant concepts)
           "order_key": "a",               // optional explicit order key
           "abstract": false}              // true ONLY for custom: grouping headers (see below)

        CUSTOM GROUPING HEADERS — when revenue or other line items have multiple
        segmentation dimensions (e.g. both product and geographic breakdowns),
        create an intermediate custom: grouping row as a named parent.  Use the
        naming convention custom:<PascalCase>:
          {"concept": "custom:ProductSegmentation",  "abstract": true,  "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "position": 0}
          {"concept": "custom:GeographicSegmentation","abstract": true, "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "position": 1}
        Then nest each segment member under its grouping header:
          {"concept": "aapl:IPhoneMember",            "parent": "custom:ProductSegmentation",   "position": 0}
          {"concept": "aapl:AmericasSegmentMember",   "parent": "custom:GeographicSegmentation","position": 0}
        Use "abstract": true ONLY for custom: grouping headers.  Never mark
        standard us-gaap: or company-specific XBRL concepts as abstract.

        Other rules enforced automatically (paths and order keys are computed for
        you when omitted — or validated when explicitly supplied):
          * only include a row ONCE;
          * ``parent`` must be another proposed concept or one that already
            exists in the stored hierarchy;
          * positions must be unique among siblings;
          * do NOT set path "555" for custom: grouping headers; they are structural parents that must remain under their parent line item;
          * do NOT hide rows; instead set ``"path": "555"`` for concepts that
            are non-relevant, auxiliary, or supplementary disclosures.

        Call validate_hierarchy afterwards to check the proposal.
        """
        import json as _json

        try:
            parsed = _json.loads(rows_json)
        except _json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"

        filing_concept_names = {c.get("concept") for c in filing_concepts if c.get("concept")}
        proposed_concepts = {
            r.get("concept") for r in parsed if isinstance(r, dict) and r.get("concept")
        }
        if not stored_rows:
            missing = filing_concept_names - proposed_concepts
            if missing:
                sample = sorted(list(missing))[:10]
                return (
                    f"Proposal REJECTED: Your proposal is missing {len(missing)} concept(s) "
                    f"reported by this filing: {', '.join(sample)}{'...' if len(missing) > 10 else ''}.\n"
                    f"You MUST include and place ALL {len(filing_concept_names)} concepts reported by the filing.\n"
                    f"For multi-dimensional segment breakdowns (like products and geographies), create custom: grouping headers "
                    f"such as custom:ProductSegmentation and custom:GeographicSegmentation with 'abstract': true, "
                    f"and place the segment members under their respective header."
                )

        proposal["rows"] = parsed
        result = plan_hierarchy(
            parsed,
            known_concepts=[r.get("concept") for r in stored_rows],
            stored_rows=stored_rows,
        )
        if not result.valid:
            return "Proposal REJECTED:\n" + "\n".join(f"  - {e}" for e in result.errors)
        return (
            f"Proposal accepted: {len(result.rows)} row(s), max depth {result.max_depth}. "
            f"Paths computed. Call validate_hierarchy() to confirm, then finalize_hierarchy()."
        )


    @tool
    def validate_hierarchy() -> str:
        """Validate the proposal submitted with propose_hierarchy() or place_new_concepts().

        Returns OK plus a summary, or the exact errors to fix.
        """
        rows = proposal.get("rows")
        if not rows:
            return "No proposal submitted yet — call place_new_concepts() or propose_hierarchy() first."
        
        filing_concept_names = {c.get("concept") for c in filing_concepts if c.get("concept")}
        if not stored_rows:
            proposed_concepts = {
                r.get("concept") for r in rows if isinstance(r, dict) and r.get("concept")
            }
            missing = filing_concept_names - proposed_concepts
            if missing:
                sample = sorted(list(missing))[:10]
                return (
                    f"INVALID: Proposal is missing {len(missing)} concept(s) reported by this filing: "
                    f"{', '.join(sample)}{'...' if len(missing) > 10 else ''}.\n"
                    f"Every reported concept must be placed in the proposal tree."
                )

        result = plan_hierarchy(
            rows,
            known_concepts=[r.get("concept") for r in stored_rows],
            stored_rows=stored_rows,
        )
        if not result.valid:
            return "INVALID:\n" + "\n".join(f"  - {e}" for e in result.errors)
        hidden = [r.concept for r in result.rows if r.hide]
        abstracts = [r.concept for r in result.rows if r.abstract]
        return (
            f"OK — {len(result.rows)} row(s), max depth {result.max_depth}, "
            f"{len(abstracts)} grouping header(s), {len(hidden)} hidden row(s).\n"
            f"Sample placement: "
            + ", ".join(f"{r.concept}→{r.path}" for r in result.rows[:5])
        )

    return [query_hierarchy, propose_hierarchy, place_new_concepts, validate_hierarchy]


def build_concept_tools(
    stored_rows: list[dict],
    incoming_concepts: list[dict],
    decisions: dict,
):
    """Build the toolset for the concept-resolution agent (one statement).

    ``decisions`` is a single-element dict the tools write into — the node reads
    it after the loop finishes.  The agent can ONLY pick a ``same_as`` target
    from ``stored_rows`` (validated by the tool) or ``null``; it can never
    invent a target or a concept name.
    """
    stored_names: set[str] = {r.get("concept") for r in stored_rows if r.get("concept")}
    stored_labels: dict[str, str] = {
        r.get("concept"): (r.get("label") or "")
        for r in stored_rows if r.get("concept")
    }
    max_stored = 300

    @tool
    def query_stored_concepts() -> str:
        """List the STORED concepts for this company+statement — the only valid
        ``same_as`` targets.

        Read this first.  Each row is ``concept | label | path``.  Only these
        names may be used as ``same_as`` targets; everything else must be new.
        """
        if not stored_rows:
            return "NO STORED CONCEPTS — every incoming concept must be created as new."
        lines = []
        for r in sorted(stored_rows, key=lambda x: (x.get("path") or "", x.get("order_key") or "")):
            lines.append(
                f"{r.get('concept', '?')}  |  {r.get('label') or ''}  |  {r.get('path') or '-'}"
            )
            if len(lines) >= max_stored:
                lines.append(f"… {len(stored_rows) - max_stored} more stored concept(s) omitted")
                break
        return f"STORED CONCEPTS ({len(stored_rows)} total):\n" + "\n".join(lines)

    @tool
    def decide_mapping(concept_json: str) -> str:
        """Record a decision for ONE new concept.

        Pass a JSON object:
          {"concept": "us-gaap:Revenues",
           "same_as": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
           "reason": "ASC 606 renamed total revenue"}

        ``same_as`` must be null (create a new concept) or the name of an
        EXISTING stored concept (alias — the same economic line item).  You
        decide equivalence only; which tag name survives is decided by period
        (newest wins), not by you.  ``concept`` must be one of the NEW concepts
        from this filing.
        """
        import json as _json

        try:
            parsed = _json.loads(concept_json)
        except _json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        concept = parsed.get("concept")
        if not isinstance(concept, str) or not concept:
            return "Error: 'concept' must be a non-empty string."
        same_as = parsed.get("same_as")
        if same_as is not None and not isinstance(same_as, str):
            return f"Error: 'same_as' must be null or a string, got {same_as!r}."
        if same_as is not None and same_as not in stored_names:
            return (
                f"Error: '{same_as}' is NOT a stored concept. "
                f"It must be null or one of: {', '.join(sorted(stored_names)[:20])}"
                + (" …" if len(stored_names) > 20 else "")
            )
        decisions[concept] = {
            "same_as": same_as,
            "reason": parsed.get("reason") or "",
            "label": stored_labels.get(same_as or ""),
        }
        if same_as is None:
            return f"OK — '{concept}' will be created as its own new concept."
        return f"OK — '{concept}' merges into existing '{same_as}'."

    return [query_stored_concepts, decide_mapping]
