# Foreign annual XBRL pilot (Phase 5)

## Public SEC extractor check

The live, read-only integration test checks one US-GAAP issuer (ASML) and one IFRS issuer (Novo Nordisk). It downloads the public SEC filings, verifies annual statement extraction and taxonomy discovery, and confirms the normalized reporting period has no quarter:

```bash
cd gotham
RUN_SEC_LIVE_INTEGRATION=1 uv run --group dev python -m pytest -q \
  tests/integration/test_foreign_annual_xbrl_live.py
```

This test makes no MongoDB writes. It is opt-in because it downloads real SEC filings.

## Staging database pilot

Run the scraper only with an isolated staging MongoDB URI and a dedicated database name. Do not use the normal production `.env` for this first write test. The filing ledger's validator and indexes are created/upgraded lazily when Gotham records the first 20-F/20-F/A accession.

```bash
cd gotham
MONGODB_URI="$STAGING_MONGODB_URI" \
DATABASE_NAME="gotham_phase5_asml" \
SEC_HTML_DOWNLOAD_PATH="/tmp/gotham-sec-html" \
uv run main --companies 0000937966 --year 2025 --end-year 2025
```

Before widening the range, check the staging database:

1. `filings` has one record per `(cik, accession_number)`, with the original `20-F`/`20-F/A` form, filing date, report date, primary document and XBRL flag.
2. `concept_values_annual` contains ASML FY2025 values for income, balance sheet and cash flow; the reporting period has `fiscal_year: 2025` and no quarter.
3. The values have traceable accession numbers and EUR units where supplied by the filing.
4. Rerunning the same accession does not add duplicate annual values.

A 20-F/A replacement should be tested only on staging: a complete amendment replaces the same-year 20-F/20-F/A annual fact set after the filing agent approves persistence. An incomplete amendment updates matching facts without deleting the existing annual set.

## Rollout gate

After ASML passes, run the same isolated pilot for Novo Nordisk (CIK `0000353278`) to verify the IFRS taxonomy path. Do not begin broad historical backfills until both issuers pass the statements, currency/taxonomy, idempotency, and amendment checks.
