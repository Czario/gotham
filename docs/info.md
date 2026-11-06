Gotham: Overview of Capabilities, Commands & AI Integration

  Gotham is an enterprise-grade SEC EDGAR financial data ingestion, normalization, and intelligence
  engine. It extracts, standardizes, validates, and stores complete 10-K, 10-Q, and 20-F financial
  statements (Income, Balance Sheet, Cash Flow, and dimensional segments) into MongoDB.
  ──────
  ## 1. Core Capabilities & Features

  ### Financial Data Ingestion & Extraction
  • SEC EDGAR Integration: Automated discovery and retrieval of company filings, submissions, and
  XBRL taxonomies.
  • Multi-Form Support: Domestic annual (10-K, 10-K/A), quarterly (10-Q, 10-Q/A), and foreign
  annual private issuer filings (20-F, 20-F/A).
  • Statements Parsed: Condensed and Consolidated Balance Sheets, Income Statements (Operations),
  and Cash Flow Statements.
  • Dimensional Data: Extracts multi-axis facts (Product breakdowns, Business/Operating segments,
  Geographic revenue, Consolidation items).
  ### Normalization & Accounting Logic
  • Quarterly Cash Flow Deaccumulation: Automatically converts cumulative YTD cash flows (6M, 9M,
  12M) into discrete 3-month quarterly values (Q₂ = YTD₂ - Q₁; Q₃ = YTD₃ - YTD₂).
  • Canonical Date Alignment: Ensures single-column consistency per fiscal quarter; eliminates
  split columns caused by off-by-one filing vs. preliminary earnings dates.
  • Smart Non-Destructive Upsert: Re-filings and new extractions overwrite and correct
  values/dates/accession numbers with full priority while safely preserving existing supplementary
  data (e.g. segment details or margin metrics from earnings releases).
  • SEC Companyfacts Reconciliation: Cross-checks and backfills gaps in historical periods against
  the official SEC companyfacts API.
  ### Infrastructure & Distributed Execution

  • Redis Queue Worker: Distributed task processing (worker_10kq / sec-scraper-worker) for
  processing filings in bulk via Redis queues.
  • Audit Trails & Validation Reports: Every statement runs through accounting mathematical balance
  checks, producing detailed validation findings in validation_reports.
  ──────
  ## 2. CLI Commands & Quick Reference

  Gotham provides four primary CLI entry points:
  ### 1. main (or sec_scraper_cli.py)

  The primary CLI for fetching, extracting, normalizing, and storing filings.

   Command / Flag                                 │ Purpose
  ────────────────────────────────────────────────┼────────────────────────────────────────────────
   uv run main --companies AAPL MSFT              │ Process all filings (from 2010 onward) for the
                                                  │ specified tickers/CIKs.
   uv run main --file tickers.txt                 │ Bulk process all companies listed in a text
                                                  │ file.
   uv run main --companies AAPL --fiscal-year     │ Process a single quarter for a company.
   2026 --quarter Q3                              │
   uv run main --companies AAPL --latest          │ Process only the single most recent filing on
                                                  │ EDGAR.
   uv run main --companies AAPL --reload          │ Force re-extraction and in-place upsert of
                                                  │ existing periods.
   uv run main --companies AAPL --statement       │ Restrict parsing to a specific statement
   income                                         │ (income, balance_sheet, cash_flow).
   uv run main --url <SEC_EDGAR_URL>              │ Ingest a single filing directly from its SEC
                                                  │ URL.
   uv run main --no-dimensions                    │ Skip multi-axis dimensional fact extraction
                                                  │ for faster ingestion.
   uv run main --no-reconciliation                │ Skip the post-processing SEC companyfacts
                                                  │ reconciliation step.
   uv run main --only-download-files              │ Download raw HTML/XBRL filings without writing
                                                  │ to the database.
  ### 2. load-statement
  Dedicated shortcut CLI to target a specific statement in a specific quarter:
    uv run load-statement --companies AAPL --fiscal-year 2026 --quarter Q3 --statement income

  ### 3. sec-scraper-worker (or worker_10kq.py)

  Runs the distributed Redis queue worker daemon to consume filing jobs published to
  sec:filings:10kq:
    uv run sec-scraper-worker --queue sec:filings:10kq
  ### 4. filings-agent (or main_filing_agent.py)
  Direct testbed to run an isolated statement fixture through the LangGraph agent pipeline:

    uv run python main_filing_agent.py --fixture fixture.json --persist -v
  ──────
  ## 3. Where & How AI (LLM) is Used
  Gotham uses an agentic LangGraph pipeline (filings_agent) where autonomous LLM agents (supporting
  DeepSeek, Gemini, Groq, and Ollama with local prompt caching) handle complex tasks that
  deterministic rules cannot reliably solve:

  │ Diagram exceeds terminal width (132 > 101 cols)
  │ Displayed as code block. Widen terminal to view inline.

    flowchart LR
        A["Extract XBRL"] --> B["Sign Agent (AI)"]
        B --> C["Hierarchy Agent (AI)"]
        C --> D["Review & Repair (AI)"]
        D --> E["Decide Node (AI)"]
        E --> F["Persist Statement"]
        F --> G["Guidance Agent (AI)"]

        style B fill:#e8f0fe,stroke:#4285f4,stroke-width:2px
        style C fill:#e8f0fe,stroke:#4285f4,stroke-width:2px
        style D fill:#e8f0fe,stroke:#4285f4,stroke-width:2px
        style E fill:#e8f0fe,stroke:#4285f4,stroke-width:2px
        style G fill:#e8f0fe,stroke:#4285f4,stroke-width:2px

  ### 1. Forward-Looking MD&A Guidance Extraction (guidance_extract_node)
  • What it does: Parses 20,000–50,000+ characters of unstructured text from the Management's
  Discussion & Analysis (MD&A) section of 10-K and 10-Q filings.
  • How AI works: Uses an agentic tool-calling loop (search, read_lines, get_document_info,
  finalize_guidance) to scan for outlooks, forecasts, and targets.
  • Output: Extracts structured forward-looking metrics (Revenue growth targets, Gross Margin
  forecasts, Capex expectations, Tax rate outlooks) and writes them to the guidance_values
  collection.

  ### 2. Concept Hierarchy & Statement Tree Placement (hierarchy_agent)

  • What it does: Solves statement tree placement for new or custom XBRL tags (e.g., custom company
  extensions).
  • How AI works: Inspects surrounding line items and existing taxonomy trees, proposing parent-
  child relationships, tree depth (path), order keys, and grouping headers (e.g.
  custom:ProductSegmentation).
  • Safety: Generates an in-memory preview of the statement tree and validates that no concepts are
  orphaned before approval.

  ### 3. Accounting Sign Convention Audit & Correction (sign_agent)

  • What it does: Fixes flipped sign anomalies where companies report positive values for credit
  balances or negative values for debit balances (e.g. Accounts Receivable, Share-Based
  Compensation, Treasury Stock).
  • How AI works: Audits flagged line items in context against US-GAAP / IFRS presentation rules
  and flips signs back to standard conventions when needed.

  ### 4. Mathematical Validation Review & Repair (review_node / repair_node)

  • What it does: Inspects accounting discrepancies (such as Balance Sheet inequality Assets ≠
  Liabilities + Equity or missing subtotal components).
  • How AI works: Evaluates validation findings, identifies whether a tag was misclassified (e.g.,
  comprehensive income mixed into operations), and proposes corrections under a strict verification
  gate.

  ### 5. Persistence Policy & Gatekeeper (decide_node)

  • What it does: Decides whether a filing is clean enough to write to MongoDB or must be held.
  • How AI works: Evaluates blocking vs. non-blocking validation defects to ensure mathematically
  corrupted filings are not saved to the production database.

  ### 6. Pipeline Self-Healing & Error Recovery (recovery.py)

  • What it does: Handles unexpected crashes or edge cases within graph nodes.
  • How AI works: Catches node exceptions, sends the stack trace and state context to an LLM
  recovery agent, and determines whether to retry with modified parameters, skip cleanly, or
  gracefully degrade.












### 1. Scope & Capabilities of the Upsert Process
  The upsert process in Gotham operates on a canonical identity matching and non-destructive merge
  model:
  #### Identity Matching
  • Quarterly: Matches records by (cik, concept_id, reporting_period.fiscal_year, reporting_period.
  quarter).
  • Annual: Matches records by (cik, concept_id, reporting_period.fiscal_year).
  • Dimensional Concepts: Lookups match across both modern dimensional structures
  (dimensional_concept_id, dimension_value: True) and legacy/earnings records (concept_id,
  dimension_value: False).
  • Matching is independent of minor period_date variations, ensuring 1-day calendar offsets (e.g.
  2026-06-27 vs 2026-06-28) do not cause duplicates or missed matches.
  #### Fields Updated When Extracted from Filing

  When a concept is extracted from a SEC filing, the upsert process updates:

  1. value: Updated to the authoritative filing fact. If the value changed, corrected_from and
  corrected_at are recorded for auditability.
  2. reporting_period: period_date and end_date are updated to the filing's canonical report dates;
  extraneous fields (like start_date) are removed.
  3. accession_number: Updated to the current SEC filing accession number (e.g. 0000320193-26-
  000020).
  4. form_type & statement_type: Updated to the filing's form (10-Q, 10-K, etc.) and statement type.
  5. fact_id & decimals: Updated to match the XBRL fact metadata.
  6. calculated: Set to False for as-reported facts (or True for deaccumulated cash flows / derived
  rows).
  7. earning_data flag: Automatically $unset / removed if the row previously came from a
  preliminary earnings release.
  8. Concurrency & Conflict Handling: Wrapped with atomic DuplicateKeyError fallback to update in-
  place if a unique key conflict arises.
  ──────
  ### 2. What Happens if a Concept Has Existing Data in a Period (e.g. 2026 Q2) but Gotham Finds No
  Data for it in the Filing?

  If a concept is already in the database for 2026 Q2 (for example, from an earlier 8-K earnings
  release, a prior filing, or a custom formula), and the new 10-Q filing does not contain that
  concept:

  1. The Existing Data is Preserved (Not Deleted)
      • Gotham does not wipe the quarter. The reload / persist process is strictly non-destructive.
      • Any concept not present in the new filing remains stored in MongoDB with its original value,
      original accession_number, and provenance flags (e.g. earning_data: True).
  2. Dates are Synchronized to the Canonical Quarter Date
      • Via _align_period_dates_for_fiscal_period, the system checks all remaining records in that
      (cik, fiscal_year, quarter).
      • If the preserved concept had an old/different date (e.g. 2026-03-28 from an earnings
      release vs 2026-03-27 from the 10-Q), its reporting_period.period_date and reporting_period.
      end_date are automatically updated to match the filing's canonical date (2026-03-27).
      • Result: The preserved concept merges cleanly into the single Q2 column in the UI rather
      than creating a split ghost column.
  3. Why This is Useful:
      • Segment & Product Breakdowns: Some companies report detailed product/segment sales in their
      earnings releases that may be omitted or structured differently in the primary 10-Q statement
      tables. These rows stay available under the unified quarter column.
      • Custom & System Calculated Rows: Custom user formulas, margin ratios (such as
      system:GrossMargin), and supplementary metrics remain intact and aligned with the newly filed
      numbers.