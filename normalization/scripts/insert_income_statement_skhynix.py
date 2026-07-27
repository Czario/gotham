#!/usr/bin/env python3
"""
Insert income statement concepts + values for SK hynix Inc. (CIK: 0002120882).

Data sources:
  - Annual 2018-2025: Euroland IR XLS (levelZero=0)
  - Quarterly 2024Q2-2026Q1: Euroland IR XLS (levelZero=1)
  - All source values are in KRW millions; converted to USD using period
    average KRW/USD exchange rates.

Collections written:
  - normalized_concepts_annual   (10-K concept structure)
  - normalized_concepts_quarterly (10-Q concept structure)
  - concept_values_annual        (annual USD values 2018-2025)
  - concept_values_quarterly     (quarterly USD values 2024Q2-2026Q1)

Idempotent: upserts concepts; skips duplicate values.

Run:
    uv run python normalization/scripts/insert_income_statement_skhynix.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from data_normalization_service.core.config import AppConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COMPANY_CIK = "0002120882"
STATEMENT_TYPE = "income_statement"

# ---------------------------------------------------------------------------
# KRW → USD conversion rates (annual average KRW per 1 USD)
# Sources: Bank of Korea / derived from SEC F-1 filing and Wikipedia
# ---------------------------------------------------------------------------
ANNUAL_FX = {
    2018: 1100.3,
    2019: 1165.7,
    2020: 1180.1,
    2021: 1144.9,
    2022: 1291.8,
    2023: 1316.0,
    2024: 1380.0,
    2025: 1413.0,
}

# Quarterly average KRW/USD rates
QUARTERLY_FX = {
    (2024, 2): 1375.0,
    (2024, 3): 1349.0,
    (2024, 4): 1400.0,
    (2025, 1): 1452.0,
    (2025, 2): 1376.0,
    (2025, 3): 1381.0,
    (2025, 4): 1475.0,
    (2026, 1): 1460.0,
}

def krw_to_usd(krw_millions: float, fx_rate: float) -> float:
    """Convert KRW millions to USD (whole dollars)."""
    return round(krw_millions * 1_000_000 / fx_rate)

# ---------------------------------------------------------------------------
# Income Statement — KRW millions (source: Euroland IR XLS)
# Columns: 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025
# ---------------------------------------------------------------------------
ANNUAL_KRW = {
    "us-gaap:Revenues": [
        40_445_066, 26_990_733, 31_900_418, 42_997_792,
        44_621_568, 32_765_719, 66_192_960, 97_146_675,
    ],
    "us-gaap:GrossProfit": [
        25_264_228,  8_171_919, 10_810_629, 18_952_192,
        15_627_855,   -533_448, 31_828_146, 58_690_790,
    ],
    "us-gaap:OperatingIncomeLoss": [
        20_843_750,  2_719_179,  5_012_624, 12_410_340,
         6_809_417, -7_730_313, 23_467_319, 47_206_319,
    ],
    "us-gaap:NetIncomeLoss": [
        15_539_984,  2_009_078,  4_758_914,  9_616_188,
         2_241_669, -9_137_547, 19_796_902, 42_947_902,
    ],
}
ANNUAL_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]

# Quarterly KRW millions — columns: 2024Q2, Q3, Q4 | 2025Q1-Q4 | 2026Q1
QUARTERLY_KRW = {
    "us-gaap:Revenues": [
        16_423_258, 17_573_069, 19_767_035,
        17_639_141, 22_231_952, 24_448_929, 32_826_654,
        52_576_287,
    ],
    "us-gaap:GrossProfit": [
         7_496_464,  9_171_384, 10_365_654,
        10_101_991, 11_983_324, 14_029_099, 22_576_376,
        41_679_414,
    ],
    "us-gaap:OperatingIncomeLoss": [
         5_468_536,  7_029_958,  8_082_797,
         7_440_504,  9_212_851, 11_383_390, 19_169_573,
        37_610_283,
    ],
    "us-gaap:NetIncomeLoss": [
         4_120_003,  5_753_373,  8_006_487,
         8_108_195,  6_996_216, 12_597_538, 15_245_953,
        40_345_909,
    ],
}
# (year, quarter) tuples matching the columns above
QUARTERLY_PERIODS = [
    (2024, 2), (2024, 3), (2024, 4),
    (2025, 1), (2025, 2), (2025, 3), (2025, 4),
    (2026, 1),
]

# Quarter end dates
QUARTER_END = {
    1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31",
}

# ---------------------------------------------------------------------------
# Concept structure (hierarchy for the normalized_concepts collections)
# ---------------------------------------------------------------------------
CONCEPTS = [
    {
        "concept": "us-gaap:Revenues",
        "label": "Revenue",
        "path": "001",
        "order_key": "a",
    },
    {
        "concept": "us-gaap:CostOfRevenue",
        "label": "Cost of sales",
        "path": "002",
        "order_key": "b",
    },
    {
        "concept": "us-gaap:GrossProfit",
        "label": "Gross profit (loss)",
        "path": "003",
        "order_key": "c",
    },
    {
        "concept": "us-gaap:OperatingExpenses",
        "label": "Total operating expenses",
        "path": "004",
        "order_key": "d",
    },
    {
        "concept": "us-gaap:OperatingIncomeLoss",
        "label": "Operating profit (loss)",
        "path": "005",
        "order_key": "e",
    },
    {
        "concept": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "label": "Profit (loss) before income tax",
        "path": "006",
        "order_key": "f",
    },
    {
        "concept": "us-gaap:NetIncomeLoss",
        "label": "Net profit (loss)",
        "path": "007",
        "order_key": "g",
    },
]

# ---------------------------------------------------------------------------
# Concept values that have data (subset of CONCEPTS)
# ---------------------------------------------------------------------------
VALUED_CONCEPTS = {
    "us-gaap:Revenues",
    "us-gaap:GrossProfit",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:NetIncomeLoss",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_concept_doc(concept_def: dict, form_type: str, now: datetime) -> dict:
    return {
        "company_cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "concept": concept_def["concept"],
        "form_type": form_type,
        "label": concept_def["label"],
        "canonical_concept": concept_def["concept"],
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


def upsert_concepts(collection, form_type: str, now: datetime) -> dict:
    """Upsert all concepts; return mapping concept_name → _id."""
    id_map: dict[str, ObjectId] = {}
    for c in CONCEPTS:
        doc = build_concept_doc(c, form_type, now)
        filter_key = {
            "company_cik": COMPANY_CIK,
            "statement_type": STATEMENT_TYPE,
            "concept": c["concept"],
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
            id_map[c["concept"]] = result.upserted_id
        else:
            existing = collection.find_one(filter_key, {"_id": 1})
            if existing:
                id_map[c["concept"]] = existing["_id"]
    return id_map


def upsert_value(
    collection,
    concept_id: ObjectId,
    form_type: str,
    reporting_period: dict,
    value: float,
    now: datetime,
) -> bool:
    """Insert value if not already present. Returns True if inserted.

    FIX: dropped reporting_period.period_date from the unique-key query (caused
    off-by-one-day duplicates); insert is now atomic via DuplicateKeyError instead
    of non-atomic find_one() + insert_one().
    """
    doc = {
        "concept_id": concept_id,
        "company_cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "form_type": form_type,
        "reporting_period": reporting_period,
        "value": value,
        "dimension_value": False,
        "decimals": "-6",
        "source": "manual_euroland_f1",
        "created_at": now,
    }
    try:
        collection.insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)
    db = client[config.database.database_name]
    now = datetime.now(timezone.utc)

    # ── Annual ────────────────────────────────────────────────────────────
    annual_concepts = db["normalized_concepts_annual"]
    annual_values   = db["concept_values_annual"]

    annual_id_map = upsert_concepts(annual_concepts, "10-K", now)
    print(f"[normalized_concepts_annual] upserted {len(annual_id_map)} concepts")

    ann_inserted = ann_skipped = 0
    for i, year in enumerate(ANNUAL_YEARS):
        fx = ANNUAL_FX[year]
        period = {
            "fiscal_year": year,
            "period_date": f"{year}-12-31",
            "start_date": datetime(year, 1, 1, tzinfo=timezone.utc),
            "end_date":   datetime(year, 12, 31, tzinfo=timezone.utc),
            "form_type": "10-K",
        }
        for concept, krw_series in ANNUAL_KRW.items():
            cid = annual_id_map.get(concept)
            if cid is None:
                continue
            usd_value = krw_to_usd(krw_series[i], fx)
            if upsert_value(annual_values, cid, "10-K", period, usd_value, now):
                ann_inserted += 1
            else:
                ann_skipped += 1

    print(f"[concept_values_annual]       inserted={ann_inserted}  skipped={ann_skipped}")

    # ── Quarterly ─────────────────────────────────────────────────────────
    quarterly_concepts = db["normalized_concepts_quarterly"]
    quarterly_values   = db["concept_values_quarterly"]

    qtr_id_map = upsert_concepts(quarterly_concepts, "10-Q", now)
    print(f"[normalized_concepts_quarterly] upserted {len(qtr_id_map)} concepts")

    qtr_inserted = qtr_skipped = 0
    for i, (year, quarter) in enumerate(QUARTERLY_PERIODS):
        fx = QUARTERLY_FX[(year, quarter)]
        month = int(QUARTER_END[quarter][:2])
        day   = int(QUARTER_END[quarter][3:])
        sq_month = ((quarter - 1) * 3) + 1
        period = {
            "fiscal_year": year,
            "quarter": quarter,
            "period_date": f"{year}-{QUARTER_END[quarter]}",
            "start_date": datetime(year, sq_month, 1, tzinfo=timezone.utc),
            "end_date":   datetime(year, month, day, tzinfo=timezone.utc),
            "form_type": "10-Q",
        }
        for concept, krw_series in QUARTERLY_KRW.items():
            cid = qtr_id_map.get(concept)
            if cid is None:
                continue
            usd_value = krw_to_usd(krw_series[i], fx)
            if upsert_value(quarterly_values, cid, "10-Q", period, usd_value, now):
                qtr_inserted += 1
            else:
                qtr_skipped += 1

    print(f"[concept_values_quarterly]    inserted={qtr_inserted}  skipped={qtr_skipped}")
    print("\nDone — SK hynix income statement loaded.")
    client.close()


if __name__ == "__main__":
    main()
