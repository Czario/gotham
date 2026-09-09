#!/usr/bin/env python3
"""Audit the current normalize_data schema — collection shapes, relations, coverage."""
import json
from pymongo import MongoClient
from collections import Counter

client = MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=4000)
db = client['normalize_data']

print("=== DATABASE: normalize_data ===")
for col in sorted(db.list_collection_names()):
    print(f"  {col:35s} ~{db[col].estimated_document_count():>10,} docs")

print("\n=== companies: sample fields / coverage ===")
n = db['companies'].estimated_document_count()
print("total:", n)
print("with ticker_symbol:", db['companies'].count_documents({'ticker_symbol': {'$ne': None}}))
print("with market_info.tickers:", db['companies'].count_documents({'market_info.tickers.0': {'$exists': True}}))
doc = db['companies'].find_one()
print("keys:", sorted(doc.keys()))
print("market_info sample:", json.dumps(doc.get('market_info'), default=str)[:600])

print("\n=== companies with concept_values coverage ===")
for cn in ['concept_values_quarterly', 'concept_values_annual']:
    ciks = db[cn].distinct('cik')
    print(f"  {cn}: distinct cik count = {len(ciks)}")

print("\n=== form_type distribution (quarterly concept values) ===")
for d in db['concept_values_quarterly'].aggregate([
    {'$group': {'_id': '$form_type', 'n': {'$sum': 1}}}, {'$sort': {'n': -1}}]):
    print(f"  {d['_id']!r:20s} {d['n']:>10,}")
print("=== form_type distribution (annual concept values) ===")
for d in db['concept_values_annual'].aggregate([
    {'$group': {'_id': '$form_type', 'n': {'$sum': 1}}}, {'$sort': {'n': -1}}]):
    print(f"  {d['_id']!r:20s} {d['n']:>10,}")

print("\n=== quarterly concept_values: distinct (cik, accession_number, form) combos (filing grain) ===")
for d in db['concept_values_quarterly'].aggregate([
    {'$group': {'_id': {'cik': '$cik', 'acc': '$accession_number', 'form': '$form_type'}, 'n': {'$sum': 1}}},
    {'$group': {'_id': '$_id.cik', 'filings': {'$sum': 1}, 'forms': {'$addToSet': '$_id.form'}}},
    {'$sort': {'filings': -1}}, {'$limit': 8}]):
    print(f"  cik={d['_id']} filings={d['filings']} forms={d['forms']}")

print("\n=== a doc-level sample: one company's accession/form/fy/quarter coverage ===")
sample = db['concept_values_quarterly'].aggregate([
    {'$group': {'_id': {'cik': '$cik', 'acc': '$accession_number', 'form': '$form_type',
                        'fy': '$reporting_period.fiscal_year', 'q': '$reporting_period.quarter',
                        'end': '$reporting_period.end_date', 'stmt': '$statement_type'}, 'n': {'$sum': 1}}},
    {'$sort': {'_id.cik': 1, '_id.end': -1}}, {'$limit': 12}])
for d in sample:
    print("  ", json.dumps(d, default=str))

print("\n=== reporting_period keys present (concept_values_quarterly) ===")
rp = db['concept_values_quarterly'].find_one().get('reporting_period', {})
print("  keys:", sorted(rp.keys()))
print("  sample:", json.dumps(rp, default=str))

print("\n=== all top-level keys across concept_values_quarterly ===")
ks = set()
for d in db['concept_values_quarterly'].find({}, {'reporting_period': 0}).limit(5000):
    ks.update(d.keys())
print(" ", sorted(ks))

print("\n=== normalized_concepts_quarterly: does it carry one doc per (cik,concept,stmt)? ===")
nc = db['normalized_concepts_quarterly']
print("  total:", nc.estimated_document_count())
print("  distinct cik:", len(nc.distinct('cik')))
for d in nc.aggregate([
    {'$group': {'_id': {'cik': '$cik', 'stmt': '$statement_type', 'concept': '$concept'}, 'n': {'$sum': 1}}},
    {'$group': {'_id': '$_id.cik', 'unique_rows': {'$sum': 1}, 'dup': {'$sum': {'$cond': [{'$gt': ['$n', 1]}, '$n', 0]}}}},
    {'$limit': 5}]):
    print("   ", d)
print("  concept prefix counts (us-gaap / custom / others):")
for d in nc.aggregate([
    {'$project': {'prefix': {'$cond': [
        {'$eq': [{'$substr': ['$concept', 0, 7]}, 'us-gaap']}, 'us-gaap',
        {'$cond': [{'$eq': [{'$substr': ['$concept', 0, 6]}, 'custom']}, 'custom', 'other']}]}}},
    {'$group': {'_id': '$prefix', 'n': {'$sum': 1}}}]):
    print(f"    {d['_id']}: {d['n']}")

# does normalized carry dimension info / fields beyond core?
extra = set()
for d in nc.find({}, {'reporting_period': 0}).limit(3000):
    extra.update(d.keys())
print("  normalized_concepts keys:", sorted(extra))
