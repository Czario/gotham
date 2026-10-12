"""Opt-in live SEC integration tests for foreign 20-F XBRL extraction.

Run explicitly with:
    RUN_SEC_LIVE_INTEGRATION=1 uv run --project gotham --group dev \
      python -m pytest -q gotham/tests/integration/test_foreign_annual_xbrl_live.py

These tests read immutable public SEC filings and do not connect to MongoDB.
"""

import os

import pytest

from core.extractors.xbrl_parser import FlexibleXBRLExtractor
from core.extractors.sec_data_extractors import extract_period_info_from_sec_api
from utilities.helpers.period_utils import is_annual_form


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SEC_LIVE_INTEGRATION") != "1",
    reason="set RUN_SEC_LIVE_INTEGRATION=1 to fetch real SEC filings",
)


@pytest.mark.parametrize(
    "cik, accession_path, index_filename, filing_date, fiscal_year, expected_taxonomy",
    [
        (
            "937966",
            "000162828026011378",
            "0001628280-26-011378-index.html",
            "2026-02-25",
            2025,
            "us-gaap",
        ),
        (
            "353278",
            "000035327826000012",
            "0000353278-26-000012-index.html",
            "2026-02-04",
            2025,
            "ifrs-full",
        ),
    ],
    ids=["ASML-US-GAAP-EUR", "Novo-Nordisk-IFRS-DKK"],
)
def test_foreign_20f_xbrl_extraction(cik, accession_path, index_filename,
                                    filing_date, fiscal_year, expected_taxonomy):
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession_path}/{index_filename}"
    )
    with FlexibleXBRLExtractor(
        enable_enhanced_dimensions=False,
        company_info={
            "cik": cik,
            "fiscal_year": fiscal_year,
            "fiscal_year_end_code": "1231",
        },
        filing_form_type="20-F",
        filing_date=filing_date,
    ) as extractor:
        result = extractor.extract_financial_statements(filing_url)

    assert result
    assert not result.get("no_xbrl_available")
    assert {"income_statement", "balance_sheet", "cash_flow"} <= set(result["statements"])
    assert expected_taxonomy in extractor.discovered_taxonomies
    primary_period = result["filing_info"]["primary_period_info"]
    assert primary_period["fiscal_year"] == fiscal_year
    assert primary_period["fiscal_period"] == "FY"
    normalized_period = extract_period_info_from_sec_api(
        {
            "form": "20-F",
            "reportDate": f"{fiscal_year}-12-31",
            "filingDate": filing_date,
        },
        {"cik": cik, "fiscalYearEnd": "1231"},
    )
    assert normalized_period["period_type"] == "annual"
    assert "quarter" not in normalized_period
