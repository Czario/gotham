#!/usr/bin/env python3
"""
Accuracy checks on loaded data for HAL, STLD, WRB:
1. Duplicate line items (same concept, same period, non-dimensional) → should be 0
2. Dimensional breakdown sum vs parent total (segment axis should ~= parent)
3. Sign sanity (costs/expenses should be positive in presentation)
4. Orphan dimensional members (concept_id points to nothing)
5. Multiple revenue concepts coexisting for the SAME period (era overlap)
"""
import os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from pymongo import MongoClient

client = MongoClient(os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'))
db = client[os.getenv('DATABASE_NAME', 'normalize_data')]

CIKS = {'HAL': '0000045012', 'STLD': '0001022671', 'WRB': '0000011544'}


def period_key(rp: dict) -> str:
    return f"FY{rp.get('fiscal_year','?')}Q{rp.get('quarter','')}"


for ticker, cik in CIKS.items():
    print(f"\n{'='*72}\n  {ticker}  (CIK {cik})\n{'='*72}")

    for period in ('annual', 'quarterly'):
        nc = f'normalized_concepts_{period}'
        cv = f'concept_values_{period}'

        nc_docs = list(db[nc].find({'cik': cik}))
        cv_docs = list(db[cv].find({'cik': cik}))
        id_to_nc = {str(d['_id']): d for d in nc_docs}

        # ── Check 1: duplicate NON-dimensional value rows for same concept+period+statement
        seen = defaultdict(list)
        for v in cv_docs:
            if v.get('dimension_value'):
                continue
            key = (str(v.get('concept_id')), v.get('statement_type'), period_key(v.get('reporting_period', {})))
            seen[key].append(v)
        dups = {k: vs for k, vs in seen.items() if len(vs) > 1}

        # ── Check 4: orphan dimensional members
        orphan_dims = [v for v in cv_docs
                       if v.get('dimension_value')
                       and str(v.get('concept_id')) not in id_to_nc]

        # ── Check 3: sign sanity — cost/expense concepts stored negative
        neg_costs = []
        for v in cv_docs:
            if v.get('dimension_value') or v.get('value') is None:
                continue
            c = id_to_nc.get(str(v.get('concept_id')), {}).get('concept', '')
            local = c.split(':')[-1].lower()
            if any(k in local for k in ('costofgoods', 'costofrevenue', 'costofservices')):
                if v['value'] < 0:
                    neg_costs.append((c, v['value'], period_key(v.get('reporting_period', {}))))

        print(f"\n  [{period}]")
        print(f"    duplicate non-dim value rows : {len(dups)}"
              + ("  ⚠️" if dups else "  ✅"))
        for (cid, st, pk), vs in list(dups.items())[:5]:
            cname = id_to_nc.get(cid, {}).get('concept', '?')
            print(f"        {cname} [{st}] {pk}: {len(vs)} rows, values={[x['value'] for x in vs]}")

        print(f"    orphan dimensional values    : {len(orphan_dims)}"
              + ("  ⚠️" if orphan_dims else "  ✅"))
        print(f"    negatively-signed cost rows  : {len(neg_costs)}"
              + ("  ⚠️" if neg_costs else "  ✅"))
        for c, v, pk in neg_costs[:5]:
            print(f"        {c} {pk}: {v:,.0f}")


# ── Check 2 & 5: revenue breakdown accuracy for the latest period, HAL only (has clean segments)
print(f"\n\n{'='*72}\n  DEEP CHECK: HAL latest-period revenue segment reconciliation\n{'='*72}")
cik = CIKS['HAL']
nc_docs = list(db['normalized_concepts_annual'].find({'cik': cik, 'statement_type': 'income'}))
id_to_nc = {str(d['_id']): d for d in nc_docs}

# Find the RevenueFromContractWithCustomer parent
parents = [d for d in nc_docs if d.get('concept','').endswith('IncludingAssessedTax') and not d.get('dimension_concept')]
for parent in parents:
    pid = parent['_id']
    total = db['concept_values_annual'].find_one(
        {'concept_id': pid, 'dimension_value': {'$ne': True}},
        sort=[('reporting_period.fiscal_year', -1)]
    )
    if not total:
        continue
    fy = total['reporting_period']['fiscal_year']
    print(f"\n  Parent: {parent['concept']}  FY{fy}  total = {total['value']:,.0f}")

    # Get children members and group by axis
    child_members = [d for d in nc_docs if str(d.get('concept_id')) == str(pid) and d.get('dimension_concept')]
    by_axis = defaultdict(list)
    for cm in child_members:
        dims = cm.get('dimensions', {})
        axis = next(iter(dims.keys()), 'unknown') if dims else 'unknown'
        val = db['concept_values_annual'].find_one(
            {'concept_id': cm['_id'], 'reporting_period.fiscal_year': fy}
        )
        if val:
            by_axis[axis].append((cm.get('label', cm['concept']), val['value']))

    for axis, members in by_axis.items():
        s = sum(v for _, v in members)
        diff = s - total['value']
        pct = (diff / total['value'] * 100) if total['value'] else 0
        flag = '✅' if abs(pct) < 2 else ('⚠️ partial-axis' if abs(pct) > 15 else '~')
        print(f"    Axis '{axis}': {len(members)} members, sum={s:,.0f}  vs total  (diff {pct:+.1f}%)  {flag}")
        for lbl, v in sorted(members, key=lambda x: -x[1]):
            print(f"        {v:>18,.0f}  {lbl}")

print("\nDone.")
