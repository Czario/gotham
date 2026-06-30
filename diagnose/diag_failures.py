"""Find which accessions failed - query DB directly and run verbose on one."""
import dotenv
dotenv.load_dotenv()
import json
from database.config.mongodb_config import DatabaseConfig
from api.sec_client import SECAPIClient

db = DatabaseConfig().get_database()
assert db is not None, "Could not connect to MongoDB"
col = db['processed_accessions']

processed = set(col.distinct('_id'))
print(f"Processed accessions in DB: {len(processed)}")

with open('tickers.json') as f:
    tdata = json.load(f)

client = SECAPIClient()
for ticker in ['WFC', 'FDS']:
    entry = next((v for v in tdata.values() if v.get('ticker') == ticker), None)
    if not entry:
        print(f"No entry for {ticker}"); continue
    cik = str(entry['cik_str']).zfill(10)
    print(f"\n{ticker} (CIK {cik})")

    # Count processed for this CIK
    done = col.count_documents({'cik': cik})
    print(f"  In DB: {done}")

    # Fetch filings from SEC API
    info, filings = client.get_company_submissions(cik)
    print(f"  From SEC API: {len(filings)} filings")
    all_acc = {f['accessionNumber'] for f in filings if f.get('accessionNumber')}
    failed = all_acc - processed
    print(f"  Not in DB ({len(failed)}):")
    for acc in sorted(failed):
        f = next((x for x in filings if x.get('accessionNumber') == acc), {})
        print(f"    {acc}  {f.get('filingDate','?')}  {f.get('form','?')}")
