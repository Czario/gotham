#!/usr/bin/env python3
"""
Verify that Revenue, Cost of Revenue, and Operating Income are correctly extracted
with ALL their dimensional breakdowns for HAL, STLD, WRB.
"""
import os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from pymongo import MongoClient
from bson import ObjectId

URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB  = os.getenv('DATABASE_NAME', 'normalize_data')
client = MongoClient(URI)
db = client[DB]

CIKS = {'HAL': '0000045012', 'STLD': '0001022671', 'WRB': '0000011544'}

REVENUE_PATTERNS    = ['Revenue', 'Sales', 'Premiums', 'PremiumsEarned', 'PremiumsWritten',
                       'NetInvestmentIncome', 'InsuranceCommission']
COST_PATTERNS       = ['CostOfRevenue', 'CostOfGoods', 'CostOfSales', 'CostOf',
                       'LossesAndLoss', 'PolicyholderBenefit', 'IncurredClaim',
                       'IncreaseDecreaseInUnearnedPremiums', 'UnderwritingExpense']
OPINCOME_PATTERNS   = ['OperatingIncomeLoss', 'GrossProfit', 'UnderwritingIncomeLoss',
                       'IncomeLossFromContinuingOperationsBeforeIncomeTaxes',
                       'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinary']

def matches(concept: str, patterns: list) -> bool:
    local = concept.split(':')[-1]
    return any(p.lower() in local.lower() for p in patterns)


for ticker, cik in CIKS.items():
    print(f"\n{'='*72}")
    print(f"  {ticker}  |  CIK: {cik}")
    print(f"{'='*72}")

    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        all_docs = list(db[nc_coll].find({'company_cik': cik, 'statement_type': 'income_statement'}))
        if not all_docs:
            continue

        # Separate line items vs dimensional members using the schema fields
        # dimension_concept=True means it's a breakdown member; concept_id = parent _id
        line_items = [d for d in all_docs if not d.get('dimension_concept', False)]
        dim_members = [d for d in all_docs if d.get('dimension_concept', False)]

        # Build parent_id (str) → [member docs]
        parent_to_dims: dict = defaultdict(list)
        for dm in dim_members:
            pid = str(dm.get('concept_id', ''))
            if pid:
                parent_to_dims[pid].append(dm)

        # Get most-recent value per concept_id
        def latest_value(nc_id):
            v = db[cv_coll].find_one(
                {'concept_id': nc_id, 'dimension_value': {'$ne': True}},
                sort=[('reporting_period.fiscal_year', -1), ('reporting_period.quarter', -1)]
            )
            if v:
                fy = v.get('reporting_period', {}).get('fiscal_year', '?')
                q  = v.get('reporting_period', {}).get('quarter', '')
                return v.get('value'), f"FY{fy}" + (f" Q{q}" if q else "")
            return None, None

        def dim_latest_value(nc_id):
            v = db[cv_coll].find_one(
                {'concept_id': nc_id},
                sort=[('reporting_period.fiscal_year', -1), ('reporting_period.quarter', -1)]
            )
            return v.get('value') if v else None

        print(f"\n  ── income_statement [{period}]  "
              f"({len(line_items)} line items, {len(dim_members)} dimensional breakdowns) ──")

        for cat_label, patterns in [
            ('REVENUE',   REVENUE_PATTERNS),
            ('COST',      COST_PATTERNS),
            ('OP INCOME', OPINCOME_PATTERNS),
        ]:
            cat_items = [d for d in line_items if matches(d.get('concept', ''), patterns)]
            if not cat_items:
                continue

            print(f"\n    ── {cat_label} ──")
            for d in cat_items:
                nc_id   = d['_id']
                concept = d.get('concept', '')
                local   = concept.split(':')[-1]
                label   = d.get('label', local)
                val, period_label = latest_value(nc_id)

                dims = parent_to_dims.get(str(nc_id), [])

                val_str = f"{val:>20,.0f}  ({period_label})" if val is not None else "  N/A"
                print(f"    {local}")
                print(f"      label   : {label}")
                print(f"      value   : {val_str}")
                if dims:
                    print(f"      breakdowns ({len(dims)}):")
                    for dm in sorted(dims, key=lambda x: -(dim_latest_value(x['_id']) or 0))[:10]:
                        dm_local = dm.get('concept', '').split(':')[-1]
                        dm_label = dm.get('label', dm_local)
                        dm_val   = dim_latest_value(dm['_id'])
                        dm_val_str = f"{dm_val:>20,.0f}" if dm_val is not None else "               N/A"
                        print(f"        • {dm_val_str}  {dm_label}")
                    if len(dims) > 10:
                        print(f"        ... +{len(dims)-10} more")
                else:
                    print(f"      breakdowns: none")

print("\nDone.")

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

# Key income statement concept patterns to watch
REVENUE_PATTERNS = [
    'Revenue', 'Sales', 'Premiums', 'NetSales', 'TotalRevenue',
    'PremiumsEarned', 'PremiumsWritten', 'NetInvestmentIncome',
    'Fee', 'Service', 'Product'
]
COST_PATTERNS = [
    'CostOfRevenue', 'CostOfGoods', 'CostOfSales', 'CostOf',
    'OperatingCost', 'LossesAndLossAdjustment', 'PolicyholderBenefits',
    'Provision', 'Losses'
]
OPINCOME_PATTERNS = [
    'OperatingIncome', 'OperatingLoss', 'IncomeLossFromOperations',
    'IncomeLossFromContinuing', 'GrossProfit', 'UnderwritingIncome',
    'PreTaxIncome'
]

def matches(concept: str, patterns: list) -> bool:
    c = concept.split(':')[-1]   # local name only
    return any(p.lower() in c.lower() for p in patterns)

def category(concept: str) -> str:
    if matches(concept, REVENUE_PATTERNS):
        return 'REVENUE'
    if matches(concept, COST_PATTERNS):
        return 'COST'
    if matches(concept, OPINCOME_PATTERNS):
        return 'OP_INCOME'
    return 'OTHER'

for ticker, cik in CIKS.items():
    print(f"\n{'='*72}")
    print(f"  {ticker}  |  CIK: {cik}")
    print(f"{'='*72}")

    for period in ('annual', 'quarterly'):
        nc_coll = f'normalized_concepts_{period}'
        cv_coll = f'concept_values_{period}'

        # All income statement concepts for this company
        inc_concepts = list(db[nc_coll].find({
            'company_cik': cik,
            'statement_type': 'income_statement'
        }))

        if not inc_concepts:
            continue

        # Separate line items from dimensional breakdowns
        line_items   = [d for d in inc_concepts if not d.get('dimension', False) and 'Member' not in d.get('concept','')]
        dim_members  = [d for d in inc_concepts if d.get('dimension', False) or 'Member' in d.get('concept','')]

        # For each line item, find its dimensional breakdowns and latest value
        # Build concept_id → concept info map
        id_to_nc = {str(d['_id']): d for d in inc_concepts}

        # Build parent → [child dimensional members] map using dimension_concept field
        parent_to_dims = defaultdict(list)
        for d in dim_members:
            parent_cid = str(d.get('dimension_concept') or d.get('parent_concept_id') or '')
            if parent_cid:
                parent_to_dims[parent_cid].append(d)

        # Build a concept_name → _id map for line items
        name_to_id = {d.get('concept',''): str(d['_id']) for d in line_items}

        print(f"\n  ── income_statement [{period}]  ({len(line_items)} line items, {len(dim_members)} breakdowns) ──")

        # Focus on Revenue / Cost / Operating Income line items
        for cat_label, patterns in [
            ('REVENUE',    REVENUE_PATTERNS),
            ('COST',       COST_PATTERNS),
            ('OP_INCOME',  OPINCOME_PATTERNS),
        ]:
            cat_items = [d for d in line_items if matches(d.get('concept',''), patterns)]
            if not cat_items:
                continue

            print(f"\n    [{cat_label}]")
            for d in cat_items:
                concept  = d.get('concept','')
                nc_id    = str(d['_id'])
                label    = d.get('label', concept.split(':')[-1])

                # Get latest value
                val_doc = db[cv_coll].find_one(
                    {'concept_id': d['_id'], 'dimension_value': {'$ne': True}},
                    sort=[('reporting_period.fiscal_year', -1), ('reporting_period.quarter', -1)]
                )
                val = val_doc.get('value') if val_doc else None
                fy  = val_doc.get('reporting_period', {}).get('fiscal_year', '?') if val_doc else '?'
                q   = val_doc.get('reporting_period', {}).get('quarter', '') if val_doc else ''
                period_label = f"FY{fy}" + (f" Q{q}" if q else "")

                # Count dimensional breakdowns for this concept
                dims = parent_to_dims.get(nc_id, [])

                print(f"      {concept}")
                print(f"        label  : {label}")
                print(f"        value  : {val:,.0f}  ({period_label})" if val else f"        value  : N/A")
                if dims:
                    print(f"        breakdowns ({len(dims)}):")
                    for dm in dims[:8]:
                        dm_concept = dm.get('concept','')
                        dm_val_doc = db[cv_coll].find_one(
                            {'concept_id': dm['_id']},
                            sort=[('reporting_period.fiscal_year', -1)]
                        )
                        dm_val = dm_val_doc.get('value') if dm_val_doc else None
                        dm_label = dm.get('label', dm_concept.split(':')[-1])
                        print(f"          • {dm_concept:<55s} val={dm_val:>18,.0f}" if dm_val else
                              f"          • {dm_concept}  val=N/A")
                    if len(dims) > 8:
                        print(f"          ... +{len(dims)-8} more breakdowns")
                else:
                    print(f"        breakdowns : none")

print("\nDone.")
