"""Regression tests for fiscal-year filing selection."""

from unittest.mock import MagicMock

import pytest

from sec_scraper_cli import (
    SECDataScraperApp,
    should_process_filing_for_download_period,
)


def _filings():
    return [
        {
            "form": "10-Q",
            "accessionNumber": "q1",
            "reportDate": "2023-03-31",
        },
        {
            "form": "10-Q",
            "accessionNumber": "q2",
            "reportDate": "2023-06-30",
        },
        {
            "form": "10-Q",
            "accessionNumber": "q3",
            "reportDate": "2023-09-30",
        },
        {
            "form": "10-K",
            "accessionNumber": "k",
            "reportDate": "2023-12-31",
        },
    ]


def _app_for_period(fiscal_quarter=None):
    # Avoid database/service initialization; this test exercises only the
    # filing-selection method.
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_fiscal_year = 2023
    app.target_fiscal_quarter = fiscal_quarter
    return app


def test_fiscal_year_only_selects_annual_filing():
    selected = _app_for_period()._filter_filings_by_fiscal_period(
        _filings(), {"fiscalYearEnd": "1231"}
    )

    assert [filing["form"] for filing in selected] == ["10-K"]
    assert [filing["accessionNumber"] for filing in selected] == ["k"]


@pytest.mark.parametrize("quarter, accession", [("Q1", "q1"), ("Q2", "q2"), ("Q3", "q3")])
def test_specific_fiscal_quarter_selects_quarterly_filing(quarter, accession):
    selected = _app_for_period(quarter)._filter_filings_by_fiscal_period(
        _filings(), {"fiscalYearEnd": "1231"}
    )

    assert [filing["accessionNumber"] for filing in selected] == [accession]


def test_without_fiscal_year_filter_keeps_all_filings():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_fiscal_year = None
    app.target_fiscal_quarter = None

    selected = app._filter_filings_by_fiscal_period(
        _filings(), {"fiscalYearEnd": "1231"}
    )

    assert len(selected) == 4


def test_q4_specific_selection_keeps_annual_filing():
    selected = _app_for_period("Q4")._filter_filings_by_fiscal_period(
        _filings(), {"fiscalYearEnd": "1231"}
    )

    assert [filing["accessionNumber"] for filing in selected] == ["k"]


def test_download_only_fiscal_year_selects_annual_filing():
    company = {"fiscalYearEnd": "1231"}

    assert should_process_filing_for_download_period(
        {"form_type": "10-K", "reportDate": "2023-12-31"},
        company,
        target_fiscal_year=2023,
    )
    assert not should_process_filing_for_download_period(
        {"form_type": "10-Q", "reportDate": "2023-09-30"},
        company,
        target_fiscal_year=2023,
    )


def test_latest_reload_filter_is_scoped_to_accession():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.latest = True
    app.target_fiscal_year = None
    app.target_fiscal_quarter = None

    reload_filter = app._build_reload_filter(
        "0000320193",
        {
            "form": "10-Q",
            "accessionNumber": "0000320193-23-000002",
            "reportDate": "2023-06-30",
        },
        "10-Q",
    )

    assert reload_filter == {
        "cik": "0000320193",
        "reporting_period.accession_number": "0000320193-23-000002",
        "form_type": "10-Q",
    }


def test_reload_deletes_after_extraction_and_before_normalization():
    events = []
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.reload = True
    app.current_company_data = {}
    app.current_company_doc = {}
    app.progress = MagicMock()
    app.financial_processor = MagicMock()
    app.financial_processor.process_filing.side_effect = lambda *args: (
        events.append("extract")
        or {"statements": {"income": {"hierarchy": []}}, "reporting_period": {}}
    )
    app._extract_line_items_from_hierarchy = MagicMock(return_value=[{"concept": "Revenue", "value": 1}])
    app._validate_statement_has_data = MagicMock(return_value=True)
    app._extract_primary_period_string = MagicMock(return_value=None)
    app.financial_transformer = MagicMock()
    app.financial_transformer.transform_statement_data.side_effect = lambda *args: (
        events.append("transform") or {"data": []}
    )
    app._delete_reload_data = MagicMock(side_effect=lambda *args: events.append("delete"))
    app._norm_service = MagicMock()
    app._norm_service.normalize_statement_in_memory.side_effect = lambda *args: events.append("save")
    app._accumulate_for_quarterly = MagicMock()
    app._log_period_information = MagicMock()

    result = app.process_financial_statements_enhanced(
        "0000320193",
        "0000320193-23-000001",
        "filing-id",
        {"form": "10-K", "reportDate": "2023-12-31"},
    )

    assert result == 1
    assert events == ["extract", "transform", "delete", "save"]
