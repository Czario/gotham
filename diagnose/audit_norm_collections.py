#!/usr/bin/env python3
"""
Full audit of normalized_concepts_* and concept_values_* collections for HAL, STLD, WRB.
Shows what line items are stored, flags any irrelevant/abstract/non-financial concepts,
and checks the relationship between normalized_concepts and concept_values.
Part 2: deep-dives structural/abstract concepts to find their source and value linkage.
"""
import os, sys, json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from pymongo import MongoClient
from bson import ObjectId

URI  = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB   = os.getenv('DATABASE_NAME', 'normalize_data')

client = MongoClient(URI)
db     = client[DB]

TARGETS = {'HAL': '0000045012', 'STLD': '0001022671', 'WRB': '0000011544'}

# ── Patterns that signal abstract / structural / non-financial concepts ────────
NON_FIN_NS   = ('dei:', 'srt:', 'country:', 'invest:')
STRUCT_PATS  = ['Abstract', 'Axis', 'Domain', 'Table', 'LineItems', 'TextBlock',
                'PolicyTextBlock']

def classify(concept: str) -> list:
    flags = []
    if concept.startswith(NON_FIN_NS):
        flags.append('NON-FIN-NS')
    for p in STRUCT_PATS:
        if p in concept:
            flags.append(f'STRUCT:{p}')
    return flags


def audit_company(ticker: str, cik: str):
    print(f"\n{'='*72}")
    print(f"  {ticker}  |  CIK: {cik}")
    print(f"{'='*72}")

    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        nc_docs = list(db[nc_coll].find({'company_cik': cik}))
        cv_docs = list(db[cv_coll].find({'company_cik': cik}))

        if not nc_docs and not cv_docs:
            print(f"\n  [{period}]  No documents found in either collection")
            continue

        # ── normalized_concepts analysis ─────────────────────────────────────
        print(f"\n  ── normalized_concepts_{period}  ({len(nc_docs)} docs) ──")

        # Group by statement_type
        by_stmt: dict[str, list] = defaultdict(list)
        flagged_nc = []
        abstract_nc = []

        for doc in nc_docs:
            concept = doc.get('concept', '')
            stmt    = doc.get('statement_type', 'unknown')
            abstract= doc.get('abstract', False)
            dimension = doc.get('dimension', False)
            dim_concept = doc.get('dimension_concept', '')
            label   = doc.get('label', '')
            by_stmt[stmt].append(concept)

            flags = classify(concept)
            if flags:
                flagged_nc.append((concept, stmt, abstract, dimension, flags))
            if abstract:
                abstract_nc.append((concept, stmt))

        for stmt, concepts in sorted(by_stmt.items()):
            # Count Members (dimensional breakdowns) vs plain line items
            plain   = [c for c in concepts if 'Member' not in c]
            members = [c for c in concepts if 'Member' in c]
            print(f"    {stmt:20s}: {len(concepts):3d} total  "
                  f"({len(plain)} line items + {len(members)} dimensional breakdowns)")

        if abstract_nc:
            print(f"\n    ⚠️  ABSTRACT concepts still present ({len(abstract_nc)}):")
            for c, s in abstract_nc[:20]:
                print(f"       {c}  [{s}]")
            if len(abstract_nc) > 20:
                print(f"       ... +{len(abstract_nc)-20} more")
        else:
            print(f"    ✅ No abstract concepts")

        non_fin_flagged = [(c, s, fl) for c, s, _, _, fl in flagged_nc
                           if any('NON-FIN' in f for f in fl)]
        struct_flagged  = [(c, s, fl) for c, s, _, _, fl in flagged_nc
                           if any('STRUCT' in f for f in fl) and 'Member' not in c]

        if non_fin_flagged:
            print(f"\n    ⚠️  NON-FINANCIAL namespace concepts ({len(non_fin_flagged)}):")
            for c, s, fl in non_fin_flagged[:20]:
                print(f"       [{','.join(fl)}]  {c}  [{s}]")
            if len(non_fin_flagged) > 20:
                print(f"       ... +{len(non_fin_flagged)-20} more")
        else:
            print(f"    ✅ No non-financial namespace concepts")

        if struct_flagged:
            print(f"\n    ⚠️  STRUCTURAL/NOISE concepts (non-Member) ({len(struct_flagged)}):")
            for c, s, fl in struct_flagged[:20]:
                print(f"       [{','.join(fl)}]  {c}  [{s}]")
            if len(struct_flagged) > 20:
                print(f"       ... +{len(struct_flagged)-20} more")
        else:
            print(f"    ✅ No structural noise concepts (non-Member)")

        # ── concept_values analysis ───────────────────────────────────────────
        print(f"\n  ── concept_values_{period}  ({len(cv_docs)} docs) ──")

        # Map concept_id -> concept name via normalized_concepts
        id_to_concept = {str(doc['_id']): doc.get('concept', '') for doc in nc_docs}

        # Analyse values
        null_values   = [d for d in cv_docs if d.get('value') is None]
        with_values   = [d for d in cv_docs if d.get('value') is not None]
        dim_values    = [d for d in cv_docs if d.get('dimension_value')]
        calculated    = [d for d in cv_docs if d.get('calculated')]

        print(f"    Total value rows  : {len(cv_docs)}")
        print(f"    With numeric value: {len(with_values)}")
        print(f"    Null value rows   : {len(null_values)}")
        print(f"    Dimensional values: {len(dim_values)}")
        print(f"    Calculated rows   : {len(calculated)}")

        # Check concept_id → concept name mapping integrity
        orphaned = [d for d in cv_docs
                    if str(d.get('concept_id','')) not in id_to_concept]
        if orphaned:
            print(f"\n    ⚠️  {len(orphaned)} value rows reference unknown concept_id")
        else:
            print(f"    ✅ All value rows linked to known concept rows")

        # Null-value rows: flag these - why store a row with no value?
        if null_values:
            print(f"\n    ⚠️  NULL VALUE ROWS ({len(null_values)}) — sample:")
            for d in null_values[:10]:
                cname = id_to_concept.get(str(d.get('concept_id','')), '?')
                print(f"       concept={cname}  stmt={d.get('statement_type')}  "
                      f"period={d.get('reporting_period',{}).get('fiscal_year')} "
                      f"dim={d.get('dimension_value','')}")
        else:
            print(f"    ✅ No null-value rows")

        # Sample a few value rows to show the structure is correct
        print(f"\n    Sample value rows:")
        for d in with_values[:5]:
            cname = id_to_concept.get(str(d.get('concept_id','')), '?')
            rp    = d.get('reporting_period', {})
            fy    = rp.get('fiscal_year', '?')
            q     = rp.get('quarter', '')
            dim   = d.get('dimension_value', '')
            val   = d.get('value')
            stmt  = d.get('statement_type', '')
            print(f"       {cname[:60]:60s}  FY{fy}{' Q'+str(q) if q else '':3s}  "
                  f"val={val}  dim={str(dim)[:40]}")


for ticker, cik in TARGETS.items():
    audit_company(ticker, cik)

print("\n\nDone.")
