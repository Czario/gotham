import os
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env')
from pymongo import MongoClient
from datetime import datetime
from bson import ObjectId

c = MongoClient(os.getenv('MONGODB_URI'))
db = c[os.getenv('DATABASE_NAME')]
cik = '320193'
period = datetime(2026, 3, 28)

# Get all concept_ids for income_statement FY2026 Q2 dimensional values
dim_values = list(db['concept_values_quarterly'].find({
    'cik': cik,
    'statement_type': 'income',
    'reporting_period.end_date': period,
    'dimension_value': True
}, {'concept_id':1,'value':1}))

print(f'Dimensional values in income_statement FY2026 Q2: {len(dim_values)}\n')

# Get the parent concepts via concept_id
cids = list({str(v['concept_id']) for v in dim_values})
concepts = {
    str(d['_id']): d
    for d in db['normalized_concepts_quarterly'].find({'_id': {'$in': [ObjectId(x) for x in cids]}})
}
print('Parent concept labels:')
for cid, doc in concepts.items():
    count = sum(1 for v in dim_values if str(v['concept_id']) == cid)
    print(f"  {doc.get('label','?')[:55]} ({doc.get('concept','?')}) — {count} dim values")

# Now check: what segments/axes are actually stored
# Look at dimensional concept records
print('\n=== Checking concept_values_quarterly for segment axis data ===')
# Sample a few dimensional value docs
sample_docs = list(db['concept_values_quarterly'].find({
    'cik': cik,
    'reporting_period.end_date': period,
    'dimension_value': True
}).limit(10))
for d in sample_docs:
    d.pop('_id',None)
    print({k: str(v)[:70] for k,v in d.items()})
    print()
