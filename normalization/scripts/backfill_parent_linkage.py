#!/usr/bin/env python3
"""Backfill parent linkage on concept rows (main, heading and dimensional).

Every row carries a materialised ``path`` (``001.002.003``) but legacy rows do
not record WHICH row owns the parent address.  Without that link a grouping
header can be moved/collapsed without anything noticing, and its children are
silently orphaned.  This script reconstructs the link from the path prefix:

    parent_path       = path.rsplit(".", 1)[0]
    parent_concept    = concept of the row whose path == parent_path
    parent_concept_id = that row's ``_id``

It also reports rows whose parent path has no owning row (a genuine orphan)
and duplicate ``(path, order_key)`` claims, which must be repaired by
reprocessing the filing (not by guessing here).

Dry-run by default; pass ``--apply`` to write.

Usage::

    MONGODB_URI=mongodb://localhost:27017/ DATABASE_NAME=normalize_data \
        python normalization/scripts/backfill_parent_linkage.py [--apply] [--cik CIK]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from typing import Optional

from pymongo import MongoClient

COLLECTIONS = ("normalized_concepts_annual", "normalized_concepts_quarterly")


def _parent_path(path: Optional[str]) -> str:
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[0]


def backfill(db, *, cik: Optional[str] = None, apply: bool = False) -> dict:
    stats: Counter = Counter()
    cik_filter = {"cik": cik} if cik else {}

    for coll in COLLECTIONS:
        collection = db[coll]
        docs = list(collection.find(cik_filter))

        # group by (cik, statement_type) — the true hierarchy scope
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for doc in docs:
            groups[(doc.get("cik"), doc.get("statement_type"))].append(doc)

        for (row_cik, statement_type), group in groups.items():
            by_path: dict[str, dict] = {}
            duplicate_paths: dict[str, list[str]] = defaultdict(list)
            for doc in group:
                path = doc.get("path")
                if not path:
                    continue
                if path in by_path:
                    duplicate_paths[str(path)].append(doc.get("concept"))
                else:
                    by_path[str(path)] = doc

            for path, names in duplicate_paths.items():
                stats["duplicate_paths"] += 1
                if stats["duplicate_paths"] <= 10:
                    print(f"  ! {coll} {row_cik}/{statement_type}: path {path} "
                          f"claimed by {names[:3]}")

            for doc in group:
                path = str(doc.get("path") or "")
                if not path or "." not in path or path.startswith("555"):
                    continue
                parent_path = _parent_path(path)
                parent_row = by_path.get(parent_path)
                if parent_row is None:
                    stats["orphans"] += 1
                    if stats["orphans"] <= 10:
                        print(f"  ! {coll} {row_cik}/{statement_type}: ORPHAN "
                              f"{path} {doc.get('concept')} (no row at {parent_path})")
                    continue

                desired = {
                    "parent_path": parent_path,
                    "parent_concept": parent_row.get("concept"),
                    "parent_concept_id": parent_row.get("_id"),
                }
                current = {k: doc.get(k) for k in desired}
                if all(current[k] == desired[k] for k in desired):
                    stats["unchanged"] += 1
                    continue
                stats["updated"] += 1
                if apply:
                    collection.update_one({"_id": doc["_id"]}, {"$set": desired})

    return dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--cik", default=None, help="limit to one company CIK")
    args = parser.parse_args()

    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("DATABASE_NAME")
    if not uri or not db_name:
        print("MONGODB_URI and DATABASE_NAME must be set", file=sys.stderr)
        return 2

    db = MongoClient(uri)[db_name]
    stats = backfill(db, cik=args.cik, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] {stats}")
    if not args.apply and (stats.get("updated") or stats.get("orphans")):
        print("Re-run with --apply to write the parent linkage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
