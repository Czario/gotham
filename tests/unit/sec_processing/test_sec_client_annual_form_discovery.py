"""SEC submissions discovery tests for foreign annual-report candidates."""

from api.sec_client import SECAPIClient


def _client():
    client = SECAPIClient.__new__(SECAPIClient)
    client.database = None
    return client


def _submissions():
    recent = {
        "form": ["10-K", "10-Q", "20-F", "20-F/A", "6-K", "20-F"],
        "accessionNumber": ["k", "q", "f", "fa", "6k", "registration"],
        "filingDate": [
            "2026-02-01", "2026-02-02", "2026-02-03", "2026-02-04",
            "2026-02-05", "2026-02-06",
        ],
        "reportDate": [
            "2025-12-31", "2025-09-30", "2025-12-31", "2025-12-31",
            "2025-12-31", "",
        ],
        "acceptanceDateTime": [None] * 6,
        "primaryDocument": ["k.htm", "q.htm", "f.htm", "fa.htm", "6k.htm", "reg.htm"],
        "primaryDocDescription": ["10-K", "10-Q", "20-F", "20-F/A", "6-K", "20-F"],
        "fiscalYearEnd": ["1231"] * 6,
        "isXBRL": [1, 1, 1, 1, 0, 1],
    }
    return {"filings": {"recent": recent}}


def test_default_discovery_includes_20f_annual_report_candidates_and_amendments():
    filings = _client()._extract_filings(_submissions(), start_year=2025)

    assert [filing["form"] for filing in filings] == [
        "10-K", "10-Q", "20-F", "20-F/A"
    ]
    assert [filing["accessionNumber"] for filing in filings[-2:]] == ["f", "fa"]
    assert all(filing["reportDate"] == "2025-12-31" for filing in filings[-2:])


def test_discovery_excludes_20f_without_reporting_period_date():
    filings = _client()._extract_filings(_submissions(), start_year=2025)

    assert "registration" not in {filing["accessionNumber"] for filing in filings}


def test_custom_form_filter_remains_respected():
    filings = _client()._extract_filings(
        _submissions(), forms_filter=["20-F"], start_year=2025
    )

    assert [filing["accessionNumber"] for filing in filings] == ["f"]
