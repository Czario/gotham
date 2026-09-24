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

> Offline mode from a local XBRL zip cache is **not implemented**: `main` always
> reads the XBRL instance from SEC EDGAR.  The previously documented `--local`
> flag does not exist.

---

## Agent pipeline

Every filing is processed by a LangGraph agent that normalises in memory, then
validates, reviews/repairs, resolves hierarchy and extracts guidance **before**
anything is written.  The agent decides what to write:

| Env var | Default | Effect |
| --- | --- | --- |
| `AGENT_MODE` | `report` | `report` never modifies values; `repair` lets the agent apply provenance-tagged corrections; `strict` also requires explicit approval |
| `STRICT_ACCURACY` | `1` | Unresolved high-severity validation findings refuse the write |
| `AGENT_REVIEW_ENABLED` | `1` | LLM review for repair decisions (only for flagged filings) |
| `AGENT_DECISION_ENABLED` | `0` | Let an LLM judge make the final write decision for flagged filings |
| `GUIDANCE_ENABLED` | `1` | Extract forward-looking MD&A guidance into `guidance_values` |
| `AGENT_AUDIT_ENABLED` | `1` | JSONL node/tool audit trail at `.filings_agent/audit.jsonl` |
| `AGENT_SESSION_FILE` | `.filings_agent/session.jsonl` | Durable run ledger used for resume |

Inspect a single filing without writing anything, or run a headless batch with
resume, audit and the company stage:

```bash
uv run filings-agent --fixture tests/fixtures/filing_agent_sample.json
uv run filings-agent --batch tests/fixtures/filing_agent_batch.json --company-stage
uv run filings-agent --batch batch.json --no-resume --audit-log /tmp/audit.jsonl
```

Decisions and findings for every filing are recorded in the
`validation_reports` MongoDB collection (including why a filing was written
partially or skipped).

