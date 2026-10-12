"""Annual period semantics for foreign Form 20-F reports."""

from datetime import datetime

import pytest

from core.extractors.sec_data_extractors import extract_period_info_from_sec_api
from utilities.helpers.period_utils import (
    FiscalYearCalculator,
    PeriodMatcher,
    is_annual_form,
)
from utilities.helpers.period_selectors import (
    select_primary_period_for_filing,
    validate_period_selection,
)


@pytest.mark.parametrize("form", ["20-F", "20-F/A"])
def test_foreign_annual_forms_have_annual_period_and_no_quarter(form):
    period = extract_period_info_from_sec_api(
        {"form": form, "reportDate": "2025-12-31", "filingDate": "2026-02-25"},
        {"cik": "0000937966", "name": "ASML HOLDING NV", "fiscalYearEnd": "1231"},
    )

    assert period["period_type"] == "annual"
    assert period["fiscal_year"] == 2025
    assert "quarter" not in period
    assert period["form_type"] == form


@pytest.mark.parametrize("form", ["20-F", "20-F/A"])
def test_foreign_annual_forms_use_twelve_month_duration(form):
    assert PeriodMatcher.get_target_duration_for_form(form) == 12
    assert is_annual_form(form)
    assert FiscalYearCalculator.determine_quarter_from_form_and_date(
        form, datetime(2025, 12, 31), "1231"
    ) is None


@pytest.mark.parametrize("form", ["20-F", "20-F/A"])
def test_foreign_annual_forms_prefer_and_validate_annual_statement_periods(form):
    periods = ["Three Months Ended December 31, 2025", "Year Ended December 31, 2025"]
    assert select_primary_period_for_filing(periods, form) == periods[1]
    assert validate_period_selection(periods[1], form)[0]
    assert not validate_period_selection(periods[0], form)[0]


def test_domestic_10k_still_uses_existing_annual_q4_convention():
    assert PeriodMatcher.get_target_duration_for_form("10-K") == 12
    assert FiscalYearCalculator.determine_quarter_from_form_and_date(
        "10-K", datetime(2025, 12, 31), "1231"
    ) == 4
