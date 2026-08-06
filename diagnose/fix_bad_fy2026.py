#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
from pymongo import MongoClient
client = MongoClient(os.getenv('MONGODB_URI'))
db = client[os.getenv('DATABASE_NAME', 'normalize_data')]
cik = '0000094845'

print("=== FY2026 rows with pre-2026 period_date ===")
for v in db.concept_values_quarterly.find({
    'cik': cik,
    'reporting_period.fiscal_year': 2026,
    'reporting_period.period_date': {'$lt': '2026-01-01'}
}):
    nc = db.normalized_concepts_quarterly.find_one({'_id': v['concept_id']})
    rp = v.get('reporting_period', {})
    c = nc.get('concept', '?').split(':')[-1] if nc else '?'
    print(f"  {c:45s}  pd={rp.get('period_date')}  Q={rp.get('quarter')}  "
          f"src={rp.get('data_source')}  calc={v.get('calculated')}  val={v.get('value')}")

print("\n=== Delete these rows and count ===")
result = db.concept_values_quarterly.delete_many({
    'cik': cik,
    'reporting_period.fiscal_year': 2026,
    'reporting_period.period_date': {'$lt': '2026-01-01'}
})
print(f"Deleted {result.deleted_count} rows")

print("\n=== Remaining FY2026 period_dates ===")
from collections import Counter
dates = Counter()
for v in db.concept_values_quarterly.find(
    {'cik': cik, 'reporting_period.fiscal_year': 2026},
    {'reporting_period.period_date': 1, 'reporting_period.quarter': 1}
):
    rp = v.get('reporting_period', {})
    dates[(str(rp.get('period_date', ''))[:10], rp.get('quarter'))] += 1
for k, cnt in sorted(dates.items()):
    print(f"  {k} -> {cnt}")
