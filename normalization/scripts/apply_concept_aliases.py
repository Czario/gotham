#!/usr/bin/env python3
"""Apply recorded concept aliases to historical values (D2).

``concept_aliases`` records that an incoming tag is the SAME economic line as an
already-stored concept.  New writes follow that decision, but values written
before the alias was recorded stay under the old concept, leaving split series
(e.g. capex across two tags).

This script runs ``promote_concept`` for every stored alias, for both form
types, which repoints the old concept's values onto the surviving concept and
deletes the emptied row.  Idempotent: an alias whose old concept is already
gone is skipped.

Dry-run by default (lists what would merge); pass ``--apply`` to write.

Usage::

    MONGODB_URI=mongodb://localhost:27017/ DATABASE_NAME=normalize_data \
        python normalization/scripts/apply_concept_aliases.py [--apply] [--cik CIK]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pymongo import MongoClient  # noqa: E402

from data_normalization_service.core.config import AppConfig  # noqa: E402
from data_normalization_service.services.normalization_service import (  # noqa: E402
    FinancialNormalizationService,
)


def apply_aliases(db, service, *, cik: Optional[str] = None, apply: bool = False) -> dict:
    stats = {"aliases": 0, "merged": 0, "skipped": 0}
    query = {"cik": cik} if cik else {}
    for alias in db["concept_aliases"].find(query):
        source = alias.get("concept")
        target = alias.get("target_concept")
        form_type = alias.get("form_type")
        statement_type = alias.get("statement_type")
        alias_cik = alias.get("cik")
        if not (source and target and form_type and statement_type):
            continue
        stats["aliases"] += 1
        print(f"  {form_type} {statement_type:12} {source} -> {target}")
        if not apply:
            continue
        receipt = service.promote_concept(
            alias_cik, statement_type, form_type, source, target
        )
        if receipt.get("skipped"):
            stats["skipped"] += 1
            print(f"      skipped: {receipt['skipped']}")
        else:
            stats["merged"] += 1
            print(
                f"      merged: moved={receipt.get('values_moved')} "
                f"dup_dropped={receipt.get('duplicate_values_dropped')} "
                f"deleted={receipt.get('deleted')}"
            )
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--cik", default=None, help="limit to one CIK")
    args = parser.parse_args(argv)

    db = MongoClient(
        os.getenv("MONGODB_URI", "mongodb://localhost:27017/"),
        serverSelectionTimeoutMS=5000,
    )[os.getenv("DATABASE_NAME", "normalize_data")]

    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} concept-alias merge")
    if args.apply:
        service = FinancialNormalizationService(AppConfig.from_env())
    else:
        service = None
    stats = apply_aliases(db, service, cik=args.cik, apply=args.apply)
    print("\nSummary:")
    for key in sorted(stats):
        print(f"  {key:10} {stats[key]}")
    if not args.apply and stats["aliases"]:
        print("\nRe-run with --apply to merge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
