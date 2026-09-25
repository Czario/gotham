import os
import json
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
client = MongoClient(os.environ["MONGODB_URI"])
db = client[os.environ["DATABASE_NAME"]]

# Let's find validation errors in the audit log for AAPL
print("Validation errors from audit.jsonl for AAPL:")
with open(".filings_agent/audit.jsonl") as f:
    for line in f:
        try:
            record = json.loads(line)
            if record.get("ticker") != "AAPL":
                continue
            
            if record.get("event") == "node" and record.get("node") == "validate_final_node" and record.get("phase") == "end":
                if "error" in record:
                    print(record)
        except Exception:
            pass

# Let's check duplicates manually for AAPL
docs = list(db.normalized_concepts_quarterly.find({"cik": "0000320193"}))

paths = defaultdict(list)
for d in docs:
    p = str(d.get("path"))
    stmt = d.get("statement_type")
    c = d.get("concept")
    o = str(d.get("order_key"))
    paths[(stmt, p, o)].append(c)

duplicates = {k: v for k, v in paths.items() if len(v) > 1}
if duplicates:
    print(f"❌ Duplicate path + order found for AAPL: {len(duplicates)}")
    for k, v in duplicates.items():
        print(f"  {k}: {v}")
else:
    print("✅ No duplicate path+order for AAPL!")
