#!/usr/bin/env python3
"""Backfill ``parent_concept`` on dimensional concept rows (H3).

Legacy dimensional rows often carry a valid ``concept_id`` (the parent row id)
but an empty ``parent_concept`` name.  The hierarchy layer uses the name to
group members under the right line item, so the missing name makes those rows
look parent-less and lets them collide with modern members on the same path.

Resolution order for each row with no ``parent_concept``:

1. ``concept_id`` → the owning concept's name (authoritative row link);
2. the row's path prefix → the concept whose path is its parent path.

Dry-run by default; pass ``--apply`` to write.

Usage::

    MONGODB_URI=mongodb://localhost:27017/ DATABASE_NAME=normalize_data \
        python normalization/scripts/backfill_dimension_parents.py [--apply] [--cik CIK]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import Optional

from pymongo import MongoClient

COLLECTIONS = ("normalized_concepts_annual", "normalized_concepts_quarterly")


def _parent_path(path: Optional[str]) -> str:
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[0]


def backfill(db, *, cik: Optional[str] = None, apply: bool = False) -> dict:
    stats = Counter()
    cik_filter = {"cik": cik} if cik else {}

    for coll in COLLECTIONS:
        collection = db[coll]
        docs = list(collection.find({**cik_filter, "dimension_concept": True}))
        by_id = {d["_id"]: d.get("concept") for d in collection.find(cik_filter)}
        by_path = {
            d.get("path"): d.get("concept")
            for d in collection.find({**cik_filter, "dimension_concept": False})
            if d.get("path") and d.get("concept")
        }

        for doc in docs:
            if doc.get("parent_concept"):
                continue
            parent = by_id.get(doc.get("concept_id"))
            if not parent:
                # Fall back to the path's parent, then to the stored concept_name.
                parent = by_path.get(_parent_path(doc.get("path"))) or doc.get("concept_name")
            if not parent:
                stats[f"{coll}_unresolved"] += 1
                continue
            stats[f"{coll}_filled"] += 1
            if apply:
                collection.update_one(
                    {"_id": doc["_id"]}, {"$set": {"parent_concept": parent}}
                )
    return dict(stats)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--cik", default=None, help="limit to one CIK")
    args = parser.parse_args(argv)

    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    db_name = os.getenv("DATABASE_NAME", "normalize_data")
    db = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name]

    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} dimension-parent backfill on {db_name}")
    stats = backfill(db, cik=args.cik, apply=args.apply)
    print("\nSummary:")
    for key in sorted(stats):
        print(f"  {key:48} {stats[key]}")
    if not args.apply and any(v for k, v in stats.items() if k.endswith("_filled")):
        print("\nRe-run with --apply to write the backfill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
