import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
client = MongoClient(os.environ["MONGODB_URI"])
db = client[os.environ["DATABASE_NAME"]]
doc = db.normalized_concepts_quarterly.find_one({"cik": "0000320193"})
print(doc)
