"""Accounting-identity (math) checks.

Each identity is only evaluated when EVERY concept it needs is present in the
bundle — a missing concept is not an error here (banks have no gross profit,
etc.).  When all inputs exist but the arithmetic does not hold, the finding is
``high`` severity and refuses persistence under ``STRICT_ACCURACY``.

Sign conventions differ between filers (costs reported positive or negative),
so each identity is accepted when EITHER the stored signs or the
absolute-value-of-subtrahends variant reconciles.  This keeps the check free of
false positives on sign style while still catching genuinely wrong numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .common import concept_value_map, statement_label
from .findings import HIGH, Finding

# Relative/absolute tolerance for an identity to be considered satisfied.
# XBRL facts are exact; the slack absorbs ``decimals`` rounding differences.
REL_TOLERANCE = 0.005   # 0.5 %
ABS_TOLERANCE = 1.0

_REVENUE_ALIASES: tuple[str, ...] = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:SalesRevenueGoodsNet",
)
_COST_ALIASES: tuple[str, ...] = (
    "us-gaap:CostOfRevenue",
    "us-gaap:CostOfGoodsAndServicesSold",
    "us-gaap:CostOfGoodsSold",
    "us-gaap:CostOfServices",
)
_OPEX_ALIASES: tuple[str, ...] = (
    "us-gaap:OperatingExpenses",
    "us-gaap:CostsAndExpenses",
)
_EQUITY_ALIASES: tuple[str, ...] = (
    "us-gaap:StockholdersEquity",
    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)


@dataclass(frozen=True)
class SumIdentity:
    """``target == Σ (sign × term)`` for a set of concept aliases."""

    name: str
    target: tuple[str, ...]
    terms: tuple[tuple[tuple[str, ...], int], ...]
    statement_type: str


_INCOME_IDENTITIES: tuple[SumIdentity, ...] = (
    SumIdentity(
        "gross_profit",
        ("us-gaap:GrossProfit",),
        ((_REVENUE_ALIASES, 1), (_COST_ALIASES, -1)),
        "income",
    ),
    SumIdentity(
        "operating_income",
        ("us-gaap:OperatingIncomeLoss",),
        ((("us-gaap:GrossProfit",), 1), (_OPEX_ALIASES, -1)),
        "income",
    ),
)

_BALANCE_IDENTITIES: tuple[SumIdentity, ...] = (
    SumIdentity(
        "assets_equals_liabilities_and_equity",
        ("us-gaap:Assets",),
        ((("us-gaap:LiabilitiesAndStockholdersEquity",), 1),),
        "balancesheet",
    ),
    SumIdentity(
        "assets_equals_liabilities_plus_equity",
        ("us-gaap:Assets",),
        ((("us-gaap:Liabilities",), 1), (_EQUITY_ALIASES, 1)),
        "balancesheet",
    ),
)

_IDENTITIES_BY_STATEMENT = {
    "income": _INCOME_IDENTITIES,
    "balancesheet": _BALANCE_IDENTITIES,
}


def _resolve(vmap: dict[str, float], aliases: Sequence[str]) -> tuple[Optional[str], Optional[float]]:
    for alias in aliases:
        if alias in vmap:
            return alias, vmap[alias]
    return None, None


def _close(target: float, expected: float) -> bool:
    return abs(target - expected) <= max(abs(target) * REL_TOLERANCE, ABS_TOLERANCE)


def check_math(bundle: Any) -> list[Finding]:
    """Run the accounting identities applicable to this statement type."""
    identities = _IDENTITIES_BY_STATEMENT.get(statement_label(bundle), ())
    if not identities:
        return []

    vmap = concept_value_map(bundle)
    if not vmap:
        return []

    findings: list[Finding] = []
    st = statement_label(bundle)

    for identity in identities:
        target_name, target_value = _resolve(vmap, identity.target)
        if target_value is None:
            continue  # nothing to check

        used: dict[str, float] = {}
        expected_stored = 0.0
        expected_abs_subtrahends = 0.0
        complete = True
        for aliases, sign in identity.terms:
            term_name, term_value = _resolve(vmap, aliases)
            if term_value is None:
                complete = False  # a required input is absent → skip, never guess
                break
            used[term_name] = term_value
            expected_stored += sign * term_value
            expected_abs_subtrahends += sign * (abs(term_value) if sign < 0 else term_value)

        if not complete:
            continue

        if _close(target_value, expected_stored) or _close(target_value, expected_abs_subtrahends):
            continue

        findings.append(
            Finding(
                "math_mismatch",
                HIGH,
                (
                    f"{st}: {identity.name} does not reconcile — "
                    f"{target_name}={target_value:,.0f} but expected "
                    f"{expected_stored:,.0f} from {', '.join(f'{k}={v:,.0f}' for k, v in used.items())}"
                ),
                statement_type=st,
                concept=target_name,
                evidence={
                    "identity": identity.name,
                    "target_concept": target_name,
                    "target_value": target_value,
                    "expected_value": expected_stored,
                    "expected_abs_subtrahends": expected_abs_subtrahends,
                    "difference": target_value - expected_stored,
                    "inputs": used,
                },
            )
        )

    return findings
