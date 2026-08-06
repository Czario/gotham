#!/usr/bin/env python3
"""
Insert Revenue concept + null-value placeholders for Samsung Electronics Co., Ltd.
(CIK: 9990005930)

Periods inserted:
  Annual  : 2010 – 2026  (17 periods, form_type "10-K")
  Quarterly: 2010 Q1 – 2026 Q1  (65 quarters, form_type "10-Q")

All values are stored as None (null).  Fill them in later once Samsung data
is sourced.

Collections written:
  - normalized_concepts_annual / normalized_concepts_quarterly
  - concept_values_annual / concept_values_quarterly

Idempotent: upserts concepts; skips duplicate period rows.

Run:
    uv run --env-file .env python normalization/scripts/insert_revenue_samsung.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from bson import ObjectId
from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

COMPANY_CIK    = "9990005930"
STATEMENT_TYPE = "income"

# ---------------------------------------------------------------------------
# Period definitions
# ---------------------------------------------------------------------------
ANNUAL_YEARS = list(range(2010, 2027))          # 2010 … 2026 inclusive

# Quarterly: 2010 Q1 through 2026 Q1
QUARTERLY_PERIODS: list[tuple[int, int]] = []
for y in range(2010, 2026):
    for q in range(1, 5):
        QUARTERLY_PERIODS.append((y, q))
QUARTERLY_PERIODS.append((2026, 1))             # add 2026 Q1

QUARTER_END   = {1: ("03", "31"), 2: ("06", "30"), 3: ("09", "30"), 4: ("12", "31")}

# ---------------------------------------------------------------------------
# Concept definition  (Revenue only)
# ---------------------------------------------------------------------------
CONCEPTS = [
    {
        "concept":   "us-gaap:Revenues",
        "label":     "Revenue",
        "path":      "001",
        "order_key": "a",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_concept_doc(concept_def: dict, form_type: str, now: datetime) -> dict:
    return {
        "cik":       COMPANY_CIK,
        "statement_type":    STATEMENT_TYPE,
        "concept":           concept_def["concept"],
        "form_type":         form_type,
        "label":             concept_def["label"],
        "canonical_concept": concept_def["concept"],
        "path":              concept_def["path"],
        "order_key":         concept_def["order_key"],
        "abstract":          False,
        "dimension":         False,
        "dimension_concept": False,
        "hide":              False,
        "active":            True,
        "merge":             False,
        "created_at":        now,
        "updated_at":        now,
        "updatedAt":         now.isoformat().replace("+00:00", "Z"),
        "__v":               0,
    }


def upsert_concepts(collection, form_type: str, now: datetime) -> dict:
    """Upsert all concepts; return mapping concept → _id."""
    id_map: dict = {}
    for c in CONCEPTS:
        doc = build_concept_doc(c, form_type, now)
        filter_key = {
            "cik":    COMPANY_CIK,
            "statement_type": STATEMENT_TYPE,
            "concept":        c["concept"],
            "form_type":      form_type,
        }
        result = collection.update_one(
            filter_key,
            {
                "$set":         {k: v for k, v in doc.items() if k != "created_at"},
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
    now: datetime,
) -> bool:
    """Insert a null-value placeholder if the period doesn't already exist."""
    query: dict = {
        "concept_id":                    concept_id,
        "cik":                   COMPANY_CIK,
        "statement_type":                STATEMENT_TYPE,
        "form_type":                     form_type,
        "reporting_period.fiscal_year":  reporting_period["fiscal_year"],
        "reporting_period.period_date":  reporting_period["period_date"],
    }
    if "quarter" in reporting_period:
        query["reporting_period.quarter"] = reporting_period["quarter"]

    if collection.find_one(query):
        return False

    collection.insert_one({
        "concept_id":       concept_id,
        "cik":      COMPANY_CIK,
        "statement_type":   STATEMENT_TYPE,
        "form_type":        form_type,
        "reporting_period": reporting_period,
        "value":            None,           # placeholder — fill when data is available
        "dimension_value":  False,
        "decimals":         "-6",
        "source":           "placeholder",
        "created_at":       now,
    })
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)
    db = client[config.database.database_name]
    now = datetime.now(timezone.utc)

    # ── Annual ────────────────────────────────────────────────────────────
    annual_id_map = upsert_concepts(db["normalized_concepts_annual"], "10-K", now)
    print(f"[normalized_concepts_annual]    upserted {len(annual_id_map)} concept(s)")

    cid_annual = annual_id_map.get("us-gaap:Revenues")
    ann_inserted = ann_skipped = 0

    for year in ANNUAL_YEARS:
        period = {
            "fiscal_year": year,
            "period_date": f"{year}-12-31",
            "start_date":  datetime(year, 1, 1, tzinfo=timezone.utc),
            "end_date":    datetime(year, 12, 31, tzinfo=timezone.utc),
            "form_type":   "10-K",
        }
        if upsert_value(db["concept_values_annual"], cid_annual, "10-K", period, now):
            ann_inserted += 1
        else:
            ann_skipped += 1

    print(f"[concept_values_annual]         inserted={ann_inserted}  skipped={ann_skipped}")

    # ── Quarterly ─────────────────────────────────────────────────────────
    qtr_id_map = upsert_concepts(db["normalized_concepts_quarterly"], "10-Q", now)
    print(f"[normalized_concepts_quarterly] upserted {len(qtr_id_map)} concept(s)")

    cid_quarterly = qtr_id_map.get("us-gaap:Revenues")
    qtr_inserted = qtr_skipped = 0

    for year, quarter in QUARTERLY_PERIODS:
        end_m, end_d   = QUARTER_END[quarter]
        start_m        = str((quarter - 1) * 3 + 1).zfill(2)
        period = {
            "fiscal_year": year,
            "quarter":     quarter,
            "period_date": f"{year}-{end_m}-{end_d}",
            "start_date":  datetime(year, int(start_m), 1, tzinfo=timezone.utc),
            "end_date":    datetime(year, int(end_m), int(end_d), tzinfo=timezone.utc),
            "form_type":   "10-Q",
        }
        if upsert_value(db["concept_values_quarterly"], cid_quarterly, "10-Q", period, now):
            qtr_inserted += 1
        else:
            qtr_skipped += 1

    print(f"[concept_values_quarterly]      inserted={qtr_inserted}  skipped={qtr_skipped}")
    print(
        f"\nDone — Samsung Electronics Revenue placeholders loaded.\n"
        f"  Annual  : {len(ANNUAL_YEARS)} periods  (2010–2026)\n"
        f"  Quarterly: {len(QUARTERLY_PERIODS)} periods (2010 Q1 – 2026 Q1)"
    )
    client.close()


if __name__ == "__main__":
    main()
