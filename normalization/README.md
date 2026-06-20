# Financial Data Normalization Service


uv run python main.py --path stocks_download.txt


A comprehensive service for normalizing financial statement data from SEC filings into a structured MongoDB database format with proper hierarchy and period management.

## Features

- **Financial Data Normalization**: Transform SEC financial data into structured, queryable format
- **Hierarchical Organization**: Maintain proper parent-child relationships with materialized paths
- **Cross-Company Hierarchy Reuse**: Automatically copy path and order_key from existing companies for consistent hierarchy (see [HIERARCHY_REUSE.md](docs/HIERARCHY_REUSE.md))
- **Period Management**: Handle quarterly and annual reporting periods with proper deaccumulation
- **Dimensional Analysis**: Support for segment-based financial analysis
- **Duplicate Prevention**: Intelligent duplicate detection and prevention
- **Database Tracking**: Smart processing status tracking to avoid reprocessing
- **Comprehensive Logging**: Multi-level logging with file rotation and progress tracking
- **Progress Monitoring**: Real-time progress bars for long-running operations

## Architecture

```
src/data_normalization_service/
├── core/                    # Core data models and configuration
│   ├── config.py           # Application configuration
│   ├── models.py           # Data models and DTOs
│   └── logging_config.py   # Comprehensive logging configuration
├── database/               # Database access layer
│   ├── database.py         # Repository implementations
│   └── tracker.py          # Processing status tracking
├── services/               # Business logic services  
│   ├── normalization_service.py  # Main normalization service
│   └── quarterly_service.py      # Quarterly calculations
└── utils/                  # Utility modules
    ├── hierarchy.py        # Hierarchy management
    ├── taxonomy.py         # XBRL taxonomy support
    ├── duplicate_prevention.py  # Duplicate prevention
    └── progress.py         # Progress bars and tracking
```

## Installation

1. Clone the repository
2. Install dependencies: `uv sync` or `pip install -e .`
3. Configure environment variables (see Configuration section)
4. **(Optional)** Create MongoDB indexes for optimal performance:
   ```bash
   uv run python scripts/create_indexes.py
   ```

   Note: Indexes are automatically created on first run, but you can create them manually for better control.

## Configuration

Set the following environment variables:

```bash
MONGODB_URI=mongodb://localhost:27017
SOURCE_DB_NAME=source_financial_data
DATABASE_NAME=normalized_financial_data
LOG_LEVEL=INFO
```

## Performance Optimization

For optimal performance with **thousands of companies**, MongoDB indexes are critical:

- **Automatic**: Indexes are created automatically on application startup
- **Manual**: Run `uv run python scripts/create_indexes.py` to create them manually
- **Impact**: 60-600x faster performance with large datasets

See [scripts/README.md](scripts/README.md) for details on index management.

## Usage

### Basic Usage

```bash
# Run full normalization (DEFAULT: sync mode - only inserts missing data)
python main.py

# Process specific company (sync mode)
python main.py --company 0000320193

# Analyze missing data without making changes
python main.py --analyze-missing

# Explicit sync mode (same as default but explicit)
python main.py --sync

# Sync specific company only
python main.py --sync --company 0000320193

# Legacy: Skip quarterly calculations
python main.py --no-quarterly

# Enable taxonomy label fixing
python main.py --fix-lab

# Logging and output options
python main.py --verbose-console    # Show all logs in console
python main.py --no-file-logging    # Disable file logging
python main.py --log-level DEBUG    # Set log level
```

### Sync Mode (Default Behavior)

**NEW**: The normalization service now operates in sync mode by default, which means:

- ✅ **Only inserts new data** - existing data is never replaced or modified
- ✅ **Detects missing periods** - finds gaps in your target database
- ✅ **Handles missing accession numbers** - adds missing accession_number fields to existing data
- ✅ **Safe to run repeatedly** - won't create duplicates or overwrite data
- ✅ **Incremental processing** - picks up where it left off
- ✅ **Preserves concepts** - when all values are deleted but concepts exist, reprocessing keeps those concepts as-is and only adds values

```bash
# Analyze what data is missing (no changes made)
python main.py --analyze-missing

# Sample output:
# Missing Data Analysis Complete:
#   - Companies needing sync: 5
#   - Total missing periods: 23
#   - Values missing accession_number: 1,247
```

### Reprocessing Scenarios

The sync behavior is especially useful when you need to reprocess data:

**Scenario 1: Values deleted, concepts remain**

```bash
# After manually deleting values from concept_values_annual/quarterly
# but keeping concepts in normalized_concepts_annual/quarterly
python main.py --company 0001318605
# Result: Concepts are preserved, only values are added back
```

**Scenario 2: Complete refresh needed**

```bash
# After deleting both concepts and values for a company
python main.py --company 0001318605
# Result: Everything is recreated fresh
```

**Scenario 3: Partial data gaps**

```bash
# When some periods are missing
python main.py --company 0001318605
# Result: Existing data preserved, only missing periods added
```

See [docs/SYNC_BEHAVIOR.md](docs/SYNC_BEHAVIOR.md) for detailed documentation on sync behavior and reprocessing.

### Logging and Progress Tracking

The service provides comprehensive logging and progress tracking:

- **Progress bars** show real-time progress for all operations
- **Console output** shows only errors, warnings, and progress by default
- **Log files** capture all detailed information in `logs/` directory
- **Multiple log files**: main log, error log, and debug log (when enabled)

See [Logging System Documentation](docs/logging_system.md) for complete details.

### Programmatic Usage

```python
from data_normalization_service.core.config import AppConfig
from data_normalization_service.services.normalization_service import FinancialNormalizationService

# Create configuration
config = AppConfig.from_env()

# Create and run service
service = FinancialNormalizationService(config)
service.normalize_data()
```

## Data Flow

### Process 1: Direct Statement Normalization

1. **Company Migration**: Copy companies from source → target
2. **Statement Processing**: For each financial statement:
   - Extract financial data items
   - Build hierarchical paths (materialized paths like "001", "001.001")
   - Generate order keys for proper sequencing
   - Create concept documents with metadata
   - Extract time-period values (YYYY-MM-DD format only)
   - Insert concepts and values into target collections

### Process 2: Quarterly Calculations & Cash Flow Normalization

**Cash Flow Deaccumulation**:

- Q1: Individual values (preserve as-is)
- Q2: Deaccumulate Q2 = Q2_cumulative - Q1_individual
- Q3: Deaccumulate Q3 = Q3_cumulative - (Q1_individual + Q2_individual)

## Database Schema

### Companies Collection

```json
{
  "_id": ObjectId,
  "cik": "0000320193",
  "name": "Apple Inc.",
  "ticker_symbol": "AAPL",
  "corporate_info": {...},
  "industry": {...},
  "market_info": {...}
}
```

### Normalized Concepts Collection

```json
{
  "_id": ObjectId,
  "company_cik": "0000320193",
  "statement_type": "income_statement",
  "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
  "label": "Net sales",
  "path": "001",
  "order_key": "a",
  "abstract": false,
  "dimension": false
}
```

### Concept Values Collection

```json
{
  "_id": ObjectId,
  "concept_id": ObjectId,
  "company_cik": "0000320193",
  "statement_type": "income_statement",
  "form_type": "10-K",
  "reporting_period": {
    "end_date": "2023-09-30T00:00:00.000+00:00",
    "period_date": "2023-09-30",
    "fiscal_year": 2023,
    "accession_number": "0000950170-25-100235"
  },
  "value": 383285000000
}
```

**New Feature**: The `accession_number` field is now automatically included in the `reporting_period` object for both `concept_values_annual` and `concept_values_quarterly` collections. This field is extracted from the source database's filing information during normalization and provides traceability back to the original SEC filing.

## Testing

Run tests with:

```bash
# Run all tests
pytest

# Run specific test categories
pytest tests/unit/
pytest tests/integration/

# Run with coverage
pytest --cov=src/data_normalization_service
```

## Development

### Adding New Features

1. Create feature branch
2. Add tests in appropriate directory
3. Implement feature
4. Update documentation
5. Submit pull request

### Code Style

- Follow PEP 8
- Use type hints
- Add docstrings for public methods
- Keep line length under 88 characters

## License

MIT License
  "statement_type": "income_statement",
  "form_type": "10-K",
  // Note: filing_id is no longer stored in target database
  "reporting_period": {
    "end_date": Date,
    "period_date": "2024-09-28",
    "period_type": "annual"
  },
  "value": 391035000000,           // Actual financial value
  "created_at": Date
}

## Service Entry Points

Main Normalization Service:

```bash
# 🚀 DEFAULT: Full normalization with quarterly calculations (RECOMMENDED)
python main.py

# Normalization without quarterly calculations
python main.py --skip-quarterly

# Only quarterly calculations
python main.py --quarterly-calculations

# Generate quarterly report
python main.py --quarterly-report CIK YEAR  
```

Advanced Usage Examples:

```bash
# 🎯 MAIN COMMAND: Process everything (DEFAULT)
# This runs both normalization and quarterly calculations
python main.py

# Generate quarterly report for Microsoft Corp fiscal year 2025
python main.py --quarterly-report 0000789019 2025

# Skip all quarterly processing during normalization
python main.py --skip-quarterly
```

## Process Flow When Running Default Command (python main.py):

```
┌─────────────────────────────────────────────────────────────┐
│ 🔄 Process 1: Direct Statement Normalization                │
│   • Copy companies from source → target database            │
│   • Process financial statements with hierarchy             │
│   • Create concept documents and extract values             │
├─────────────────────────────────────────────────────────────┤
│ 🔄 Process 2: Quarterly Calculations & Cash Flow           │
│   • Deaccumulate cumulative cash flow data (Q1, Q2, Q3)    │
│   • Process individual quarterly values                     │
└─────────────────────────────────────────────────────────────┘  
```
