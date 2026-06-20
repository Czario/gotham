#!/usr/bin/env python3
"""
Run the SEC companyfacts reconciliation pass to fill genuine extraction gaps.

Usage:
    uv run python normalization/scripts/reconcile_companyfacts.py [--dry-run] [--cik CIK ...]

With no --cik, processes every company in the normalize_data companies collection.
INSERT-ONLY: never overwrites primary-extracted values. Recovered values are
tagged source='sec_companyfacts'.
"""
import argparse
import os
import sys

from dotenv import load_dotenv, find_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Repo root for api.sec_client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from data_normalization_service.services.companyfacts_reconciliation import (  # noqa: E402
    CompanyFactsReconciliationService,
)
from api.sec_client import SECAPIClient  # noqa: E402

load_dotenv(find_dotenv(usecwd=True) or ".env")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be filled without writing")
    ap.add_argument("--cik", nargs="*", help="specific CIK(s); default = all")
    ap.add_argument("--interpolation-only", action="store_true",
                    help="only fill gaps strictly inside a concept's filled "
                         "range (skip trailing/leading-edge periods)")
    args = ap.parse_args()

    db = MongoClient(os.getenv("MONGODB_URI"))[os.getenv("DATABASE_NAME")]
    sec_client = SECAPIClient(
        user_agent=os.getenv("SEC_USER_AGENT", "research reconcile@example.com"))
    svc = CompanyFactsReconciliationService(db, sec_client)

    if args.cik:
        ciks = [c.zfill(10) for c in args.cik]
    else:
        ciks = sorted({d["cik"] for d in db.companies.find({}, {"cik": 1})})

    grand = {"gaps": 0, "filled": 0, "skipped_absent": 0, "skipped_exists": 0}
    for cik in ciks:
        for freq in ("annual", "quarterly"):
            s = svc.reconcile_company(
                cik, frequency=freq, dry_run=args.dry_run,
                include_edge=not args.interpolation_only)
            for k in grand:
                grand[k] += s[k]
            if s["gaps"]:
                print(f"  {cik} {freq:9}: gaps={s['gaps']:4} "
                      f"filled={s['filled']:4} absent={s['skipped_absent']:4} "
                      f"exists={s['skipped_exists']:3}")

    tag = " [DRY RUN]" if args.dry_run else ""
    print(f"\nTOTAL{tag}: gaps={grand['gaps']} filled={grand['filled']} "
          f"absent(genuine)={grand['skipped_absent']} exists={grand['skipped_exists']}")
    if grand["gaps"]:
        print(f"Fill rate: {100.0*grand['filled']/grand['gaps']:.1f}% of interpolation gaps")


if __name__ == "__main__":
    main()
