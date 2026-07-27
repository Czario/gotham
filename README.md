## Common Commands

### Process companies from a file (most common)

```bash
uv run main --file stocks_download.txt

# Load a range
uv run main --file stocks_download.txt --year 2015 --end-year 2026

# Reload (force reprocess) the same range
uv run main --file stocks_download.txt --year 2015 --end-year 2026 --reload
```

### Process specific companies by CIK

```bash
uv run main --companies 0000320193 0000789019
```

### Process top N companies from tickers.json

```bash
uv run main --tickers --limit 10
```

### Process a single filing by URL

```bash
uv run main --url "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"
```

### Incremental — only new filings since last run

```bash
uv run main --file stocks_download.txt --latest
```

### Force reload a specific fiscal year

```bash
uv run main --file stocks_download.txt --reload --fiscal-year 2023
```

### Force reload a specific quarter

```bash
uv run main --file stocks_download.txt --reload --fiscal-year 2023 --fiscal-quarter Q2
```

### Process from local XBRL zip cache (offline)

```bash
uv run main --file stocks_download.txt --local
```

