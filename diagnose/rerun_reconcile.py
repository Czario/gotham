#!/usr/bin/env python3
"""Re-run reconciliation for LEVI quarterly and verify comparative rows go to correct FY."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
from pymongo import MongoClient
from api.sec_client import SECAPIClient
from data_normalization_service.services.companyfacts_reconciliation import CompanyFactsReconciliationService
from collections import Counter

client = MongoClient(os.getenv('MONGODB_URI'))
db = client[os.getenv('DATABASE_NAME', 'normalize_data')]
sec = SECAPIClient()
svc = CompanyFactsReconciliationService(db, sec)

print("Running quarterly reconciliation for LEVI...")
stats = svc.reconcile_company('0000094845', frequency='quarterly', include_edge=True)
print(f"Stats: {stats}")

print("\nFY2026 period_dates after reconciliation:")
dates = Counter()
for v in db.concept_values_quarterly.find(
    {'company_cik': '0000094845', 'reporting_period.fiscal_year': 2026},
    {'reporting_period.period_date': 1, 'reporting_period.quarter': 1}
):
    rp = v.get('reporting_period', {})
    dates[(str(rp.get('period_date', ''))[:10], rp.get('quarter'))] += 1
for k, cnt in sorted(dates.items()):
    flag = '✅' if k[0] >= '2026-01-01' else '⚠️  pre-2026 in FY2026'
    print(f"  {k} -> {cnt}  {flag}")

print("\nFY2025 Q1 rows inserted by reconciliation (should now include the 13):")
cnt_2025 = db.concept_values_quarterly.count_documents({
    'company_cik': '0000094845',
    'reporting_period.fiscal_year': 2025,
    'reporting_period.quarter': 1,
    'reporting_period.data_source': 'sec_companyfacts_reconciliation'
})
print(f"  FY2025 Q1 reconciliation rows: {cnt_2025}")
