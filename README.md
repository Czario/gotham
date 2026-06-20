# SEC 10-K / 10-Q Extraction Service

Extracts XBRL financial data from SEC EDGAR filings, normalizes it, and stores it in MongoDB.
Dimensions and HTML downloads are **on by default**. SEC companyfacts gap-fill reconciliation runs automatically after each company.

---

## Setup

```bash
cp env.example .env          # fill in MONGODB_URI, DATABASE_NAME, SEC_HTML_DOWNLOAD_PATH, XBRL_ZIP_CACHE_PATH
uv sync                      # install dependencies + registers the `sec-scraper` command
```

---

## Common Commands

### Process companies from a file (most common)
```bash
uv run sec-scraper --file stocks_download.txt
```

### Process specific companies by CIK
```bash
uv run sec-scraper --companies 0000320193 0000789019
```

### Process top N companies from tickers.json
```bash
uv run sec-scraper --tickers --limit 10
```

### Process a single filing by URL
```bash
uv run sec-scraper --url "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"
```

### Incremental — only new filings since last run
```bash
uv run sec-scraper --file stocks_download.txt --incremental
```

### Force reload a specific fiscal year
```bash
uv run sec-scraper --file stocks_download.txt --reload --fiscal-year 2023
```

### Force reload a specific quarter
```bash
uv run sec-scraper --file stocks_download.txt --reload --fiscal-year 2023 --fiscal-quarter Q2
```

### Process from local XBRL zip cache (offline)
```bash
uv run sec-scraper --file stocks_download.txt --local
```

---

## Output Modes

| Mode | Command | What you see |
|---|---|---|
| **Clean** (default) | *(no flag)* | tqdm progress bars — company + per-filing |
| **Verbose** | `--verbose` | Full INFO logs, no bars |
| **Debug** | `--debug` | Full DEBUG logs, no bars |

---

## Opt-out Flags

| Flag | Effect |
|---|---|
| `--no-dimensions` | Skip dimensional data (segments, geography, products) |
| `--no-download-html-filings` | Skip HTML filing downloads |
| `--no-reconciliation` | Skip SEC companyfacts gap-fill pass |

---

## Extras

```bash
# Fill gaps from SEC companyfacts API manually (all companies)
uv run python normalization/scripts/reconcile_companyfacts.py

# Dry-run (shows what would be filled, no writes)
uv run python normalization/scripts/reconcile_companyfacts.py --dry-run

# Fill gaps for specific CIK only
uv run python normalization/scripts/reconcile_companyfacts.py --cik 0000320193

# Run tests
uv run python run_tests.py unit
uv run python run_tests.py all
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MONGODB_URI` | ✅ | MongoDB connection string |
| `DATABASE_NAME` | ✅ | Target database name |
| `SEC_HTML_DOWNLOAD_PATH` | ✅ | Directory to save HTML filings |
| `XBRL_ZIP_CACHE_PATH` | ✅ | Directory to cache XBRL zip files |
| `SEC_USER_AGENT` | optional | Override HTTP User-Agent header sent to SEC |
