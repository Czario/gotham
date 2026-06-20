uv run python sec_scraper_cli.py --file "stocks_download.txt" --dimensions --download-html-filings



uv run python sec_scraper_cli.py --companies 0000320193 0000789019  --dimensions --extra-data

# Process only new filings for all tickers since last update

python sec_scraper_cli.py --tickers --dimensions --incremental

# Process only new filings for specific company since last update

python sec_scraper_cli.py --companies 0000789019 --dimensions --incremental

uv run python sec_scraper_cli.py --companies 0000789019 --dimensions --download-html-filings --fiscal-year 2012 --fiscal-quarter Q2

# Combine with other options

python sec_scraper_cli.py --tickers --limit 5 --dimensions --incremental --debug

# SEC Data Scraper v9 - Modern & Optimized

A production-ready SEC filing data scraper with MongoDB integration, modern CLI, and intelligent company discovery powered by [Arelle](https://arelle.org) XBRL processing.

## Features

- **XBRL Processing**: Uses Arelle for robust, standards-compliant XBRL parsing
- **Smart Company Discovery**: Uses comprehensive ticker database (`tickers.json`) with 10,000+ companies
- **Modern CLI**: Clean, intuitive command-line interface with built-in help
- **MongoDB Integration**: Efficient data storage and retrieval
- **Rate Limiting**: Respects SEC API limits automatically
- **Performance Monitoring**: Real-time metrics and reporting
- **Dimensional Support**: Full XBRL dimensions support for complex financial data

## Quick Start

### Prerequisites

1. Python 3.8+
2. MongoDB running locally or remote connection
3. UV package manager (recommended)

### Installation

```bash
# Install dependencies
uv sync
```

### Basic Usage

```bash
# Process 10 companies from ticker database (default)
python sec_scraper_cli.py

uv run python sec_scraper_cli.py --companies 0000320193 --dimensions

# Process 50 companies from ticker database
python sec_scraper_cli.py --tickers --limit 50

# Process specific companies by CIK
python sec_scraper_cli.py --companies 0000320193 0000789019

# Process from a custom file
python sec_scraper_cli.py --companies-file my_companies.txt

# Process filings from 2020 onwards
python sec_scraper_cli.py --year 2020

# Process Apple and Microsoft with dimensions and extra data extraction
python sec_scraper_cli.py --report

# Reset and start fresh
python main.py --reset
```

## XBRL Processing with Arelle

This project uses [Arelle](https://arelle.org), the industry-standard XBRL processor, for parsing SEC filings. Arelle provides:

- **Standards Compliance**: Full XBRL 2.1 and XBRL Dimensions support
- **SEC EDGAR Support**: Built-in support for SEC filing formats
- **Validation**: Comprehensive XBRL validation capabilities
- **Plugin Architecture**: Extensible with additional plugins

### Testing Arelle Integration

```bash
# Test that Arelle is working correctly
uv run python test_arelle_integration.py

# Access Arelle command line tools
uv run arelleCmdLine --help
```

For detailed information, see [ARELLE_INTEGRATION.md](ARELLE_INTEGRATION.md).

## Company Sources

### Default: Ticker Database (Recommended)

The scraper includes `tickers.json` with 10,000+ public companies. By default, it processes 10 companies:

```bash
python main.py                    # Process 10 companies
python main.py --tickers --limit 25  # Process 25 companies
```

### Custom Company Lists

You can also provide specific companies or use a custom file:

```bash
# Specific CIKs
python main.py --companies 0000320193 0000789019

# Custom file (one CIK per line)
python main.py --companies-file custom_list.txt
```

### Enhanced Data Extraction

Enable comprehensive missing facts detection to find additional financial data:

```bash
# Standard extraction
python main.py --companies 0001568651

# With extra data detection enabled
python main.py --companies 0001568651 --extra-data
```

The `--extra-data` flag enables advanced pattern matching to find financial facts that might be missed in the primary extraction. See `EXTRA_DATA_DOCUMENTATION.md` for detailed information.

## Project Structure

```
├── sec_scraper_cli.py      # CLI entry point
├── financial_dashboard.py  # Streamlit web dashboard
├── tickers.json           # Company ticker database  
├── config/
│   ├── mongodb_config.py  # Basic MongoDB configuration
│   └── mongodb_dimensional_config.py # Advanced MongoDB with dimensions
├── sec_processing/
│   ├── sec_api_client.py  # SEC API client
│   └── financial_statement_processor.py # Financial data processing
├── data_access/
│   └── mongodb_repositories.py # Database operations
└── utils/
    ├── data_transformers.py # Data transformation
    ├── period_selectors.py  # Period selection logic
    ├── dimensional_filters.py # XBRL dimension filtering
    └── sec_data_extractors.py # SEC data extraction
```

## Testing

The project includes a unified test runner for easy testing:

```bash
# Run unit tests (fastest)
python run_tests.py unit

# Run all tests  
python run_tests.py all

# Run with coverage report
python run_tests.py coverage

# Debug mode with verbose output
python run_tests.py unit --debug -v

# Show all available options
python run_tests.py --help
```

### Test Suites

- **Unit Tests**: Fast, isolated tests (`python run_tests.py unit`)
- **Integration Tests**: Component interaction tests (`python run_tests.py integration`)
- **End-to-End Tests**: Full workflow tests (`python run_tests.py e2e`)
- **Combined**: Fast development cycle (`python run_tests.py fast`)

For detailed testing documentation, see [TEST_RUNNER_DOCS.md](TEST_RUNNER_DOCS.md).

## Database Configuration

The scraper uses MongoDB for data storage. Configure your database connection in `config/mongodb_config.py` or through environment variables:

```bash
export MONGODB_URI="mongodb://localhost:27017"
export MONGODB_DATABASE="sec_filings"
```

## Advanced Usage

### Year Range Processing

```bash
python sec_scraper_cli.py --year 2015  # Process filings from 2015 onwards
```

## CLI Reference

```
python sec_scraper_cli.py [OPTIONS]

Options:
  --tickers              Use ticker database (default)
  --limit INTEGER        Number of companies to process (with --tickers)
  --companies CIK...     Process specific company CIKs
  --file FILE           Process companies from file
  --year INTEGER         Process filings from year onwards
  --fiscal-year INTEGER  Process specific fiscal year
  --fiscal-quarter Q     Process specific fiscal quarter (Q1, Q2, Q3, Q4)
  --dimensions          Enable dimensional data processing
  --extra-data          Enable comprehensive missing facts detection
  --local               Process local XBRL files instead of downloading
  --reload              Force reload of existing data for specified period
  --debug               Enable debug logging
  --help                Show help message
```

### Reload Functionality

The `--reload` flag allows you to refresh existing data in the database:

```bash
# Reload all data for specific companies from 2020 onwards
python sec_scraper_cli.py --companies 0000320193 0000789019 --year 2020 --reload

# Reload specific fiscal year data
python sec_scraper_cli.py --tickers --limit 5 --fiscal-year 2023 --reload

# Reload specific fiscal quarter
python sec_scraper_cli.py --companies 0000320193 --fiscal-year 2023 --fiscal-quarter Q4 --reload
```

**How reload works:**

- **Company level**: Updates company information
- **Filing level**: Removes existing filings matching the period filter and re-downloads
- **Statement level**: Removes existing financial statements and re-extracts from XBRL
- **Period filtering**: Only reloads data matching `--fiscal-year` and `--fiscal-quarter` if specified

## Troubleshooting

1. **Database Connection**: Ensure MongoDB is running and accessible
2. **Rate Limiting**: Automatically handled, respects SEC API limits
3. **Interrupted Processing**: Simply re-run - the application will avoid reprocessing existing data

## Migration Notes

- **v9** uses `tickers.json` as the default company source
- Legacy `companies.txt` files can still be used with `--file`
- Entry point is `sec_scraper_cli.py`

## Development

Modern, modular architecture:

- **main.py**: CLI interface and user interaction
- **scraper.py**: Core scraping library and functions
- **services/**: External API integrations (SEC, etc.)
- **repositories/**: Database operations and data persistence
- **utils/**: Utility functions and data transformation
- **config/**: Configuration and database setup

getaddrinfo ENOTFOUND mongo error happens because your system or Node.js app cannot resolve the hostname mongo

sudo nano /etc/hosts

127.0.0.1   mongo
