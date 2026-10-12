"""Eligibility tests for foreign-issuer annual-report forms."""

import pytest

from utilities.sec_filing_classifier import is_annual_20f_filing


ASML_ANNUAL_COVER = """
<html><body>
  <ix:header><ix:hidden>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentType">20-F</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentAnnualReport"
      format="ixt:fixed-true">TRUE</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentRegistrationStatement"
      format="ixt:fixed-false">FALSE</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentTransitionReport"
      format="ixt:fixed-false">FALSE</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentShellCompanyReport"
      format="ixt:fixed-false">FALSE</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentPeriodEndDate">2025-12-31</ix:nonNumeric>
    <ix:nonNumeric contextRef="c-1" name="dei:DocumentFiscalPeriodFocus">FY</ix:nonNumeric>
  </ix:hidden></ix:header>
</body></html>
"""


def test_classifies_inline_xbrl_20f_annual_report():
    assert is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}, ASML_ANNUAL_COVER
    )


def test_classifies_inline_xbrl_20f_amendment_as_annual():
    amendment_cover = ASML_ANNUAL_COVER.replace(
        "<ix:nonNumeric contextRef=\"c-1\" name=\"dei:DocumentType\">20-F</ix:nonNumeric>",
        "<ix:nonNumeric contextRef=\"c-1\" name=\"dei:DocumentType\">20-F/A</ix:nonNumeric>",
    )
    assert is_annual_20f_filing(
        {"form": "20-F/A", "reportDate": "2025-12-31"}, amendment_cover
    )


@pytest.mark.parametrize("form", ["10-K", "10-Q", "6-K", "40-F", "S-1", ""])
def test_rejects_forms_outside_phase_one_scope(form):
    assert not is_annual_20f_filing(
        {"form": form, "reportDate": "2025-12-31"}, ASML_ANNUAL_COVER
    )


def test_rejects_20f_registration_statement():
    registration_cover = ASML_ANNUAL_COVER.replace(
        'name="dei:DocumentAnnualReport"\n      format="ixt:fixed-true">TRUE',
        'name="dei:DocumentAnnualReport"\n      format="ixt:fixed-false">FALSE',
    ).replace(
        'name="dei:DocumentRegistrationStatement"\n      format="ixt:fixed-false">FALSE',
        'name="dei:DocumentRegistrationStatement"\n      format="ixt:fixed-true">TRUE',
    )
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}, registration_cover
    )


def test_rejects_conflicting_period_end():
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"},
        ASML_ANNUAL_COVER.replace("2025-12-31", "2024-12-31"),
    )


def test_uses_inline_period_end_when_submissions_report_date_is_missing():
    assert is_annual_20f_filing(
        {"form": "20-F", "reportDate": ""}, ASML_ANNUAL_COVER
    )


@pytest.mark.parametrize("report_date", [None, "", "0000-00-00", "2025/12/31", "invalid"])
def test_requires_period_date_from_submission_or_inline_xbrl(report_date):
    cover_without_dei_period = """
    <html><body>
      <p>☒ ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)</p>
    </body></html>
    """
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": report_date}, cover_without_dei_period
    )


def test_requires_primary_cover_or_dei_evidence():
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}
    )
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"},
        "<html><body>20-F Annual Report FY2025</body></html>",
    )


def test_accepts_checked_annual_cover_without_inline_xbrl():
    cover = """
    <html><body>
      <p>☐ REGISTRATION STATEMENT PURSUANT TO SECTION 12(b) OR (g)</p>
      <p>☒ ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)</p>
      <p>For the fiscal year ended December 31, 2025</p>
    </body></html>
    """
    assert is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}, cover
    )


def test_rejects_unchecked_annual_cover():
    cover = """
    <html><body>
      <p>☐ ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)</p>
    </body></html>
    """
    assert not is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}, cover
    )


def test_accepts_checked_annual_cover_encoded_as_html_inputs():
    cover = """
    <html><body>
      <input type="checkbox"> REGISTRATION STATEMENT PURSUANT TO SECTION 12(b)
      <input type="checkbox" checked="checked">
      ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)
    </body></html>
    """
    assert is_annual_20f_filing(
        {"form": "20-F", "reportDate": "2025-12-31"}, cover
    )
