# SEC EDGAR Financial Data Scraper & Normalization Pipeline

Extracts XBRL financial data (`10-K`, `10-Q`, `20-F`, `20-F/A`) from SEC EDGAR filings, normalizes concepts and dimensional breakdowns, validates and repairs through an agent pipeline, and stores canonical data in MongoDB.

---

## Setup

```bash
# 1. Configure environment
cp env.example .env

# 2. Install dependencies
uv sync
```

---

## Common Commands

### Process companies from a file (most common)

```bash
uv run main --file stocks_download.txt

# Load a specific range
uv run main --file stocks_download.txt --year 2015 --end-year 2026

# Reload (force reprocess) the same range
uv run main --file stocks_download.txt --year 2015 --end-year 2026 --reload
```

### Process specific companies by ticker or CIK

```bash
uv run main --companies AAPL MSFT
uv run main --companies 0000320193 0000789019
```

### Process a single filing by URL

```bash
uv run main --url "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"
```

### Incremental — only new filings since last run

```bash
uv run main --file stocks_download.txt --latest
```

To force-refresh only the latest SEC filing, combine `--latest` and `--reload`.
The selected filing is extracted first, then deleted by accession number immediately before replacement data is saved:

```bash
uv run main --file stocks_download.txt --latest --reload
```

### Force reload a specific fiscal year

A fiscal year without `--fiscal-quarter` selects only the annual `10-K` filing. It does not process the three quarterly `10-Q` filings for that year:

```bash
uv run main --file stocks_download.txt --reload --fiscal-year 2023
```

### Force reload a specific quarter

```bash
uv run main --file stocks_download.txt --reload --fiscal-year 2023 --fiscal-quarter Q2
```

*(Note: `--quarter` or `-q` can be used as aliases for `--fiscal-quarter`)*

### Load or reload a specific statement for a specific quarter

You can restrict extraction and loading to a single financial statement for a specific quarter.

Supported statement types & aliases:
- **Balance Sheet**: `balance_sheet`, `balancesheet`, `balance-sheet`, `bs`
- **Income Statement**: `income`, `income_statement`, `income-statement`, `is`
- **Cash Flow**: `cash_flow`, `cashflow`, `cash-flow`, `cash_flows`, `cf`

#### Option A: Using `main` CLI

```bash
# Load balance sheet for Q2 2023
uv run main --companies AAPL --fiscal-year 2023 --quarter Q2 --statement balance_sheet
uv run main --companies AAPL --fiscal-year 2023 --quarter Q2 --statement bs

# Force reload income statement for Q1 2024 (upserts: preserves correct values, fills gaps, corrects differing rows)
uv run main --companies AAPL --reload --fiscal-year 2024 --quarter Q1 --statement income
uv run main --companies AAPL --reload --fiscal-year 2024 --quarter Q1 --statement is
uv run main --companies AAPL --reload --fiscal-year 2024 --quarter Q1 --statement cf
```

#### Option B: Using the dedicated `load-statement` command

```bash
uv run load-statement --company AAPL --fiscal-year 2023 --quarter Q2 --statement balance_sheet
uv run load-statement --company AAPL --fiscal-year 2024 --quarter Q1 --statement income --reload
```

#### Option C: Using the modular CLI entrypoint

```bash
uv run python -m cli.main statement --ticker AAPL --fiscal-year 2023 --quarter Q2 --statement balance_sheet
```

> **Reload Scoping & Non-destructive Upsert**: Force reload (`--reload`) performs a non-destructive upsert. Rather than wiping existing documents:
> - **Preserves correct data**: If a stored value matches the filing, it is preserved and any missing metadata gaps (`accession_number`, `decimals`, `fact_id`, `statement_type`, `form_type`, `reporting_period`) are filled in.
> - **In-place correction**: If a stored value is incorrect (differs from the incoming/repaired value), it is corrected in place with audit provenance (`corrected_from`, `corrected_at`).
> - **Missing document insertion**: Any missing line items or dimensional concepts are inserted as complete documents.
> - **Statement scoping**: When `--statement` is specified, reload operations only evaluate and touch concepts belonging to that statement type, leaving other statements completely unaffected.

> Offline mode from a local XBRL zip cache is **not implemented**: `main` always reads the XBRL instance from SEC EDGAR.

---

## CLI Options & Flags

| Flag | Category | Description |
| --- | --- | --- |
| `--fiscal-year YEAR` | Filtering | Target fiscal year (e.g. `2023`) |
| `--fiscal-quarter QUARTER`, `--quarter`, `-q` | Filtering | Target fiscal quarter (`Q1`, `Q2`, `Q3`, `Q4`) |
| `--statement STATEMENT`, `-s` | Filtering | Specific statement (`balance_sheet`, `income`, `cash_flow`) |
| `--reload` | Processing | Non-destructive upsert: preserves correct data, fills gaps, and updates incorrect values |
| `--latest` | Processing | Process only the most recent filing for each company |
| `--no-dimensions` | Processing | Skip dimensional (segment / product / geography) extraction |
| `--no-reconciliation` | Processing | Disable SEC companyfacts gap-fill reconciliation pass |
| `--no-download-html-filings` | HTML | Skip downloading HTML/HTM filings (enabled by default to `SEC_HTML_DOWNLOAD_PATH` or `~/sec_html_filings`, saving both `.htm` and `.html.gz`) |
| `--verbose` | Output | Detailed per-filing INFO log output (disables progress bars) |
| `--debug` | Output | Full DEBUG-level logs (implies `--verbose`) |

---

## Agent Pipeline

Every filing is processed by a LangGraph agent that normalises in memory, then validates, reviews/repairs, resolves hierarchy and extracts guidance **before** anything is written. The agent decides what to write:

| Env var | Default | Effect |
| --- | --- | --- |
| `AGENT_MODE` | `report` | `report` never modifies values; `repair` lets the agent apply provenance-tagged corrections; `strict` also requires explicit approval |
| `STRICT_ACCURACY` | `1` | Unresolved high-severity validation findings refuse the write |
| `AGENT_REVIEW_ENABLED` | `1` | LLM review for repair decisions (only for flagged filings) |
| `AGENT_DECISION_ENABLED` | `0` | Let an LLM judge make the final write decision for flagged filings |
| `GUIDANCE_ENABLED` | `1` | Extract forward-looking MD&A guidance into `guidance_values` |
| `AGENT_AUDIT_ENABLED` | `1` | JSONL node/tool audit trail at `.filings_agent/audit.jsonl` |
| `AGENT_SESSION_FILE` | `.filings_agent/session.jsonl` | Durable run ledger used for resume |

Inspect a single filing without writing anything, or run a headless batch with resume, audit and the company stage:

```bash
uv run filings-agent --fixture tests/fixtures/filing_agent_sample.json
uv run filings-agent --batch tests/fixtures/filing_agent_batch.json --company-stage
uv run filings-agent --batch batch.json --no-resume --audit-log /tmp/audit.jsonl
```

Decisions and findings for every filing are recorded in the `validation_reports` MongoDB collection (including why a filing was written partially or skipped).
