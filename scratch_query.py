from pymongo import MongoClient
import pprint

client = MongoClient('mongodb://localhost:27017')
db = client['normalize_data']
col = db['normalized_concepts_annual']

print("All DigitalMediaMember concepts:")
for doc in col.find({"cik": "0000796343", "concept": "adbe:DigitalMediaMember"}):
    doc.pop('_id', None)
    pprint.pprint(doc)
    print("---")
