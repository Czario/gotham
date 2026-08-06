#!/usr/bin/env python3
"""
Insert balance sheet labels for SpaceX (CIK: 0001181412) into
normalized_concepts_annual (10-K) and normalized_concepts_quarterly (10-Q).

Idempotent: uses update_one with upsert=True keyed on
(company_cik, statement_type, concept, form_type).

Run:
    uv run python scripts/insert_balance_sheet_spacex.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

# ---------------------------------------------------------------------------
# Balance sheet concept definitions
# Paths are materialized paths (001, 001.001, etc.)
# order_keys are hierarchically scoped lexicographic keys.
# abstract is False for all items per business requirement.
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ── Assets ──────────────────────────────────────────────────────────────
    {
        "concept": "us-gaap:Assets",
        "label": "Total assets",
        "path": "001",
        "order_key": "a",
    },
    {
        "concept": "us-gaap:AssetsCurrent",
        "label": "Total current assets",
        "path": "001.001",
        "order_key": "aa",
    },
    {
        "concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "label": "Cash and cash equivalents",
        "path": "001.001.001",
        "order_key": "aaa",
    },
    {
        "concept": "us-gaap:MarketableSecuritiesCurrent",
        "label": "Marketable securities",
        "path": "001.001.002",
        "order_key": "aab",
    },
    {
        "concept": "us-gaap:AccountsReceivableNetCurrent",
        "label": "Accounts receivable, net of allowance for credit losses",
        "path": "001.001.003",
        "order_key": "aac",
    },
    {
        "concept": "us-gaap:InventoryNet",
        "label": "Inventory",
        "path": "001.001.004",
        "order_key": "aad",
    },
    {
        "concept": "us-gaap:PrepaidExpenseAndOtherAssetsCurrent",
        "label": "Prepaid expenses and other current assets",
        "path": "001.001.005",
        "order_key": "aae",
    },
    {
        "concept": "us-gaap:PropertyPlantAndEquipmentNet",
        "label": "Property, plant, and equipment, net",
        "path": "001.002",
        "order_key": "ab",
    },
    {
        "concept": "us-gaap:FinanceLeaseRightOfUseAsset",
        "label": "Finance lease right-of-use assets",
        "path": "001.003",
        "order_key": "ac",
    },
    {
        "concept": "us-gaap:IntangibleAssetsNetExcludingGoodwill",
        "label": "Intangible assets, net",
        "path": "001.004",
        "order_key": "ad",
    },
    {
        "concept": "custom:DigitalAssets",
        "label": "Digital assets",
        "path": "001.005",
        "order_key": "ae",
    },
    {
        "concept": "us-gaap:Goodwill",
        "label": "Goodwill",
        "path": "001.006",
        "order_key": "af",
    },
    {
        "concept": "us-gaap:DeferredIncomeTaxAssetsNet",
        "label": "Deferred tax assets",
        "path": "001.007",
        "order_key": "ag",
    },
    {
        "concept": "us-gaap:OtherAssetsNoncurrent",
        "label": "Other assets",
        "path": "001.008",
        "order_key": "ah",
    },
    # ── Liabilities, mezzanine equity, and shareholders' equity ─────────────
    {
        "concept": "us-gaap:LiabilitiesAndStockholdersEquity",
        "label": "Total liabilities, redeemable convertible preferred stock, and shareholders' equity",
        "path": "002",
        "order_key": "b",
    },
    # ── Liabilities ──────────────────────────────────────────────────────────
    {
        "concept": "us-gaap:Liabilities",
        "label": "Total liabilities",
        "path": "002.001",
        "order_key": "ba",
    },
    {
        "concept": "us-gaap:LiabilitiesCurrent",
        "label": "Total current liabilities",
        "path": "002.001.001",
        "order_key": "baa",
    },
    {
        "concept": "us-gaap:AccountsPayableCurrent",
        "label": "Accounts payable",
        "path": "002.001.001.001",
        "order_key": "baaa",
    },
    {
        "concept": "us-gaap:DeferredRevenueCurrent",
        "label": "Deferred revenue, current",
        "path": "002.001.001.002",
        "order_key": "baab",
    },
    {
        "concept": "custom:DebtAndFinanceLeasesCurrent",
        "label": "Debt and finance leases, current",
        "path": "002.001.001.003",
        "order_key": "baac",
    },
    {
        "concept": "us-gaap:AccruedLiabilitiesCurrent",
        "label": "Accrued expenses and other current liabilities",
        "path": "002.001.001.004",
        "order_key": "baad",
    },
    {
        "concept": "custom:LongTermLiabilities",
        "label": "Long-term liabilities",
        "path": "002.001.002",
        "order_key": "bab",
    },
    {
        "concept": "us-gaap:DeferredRevenueNoncurrent",
        "label": "Deferred revenue, net of current",
        "path": "002.001.003",
        "order_key": "bac",
    },
    {
        "concept": "custom:DebtAndFinanceLeasesNoncurrent",
        "label": "Debt and finance leases, net of current",
        "path": "002.001.004",
        "order_key": "bad",
    },
    {
        "concept": "us-gaap:OtherLiabilitiesNoncurrent",
        "label": "Other liabilities",
        "path": "002.001.005",
        "order_key": "bae",
    },
    # ── Mezzanine / commitments ───────────────────────────────────────────────
    {
        "concept": "us-gaap:CommitmentsAndContingencies",
        "label": "Commitments and contingencies",
        "path": "002.002",
        "order_key": "bb",
    },
    {
        "concept": "us-gaap:TemporaryEquityCarryingAmountAttributableToParent",
        "label": "Redeemable convertible preferred stock",
        "path": "002.003",
        "order_key": "bc",
    },
    # ── Shareholders' equity ─────────────────────────────────────────────────
    {
        "concept": "us-gaap:StockholdersEquity",
        "label": "Total shareholders' equity",
        "path": "002.004",
        "order_key": "bd",
    },
    {
        "concept": "custom:CommonStockClassA",
        "label": "Class A common stock",
        "path": "002.004.001",
        "order_key": "bda",
    },
    {
        "concept": "custom:CommonStockClassB",
        "label": "Class B common stock",
        "path": "002.004.002",
        "order_key": "bdb",
    },
    {
        "concept": "custom:CommonStockClassC",
        "label": "Class C common stock",
        "path": "002.004.003",
        "order_key": "bdc",
    },
    {
        "concept": "custom:CommonStockClassD",
        "label": "Class D common stock",
        "path": "002.004.004",
        "order_key": "bdd",
    },
    {
        "concept": "us-gaap:AdditionalPaidInCapital",
        "label": "Additional paid-in capital",
        "path": "002.004.005",
        "order_key": "bde",
    },
    {
        "concept": "us-gaap:RetainedEarningsAccumulatedDeficit",
        "label": "Accumulated deficit",
        "path": "002.004.006",
        "order_key": "bdf",
    },
    {
        "concept": "us-gaap:AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        "label": "Accumulated other comprehensive income",
        "path": "002.004.007",
        "order_key": "bdg",
    },
]

COMPANY_CIK = "0001181412"
STATEMENT_TYPE = "balancesheet"


def build_document(concept_def: dict, form_type: str, now: datetime) -> dict:
    """Build a full concept document matching the normalized_concepts schema."""
    return {
        "cik": COMPANY_CIK,
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
            "cik": COMPANY_CIK,
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
