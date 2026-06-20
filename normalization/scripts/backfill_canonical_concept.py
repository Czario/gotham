#!/usr/bin/env python3
"""
Backfill `canonical_concept` on existing normalized_concepts_* documents.

Adds the cross-era canonical concept name (see concept_canonicalization.py) to
every concept document so equivalent concepts (ASC 606 revenue, continuing-ops
cash-flow variants, ASU 2016-18 restricted cash, etc.) share a canonical key and
form one continuous time series. Identity-mapped concepts get canonical == concept.

Idempotent: safe to re-run. Only writes when the value would change.
"""
import os
import sys

from dotenv import load_dotenv, find_dotenv
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from data_normalization_service.core.concept_canonicalization import canonical_concept  # noqa: E402

load_dotenv(find_dotenv(usecwd=True) or ".env")
db = MongoClient(os.getenv("MONGODB_URI"))[os.getenv("DATABASE_NAME")]


def backfill(coll_name, dry_run=False):
    coll = db[coll_name]
    ops = []
    changed = remapped = 0
    for doc in coll.find({}, {"concept": 1, "canonical_concept": 1}):
        concept = doc.get("concept")
        if not concept:
            continue
        canon = canonical_concept(concept)
        if doc.get("canonical_concept") != canon:
            ops.append(UpdateOne({"_id": doc["_id"]},
                                 {"$set": {"canonical_concept": canon}}))
            changed += 1
            if canon != concept:
                remapped += 1
    if ops and not dry_run:
        for i in range(0, len(ops), 1000):
            coll.bulk_write(ops[i:i + 1000], ordered=False)
    print(f"{coll_name}: {changed} docs updated "
          f"({remapped} mapped to a different canonical concept)"
          f"{' [DRY RUN]' if dry_run else ''}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    backfill("normalized_concepts_annual", dry)
    backfill("normalized_concepts_quarterly", dry)
    print("Done.")
