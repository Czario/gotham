# SEC EDGAR Financial Data Scraper & Normalization Pipeline

Extracts financial statements and forward-looking guidance from SEC EDGAR XBRL filings (`10-K`, `10-Q`, `20-F`, `20-F/A`), earnings releases (`8-K`, `6-K`), and IR press-release PDFs/URLs, normalizes concepts and dimensional breakdowns, validates and repairs through an agent pipeline, and stores canonical data in MongoDB (`normalize_data`).

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

### Keep stored hierarchy untouched (`--no-new-concept`)

When `--no-new-concept` is passed, the pipeline keeps the existing stored hierarchy in MongoDB untouched: incoming concepts that map to an existing concept in the stored hierarchy are allowed (values attach to the stored concept), while genuinely new unmapped concepts are skipped so values are only inserted into existing hierarchy rows.

```bash
uv run main --companies AAPL --reload --fiscal-year 2026 --fiscal-quarter Q3 --no-new-concept
```

### Load guidance only (`--guidence-only` / `--guidance-only`)

Extract and save forward-looking guidance (from `10-K`/`10-Q`/`20-F` MD&A and/or `8-K`/`6-K` earnings releases) into `guidance_values` without extracting, normalizing, validating, or writing financial statements, company metadata, or post-processing stages (Q4 calculation / SEC `companyfacts` reconciliation).

- Skips filings that already have guidance stored in `guidance_values` (even if financial statements are already stored for the filing).
- With `--reload`, re-extracts guidance for the selected filings and upserts in place (sweeping stale `source="llm"` rows while keeping `source="manual"` entries untouched, and never touching financial statements).

```bash
# Load guidance across ALL relevant forms (US: 10-K/10-Q/8-K; Foreign: 20-F/6-K)
uv run main --companies MSFT --form all --guidence-only
uv run main --companies ASML --form all --guidence-only --year 2024

# Load guidance for a company from a specific year onwards
uv run main --companies AAPL --year 2025 --guidence-only

# Load guidance for only the latest filing
uv run main --companies AAPL --latest --guidence-only

# Force re-extract guidance for a specific quarter
uv run main --companies AAPL --fiscal-year 2025 --quarter Q2 --guidence-only --reload

# Load guidance for a single filing by URL
uv run main --url "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm" --guidence-only
```

*(Note: `--guidance-only` and `--guidence-only` are interchangeable aliases.)*

### Select SEC forms (`--form`)

Control exactly which SEC forms to process — works with `--year`, `--end-year`, `--fiscal-year`, `--quarter`, `--latest`, `--reload`, and `--statement`:

| `--form` value | What it processes |
| --- | --- |
| *(omitted)* | Default XBRL (`10-K`, `10-Q`, `20-F`, `20-F/A`) |
| `all` | **Auto-detected per company** — US: `10-K`, `10-Q`, `8-K`; Foreign: `20-F`, `20-F/A`, `6-K` |
| `10k-10q` | Only `10-K` and `10-Q` (`10-K` or `10-Q` alone also supported) |
| `20-F` | Only `20-F` / `20-F/A` |
| `8-K` | All `8-K` earnings releases from `--year` (default `2010`) to now |
| `6-K` | All `6-K` foreign earnings releases from `--year` (default `2010`) to now |
| `20f-6k`, `10k-10q-8k` | Any combination of XBRL + earnings forms |

```bash
# Complete data (financial statements + guidance) — auto-detects US vs foreign
uv run main --companies MSFT --form all                  # US: 10-K, 10-Q, 8-K
uv run main --companies ASML --form all                  # Foreign: 20-F, 6-K

# Specific forms (2010 → now by default, or scoped by year/quarter)
uv run main --companies MSFT --form 10k-10q
uv run main --companies MSFT --form 8-K --year 2024
uv run main --companies ASML --form 6-K --fiscal-year 2025 --quarter Q2 --reload

# --latest works for ANY form (processes only the single newest filing)
uv run main --companies MSFT --form all --latest
uv run main --companies MSFT --form 8-K --latest
uv run main --companies ASML --form 6-K --latest

# Manual IR PDF / press-release URL, pinned accession, or failure inspection
uv run main --companies MSFT --form 8-K --url https://example.com/release.pdf
uv run main --companies ASML --form 6-K --accession 0001628280-26-048235
uv run main --failures --companies MSFT
```

### Background Redis Workers (Docker Compose)

`gotham` runs both workers from a single multi-stage `Dockerfile`:
- `worker-10kq` — 10-K / 10-Q / 20-F XBRL filings (`sec:filings:10kq`, lean image)
- `worker-8k` — 8-K / 6-K / IR PDF earnings releases (`sec:filings:8k`, includes Playwright + Chromium)

```bash
# Docker Compose (recommended)
docker compose up -d --build                 # build and start both workers
docker compose restart worker-10kq worker-8k # restart after code edits (no rebuild)
docker compose logs -f worker-10kq worker-8k

# Local workers (without Docker)
uv run sec-scraper-worker                    # 10-K / 10-Q / 20-F worker
uv run earnings-8k-worker                    # 8-K / 6-K / IR PDF worker
```

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
| `--no-new-concept` | Processing | Keep stored hierarchy untouched: map matching tags to existing concepts and skip genuinely new concepts so values only populate existing hierarchy rows |
| `--guidance-only`, `--guidence-only` | Processing | Extract and save forward-looking guidance (`10-K`/`10-Q`/`20-F` MD&A and `8-K`/`6-K` releases) into `guidance_values` only — skips financial statement writes and company post-processing |
| `--form FORM`, `--from FORM` | Filtering | SEC form(s): `all` (auto US `10-K/10-Q/8-K` or foreign `20-F/6-K`), `10-K`, `10-Q`, `10k-10q`, `20-F`, `8-K`, `6-K`, `20f-6k`, `10k-10q-8k` |
| `--earnings` | Earnings | Shortcut to run `8-K`/`6-K` earnings release extraction (optional when `--form 8-K` or `--form 6-K` is passed) |
| `--accession ACC [...]` | Earnings | Pin one or more exact accession numbers for `8-K` / `6-K` |
| `--quarterly-only` | Earnings | Skip annual filings and post-save Q4 derivation for `8-K` / `6-K` |
| `--dry-run` | Earnings | Preview matching `8-K` / `6-K` filings or test connectivity without LLM calls or DB writes |
| `--failures` | Earnings | Inspect degraded/failed `8-K` / `6-K` runs in MongoDB |
| `--fix-hierarchy`, `--fix-hi` | Processing | Fix stored hierarchy in place using the Hierarchy Agent and SEC HTML tables without re-scraping values |
| `--no-download-html-filings` | HTML | Skip downloading HTML/HTM filings (enabled by default to `SEC_HTML_DOWNLOAD_PATH` or `~/sec_html_filings`, saving both `.htm` and `.html.gz`) |
| `--only-download-files` | HTML | Download HTML filings only — no XBRL extraction or database writes |
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
