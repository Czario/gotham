#!/usr/bin/env python3
"""
Deep audit: find structural/abstract concepts in normalized_concepts_* for HAL/STLD/WRB,
check if they have value entries, check their creation time, and report on non-financial
dimensional members (srt:/country: Members).
"""
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from pymongo import MongoClient

URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB  = os.getenv('DATABASE_NAME', 'normalize_data')
client = MongoClient(URI)
db = client[DB]

CIKS = {'HAL': '0000045012', 'STLD': '0001022671', 'WRB': '0000011544'}

STRUCT_PATS = ['Abstract', 'Axis', 'Domain', 'Table', 'LineItems', 'TextBlock', 'PolicyTextBlock']
NON_FIN_NS  = ('dei:', 'srt:', 'country:', 'invest:')

print("=" * 70)
print("SECTION 1: Structural/Abstract concepts (should be ZERO after fix)")
print("=" * 70)

any_struct_found = False
for ticker, cik in CIKS.items():
    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        struct_docs = [
            d for d in db[nc_coll].find({'cik': cik})
            if any(p in d.get('concept', '') for p in STRUCT_PATS)
            and 'Member' not in d.get('concept', '')
        ]

        if not struct_docs:
            continue

        any_struct_found = True
        print(f"\n  ⚠️  {ticker} [{period}] — {len(struct_docs)} structural concepts:")
        for d in struct_docs:
            cid     = d['_id']
            concept = d.get('concept', '')
            val_cnt = db[cv_coll].count_documents({'concept_id': cid})
            gen_ts  = cid.generation_time.strftime('%Y-%m-%d %H:%M') if hasattr(cid, 'generation_time') else 'N/A'
            print(f"    {concept}")
            print(f"      abstract={d.get('abstract')}  value_rows={val_cnt}  inserted={gen_ts}")

if not any_struct_found:
    print("  ✅ None found — extraction filter is working correctly")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SECTION 2: Non-financial Members (srt:/country:) — are they REAL breakdowns?")
print("Each should be a dimensional breakdown of a us-gaap/company concept")
print("=" * 70)

for ticker, cik in CIKS.items():
    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        nf_docs = [
            d for d in db[nc_coll].find({'cik': cik})
            if d.get('concept', '').startswith(NON_FIN_NS)
        ]
        if not nf_docs:
            continue

        print(f"\n  {ticker} [{period}] — {len(nf_docs)} non-fin-ns concepts (Members of real line items):")

        # Group by parent dimension_concept
        by_parent = defaultdict(list)
        for d in nf_docs:
            parent = d.get('dimension_concept') or d.get('canonical_concept') or 'UNKNOWN_PARENT'
            by_parent[parent].append(d)

        for parent, docs in sorted(by_parent.items()):
            val_cnt_total = sum(db[cv_coll].count_documents({'concept_id': d['_id']}) for d in docs)
            members = [d.get('concept','') for d in docs[:5]]
            print(f"    Parent line item: {parent}")
            print(f"    Breakdown members ({len(docs)}): {members}")
            print(f"    Value rows total: {val_cnt_total}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SECTION 3: concept_values_* ↔ normalized_concepts_* linkage integrity")
print("=" * 70)

for ticker, cik in CIKS.items():
    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        nc_ids  = {str(d['_id']) for d in db[nc_coll].find({'cik': cik}, {'_id': 1})}
        cv_docs = list(db[cv_coll].find({'cik': cik}, {'concept_id': 1, 'value': 1, 'statement_type': 1}))

        orphan_cv = [d for d in cv_docs if str(d.get('concept_id','')) not in nc_ids]
        null_cv   = [d for d in cv_docs if d.get('value') is None]

        status = '✅' if not orphan_cv and not null_cv else '⚠️ '
        print(f"  {status} {ticker} [{period}]:  "
              f"concepts={len(nc_ids)}  values={len(cv_docs)}  "
              f"orphan_values={len(orphan_cv)}  null_values={len(null_cv)}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SECTION 4: Plain line item summary per company/statement/period")
print("=" * 70)

for ticker, cik in CIKS.items():
    print(f"\n  {ticker}")
    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        by_stmt: dict = defaultdict(lambda: {'plain': 0, 'dim': 0})
        for d in db[nc_coll].find({'cik': cik}):
            concept = d.get('concept', '')
            stmt    = d.get('statement_type', '?')
            if 'Member' in concept or concept.startswith(NON_FIN_NS):
                by_stmt[stmt]['dim'] += 1
            else:
                by_stmt[stmt]['plain'] += 1
        if by_stmt:
            for stmt, counts in sorted(by_stmt.items()):
                print(f"    [{period}] {stmt:20s}: {counts['plain']:3d} line items + "
                      f"{counts['dim']:3d} dimensional breakdowns")

print("\nDone.")
