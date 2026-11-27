# Gotham — SEC EDGAR Financial & Guidance Pipeline

## Setup
```bash
cp env.example .env
uv sync
```

---

## Commands (`--form` is required)

```bash
# US XBRL (10-K / 10-Q)
uv run main --companies MSFT --form 10k-10q
uv run main --file stocks_download.txt --form 10k-10q --year 2020 --end-year 2026
uv run main --companies MSFT --form 10-K --fiscal-year 2025
uv run main --companies MSFT --form 10-Q --fiscal-year 2026 --quarter Q2 -s income

# US Combined (8-K first, then 10-K/10-Q; retains 8-K-only values & guidance)
uv run main --companies MSFT --form 8k-10k-10q --year 2024

# Foreign (20-F first, then 6-K)
uv run main --companies ASML --form 20-F --year 2024
uv run main --companies ASML --form 20f-6k --year 2024

# Earnings only (8-K / 6-K / IR PDF URL / local PDF)
uv run main --companies MSFT --form 8-K --year 2024
uv run main --companies ASML --form 6-K --latest
uv run main --companies MSFT --form 8-K --url https://example.com/release.pdf
uv run main --companies MSFT --form 8-K --pdf /path/to/earnings_release.pdf --upsert
uv run main --companies ASML --form 6-K --accession 0001628280-26-048235

# Latest only (works with any form)
uv run main --companies MSFT --form 10k-10q --latest
uv run main --companies MSFT --form 8-K --latest

# Upsert vs Hard Replace
uv run main --companies MSFT --form 10-Q --fiscal-year 2026 --quarter Q2 --upsert
uv run main --companies MSFT --form 10-Q --fiscal-year 2026 --quarter Q2 --replace

# Guidance only (no financial statement writes)
uv run main --companies MSFT --form 8k-10k-10q --guidence-only --year 2024
uv run main --companies ASML --form 20f-6k --guidence-only --year 2024

# Utilities
uv run main --companies MSFT --profile-only --upsert
uv run main --companies MSFT --fix-hierarchy
uv run main --failures --companies MSFT
uv run load-statement --company AAPL --fiscal-year 2024 --quarter Q1 -s income --upsert
```

---

## Workers (Docker & Local)

```bash
docker compose up -d --build                  # build and start single worker container (10kq + 8k)
docker compose restart worker                 # restart after code edits
docker compose logs -f worker

uv run sec-scraper-worker                     # local 10-K/10-Q/20-F worker
uv run earnings-8k-worker                     # local 8-K/6-K/IR PDF worker
```

---

## Flags

| Flag | Description |
| --- | --- |
| `--companies CIK/TICKER [...]` | One or more tickers or CIKs |
| `--file FILE` | Plain-text file with tickers/CIKs |
| `--url URL`, `--filing-url URL` | Direct URL (`8-K`/`6-K`: HTML or PDF URL; `10-K`/`10-Q`/`20-F`: SEC EDGAR URL) |
| `--pdf PATH`, `--pdf-path PATH` | Extract from a local PDF file on disk (`8-K`/`6-K` only) |
| `--form FORM`, `--from FORM` | **Required.** `10-K`, `10-Q`, `10k-10q`, `20-F`, `8-K`, `6-K`, `8k-10k-10q`, `20f-6k` |
| `--year YEAR` | Start year (default: `2010`) |
| `--end-year YEAR` | End year (inclusive) |
| `--fiscal-year YEAR` | Specific fiscal year |
| `--fiscal-quarter Q`, `--quarter`, `-q` | Specific quarter (`Q1`–`Q4` or `1`–`4`; requires `--fiscal-year`) |
| `--statement TYPE`, `-s` | Statement filter: `income`/`is`, `balance_sheet`/`bs`, `cash_flow`/`cf` |
| `--latest` | Process only the newest filing for each company |
| `--upsert` | Non-destructive re-run: updates differing values, fills gaps, keeps extra rows (default skips stored filings) |
| `--replace` | Hard replace: deletes all existing values for the target period first, then inserts fresh |
| `--guidence-only`, `--guidance-only` | Extract & save guidance only (skips financial statements & company stage) |
| `--profile-only` | Create/update `company_profiles` from SEC submissions, DEI shares, and latest `10-K`/`20-F` |
| `--no-new-concept` | Keep stored hierarchy untouched (map to existing concepts only) |
| `--no-dimensions` | Skip dimensional (segment/product/geography) extraction |
| `--no-reconciliation` | Skip SEC `companyfacts` gap-fill reconciliation |
| `--fix-hierarchy`, `--fix-hi` | Fix stored hierarchy in place without re-scraping values |
| `--accession ACC [...]` | Pin exact accession number(s) for `8-K`/`6-K` |
| `--quarterly-only` | Skip annual filings & Q4 derivation for `8-K`/`6-K` |
| `--dry-run` | Preview `8-K`/`6-K` candidates / test connectivity without LLM calls or writes |
| `--failures` | List degraded/failed `8-K`/`6-K` runs from MongoDB |
| `--no-download-html-filings` | Skip downloading filing HTML files |
| `--only-download-files` | Download HTML filings only (no extraction or DB writes) |
| `--verbose` / `--debug` | Detailed INFO / DEBUG logs |
