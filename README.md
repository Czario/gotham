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

### Process a single filing by URL

```bash
uv run main --url "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm"
```

### Incremental — only new filings since last run

```bash
uv run main --file stocks_download.txt --latest
```

To force-refresh only the latest SEC filing, combine `--latest` and `--reload`.
The selected filing is extracted first, then deleted by accession number immediately before the replacement data is saved:

```bash
uv run main --file stocks_download.txt --latest --reload
```

### Force reload a specific fiscal year

A fiscal year without `--fiscal-quarter` selects only the annual `10-K` filing.
It does not process the three quarterly `10-Q` filings for that year.

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

