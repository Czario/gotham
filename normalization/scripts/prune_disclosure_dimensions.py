#!/usr/bin/env python3
"""Prune disclosure-only dimensional concepts already stored (H9).

The extractor now excludes derivative hedge / AOCI-reclassification members
(``ForeignExchangeContractMember``, ``InterestRateContractMember``,
``ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember``).  Rows
written before that filter existed are still present; this script removes each
matching dimensional concept row and its values.

Dry-run by default; pass ``--apply`` to delete.

Usage::

    MONGODB_URI=mongodb://localhost:27017/ DATABASE_NAME=normalize_data \
        python normalization/scripts/prune_disclosure_dimensions.py [--apply] [--cik CIK]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import Optional

from pymongo import MongoClient

# Keep in sync with core/extractors/dimensional_context_filters.EXCLUDED_MEMBERS
DISCLOSURE_MEMBERS = (
    "us-gaap:ForeignExchangeContractMember",
    "ForeignExchangeContractMember",
    "us-gaap:InterestRateContractMember",
    "InterestRateContractMember",
    "us-gaap:ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember",
    "ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember",
)

COLLECTIONS = (
    ("normalized_concepts_annual", "concept_values_annual"),
    ("normalized_concepts_quarterly", "concept_values_quarterly"),
)


def prune(db, *, cik: Optional[str] = None, apply: bool = False) -> dict:
    stats = Counter()
    cik_filter = {"cik": cik} if cik else {}

    for ccoll, vcoll in COLLECTIONS:
        query = {**cik_filter, "dimension_concept": True, "concept": {"$in": list(DISCLOSURE_MEMBERS)}}
        for doc in db[ccoll].find(query):
            n_values = db[vcoll].count_documents({"concept_id": doc["_id"]})
            stats["concepts_deleted"] += 1
            stats["values_deleted"] += n_values
            print(
                f"  {ccoll}: {doc.get('statement_type')} path={doc.get('path')} "
                f"| {doc.get('concept')} | values={n_values}"
            )
            if apply:
                db[vcoll].delete_many({"concept_id": doc["_id"]})
                db[ccoll].delete_one({"_id": doc["_id"]})
    return dict(stats)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry-run)")
    parser.add_argument("--cik", default=None, help="limit to one CIK")
    args = parser.parse_args(argv)

    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    db_name = os.getenv("DATABASE_NAME", "normalize_data")
    db = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name]

    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} disclosure-dimension prune on {db_name}")
    stats = prune(db, cik=args.cik, apply=args.apply)
    print("\nSummary:")
    for key in sorted(stats):
        print(f"  {key:20} {stats[key]}")
    if not args.apply and stats.get("concepts_deleted"):
        print("\nRe-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
