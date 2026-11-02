"""Unit tests for loading a specific statement for a specific quarter."""

from unittest.mock import MagicMock, patch
import pytest

from sec_scraper_cli import (
    SECDataScraperApp,
    canonicalize_statement_type,
    normalize_quarter,
    create_load_statement_parser,
    load_statement_cli,
)
from cli.main import create_parser as create_cli_main_parser


def test_canonicalize_statement_type_valid():
    assert canonicalize_statement_type("income") == "income"
    assert canonicalize_statement_type("income_statement") == "income"
    assert canonicalize_statement_type("income-statement") == "income"
    assert canonicalize_statement_type("is") == "income"

    assert canonicalize_statement_type("balance_sheet") == "balancesheet"
    assert canonicalize_statement_type("balancesheet") == "balancesheet"
    assert canonicalize_statement_type("balance-sheet") == "balancesheet"
    assert canonicalize_statement_type("bs") == "balancesheet"

    assert canonicalize_statement_type("cash_flow") == "cashflow"
    assert canonicalize_statement_type("cashflow") == "cashflow"
    assert canonicalize_statement_type("cash_flows") == "cashflow"
    assert canonicalize_statement_type("cf") == "cashflow"


def test_canonicalize_statement_type_invalid():
    with pytest.raises(ValueError, match="Invalid statement type"):
        canonicalize_statement_type("unsupported_stmt")


def test_normalize_quarter_valid():
    assert normalize_quarter("Q1") == "Q1"
    assert normalize_quarter("q1") == "Q1"
    assert normalize_quarter("1") == "Q1"
    assert normalize_quarter(1) == "Q1"

    assert normalize_quarter("Q2") == "Q2"
    assert normalize_quarter("q2") == "Q2"
    assert normalize_quarter("2") == "Q2"
    assert normalize_quarter(2) == "Q2"

    assert normalize_quarter("Q3") == "Q3"
    assert normalize_quarter("q3") == "Q3"
    assert normalize_quarter("3") == "Q3"
    assert normalize_quarter(3) == "Q3"

    assert normalize_quarter("Q4") == "Q4"
    assert normalize_quarter("q4") == "Q4"
    assert normalize_quarter("4") == "Q4"
    assert normalize_quarter(4) == "Q4"


def test_normalize_quarter_invalid():
    with pytest.raises(ValueError, match="Invalid fiscal quarter"):
        normalize_quarter("Q5")
    with pytest.raises(ValueError, match="Invalid fiscal quarter"):
        normalize_quarter("invalid")


def test_build_reload_filter_includes_target_statement():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.latest = False
    app.target_fiscal_year = 2023
    app.target_fiscal_quarter = "Q2"
    app.target_statement = "balancesheet"

    filing = {
        "form": "10-Q",
        "accessionNumber": "0000320193-23-000077",
        "reportDate": "2023-06-30",
    }
    reload_filter = app._build_reload_filter("0000320193", filing, "10-Q")

    assert reload_filter["cik"] == "0000320193"
    assert reload_filter["form_type"] == "10-Q"
    assert reload_filter["reporting_period.fiscal_year"] == 2023
    assert reload_filter["reporting_period.quarter"] == 2
    assert reload_filter["statement_type"] == "balancesheet"


def test_find_existing_filing_filters_by_statement():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_statement = "income"
    app.current_company_data = {"cik": "0000320193"}

    mock_annual_col = MagicMock()
    mock_quarterly_col = MagicMock()
    app._cv_annual_col = mock_annual_col
    app._cv_quarterly_col = mock_quarterly_col

    # Query with accession number
    filing = {
        "form": "10-Q",
        "accessionNumber": "0000320193-23-000077",
        "reportDate": "2023-06-30",
    }
    mock_annual_col.find_one.return_value = None
    mock_quarterly_col.find_one.return_value = {"_id": "found"}

    result = app._find_existing_filing("0000320193", filing)
    assert result == {"_id": "found"}

    # Verify statement_type was in the search filter
    called_filter = mock_annual_col.find_one.call_args[0][0]
    assert called_filter["statement_type"] == "income"
    assert called_filter["cik"] == "0000320193"


def test_delete_reload_data_with_statement_deletes_only_target_statement():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.reload = True
    app.latest = False
    app.target_fiscal_year = 2023
    app.target_fiscal_quarter = "Q2"
    app.target_statement = "cashflow"

    app._cv_annual_col = MagicMock()
    app._cv_quarterly_col = MagicMock()
    app._cv_annual_col.delete_many.return_value.deleted_count = 0
    app._cv_quarterly_col.delete_many.return_value.deleted_count = 5
    app.progress = MagicMock()

    filing_info = {
        "form": "10-Q",
        "accessionNumber": "0000320193-23-000077",
        "reportDate": "2023-06-30",
    }
    # When force=True, delete_many is executed
    app._delete_reload_data("0000320193", filing_info, force=True)

    expected_filter = {
        "cik": "0000320193",
        "reporting_period.fiscal_year": 2023,
        "reporting_period.quarter": 2,
        "form_type": "10-Q",
        "statement_type": "cashflow",
    }
    app._cv_annual_col.delete_many.assert_called_once_with(expected_filter)
    app._cv_quarterly_col.delete_many.assert_called_once_with(expected_filter)


def test_delete_reload_data_non_destructive_upsert():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.reload = True
    app.latest = False
    app.target_fiscal_year = 2023
    app.target_fiscal_quarter = "Q2"
    app.target_statement = "cashflow"

    app._cv_annual_col = MagicMock()
    app._cv_quarterly_col = MagicMock()
    app.progress = MagicMock()

    filing_info = {
        "form": "10-Q",
        "accessionNumber": "0000320193-23-000077",
        "reportDate": "2023-06-30",
    }
    # Standard reload (force=False) must NOT delete rows, but perform upsert
    app._delete_reload_data("0000320193", filing_info, force=False)

    app._cv_annual_col.delete_many.assert_not_called()
    app._cv_quarterly_col.delete_many.assert_not_called()
    app.progress.status.assert_called_once()
    assert "upserting existing rows" in app.progress.status.call_args[0][0]


def test_process_financial_statements_enhanced_filters_statement():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app._audit_checked = True
    app._presenter = None
    app.reload = False
    app.target_statement = "balancesheet"
    app.current_company_data = {}
    app.current_company_doc = {}
    app.progress = MagicMock()

    # XBRL data returns both income and balancesheet
    app.financial_processor = MagicMock()
    app.financial_processor.process_filing.return_value = {
        "statements": {
            "income": {"hierarchy": []},
            "balancesheet": {"hierarchy": []},
            "cashflow": {"hierarchy": []},
        },
        "reporting_period": {"fiscal_year": 2023, "quarter": 2},
    }
    app._extract_line_items_from_hierarchy = MagicMock(return_value=[{"concept": "Assets", "value": 100}])
    app._validate_statement_has_data = MagicMock(return_value=True)
    app._extract_primary_period_string = MagicMock(return_value=None)
    app.financial_transformer = MagicMock()
    app.financial_transformer.transform_statement_data.return_value = {"statement_type": "balancesheet", "data": []}

    app.sec_client = MagicMock()
    app.sec_client.get_ticker_from_cik.return_value = "TEST"

    captured_statements = []
    fake_graph = MagicMock()
    def _invoke(state):
        captured_statements.extend([doc.get("statement_type") for doc in state.get("statement_docs", [])])
        return {"status": "saved", "persist_receipt": {"statements_written": len(captured_statements)}}

    fake_graph.invoke.side_effect = _invoke
    app._build_filing_agent_graph = MagicMock(return_value=fake_graph)
    app._accumulate_for_quarterly = MagicMock()
    app._log_period_information = MagicMock()

    written = app.process_financial_statements_enhanced(
        "0000320193",
        "0000320193-23-000077",
        "filing-id",
        {"form": "10-Q", "reportDate": "2023-06-30"},
    )

    assert written == 1
    # Only balancesheet was processed and passed to the agent
    assert captured_statements == ["balancesheet"]
    assert app.financial_transformer.transform_statement_data.call_count == 1
    call_args = app.financial_transformer.transform_statement_data.call_args[0]
    assert call_args[3] == "balancesheet"


def test_load_statement_parser():
    parser = create_load_statement_parser()
    args = parser.parse_args([
        "--companies", "AAPL",
        "--fiscal-year", "2023",
        "--quarter", "Q2",
        "--statement", "balance_sheet",
        "--reload",
    ])
    assert args.companies == ["AAPL"]
    assert args.fiscal_year == 2023
    assert args.quarter == "Q2"
    assert args.statement == "balance_sheet"
    assert args.reload is True


def test_load_statement_cli_delegates_to_main():
    with patch("sec_scraper_cli.main") as mock_main:
        mock_main.return_value = 0
        ret = load_statement_cli([
            "--company", "AAPL",
            "--year", "2023",
            "--quarter", "Q3",
            "--statement", "income",
            "--reload",
        ])
        assert ret == 0
        mock_main.assert_called_once_with([
            "--companies", "AAPL",
            "--fiscal-year", "2023",
            "--fiscal-quarter", "Q3",
            "--statement", "income",
            "--reload",
        ])


def test_cli_main_statement_command():
    parser = create_cli_main_parser()
    args = parser.parse_args([
        "statement",
        "--ticker", "MSFT",
        "--fiscal-year", "2024",
        "--quarter", "Q1",
        "--statement", "cash_flow",
    ])
    assert args.command == "statement"
    assert args.ticker == "MSFT"
    assert args.fiscal_year == 2024
    assert args.quarter == "Q1"
    assert args.statement == "cash_flow"


def test_download_html_filing_saves_both_htm_and_html_gz(tmp_path):
    import gzip
    from api.sec_client import SECAPIClient

    client = SECAPIClient.__new__(SECAPIClient)
    client.url_detector = MagicMock()
    client.url_detector.detect_filing_urls.return_value = {
        "html_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230701.htm"
    }
    client.url_detector.download_file.return_value = (True, "<html><body>Financial Report</body></html>", None)

    cik = "0000320193"
    accession = "0000320193-23-000077"
    filing_date = "2023-08-04"

    success = client.download_html_filing(cik, accession, filing_date, str(tmp_path), ticker="AAPL")
    assert success is True

    ticker_folder = tmp_path / "AAPL"
    htm_file = ticker_folder / f"{accession}.htm"
    gz_file = ticker_folder / f"{accession}.html.gz"
    doc_file = ticker_folder / "aapl-20230701.htm"

    assert htm_file.exists()
    assert gz_file.exists()
    assert doc_file.exists()

    # Check contents
    assert htm_file.read_text(encoding="utf-8") == "<html><body>Financial Report</body></html>"
    with gzip.open(gz_file, "rt", encoding="utf-8") as f:
        assert f.read() == "<html><body>Financial Report</body></html>"


def test_download_html_filing_extracts_missing_htm_from_gz_cache(tmp_path):
    import gzip
    from api.sec_client import SECAPIClient

    client = SECAPIClient.__new__(SECAPIClient)
    client.url_detector = MagicMock()

    cik = "0000320193"
    accession = "0000320193-23-000077"
    filing_date = "2023-08-04"

    ticker_folder = tmp_path / "AAPL"
    ticker_folder.mkdir(parents=True)
    gz_file = ticker_folder / f"{accession}.html.gz"
    with gzip.open(gz_file, "wt", encoding="utf-8") as f:
        f.write("<html>Cached Filing</html>")

    htm_file = ticker_folder / f"{accession}.htm"
    assert not htm_file.exists()

    success = client.download_html_filing(cik, accession, filing_date, str(tmp_path), ticker="AAPL", reload=False)
    assert success is True
    # Does not call network download because it extracts from cache
    client.url_detector.detect_filing_urls.assert_not_called()
    assert htm_file.exists()
    assert htm_file.read_text(encoding="utf-8") == "<html>Cached Filing</html>"


def test_download_html_filing_reload_overwrites(tmp_path):
    from api.sec_client import SECAPIClient

    client = SECAPIClient.__new__(SECAPIClient)
    client.url_detector = MagicMock()
    client.url_detector.detect_filing_urls.return_value = {
        "html_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230701.htm"
    }
    client.url_detector.download_file.return_value = (True, "<html>Updated Filing</html>", None)

    cik = "0000320193"
    accession = "0000320193-23-000077"
    filing_date = "2023-08-04"

    # Pre-create old files
    ticker_folder = tmp_path / "AAPL"
    ticker_folder.mkdir(parents=True)
    (ticker_folder / f"{accession}.htm").write_text("Old", encoding="utf-8")

    success = client.download_html_filing(cik, accession, filing_date, str(tmp_path), ticker="AAPL", reload=True)
    assert success is True
    client.url_detector.detect_filing_urls.assert_called_once()
    assert (ticker_folder / f"{accession}.htm").read_text(encoding="utf-8") == "<html>Updated Filing</html>"
