"""Find which accessions failed - query DB directly and run verbose on one."""
import dotenv
dotenv.load_dotenv()
from database.config.mongodb_config import DatabaseConfig
from api.sec_client import SECAPIClient
from utilities.helpers.ticker_resolver import resolve_ticker, get_cik_to_name

db = DatabaseConfig().get_database()
assert db is not None, "Could not connect to MongoDB"
col = db['processed_accessions']

processed = set(col.distinct('_id'))
print(f"Processed accessions in DB: {len(processed)}")

cik_to_name = get_cik_to_name()
client = SECAPIClient()
for ticker in ['WFC', 'FDS']:
    cik = resolve_ticker(ticker)
    if not cik:
        print(f"No entry for {ticker}"); continue
    print(f"\n{ticker} (CIK {cik}) — {cik_to_name.get(cik, '?')}")

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
