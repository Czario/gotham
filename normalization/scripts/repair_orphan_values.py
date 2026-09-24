#!/usr/bin/env python3
"""Repair orphan concept values (D1).

A value row stores ``concept_id``.  When a concept is only created in one
form-type collection (annual ``10-K`` / quarterly ``10-Q``) but values are
written into the other collection's value set, the value can no longer be
joined to a concept and shows up as a blank / missing row.

This script, for each ``concept_values_*`` row whose ``concept_id`` is absent
from the matching ``normalized_concepts_*`` collection:

1. resolves the concept name (from the other concepts collection);
2. follows the recorded ``concept_aliases`` target for that form type when one
   exists and the target concept is present in the matching collection;
3. otherwise creates a mirror concept document (copy of the source, new
   ``_id``, correct ``form_type``);
4. repoints the value to the target concept id.  If the target already holds
   the same period (the unique value index), the orphan is a redundant
   duplicate and is deleted instead.

Dry-run by default; pass ``--apply`` to write.

Usage::

    MONGODB_URI=mongodb://localhost:27017/ DATABASE_NAME=normalize_data \
        python normalization/scripts/repair_orphan_values.py [--apply] [--cik CIK]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Optional

from bson import ObjectId
from pymongo import MongoClient

ANNUAL = ("normalized_concepts_annual", "concept_values_annual", "10-K")
QUARTERLY = ("normalized_concepts_quarterly", "concept_values_quarterly", "10-Q")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _period_key(value: dict, form_type: str) -> dict:
    rp = value.get("reporting_period") or {}
    key: dict[str, Any] = {"reporting_period.fiscal_year": rp.get("fiscal_year")}
    if form_type == "10-Q":
        key["reporting_period.quarter"] = rp.get("quarter")
    return key


def repair(db, *, cik: Optional[str] = None, apply: bool = False) -> dict:
    stats = Counter()
    pairs = {"10-K": ANNUAL, "10-Q": QUARTERLY}
    cik_filter = {"cik": cik} if cik else {}

    for form_type, (ccoll, vcoll, _ft) in pairs.items():
        concepts_col = db[ccoll]
        values_col = db[vcoll]
        other_concepts = db[QUARTERLY[0] if form_type == "10-K" else ANNUAL[0]]

        valid_ids = set(concepts_col.distinct("_id", cik_filter))
        orphans = list(values_col.find({**cik_filter, "concept_id": {"$nin": list(valid_ids)}}))
        stats[f"{form_type}_orphans"] = len(orphans)
        if not orphans:
            continue

        # concept_id -> concept name (from the other collection)
        other_docs = {
            d["_id"]: d
            for d in other_concepts.find({"_id": {"$in": list({o["concept_id"] for o in orphans})}})
        }

        # (cik, statement_type) -> {concept name: _id} in the matching collection
        name_index: dict[tuple, dict[str, ObjectId]] = defaultdict(dict)
        for d in concepts_col.find(cik_filter or {}):
            name_index[(d.get("cik"), d.get("statement_type"))][d.get("concept")] = d["_id"]

        for orphan in orphans:
            src = other_docs.get(orphan["concept_id"])
            if not src:
                stats["unresolved"] += 1
                _log(f"  ? cannot resolve concept_id {orphan['concept_id']} for value {orphan['_id']}")
                continue
            name = src.get("concept")
            statement_type = src.get("statement_type")
            cik_val = src.get("cik")

            target_name = name
            alias = db["concept_aliases"].find_one(
                {"cik": cik_val, "statement_type": statement_type,
                 "form_type": form_type, "concept": name}
            )
            if alias and alias.get("target_concept"):
                target_name = alias["target_concept"]

            target_id = name_index.get((cik_val, statement_type), {}).get(target_name)
            if target_id is None:
                target_id = name_index.get((cik_val, statement_type), {}).get(name)

            if target_id is None:
                # Create a mirror concept in the matching collection.
                new_doc = {k: v for k, v in src.items() if k != "_id"}
                new_doc["form_type"] = form_type
                if apply:
                    target_id = concepts_col.insert_one(new_doc).inserted_id
                    name_index[(cik_val, statement_type)][src.get("concept")] = target_id
                stats["concepts_created"] += 1
                _log(f"  + create concept {name} ({form_type}) for orphan values")
            elif target_name != name:
                stats["aliased"] += 1

            if target_id is None:
                continue

            existing = values_col.find_one(
                {"cik": cik_val, "concept_id": target_id, "dimension_value": {"$ne": True},
                 **_period_key(orphan, form_type)}
            )
            if existing:
                stats["duplicates_removed"] += 1
                if apply:
                    values_col.delete_one({"_id": orphan["_id"]})
            else:
                stats["repointed"] += 1
                if apply:
                    values_col.update_one(
                        {"_id": orphan["_id"]}, {"$set": {"concept_id": target_id}}
                    )

    return dict(stats)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--cik", default=None, help="limit to one CIK")
    args = parser.parse_args(argv)

    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    db_name = os.getenv("DATABASE_NAME", "normalize_data")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = client[db_name]

    _log(f"{'APPLYING' if args.apply else 'DRY-RUN'} orphan-value repair on {db_name}")
    stats = repair(db, cik=args.cik, apply=args.apply)
    _log("\nSummary:")
    for key in sorted(stats):
        _log(f"  {key:24} {stats[key]}")
    if not args.apply and any(v for k, v in stats.items() if "orphan" in k):
        _log("\nRe-run with --apply to write the repairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
