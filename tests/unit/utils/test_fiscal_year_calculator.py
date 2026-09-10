"""Unit tests for FiscalYearCalculator fiscal year / quarter determination.

Covers:
  * Standard fixed-date fiscal year ends (Dec 31, June 30) - end-year convention.
  * Early-year filers using END-year naming (Walmart-style).
  * Early-year filers using START-year naming (Chewy-style), incl. the specific
    regression where a mid-year quarter was mislabeled with the wrong fiscal year.
  * 52/53-week (weekday-closest) fiscal year ends.
  * Fiscal year naming-convention detection from company anchors.
"""
from datetime import datetime, timedelta
import pytest

from utilities.helpers.period_utils import FiscalYearCalculator as FYC


def calc(date_str, code, weekday=None, convention="end"):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return FYC.calculate_fiscal_year_and_quarter(d, code, weekday, convention)


# --- End-year convention (default) - preserves historical behavior ---
@pytest.mark.parametrize("date_str,code,exp_fy,exp_q", [
    ("2024-03-31", "1231", 2024, 1),  # calendar year
    ("2024-12-31", "1231", 2024, 4),  # calendar year
    ("2024-08-03", "0630", 2025, 1),  # Jun 30 filer: Aug 2024 -> FY2025 Q1
    ("2025-08-03", "0630", 2026, 1),  # Jun 30 filer: Aug 2025 -> FY2026 Q1
    ("2023-12-31", "0630", 2024, 2),  # Jun 30 filer: Dec 2023 -> FY2024 Q2
    ("2024-05-04", "0131", 2025, 1),  # Walmart-style end-year: May 2024 -> FY2025 Q1
    ("2024-11-02", "0131", 2025, 3),  # Walmart-style end-year: Nov 2024 -> FY2025 Q3
])
def test_end_year_convention(date_str, code, exp_fy, exp_q):
    assert calc(date_str, code) == (exp_fy, exp_q)


# --- Start-year convention (Chewy-style) ---
@pytest.mark.parametrize("date_str,exp_fy,exp_q", [
    ("2025-05-04", 2025, 1),  # Chewy FY2025 Q1
    ("2025-08-03", 2025, 2),  # Chewy FY2025 Q2  <-- regression fix
    ("2025-11-02", 2025, 3),  # Chewy FY2025 Q3
    ("2026-05-03", 2026, 1),  # Chewy FY2026 Q1
])
def test_start_year_convention_chewy(date_str, exp_fy, exp_q):
    assert calc(date_str, "0201", convention="start") == (exp_fy, exp_q)


# --- Uneven fiscal quarters (16/12/12/12-week retailers) ---
@pytest.mark.parametrize("date_str,exp_fy,exp_q", [
    # Kroger's first quarter is 16 weeks, so it ends in late May instead of
    # late April.  The quarter grid is still 3-month based, but the real Q1
    # end is closest to the Q1 grid end, so it must be labelled Q1.
    ("2022-05-21", 2023, 1),
    ("2022-08-13", 2023, 2),
    ("2022-11-05", 2023, 3),
    ("2023-05-20", 2024, 1),
    ("2023-08-12", 2024, 2),
    ("2023-11-04", 2024, 3),
    ("2024-05-25", 2025, 1),
    ("2024-08-17", 2025, 2),
    ("2024-11-09", 2025, 3),
    ("2025-05-24", 2026, 1),
    ("2025-08-16", 2026, 2),
    ("2025-11-08", 2026, 3),
    ("2026-05-23", 2027, 1),
])
def test_kroger_16_week_first_quarter(date_str, exp_fy, exp_q):
    # Kroger ends its fiscal year on the Saturday closest to Jan 31 and never
    # files a 10-Q for Q4 (that is the 10-K), so only Q1-Q3 10-Q dates are used.
    assert calc(date_str, "0131") == (exp_fy, exp_q)
    # Q4 is still the annual period (late Jan / early Feb).
    assert calc("2025-02-01", "0131") == (2025, 4)


# --- 52/53-week weekday-closest fiscal year end ---
def test_52_53_week_sunday_closest_jan31():
    # Chewy ends fiscal year on the Sunday closest to Jan 31.
    assert calc("2025-08-03", "0131", weekday="sunday", convention="start") == (2025, 2)
    assert calc("2025-11-02", "0131", weekday="sunday", convention="start") == (2025, 3)


# --- Fiscal year naming-convention detection ---
def test_detect_start_convention():
    anchors = [
        ("2025-05-04", 2025), ("2025-11-02", 2025), ("2026-05-03", 2026),
        ("2024-05-04", 2024),
    ]
    assert FYC.determine_fiscal_year_convention(anchors, "0201") == "start"


def test_detect_end_convention():
    anchors = [
        ("2024-05-04", 2025), ("2024-11-02", 2025), ("2023-05-04", 2024),
    ]
    assert FYC.determine_fiscal_year_convention(anchors, "0131") == "end"


def test_calendar_year_convention_ambiguous():
    anchors = [("2024-03-31", 2024), ("2024-12-31", 2024)]
    assert FYC.determine_fiscal_year_convention(anchors, "1231") is None


def test_quarter_boundaries_chewy_start():
    # FY2025 (Chewy) Q2 should end around early Aug 2025.
    q_start, q_end = FYC.calculate_fiscal_quarter_boundaries(
        2025, 2, "0201", None, "start"
    )
    assert q_start <= datetime(2025, 8, 3) <= q_end + timedelta(days=7)
