#!/usr/bin/env python3
"""
Insert income statement labels for SpaceX (CIK: 0001181412) into
normalized_concepts_annual (10-K) and normalized_concepts_quarterly (10-Q).

Idempotent: uses update_one with upsert=True keyed on
(company_cik, statement_type, concept, form_type).

Run:
    uv run python scripts/insert_income_statement_spacex.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

# ---------------------------------------------------------------------------
# Income statement concept definitions
# order_key is per-sibling sequential (a, b, c, ...) relative to parent_path.
# abstract is False for all items per business requirement.
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ── Root level ──────────────────────────────────────────────────────────
    {
        "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "label": "Revenue",
        "path": "001",
        "order_key": "a",
    },
    # ── Revenue disaggregated by products and services ──────────────────────
    {
        "concept": "custom:RevDisaggByProductsAndServices",
        "label": "Revenue disaggregated by products and services",
        "path": "001.001",
        "order_key": "aa",
    },
    {
        "concept": "custom:ProductsRevenue",
        "label": "Products",
        "path": "001.001.001",
        "order_key": "aaa",
    },
    {
        "concept": "custom:ServicesRevenue",
        "label": "Services",
        "path": "001.001.002",
        "order_key": "aab",
    },
    # ── Revenue disaggregated by type and segment ───────────────────────────
    {
        "concept": "custom:RevDisaggByTypeAndSegment",
        "label": "Revenue disaggregated by type and segment",
        "path": "001.002",
        "order_key": "ab",
    },
    # Space segment
    {
        "concept": "custom:SpaceSegmentRev",
        "label": "Space",
        "path": "001.002.001",
        "order_key": "aba",
    },
    {
        "concept": "custom:LaunchServicesRev",
        "label": "Launch Services",
        "path": "001.002.001.001",
        "order_key": "abaa",
    },
    {
        "concept": "custom:LaunchAndDevelopmentRev",
        "label": "Launch & Development",
        "path": "001.002.001.002",
        "order_key": "abab",
    },
    # Connectivity segment
    {
        "concept": "custom:ConnectivitySegmentRev",
        "label": "Connectivity",
        "path": "001.002.002",
        "order_key": "abb",
    },
    {
        "concept": "custom:ConsumerRev",
        "label": "Consumer",
        "path": "001.002.002.001",
        "order_key": "abba",
    },
    {
        "concept": "custom:EnterpriseAndGovernmentRev",
        "label": "Enterprise & Government",
        "path": "001.002.002.002",
        "order_key": "abbb",
    },
    # AI segment
    {
        "concept": "custom:AISegmentRev",
        "label": "AI",
        "path": "001.002.003",
        "order_key": "abc",
    },
    {
        "concept": "custom:AdvertisingRev",
        "label": "Advertising",
        "path": "001.002.003.001",
        "order_key": "abca",
    },
    {
        "concept": "custom:AISolutionsAndInfrastructureRev",
        "label": "AI Solutions & Infrastructure",
        "path": "001.002.003.002",
        "order_key": "abcb",
    },
    # ── Costs and expenses ──────────────────────────────────────────────────
    {
        "concept": "us-gaap:CostsAndExpenses",
        "label": "Total costs and expenses",
        "path": "002",
        "order_key": "b",
    },
    {
        "concept": "us-gaap:CostOfRevenue",
        "label": "Cost of revenue",
        "path": "002.001",
        "order_key": "ba",
    },
    {
        "concept": "us-gaap:ResearchAndDevelopmentExpense",
        "label": "Research and development",
        "path": "002.002",
        "order_key": "bb",
    },
    {
        "concept": "us-gaap:SellingGeneralAndAdministrativeExpense",
        "label": "Selling, general, and administrative",
        "path": "002.003",
        "order_key": "bc",
    },
    {
        "concept": "us-gaap:RestructuringCharges",
        "label": "Restructuring charges (credits)",
        "path": "002.004",
        "order_key": "bd",
    },
    {
        "concept": "us-gaap:AssetImpairmentCharges",
        "label": "Impairment",
        "path": "002.005",
        "order_key": "be",
    },
    # ── Below-the-line items ────────────────────────────────────────────────
    {
        "concept": "us-gaap:OperatingIncomeLoss",
        "label": "Income (loss) from operations",
        "path": "003",
        "order_key": "c",
    },
    {
        "concept": "us-gaap:InterestExpenseNonoperating",
        "label": "Interest expense",
        "path": "004",
        "order_key": "d",
    },
    {
        "concept": "us-gaap:InvestmentIncomeInterest",
        "label": "Interest income",
        "path": "005",
        "order_key": "e",
    },
    {
        "concept": "us-gaap:OtherNonoperatingIncomeExpense",
        "label": "Other expense, net",
        "path": "006",
        "order_key": "f",
    },
    {
        "concept": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "label": "Loss before income taxes",
        "path": "007",
        "order_key": "g",
    },
    {
        "concept": "us-gaap:IncomeTaxExpenseBenefit",
        "label": "Provision for income taxes",
        "path": "008",
        "order_key": "h",
    },
    {
        "concept": "us-gaap:NetIncomeLoss",
        "label": "Net loss",
        "path": "009",
        "order_key": "i",
    },
    {
        "concept": "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
        "label": "Net loss attributable to shareholders - basic and diluted",
        "path": "010",
        "order_key": "j",
    },
    {
        "concept": "us-gaap:EarningsPerShareBasicAndDiluted",
        "label": "Earnings Per Share, Basic and Diluted",
        "path": "011",
        "order_key": "k",
    },
    {
        "concept": "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
        "label": "Weighted Average Number of Shares Outstanding, Basic and Diluted",
        "path": "012",
        "order_key": "l",
    },
]

COMPANY_CIK = "0001181412"
STATEMENT_TYPE = "income_statement"


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
        # Only set created_at on insert, update the rest on every run
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
