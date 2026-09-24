"""Plausibility checks — cheap sanity nets on the values themselves.

These are deliberately conservative (medium/low severity) so they surface data
problems without blocking otherwise-fine filings.  Only ``high`` findings refuse
persistence, and nothing here is high.
"""
from __future__ import annotations

from typing import Any

from .common import local_name, numeric_items, statement_label
from .findings import LOW, MEDIUM, Finding

# A value this large (with no plausible scale) is almost certainly a
# unit/scale mistake rather than a real fact.
_IMPLAUSIBLE_ABS = 1e15

# Concepts that can never legitimately be negative.
_NON_NEGATIVE_LOCALS = frozenset({
    "Assets",
    "AssetsCurrent",
    "AssetsNoncurrent",
    "Liabilities",
    "LiabilitiesCurrent",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
})


def check_plausibility(bundle: Any) -> list[Finding]:
    findings: list[Finding] = []
    st = statement_label(bundle)
    items = numeric_items(bundle)
    if not items:
        return findings

    implausible: list[dict] = []
    unexpected_negative: list[str] = []
    values: list[float] = []

    for item in items:
        concept = item.get("concept") or ""
        value = float(item["value"])
        values.append(value)

        if abs(value) > _IMPLAUSIBLE_ABS:
            implausible.append({"concept": concept, "value": value})

        if value < 0 and local_name(concept) in _NON_NEGATIVE_LOCALS:
            unexpected_negative.append(concept)

    if implausible:
        findings.append(
            Finding(
                "implausible_magnitude",
                MEDIUM,
                f"{st}: {len(implausible)} value(s) exceed the plausible range "
                f"(first: {implausible[0]['concept']}={implausible[0]['value']:.3g})",
                statement_type=st,
                evidence={"samples": implausible[:5]},
            )
        )

    if unexpected_negative:
        findings.append(
            Finding(
                "unexpected_negative_value",
                MEDIUM,
                f"{st}: {len(unexpected_negative)} normally-positive concept(s) "
                f"are negative: {', '.join(unexpected_negative[:5])}",
                statement_type=st,
                evidence={"concepts": unexpected_negative[:10]},
            )
        )

    if len(values) >= 3 and len(set(values)) == 1:
        findings.append(
            Finding(
                "all_values_identical",
                LOW,
                f"{st}: all {len(values)} values are identical ({values[0]:,.0f}) — "
                f"possible extraction/scale problem",
                statement_type=st,
            )
        )

    return findings
