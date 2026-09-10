#!/usr/bin/env python3
"""Recompute ``reporting_period.quarter`` for stored quarterly values.

Fixes companies whose real fiscal quarters do not line up with a naive
3-month grid — most notably 52/53-week retailers such as Kroger (KR), whose
first quarter is 16 weeks long and therefore ends in late May instead of late
April.  Before the fix those Q1 filings were stored as Q2 (and every later
quarter was shifted by one), so a company's quarterly series showed
Q2, Q3, Q4 instead of Q1, Q2, Q3.

Usage::

    # Dry run for one company (shows what would change)
    python -m diagnose.fix_fiscal_quarters --ticker KR

    # Actually write the corrected quarters
    python -m diagnose.fix_fiscal_quarters --ticker KR --apply

    # Every company (dry run)
    python -m diagnose.fix_fiscal_quarters --all
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from pymongo import MongoClient

from utilities.helpers.period_utils import FiscalYearCalculator


VALUE_COLLECTION = "concept_values_quarterly"


def _fiscal_year_end(company: Dict) -> Optional[str]:
    return (
        (company.get("corporate_info") or {}).get("fiscal_year_end")
        or company.get("fiscal_year_end_code")
        or company.get("fiscalYearEnd")
    )


def _quarter_anchors(db, cik: str, limit: int = 12):
    """Return recent ``(period_date, fiscal_year)`` pairs for convention detection."""
    anchors = []
    for col in ("concept_values_quarterly", "concept_values_annual"):
        if col not in db.list_collection_names():
            continue
        cursor = db[col].aggregate([
            {"$match": {
                "cik": str(cik),
                "reporting_period.period_date": {"$exists": True, "$ne": ""},
                "reporting_period.fiscal_year": {"$exists": True},
            }},
            {"$group": {"_id": {
                "pd": "$reporting_period.period_date",
                "fy": "$reporting_period.fiscal_year",
            }}},
            {"$sort": {"_id.pd": -1}},
            {"$limit": limit},
        ])
        anchors.extend((d["_id"]["pd"], d["_id"]["fy"]) for d in cursor)
    return anchors


def _iter_periods(db, cik: str) -> Iterable[Tuple[str, Optional[int], Optional[int], Optional[str], int]]:
    """Yield ``(period_date, fiscal_year, stored_quarter, form_type, doc_count)``."""
    pipeline = [
        {"$match": {"cik": str(cik)}},
        {"$group": {
            "_id": {
                "pd": "$reporting_period.period_date",
                "fy": "$reporting_period.fiscal_year",
                "q": "$reporting_period.quarter",
                "form": "$form_type",
            },
            "count": {"$sum": 1},
        }},
    ]
    for doc in db[VALUE_COLLECTION].aggregate(pipeline):
        key = doc["_id"]
        if not key.get("pd"):
            continue
        yield key["pd"], key.get("fy"), key.get("q"), key.get("form"), doc["count"]


def fix_company(db, company: Dict, apply: bool = False) -> int:
    cik = company.get("cik")
    ticker = company.get("ticker_symbol") or cik
    fiscal_year_end = _fiscal_year_end(company)
    if not fiscal_year_end:
        print(f"  [{ticker}] no fiscal year end on file — skipping")
        return 0

    anchors = _quarter_anchors(db, cik)
    convention = (
        FiscalYearCalculator.determine_fiscal_year_convention(anchors, fiscal_year_end)
        if anchors else None
    ) or "end"

    changes = []
    for period_date, stored_fy, stored_q, form_type, count in sorted(
        _iter_periods(db, cik), key=lambda r: str(r[0])
    ):
        try:
            end_dt = datetime.strptime(str(period_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        _fy, new_q = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
            end_dt, fiscal_year_end, None, convention
        )
        if new_q is None:
            continue
        if stored_q is None:
            # Only fill a missing quarter for a genuine quarterly filing.
            if count and stored_fy is not None and (form_type or "10-Q") == "10-Q":
                changes.append((period_date, stored_fy, None, form_type, new_q, count))
            continue
        if new_q != stored_q:
            changes.append((period_date, stored_fy, stored_q, form_type, new_q, count))

    if not changes:
        print(f"  [{ticker}] nothing to fix ({fiscal_year_end}, convention={convention})")
        return 0

    total_docs = sum(c[-1] for c in changes)
    print(f"  [{ticker}] fiscal_year_end={fiscal_year_end} "
          f"convention={convention} -> {len(changes)} period(s), {total_docs} doc(s)")
    for period_date, fy, old_q, _form, new_q, count in changes:
        old_label = f"Q{old_q}" if old_q is not None else "(none)"
        print(f"      {str(period_date)[:10]}  FY{fy}  {old_label} -> Q{new_q}  ({count} docs)")

    if not apply:
        return total_docs

    updated = 0
    for period_date, fy, old_q, form_type, new_q, _count in changes:
        query = {
            "cik": str(cik),
            "reporting_period.period_date": period_date,
            "reporting_period.fiscal_year": fy,
        }
        if form_type:
            query["form_type"] = form_type
        if old_q is None:
            query["reporting_period.quarter"] = {"$exists": False}
        else:
            query["reporting_period.quarter"] = old_q
        result = db[VALUE_COLLECTION].update_many(
            query, {"$set": {"reporting_period.quarter": new_q}}
        )
        updated += result.modified_count
    print(f"  [{ticker}] updated {updated} document(s)")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Company ticker symbol (e.g. KR)")
    group.add_argument("--cik", help="Company CIK (zero-padded or not)")
    group.add_argument("--all", action="store_true", help="Every company with quarterly data")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default is a dry run)")
    args = parser.parse_args()

    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
    db = client[os.getenv("DATABASE_NAME", "normalize_data")]

    if args.all:
        companies = list(db.companies.find({}))
    else:
        if args.ticker:
            query = {"$or": [
                {"ticker_symbol": args.ticker.upper()},
                {"market_info.tickers": args.ticker.upper()},
            ]}
        else:
            cik = args.cik.strip()
            query = {"cik": {"$in": [cik, cik.zfill(10)]}}
        companies = list(db.companies.find(query))

    if not companies:
        print("No matching company found.")
        return

    print(f"{'APPLYING' if args.apply else 'DRY RUN'}: {len(companies)} company(ies)")
    total = 0
    for company in companies:
        total += fix_company(db, company, apply=args.apply)
    print(f"Total documents {'updated' if args.apply else 'to update'}: {total}")
    if not args.apply and total:
        print("Re-run with --apply to write the corrections.")


if __name__ == "__main__":
    main()
