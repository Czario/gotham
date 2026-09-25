import os
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
client = MongoClient(os.environ["MONGODB_URI"])
db = client[os.environ["DATABASE_NAME"]]

docs = list(db.normalized_concepts_quarterly.find({"cik": "0000320193"}))
print(f"Total docs: {len(docs)}")

# Map path -> list of concepts
paths = defaultdict(list)
for d in docs:
    p = str(d.get("path"))
    c = d.get("concept")
    paths[p].append(c)

collisions = {p: concepts for p, concepts in paths.items() if len(concepts) > 1}
if collisions:
    print(f"❌ Collisions found: {len(collisions)}")
    for p, c in collisions.items():
        print(f"  {p}: {c}")
else:
    print("✅ No collisions!")
