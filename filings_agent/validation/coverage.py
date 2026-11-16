"""Coverage validation — checks presence of mandatory financial statement concepts.

If an income statement lacks revenue, or a balance sheet lacks assets, this
validator raises a finding so the review agent or fallback extractor can intervene.
"""
from __future__ import annotations

from typing import Any

from .findings import HIGH, Finding

MANDATORY_CONCEPTS = {
    "income": {
        "tags": {
            "Revenues",
            "Revenue",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RevenueFromContractWithCustomer",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "RevenueFromSaleOfGoods",
            "OperatingIncomeLoss",
            "IncomeLossFromOperations",
        },
        # Open semantic keywords (concept local name or label) so any new
        # US-GAAP, IFRS, bank/insurer, or custom tag is accepted and passed
        # to the ADK Hierarchy Agent instead of being blocked.
        "keywords": (
            "revenue", "sales", "turnover", "operatingincome", "operating income",
            "operating profit", "netincome", "net income", "profitloss",
            "interestincome", "interest income", "feeincome", "premium",
        ),
        "description": "Revenue or Operating Income",
    },
    "balance": {
        "tags": {
            "Assets",
            "Liabilities",
            "StockholdersEquity",
            "LiabilitiesAndStockholdersEquity",
        },
        "keywords": ("asset", "liabilit", "equity"),
        "description": "Assets or Liabilities",
    },
    "cash_flow": {
        "tags": {
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInInvestingActivities",
            "NetCashProvidedByUsedInFinancingActivities",
        },
        "keywords": ("operating", "investing", "financing", "cashflow", "cash flow"),
        "description": "Operating Cash Flow",
    },
}


def check_coverage(bundle: Any) -> list[Finding]:
    """Check that mandatory concepts for the statement type exist with non-null values."""
    statement_type = getattr(bundle, "statement_type", "") or ""
    normalized_type = statement_type.lower().replace("-", "_").replace(" ", "_")
    if "income" in normalized_type:
        key = "income"
    elif "balance" in normalized_type:
        key = "balance"
    elif "cash" in normalized_type or "flow" in normalized_type:
        key = "cash_flow"
    else:
        return []

    rules = MANDATORY_CONCEPTS.get(key)
    if not rules:
        return []

    raw_concepts = getattr(bundle, "concepts", None) or []
    if not raw_concepts:
        return []

    concepts_present = set()
    searchable_text: list[str] = []
    for item in raw_concepts:
        if isinstance(item, dict) and item.get("value") is not None:
            c = item.get("concept") or ""
            local_name = c.split(":")[-1]
            concepts_present.add(local_name)
            searchable_text.append(local_name.lower())
            if item.get("label"):
                searchable_text.append(str(item["label"]).lower())

    # Check exact tags first, then open semantic keyword match on concept/label
    matched = bool(rules["tags"] & concepts_present)
    if not matched:
        keywords = rules.get("keywords") or ()
        matched = any(kw in txt for txt in searchable_text for kw in keywords)
    if not matched:
        return [
            Finding(
                type="missing_mandatory_concept",
                severity=HIGH,
                message=f"{statement_type} statement missing mandatory concept ({rules['description']}).",
                statement_type=statement_type,
                evidence={
                    "expected_any_of": sorted(list(rules["tags"])),
                    "total_concepts_with_value": len(concepts_present),
                },
            )
        ]

    return []
