"""Tools for the sign subagent.

Read/fix-only and entirely in-memory: they inspect the bundles and mutate the
in-memory ``value`` of a row.  No MongoDB access happens here — the persist
node stays the single writer, so a sign fix can never touch the database on its
own.

``set_sign`` is deliberately a *set* (``value = sign * abs(value)``), never a
tail, so running the subagent twice cannot flip a value back.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from ..signs import policy


def _numeric_rows(bundles: list[Any]) -> list[dict]:
    rows: list[dict] = []
    for bundle in bundles:
        statement_type = getattr(bundle, "statement_type", "") or ""
        for item in (getattr(bundle, "concepts", None) or []):
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            rows.append(
                {
                    "bundle": bundle,
                    "statement_type": statement_type,
                    "item": item,
                    "concept": item.get("concept") or "",
                    "label": item.get("label"),
                    "value": float(value),
                }
            )
    return rows


def _sync_flattened(bundle: Any, concept: str, value: float) -> None:
    """Keep the informational flattened views consistent with the row."""
    for attr in ("values", "dimensional_values"):
        for entry in (getattr(bundle, attr, None) or []):
            if isinstance(entry, dict) and entry.get("concept") == concept:
                entry["value"] = value


def build_sign_tools(bundles: list[Any], proposal: dict) -> list[Any]:
    """Build the sign subagent's tools bound to *bundles* and *proposal*."""

    rows = _numeric_rows(bundles)

    def _find(statement_type: str, concept: str) -> dict | None:
        for row in rows:
            if row["statement_type"] == statement_type and row["concept"] == concept:
                return row
        return None

    @tool
    def list_sign_candidates() -> str:
        """List every row this filing has that is covered by a sign convention.

        For each row it shows the statement, concept, label, current value, the
        sign it currently carries and the sign it must carry.  Start here.
        """
        covered = [c for b in bundles for c in policy.classify(b)]
        if not covered:
            return "No row in this filing is covered by a sign convention — nothing to fix."
        lines = [
            f"{c['statement_type']} | {c['concept']} | {c['label']} | "
            f"value={c['value']:g} | has={c['current_sign']} | must={c['required_sign']} | "
            f"{'OK' if c['compliant'] else 'VIOLATION'}"
            for c in covered
        ]
        return f"{len(covered)} covered row(s):\n" + "\n".join(lines)

    @tool
    def query_statement_rows(statement_type: str) -> str:
        """List every numeric row of one statement.

        Pass one of ``income`` / ``balancesheet`` / ``cashflow``.  Use this to
        judge a row that looks like one of the conventions but is not covered
        by it (for example a "net interest" or a working-capital delta).
        """
        target = (statement_type or "").strip().lower()
        selected = [r for r in rows if r["statement_type"] == target]
        if not selected:
            return f"No numeric rows for statement_type={target!r}."
        lines = [
            f"{r['concept']} | {r['label']} | value={r['value']:g}"
            for r in selected
        ]
        return f"{len(selected)} row(s) in {target}:\n" + "\n".join(lines)

    @tool
    def set_sign(row_json: str) -> str:
        """Force one row to its required sign (idempotent — it sets, never flips).

        Pass ``{"statement_type": "...", "concept": "..."}``.  Add
        ``"sign": "positive"|"negative"`` only to override, and ``"reason"`` to
        record why.  The magnitude is always preserved.
        """
        try:
            parsed = json.loads(row_json)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        if not isinstance(parsed, dict):
            return "Error: row_json must be a JSON object."

        statement_type = parsed.get("statement_type")
        concept = parsed.get("concept")
        if not statement_type or not concept:
            return "Error: 'statement_type' and 'concept' are both required."

        sign = parsed.get("sign") or policy.required_sign(statement_type, concept)
        if sign not in (policy.POSITIVE, policy.NEGATIVE):
            return (
                f"Error: no convention covers {statement_type}/{concept}. Only pass an "
                'explicit "sign" when you are certain it is one of the declared rule '
                "families; otherwise leave the row alone."
            )

        row = _find(statement_type, concept)
        if row is None:
            return f"Error: no numeric row {statement_type}/{concept} in this filing."

        old = row["value"]
        if old == 0:
            return f"{statement_type}/{concept} is 0 — no sign to set."

        new = abs(old) if sign == policy.POSITIVE else -abs(old)
        if new == old:
            return f"{statement_type}/{concept} already {sign} ({old:g}) — unchanged."

        row["item"]["value"] = new
        row["item"]["sign_fix_source"] = "sign_agent"
        row["item"]["sign_fix_old_value"] = old
        row["value"] = new
        _sync_flattened(row["bundle"], concept, new)
        proposal.setdefault("fixes", []).append(
            {
                "statement_type": statement_type,
                "concept": concept,
                "old_value": old,
                "new_value": new,
                "required_sign": sign,
                "reason": parsed.get("reason"),
                "source": "sign_agent",
            }
        )
        return (
            f"OK — {statement_type}/{concept}: {old:g} → {new:g} ({sign}). "
            "Call check_signs() to confirm nothing is left."
        )

    @tool
    def check_signs() -> str:
        """Critic: report every covered row that still has the wrong sign.

        Call this after your fixes; finalize only when it reports clean.
        """
        remaining = policy.violations(bundles)
        if not remaining:
            return "OK — every convention-covered row carries the required sign."
        lines = [
            f"{r['statement_type']} | {r['concept']} | value={r['value']:g} | "
            f"has={r['current_sign']} | must={r['required_sign']}"
            for r in remaining
        ]
        return (
            f"{len(remaining)} sign violation(s) remaining — fix with set_sign() "
            "or explain why the row is exempt:\n" + "\n".join(lines)
        )

    return [list_sign_candidates, query_statement_rows, set_sign, check_signs]
