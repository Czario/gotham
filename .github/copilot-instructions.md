# Copilot Instructions

## Build & Dependency Management

This project uses **UV** as the package manager (not pip/pip3 directly).

```bash
uv sync                  # Install/sync all dependencies
uv run python <script>   # Run a script in the venv
```

## Testing

```bash
# Run a single test file
uv run pytest tests/unit/test_something.py -v

# Run a single test function
uv run pytest tests/unit/test_something.py::TestClass::test_name -v

# Test suites via the unified runner
uv run python run_tests.py unit          # Fast, isolated (tests/unit/)
uv run python run_tests.py integration   # Component interaction (tests/integration/)
uv run python run_tests.py e2e           # Full workflow (tests/e2e/)
uv run python run_tests.py all           # All suites
uv run python run_tests.py coverage      # Unit + integration with coverage report
```

pytest markers: `unit`, `integration`, `e2e`, `slow`, `requires_network`, `requires_database`

## Environment Setup

Copy `env.example` to `.env`. All four variables are **required** — the app raises `RuntimeError` at startup if any are missing:

```
MONGODB_URI=mongodb://localhost:27017/
DATABASE_NAME=normalize_data
SEC_HTML_DOWNLOAD_PATH=/path/to/sec_html_filings
XBRL_ZIP_CACHE_PATH=/path/to/xbrl_zip_cache
```

## Architecture

### Data Flow

```
SEC EDGAR API → XBRL Download & Parse (Arelle) → Extraction → Transformation → MongoDB
                                                                                     ↓
                                                              Normalization Service (merged pipeline)
```

### Module Map

| Path | Responsibility |
|---|---|
| `sec_scraper_cli.py` | CLI entry point; orchestrates the full pipeline |
| `api/sec_client.py` | SEC EDGAR HTTP client; rate-limited, uses `tickers.json` for CIK→ticker mapping |
| `core/extractors/` | XBRL parsing via Arelle; dimensional data extraction |
| `core/processors/statement_processor.py` | Assembles financial statements from raw XBRL facts |
| `core/transformers/` | Data transformation and sign-convention normalization |
| `database/config/mongodb_config.py` | `DatabaseConfig` class + MongoDB collection schemas and indexes |
| `database/repositories/repositories.py` | MongoDB CRUD operations |
| `utilities/` | Logging, error handling, period utilities, SEC URL detection, filters |
| `normalization/` | Local editable package (`data-normalization-service`); normalizes and writes final data to MongoDB |

### Normalization Package

`normalization/` is a local editable package installed as `data-normalization-service` (see `pyproject.toml`). It runs as a **merged pipeline** — it reads extracted data from the same MongoDB database and writes normalized output to the `normalize_data` database directly (no separate service call needed). Key entry points:

- `data_normalization_service.services.normalization_service.FinancialNormalizationService`
- `data_normalization_service.services.quarterly_service.PeriodBasedFinancialCalculationService`

### MongoDB Collections

Three primary collections defined with JSON Schema validators in `database/config/mongodb_config.py`:

- **`companies`** — indexed by `cik` (unique), name, tickers, SIC code
- **`filings`** — `(company_cik, accession_number)` unique; `form_type` is an enum: `10-K`, `10-Q`, `8-K`, `DEF 14A`, `S-1`, `S-3`
- **`financial_statements`** — `financial_data` is an array where each item has `order`, `concept`, `label`, `level`, `abstract` (bool), `dimension` (bool); `statement_type` is an enum: `income_statement`, `balance_sheet`, `cash_flows`, `equity_changes`, `comprehensive_income`

## Key Conventions

- **CIKs** are always zero-padded 10-digit strings (e.g., `"0000320193"` for Apple).
- **Incremental processing**: the `--incremental` flag (and default behavior) skips already-processed filings. Use `--reload` to force reprocessing.
- **Arelle warnings suppressed intentionally**: `invalidTransformation`, `unrecognized transformation namespace`, `resourceIdDuplication`, and `xmlSchema:syntax` warnings are filtered out in `core/extractors/xbrl_parser.py` — they are harmless artifacts of old SEC filings.
- **Logging**: configured via `utilities/helpers/logger_config.py` (`LoggerConfig`). Each module uses `get_module_logger(__name__)`. Log output goes to `logs/`.
- **Error handling**: use helpers from `utilities/helpers/error_handling.py` (`safe_processing_operation`, `validate_required_fields`, `log_operation_result`) rather than bare try/except in pipeline code.
- **`--extra-data` flag**: enables advanced pattern matching to find financial facts missed by the primary extraction pass.
- The `data_access/mongodb_repositories.py` file is a legacy layer; current code uses `database/repositories/repositories.py`.
