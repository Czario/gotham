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


def _company(cik="0000320193"):
    """Company payload carrying a CIK (the fiscal-year-end lookup key)."""
    return {"cik": cik}


def _app_for_period(fiscal_quarter=None, fiscal_year_end="1231", in_db=True):
    """App whose authoritative fiscal-year-end comes from the DB.

    ``_get_authoritative_fiscal_year_end`` deliberately reads
    ``companies.corporate_info.fiscal_year_end`` and ignores SEC
    ``fiscalYearEnd`` metadata (unreliable for non-calendar filers), so the
    tests provide a fake DB rather than SEC metadata.
    """
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_fiscal_year = 2023
    app.target_fiscal_quarter = fiscal_quarter
    app.current_company_data = _company()
    app.db = MagicMock()
    app.db.companies.find_one.return_value = (
        {"corporate_info": {"fiscal_year_end": fiscal_year_end}} if in_db else None
    )
    app._fye_cache = {}
    return app


def test_fiscal_year_only_selects_annual_filing():
    selected = _app_for_period()._filter_filings_by_fiscal_period(
        _filings(), _company()
    )

    assert [filing["form"] for filing in selected] == ["10-K"]
    assert [filing["accessionNumber"] for filing in selected] == ["k"]


def test_filing_period_label_uses_company_fiscal_year():
    # September fiscal year end: a March 31 report date is fiscal Q2.
    app = _app_for_period(fiscal_year_end="0930")

    assert app._format_filing_period(
        {"form": "10-Q", "reportDate": "2023-03-31"}, _company()
    ) == "FY2023 Q2"
    assert app._format_filing_period(
        {"form": "10-K", "reportDate": "2023-09-30"}, _company()
    ) == "FY2023 Annual"


@pytest.mark.parametrize("quarter, accession", [("Q1", "q1"), ("Q2", "q2"), ("Q3", "q3")])
def test_specific_fiscal_quarter_selects_quarterly_filing(quarter, accession):
    selected = _app_for_period(quarter)._filter_filings_by_fiscal_period(
        _filings(), _company()
    )

    assert [filing["accessionNumber"] for filing in selected] == [accession]


def test_without_fiscal_year_filter_keeps_all_filings():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.target_fiscal_year = None
    app.target_fiscal_quarter = None

    selected = app._filter_filings_by_fiscal_period(_filings(), _company())

    assert len(selected) == 4


def test_q4_specific_selection_keeps_annual_filing():
    selected = _app_for_period("Q4")._filter_filings_by_fiscal_period(
        _filings(), _company()
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


def test_existing_filing_falls_back_to_fiscal_period_when_accession_is_missing():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.current_company_data = _company()
    app.db = MagicMock()
    app.db.companies.find_one.return_value = {
        "corporate_info": {"fiscal_year_end": "0930"}
    }
    app._fye_cache = {}
    app._cv_annual_col = MagicMock()
    app._cv_quarterly_col = MagicMock()
    app._cv_annual_col.find_one.side_effect = [None, None]
    app._cv_quarterly_col.find_one.side_effect = [None, {"_id": "existing"}]

    existing = app._find_existing_filing(
        "0000320193",
        {
            "form": "10-Q",
            "accessionNumber": "0000320193-23-000002",
            "reportDate": "2023-03-31",
        },
    )

    assert existing == {"_id": "existing"}
    period_query = app._cv_quarterly_col.find_one.call_args_list[1].args[0]
    assert period_query["cik"] == "0000320193"
    assert period_query["form_type"] == "10-Q"
    assert period_query["reporting_period.fiscal_year"] == 2023
    assert period_query["reporting_period.quarter"] == 2


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
        "$or": [
            {"accession_number": "0000320193-23-000002"},
            {"reporting_period.accession_number": "0000320193-23-000002"},
        ],
        "form_type": "10-Q",
    }


def test_reload_delete_is_deferred_to_the_agent_write():
    """The --reload delete must NOT happen before the agent runs.

    It is handed to the graph as a pre-write hook so an agent decision to skip
    the filing can never delete existing rows (see filings_agent persist node).
    """
    events = []
    captured = {}
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app._audit_checked = True     # narration disabled for this unit test
    app._presenter = None
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
    app.sec_client = MagicMock()
    app.sec_client.get_ticker_from_cik.return_value = "TEST"
    app._build_filing_agent_graph = MagicMock()
    fake_graph = MagicMock()
    def _invoke(state):
        events.append("agent")
        captured["hook"] = state.get("pre_write_hook")
        return {"status": "saved", "persist_receipt": {"statements_written": 1}}

    fake_graph.invoke.side_effect = _invoke
    app._build_filing_agent_graph.return_value = fake_graph
    app._accumulate_for_quarterly = MagicMock()
    app._log_period_information = MagicMock()

    result = app.process_financial_statements_enhanced(
        "0000320193",
        "0000320193-23-000001",
        "filing-id",
        {"form": "10-K", "reportDate": "2023-12-31"},
    )

    assert result == 1
    # No delete before the agent runs...
    assert events == ["extract", "transform", "agent"]
    assert "delete" not in events
    # ...but the deferred delete is wired into the graph state for the write.
    assert callable(captured["hook"])
    captured["hook"]()
    assert events[-1] == "delete"


# ── the fiscal-year-end source is deliberate (regression guards) ─────────────


def test_sec_fiscal_year_end_metadata_is_ignored():
    """SEC ``fiscalYearEnd`` is unreliable for non-calendar filers.

    The documented example: SEC metadata says 1231 for Dell while its fiscal
    year actually ends the Friday nearest January 31.  Only the DB's
    ``companies.corporate_info.fiscal_year_end`` is authoritative, so a company
    whose DB record says 0131 must not be filtered as if it were a calendar
    filer.
    """
    app = _app_for_period(fiscal_year_end="0131")
    # SEC metadata claims a December year end; the DB says late January.
    company = {"cik": "0000320193", "fiscalYearEnd": "1231"}

    # With a 0131 fiscal year, the Dec-2023 10-K belongs to FY2024, so a
    # FY2023 request must not select it.
    selected = app._filter_filings_by_fiscal_period(_filings(), company)
    assert [f["accessionNumber"] for f in selected] == []


def test_missing_db_company_skips_period_filtering_rather_than_guessing():
    """No DB record -> no filtering, and every filing is returned.

    Deliberate: the pipeline refuses to infer a fiscal year end.  This means
    ``--fiscal-year`` only narrows once the company exists in ``companies``
    (i.e. from the second run onwards).
    """
    app = _app_for_period(in_db=False)
    selected = app._filter_filings_by_fiscal_period(_filings(), _company())
    assert len(selected) == 4
    assert app._format_filing_period(
        {"form": "10-Q", "reportDate": "2023-03-31"}, _company()
    ) == "FY?"


# ── --reload delete scope must match the write identity ─────────────────────


def test_latest_reload_filter_is_period_scoped_when_period_is_known():
    """Values are unique per (cik, concept_id, fiscal_year[, quarter]), so a
    --latest --reload must delete by that period identity.  Deleting by
    accession alone left same-period rows from another accession behind."""
    # Apple's fiscal year ends late September, so its FY2026 Q3 ends 2026-06-27
    # (the exact filing that failed in production).
    app = _app_for_period(fiscal_year_end="0927")
    app.latest = True

    reload_filter = app._build_reload_filter(
        "0000320193",
        {"form": "10-Q", "accessionNumber": "0000320193-26-000020",
         "reportDate": "2026-06-27"},
        "10-Q",
    )

    assert reload_filter["cik"] == "0000320193"
    assert reload_filter["form_type"] == "10-Q"
    assert reload_filter["reporting_period.fiscal_year"] == 2026
    assert reload_filter["reporting_period.quarter"] == 3
    assert "$or" not in reload_filter  # not accession-scoped


def test_annual_reload_filter_has_no_quarter():
    """10-K values are stored without a quarter, matching the annual index."""
    app = _app_for_period(fiscal_year_end="1231")
    app.latest = True

    reload_filter = app._build_reload_filter(
        "0000320193",
        {"form": "10-K", "accessionNumber": "acc", "reportDate": "2026-12-31"},
        "10-K",
    )

    assert reload_filter["reporting_period.fiscal_year"] == 2026
    assert "reporting_period.quarter" not in reload_filter


def test_latest_reload_falls_back_to_accession_without_a_fiscal_year_end():
    """No DB fiscal-year-end (first run) -> keep the accession-scoped delete."""
    app = _app_for_period(in_db=False)
    app.latest = True

    reload_filter = app._build_reload_filter(
        "0000320193",
        {"form": "10-Q", "accessionNumber": "0000320193-26-000020",
         "reportDate": "2026-06-27"},
        "10-Q",
    )

    assert reload_filter["$or"] == [
        {"accession_number": "0000320193-26-000020"},
        {"reporting_period.accession_number": "0000320193-26-000020"},
    ]
