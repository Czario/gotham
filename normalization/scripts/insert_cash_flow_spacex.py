#!/usr/bin/env python3
"""
Insert cash flow statement labels for SpaceX (CIK: 0001181412) into
normalized_concepts_annual (10-K) and normalized_concepts_quarterly (10-Q).

Idempotent: uses update_one with upsert=True keyed on
(company_cik, statement_type, concept, form_type).

Run:
    uv run python scripts/insert_cash_flow_spacex.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

# ---------------------------------------------------------------------------
# Cash flow concept definitions
# Paths are materialized paths (001, 001.001, etc.)
# order_keys are hierarchically scoped lexicographic keys.
# abstract is False for all items per business requirement.
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ── Operating activities ─────────────────────────────────────────────────
    {
        "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "label": "Net cash provided by operating activities",
        "path": "001",
        "order_key": "a",
    },
    {
        "concept": "us-gaap:NetIncomeLoss",
        "label": "Net loss",
        "path": "001.001",
        "order_key": "aa",
    },
    {
        "concept": "us-gaap:AdjustmentsToReconcileNetIncomeLossToCashProvidedByUsedInOperatingActivities",
        "label": "Adjustments to reconcile net loss to net cash provided by operating activities",
        "path": "001.002",
        "order_key": "ab",
    },
    {
        "concept": "us-gaap:DepreciationDepletionAndAmortization",
        "label": "Depreciation and amortization",
        "path": "001.002.001",
        "order_key": "aba",
    },
    {
        "concept": "us-gaap:ShareBasedCompensation",
        "label": "Share-based compensation",
        "path": "001.002.002",
        "order_key": "abb",
    },
    {
        "concept": "custom:UnrealizedLossOnDigitalAssets",
        "label": "Unrealized loss on digital assets",
        "path": "001.002.003",
        "order_key": "abc",
    },
    {
        "concept": "custom:ImpairmentAndLossOnDisposalOfFixedAssetsNet",
        "label": "Impairment and loss on disposal of fixed assets, net",
        "path": "001.002.004",
        "order_key": "abd",
    },
    {
        "concept": "us-gaap:AmortizationOfFinancingCosts",
        "label": "Amortization of debt discount and issuance costs",
        "path": "001.002.005",
        "order_key": "abe",
    },
    {
        "concept": "us-gaap:GainsLossesOnExtinguishmentOfDebt",
        "label": "Loss on debt extinguishment",
        "path": "001.002.006",
        "order_key": "abf",
    },
    {
        "concept": "custom:OtherOperatingAdjustments",
        "label": "Other",
        "path": "001.002.007",
        "order_key": "abg",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInOperatingCapital",
        "label": "Changes in operating assets and liabilities",
        "path": "001.002.008",
        "order_key": "abh",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInAccountsReceivable",
        "label": "Accounts receivable",
        "path": "001.002.008.001",
        "order_key": "abha",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInInventories",
        "label": "Inventory",
        "path": "001.002.008.002",
        "order_key": "abhb",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
        "label": "Prepaid expenses and other assets",
        "path": "001.002.008.003",
        "order_key": "abhc",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInAccountsPayable",
        "label": "Accounts payable",
        "path": "001.002.008.004",
        "order_key": "abhd",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInDeferredRevenue",
        "label": "Deferred revenue",
        "path": "001.002.008.005",
        "order_key": "abhe",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInOperatingLeaseLiability",
        "label": "Operating lease liabilities, net",
        "path": "001.002.008.006",
        "order_key": "abhf",
    },
    {
        "concept": "us-gaap:IncreaseDecreaseInOtherOperatingLiabilities",
        "label": "Other liabilities",
        "path": "001.002.008.007",
        "order_key": "abhg",
    },
    # ── Investing activities ─────────────────────────────────────────────────
    {
        "concept": "us-gaap:NetCashProvidedByUsedInInvestingActivities",
        "label": "Net cash used in investing activities",
        "path": "002",
        "order_key": "b",
    },
    {
        "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "label": "Purchases of property, plant, and equipment",
        "path": "002.001",
        "order_key": "ba",
    },
    {
        "concept": "custom:CapitalizedInterest",
        "label": "Capitalized interest",
        "path": "002.002",
        "order_key": "bb",
    },
    {
        "concept": "custom:ProceedsFromProductRebates",
        "label": "Proceeds from product rebates",
        "path": "002.003",
        "order_key": "bc",
    },
    {
        "concept": "us-gaap:PaymentsToAcquireMarketableSecurities",
        "label": "Purchases of marketable securities",
        "path": "002.004",
        "order_key": "bd",
    },
    {
        "concept": "us-gaap:ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
        "label": "Maturities of marketable securities",
        "path": "002.005",
        "order_key": "be",
    },
    {
        "concept": "us-gaap:PaymentsForProceedsFromOtherInvestingActivities",
        "label": "Other investing activities, net",
        "path": "002.006",
        "order_key": "bf",
    },
    # ── Financing activities ─────────────────────────────────────────────────
    {
        "concept": "us-gaap:NetCashProvidedByUsedInFinancingActivities",
        "label": "Net cash provided by financing activities",
        "path": "003",
        "order_key": "c",
    },
    {
        "concept": "us-gaap:FinanceLeasePrincipalPayments",
        "label": "Principal repayments on finance leases",
        "path": "003.001",
        "order_key": "ca",
    },
    {
        "concept": "us-gaap:ProceedsFromIssuanceOfDebt",
        "label": "Proceeds from debt and other financing obligations",
        "path": "003.002",
        "order_key": "cb",
    },
    {
        "concept": "us-gaap:PaymentsOfDebtIssuanceCosts",
        "label": "Payment of debt issuance costs",
        "path": "003.003",
        "order_key": "cc",
    },
    {
        "concept": "us-gaap:RepaymentsOfDebt",
        "label": "Repayments on debt and other financing obligations",
        "path": "003.004",
        "order_key": "cd",
    },
    {
        "concept": "custom:PaymentOfDebtExtinguishmentPremium",
        "label": "Payment of debt extinguishment premium",
        "path": "003.005",
        "order_key": "ce",
    },
    {
        "concept": "us-gaap:ProceedsFromIssuanceOfCommonStock",
        "label": "Proceeds from issuance of capital stock, net of issuance costs",
        "path": "003.006",
        "order_key": "cf",
    },
    {
        "concept": "us-gaap:ProceedsFromStockOptionsExercised",
        "label": "Proceeds from employee equity award plans",
        "path": "003.007",
        "order_key": "cg",
    },
    {
        "concept": "us-gaap:PaymentsForRepurchaseOfCommonStock",
        "label": "Payments for repurchase of common and redeemable convertible preferred stock",
        "path": "003.008",
        "order_key": "ch",
    },
    {
        "concept": "us-gaap:PaymentsRelatedToTaxWithholdingForShareBasedCompensation",
        "label": "Taxes paid related to net share settlement of equity awards",
        "path": "003.009",
        "order_key": "ci",
    },
    # ── Totals and reconciliation ────────────────────────────────────────────
    {
        "concept": "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "label": "Effect of exchange rate changes on cash and cash equivalents",
        "path": "004",
        "order_key": "d",
    },
    {
        "concept": "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "label": "Net change in cash and cash equivalents and restricted cash",
        "path": "005",
        "order_key": "e",
    },
    {
        "concept": "custom:CashCashEquivalentsAndRestrictedCashBeginningOfPeriod",
        "label": "Cash and cash equivalents and restricted cash, beginning of the period",
        "path": "006",
        "order_key": "f",
    },
    {
        "concept": "custom:CashCashEquivalentsAndRestrictedCashEndOfPeriod",
        "label": "Cash and cash equivalents and restricted cash, end of the period",
        "path": "007",
        "order_key": "g",
    },
]

COMPANY_CIK = "0001181412"
STATEMENT_TYPE = "cash_flows"


def build_document(concept_def: dict, form_type: str, now: datetime) -> dict:
    """Build a full concept document matching the normalized_concepts schema."""
    return {
        "company_cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "concept": concept_def["concept"],
        "form_type": form_type,
        "label": concept_def["label"],
        "path": concept_def["path"],
        "order_key": concept_def["order_key"],
        "abstract": False,
        "dimension": False,
        "dimension_concept": False,
        "hide": False,
        "active": True,
        "merge": False,
        "created_at": now,
        "updated_at": now,
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "__v": 0,
    }


def upsert_concepts(collection, form_type: str, now: datetime) -> tuple[int, int]:
    """
    Upsert all concepts into collection.
    Returns (inserted_count, matched_count).
    """
    inserted = 0
    matched = 0

    for concept_def in CONCEPTS:
        doc = build_document(concept_def, form_type, now)
        filter_key = {
            "company_cik": COMPANY_CIK,
            "statement_type": STATEMENT_TYPE,
            "concept": concept_def["concept"],
            "form_type": form_type,
        }
        result = collection.update_one(
            filter_key,
            {
                "$set": {k: v for k, v in doc.items() if k != "created_at"},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

        if result.upserted_id:
            inserted += 1
        else:
            matched += 1

    return inserted, matched


def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)
    db = client[config.database.database_name]
    now = datetime.now(timezone.utc)

    collections = {
        "normalized_concepts_annual": "10-K",
        "normalized_concepts_quarterly": "10-Q",
    }

    total_inserted = 0
    total_matched = 0

    for collection_name, form_type in collections.items():
        col = db[collection_name]
        inserted, matched = upsert_concepts(col, form_type, now)
        total_inserted += inserted
        total_matched += matched
        print(
            f"[{collection_name}] ({form_type})  "
            f"inserted={inserted}  already_existed={matched}"
        )

    print(
        f"\nDone — total inserted: {total_inserted}, "
        f"total already existed / updated: {total_matched}"
    )
    client.close()


if __name__ == "__main__":
    main()
