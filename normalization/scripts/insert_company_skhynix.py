#!/usr/bin/env python3
"""
Insert SK hynix Inc. into the companies collection.

CIK: 0002120882  (real SEC CIK from F-1 filed 2026-06-24)
SIC: 3674 — Semiconductors & Related Devices
Exchange: KRX (Korea Exchange), ticker 000660

Idempotent: uses update_one with upsert=True keyed on cik.

Run:
    uv run python normalization/scripts/insert_company_skhynix.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

COMPANY_CIK = "0002120882"

def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)

    # Insert into the normalize_data DB (merged pipeline — single DB)
    source_db = client[config.database.database_name]
    companies = source_db["companies"]

    now = datetime.now(timezone.utc)

    company_doc = {
        "cik": COMPANY_CIK,
        "name": "SK hynix Inc.",
        "ticker_symbol": "000660",
        "industry": {
            "sic_code": 3674,
            "sic_description": "Semiconductors & Related Devices",
        },
        "market_info": {
            "tickers": ["000660", "HXSCL"],
            "exchanges": ["KRX"],
        },
        "corporate_info": {
            "state_of_incorporation": "KR",
            "fiscal_year_end": "1231",
            "entity_type": "other",
            "business_address": {
                "street": "2091 Gyeongchung-daero, Bubal-eup",
                "city": "Icheon-si",
                "state": "Gyeonggi-do",
                "zip_code": None,
            },
            "phone": "+82-31-5185-4114",
        },
        "updated_at": now,
    }

    result = companies.update_one(
        {"cik": COMPANY_CIK},
        {
            "$set": {k: v for k, v in company_doc.items() if k != "created_at"},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    if result.upserted_id:
        print(f"Inserted SK hynix Inc. with _id={result.upserted_id}")
    else:
        print(f"SK hynix Inc. already exists — updated (matched={result.matched_count})")

    client.close()


if __name__ == "__main__":
    main()
