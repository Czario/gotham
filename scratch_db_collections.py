import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
client = MongoClient(os.environ["MONGODB_URI"])
db = client[os.environ["DATABASE_NAME"]]
print(db.list_collection_names())

if "filing_status" in db.list_collection_names():
    statuses = list(db.filing_status.find({"cik": "0000320193"}))
    for s in statuses:
        if s.get("status") == "pass_with_findings":
            print(f"Filing {s.get('accession_number')} has findings:")
            print(s.get("findings", {}))
