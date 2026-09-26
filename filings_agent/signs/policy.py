"""Sign conventions for normalized statement values, and a deterministic critic.

The *convention* — which sign a family of concepts must carry — is declared
here in one place.  The *fixing* is done by the sign subagent through tools
(``tools/sign_tools.py``); this module never mutates a bundle, it only
classifies rows and reports violations — the same split as the hierarchy
critic (code checks, the agent decides and acts).

Families match the concept's LOCAL name (the part after ``:``), so a standard
``us-gaap:InterestExpenseDebt`` and a company-specific
``aapl:InterestExpenseDebt`` are treated identically — no per-tag enumeration
is needed.

Declared conventions
--------------------
* income statement   — interest EXPENSE is negative
* balance sheet      — accounts receivable (an asset) is positive
* cash flow          — share-based compensation (a non-cash add-back) is positive

Concepts that merely *contain* one of those words but are a genuinely different
line item (net interest, working-capital deltas, contra-asset allowances, cash
withholding/benefit flows) are excluded explicitly rather than by luck.
"""
from __future__ import annotations

import re
from typing import Any, Optional

POSITIVE = "positive"
NEGATIVE = "negative"

# (statement_type, family regex on the concept LOCAL name, required sign)
RULES: tuple[tuple[str, str, str], ...] = (
    # ── Income statement: interest expense is negative ──────────────────────
    ("income", r"^InterestExpense", NEGATIVE),
    ("income", r"^InterestAndDebtExpense$", NEGATIVE),
    # ── Balance sheet: receivables are assets, hence positive ───────────────
    ("balancesheet", r"^AccountsReceivable", POSITIVE),
    ("balancesheet", r"^AccountsNotesAndLoansReceivable", POSITIVE),
    ("balancesheet", r"^ReceivablesNetCurrent$", POSITIVE),
    ("balancesheet", r"^NotesReceivable", POSITIVE),
    ("balancesheet", r"^NontradeReceivables", POSITIVE),
    # ── Cash flow: share-based compensation is a positive non-cash add-back ─
    ("cashflow", r"^ShareBasedCompensation$", POSITIVE),
    ("cashflow", r"^AllocatedShareBasedCompensation", POSITIVE),
    (
        "cashflow",
        r"^ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost$",
        POSITIVE,
    ),
)

# Local names never touched, even when a family regex would otherwise match.
# These are the near-misses that make naive substring matching dangerous.
EXCLUSIONS: tuple[str, ...] = (
    r"^InterestIncome",          # interest INCOME, not expense
    r"^InterestIncomeExpenseNet",  # net interest — the sign is either way
    r"^AllowanceFor",            # contra-asset allowance — legitimately negative
    r"^IncreaseDecreaseIn",      # cash-flow working-capital deltas
    r"^Payments",                # cash outflows (withholding etc.)
    r"^Proceeds",                # cash inflows
    r"^ExcessTaxBenefit",        # financing tax benefit, not the SBC add-back
)

_EXCLUSION_RE = tuple(re.compile(p) for p in EXCLUSIONS)
_RULE_RE = tuple((st, re.compile(p), sign) for st, p, sign in RULES)


def _local(concept: str) -> str:
    return (concept or "").rsplit(":", 1)[-1]


def required_sign(statement_type: str, concept: str) -> Optional[str]:
    """The sign ``concept`` must carry, or ``None`` when no rule covers it."""
    local = _local(concept)
    if not local:
        return None
    if any(rx.match(local) for rx in _EXCLUSION_RE):
        return None
    for st, rx, sign in _RULE_RE:
        if st == statement_type and rx.match(local):
            return sign
    return None


def sign_of(value: float) -> Optional[str]:
    """``positive`` / ``negative`` / ``None`` (for zero)."""
    if value > 0:
        return POSITIVE
    if value < 0:
        return NEGATIVE
    return None


def _numeric(item: Any) -> Optional[float]:
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def classify(bundle: Any) -> list[dict]:
    """Every convention-covered numeric row of *bundle* with its signs.

    A zero value is treated as compliant (there is no sign to get wrong).
    """
    statement_type = getattr(bundle, "statement_type", "") or ""
    out: list[dict] = []
    for item in (getattr(bundle, "concepts", None) or []):
        value = _numeric(item)
        if value is None:
            continue
        concept = item.get("concept") or ""
        required = required_sign(statement_type, concept)
        if required is None:
            continue
        current = sign_of(value)
        out.append(
            {
                "statement_type": statement_type,
                "concept": concept,
                "label": item.get("label"),
                "value": value,
                "current_sign": current,
                "required_sign": required,
                "compliant": current is None or current == required,
            }
        )
    return out


def violations(bundles: Any) -> list[dict]:
    """Every covered row (across *bundles*) that still carries the wrong sign."""
    return [
        row
        for bundle in (bundles or [])
        for row in classify(bundle)
        if not row["compliant"]
    ]
