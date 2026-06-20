"""
Concept canonicalization for cross-era continuity.

SEC filers re-tag the SAME economic line item under different us-gaap concept
names when FASB accounting standards change (e.g. ASC 606 revenue, ASU 2016-18
restricted cash, continuing-operations cash-flow variants). When each concept is
viewed in isolation the time series looks like it has gaps, even though the value
was correctly extracted under a sibling/successor concept.

`canonical_concept()` maps each known concept variant to a single canonical name
so downstream consumers get one continuous series per economic line item.

The mapping is deterministic and based on documented FASB standard transitions —
no inference or LLM involved. Concepts not in any equivalence class map to
themselves (identity), so this is always safe to apply.
"""
from typing import Dict

# Each tuple is an equivalence class of us-gaap concepts that represent the same
# economic line item across accounting-standard eras. The FIRST element is the
# canonical name (preferred modern/most-common tag).
_EQUIVALENCE_CLASSES = [
    # --- Cash flow statement totals: continuing-operations variants ---
    # Pre/post ASU presentations and filers that tag the "...ContinuingOperations"
    # form when they have no discontinued operations.
    (
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    (
        "us-gaap:NetCashProvidedByUsedInInvestingActivities",
        "us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ),
    (
        "us-gaap:NetCashProvidedByUsedInFinancingActivities",
        "us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ),
    # --- Revenue: ASC 606 (effective 2018) and predecessors ---
    (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:Revenues",
        "us-gaap:SalesRevenueNet",
        "us-gaap:SalesRevenueGoodsNet",
        "us-gaap:SalesRevenueServicesNet",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    # --- Net change in cash: ASU 2016-18 restricted-cash inclusion (2018) ---
    (
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease",
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
    ),
    # --- Cash & equivalents period-end balance: ASU 2016-18 ---
    (
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    ),
    # --- Effect of FX on cash: ASU 2016-18 ---
    (
        "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:EffectOfExchangeRateOnCashAndCashEquivalents",
        "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsContinuingOperations",
    ),
    # --- Income before taxes: minority-interest/equity-method label change ---
    (
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ),
    # --- Long-term debt, noncurrent: pre/post ASU 2015-03 presentations ---
    (
        "us-gaap:LongTermDebtNoncurrent",
        "us-gaap:LongTermDebt",
    ),
    # --- Repayments of long-term debt ---
    (
        "us-gaap:RepaymentsOfLongTermDebt",
        "us-gaap:RepaymentsOfDebt",
    ),
    # --- Dividends paid ---
    (
        "us-gaap:PaymentsOfDividendsCommonStock",
        "us-gaap:PaymentsOfDividends",
    ),
    # --- Income taxes paid: net-of-refunds label variant ---
    (
        "us-gaap:IncomeTaxesPaidNet",
        "us-gaap:IncomeTaxesPaid",
    ),
    # --- Interest paid: net-of-capitalized label variant ---
    (
        "us-gaap:InterestPaidNet",
        "us-gaap:InterestPaid",
    ),
    # --- Operating lease ROU asset (ASC 842) has no true predecessor; left alone ---
]

# Flat lookup: variant concept -> canonical concept
_CANONICAL: Dict[str, str] = {}
for _cls in _EQUIVALENCE_CLASSES:
    _canon = _cls[0]
    for _variant in _cls:
        _CANONICAL[_variant] = _canon


def canonical_concept(concept: str) -> str:
    """Return the canonical concept name for an equivalence class.

    Concepts not in any known class map to themselves (identity).
    """
    if not concept:
        return concept
    return _CANONICAL.get(concept, concept)


def is_canonicalized(concept: str) -> bool:
    """True if the concept belongs to a known equivalence class (i.e. its
    canonical form differs from itself)."""
    return concept in _CANONICAL and _CANONICAL[concept] != concept


def equivalence_class(concept: str) -> tuple:
    """Return all concept names in the same equivalence class (including the
    concept itself). For an unknown concept, returns a single-element tuple.

    Used by the companyfacts reconciliation pass to look up a value under any
    of the equivalent us-gaap tags a filer may have used across eras.
    """
    canon = canonical_concept(concept)
    for cls in _EQUIVALENCE_CLASSES:
        if cls[0] == canon:
            return cls
    return (concept,)
