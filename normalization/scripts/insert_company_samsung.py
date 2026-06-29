#!/usr/bin/env python3
"""
Insert Samsung Electronics Co., Ltd. into the companies collection.

No SEC CIK — uses synthetic CIK "9990005930" (KRX ticker: 005930).
Exchange: KRX (Korea Stock Exchange).

Idempotent: upserts on cik.

Run:
    uv run --env-file .env python normalization/scripts/insert_company_samsung.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

COMPANY_CIK = "9990005930"  # synthetic — Samsung KRX ticker 005930


def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)
    db = client[config.database.database_name]
    companies = db["companies"]
    now = datetime.now(timezone.utc)

    company_doc = {
        "cik": COMPANY_CIK,
        "name": "Samsung Electronics Co., Ltd.",
        "ticker_symbol": "005930",
        "industry": {
            "sic_code": 3674,
            "sic_description": "Semiconductors & Related Devices",
        },
        "market_info": {
            "tickers": ["005930", "SSNLF"],
            "exchanges": ["KRX"],
        },
        "corporate_info": {
            "state_of_incorporation": "KR",
            "fiscal_year_end": "1231",
            "entity_type": "other",
            "business_address": {
                "street": "129 Samsung-ro, Yeongtong-gu",
                "city": "Suwon-si",
                "state": "Gyeonggi-do",
                "zip_code": "16677",
            },
            "phone": "+82-31-200-1114",
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
        print(f"Inserted Samsung Electronics Co., Ltd.  _id={result.upserted_id}")
    else:
        print(f"Samsung Electronics already exists — updated (matched={result.matched_count})")

    client.close()


if __name__ == "__main__":
    main()
