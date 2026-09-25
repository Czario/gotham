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
    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }
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


def build_review_tools(bundles: list[Any], proposals: list[dict[str, Any]]) -> list[Any]:
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
