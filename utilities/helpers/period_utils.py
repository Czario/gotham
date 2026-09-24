#!/usr/bin/env python3
"""
Centralized period utilities to eliminate duplication across the codebase
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dateutil.relativedelta import relativedelta
import re

# Import existing normalize_period_type function to avoid duplication
from .period_normalizer import normalize_period_type


class PeriodConfig:
    """Configuration constants for period processing"""
    
    # Filing type to target duration mapping
    FILING_TYPE_DURATIONS = {
        '10-Q': 3,   # Quarterly
        '10-K': 12,  # Annual
    }
    
    # Period type indicators
    QUARTERLY_INDICATORS = [
        'three months', '3 months', 'three-months',
        'quarter', 'quarterly', 'q1', 'q2', 'q3', 'q4'
    ]
    
    CUMULATIVE_INDICATORS = [
        'six months', '6 months', 'six-months',
        'nine months', '9 months', 'nine-months', 
        'twelve months', '12 months', 'twelve-months',
        'year to date', 'ytd', 'cumulative'
    ]
    
    ANNUAL_INDICATORS = [
        'twelve months', '12 months', 'twelve-months',
        'year ended', 'annual', 'yearly'
    ]
    
    # Tolerance settings - STRICT MODE: Only exact 3-month periods allowed
    DEFAULT_DURATION_TOLERANCE = 0.1  # Very strict tolerance (0.1 months = ~3 days)
    # Tolerance for a "quarterly" duration.  A 13-week quarter is ~2.99 months
    # and a 14-week quarter (Apple and most retailers) is ~3.22 months, so a
    # day-tight tolerance (0.05) rejected every 14-week quarter.  That made
    # ``_select_best_primary_fact`` fall back to an arbitrary fact — e.g. the
    # dimensional "Products" revenue (96.4B) instead of total net sales
    # (117.2B).  0.35 months (~10.6 days) accepts 14-week quarters while still
    # rejecting cumulative 6/9/12-month periods.
    QUARTERLY_STRICT_TOLERANCE = 0.35
    ANNUAL_DURATION_TOLERANCE = 1.0
    DATE_TOLERANCE_DAYS = 7
    FISCAL_YEAR_TOLERANCE_DAYS = 7
    
    # Strict quarterly enforcement
    STRICT_QUARTERLY_MODE = True  # Reject ALL non-quarterly periods for quarterly filings


class PeriodParser:
    """Centralized period parsing utilities"""
    
    @staticmethod
    def extract_start_date_from_period_string(period_string: str) -> Optional[datetime]:
        """Extract start date from period string format 'YYYY-MM-DD HH:MM:SS to YYYY-MM-DD HH:MM:SS'"""
        if not period_string or ' to ' not in period_string:
            return None
        
        try:
            start_part = period_string.split(' to ')[0].strip()
            # Handle both date-only and datetime formats
            if ' ' in start_part:
                date_part = start_part.split(' ')[0]
            else:
                date_part = start_part
            
            return datetime.strptime(date_part, '%Y-%m-%d')
        except (ValueError, IndexError):
            return None

    @staticmethod
    def extract_end_date_from_period_string(period_string: str) -> Optional[datetime]:
        """Extract end date from period string format 'YYYY-MM-DD HH:MM:SS to YYYY-MM-DD HH:MM:SS'"""
        if not period_string:
            return None
        
        try:
            if ' to ' in period_string:
                end_part = period_string.split(' to ')[1].strip()
            else:
                end_part = period_string.strip()
            
            # Handle both date-only and datetime formats
            if ' ' in end_part:
                date_part = end_part.split(' ')[0]
            else:
                date_part = end_part
            
            return datetime.strptime(date_part, '%Y-%m-%d')
        except (ValueError, IndexError):
            return None

    @staticmethod
    def extract_date_from_period_name(period_name: str) -> Optional[datetime]:
        """Extract date from period column name like '2025-03-29 (Q1)' or '2025-03-29'"""
        if not period_name:
            return None
        
        # Try to extract YYYY-MM-DD pattern
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', period_name)
        if date_match:
            try:
                return datetime.strptime(date_match.group(1), '%Y-%m-%d')
            except ValueError:
                pass
        
        return None

    @staticmethod
    def calculate_duration_months(start_date: datetime, end_date: datetime) -> float:
        """Calculate duration in months between two dates"""
        duration_days = (end_date - start_date).days
        return duration_days / 30.4  # Approximate month length


class PeriodClassifier:
    """Utilities for classifying period types"""
    
    @staticmethod
    def is_quarterly_period(period_str: str) -> bool:
        """Check if a period represents a quarterly period (3 months)"""
        if not period_str:
            return False
        
        period_lower = period_str.lower()
        return any(indicator in period_lower for indicator in PeriodConfig.QUARTERLY_INDICATORS)

    @staticmethod
    def is_cumulative_period(period_str: str) -> bool:
        """Check if a period represents a cumulative period (6, 9, or 12 months)"""
        if not period_str:
            return False
        
        period_lower = period_str.lower()
        return any(indicator in period_lower for indicator in PeriodConfig.CUMULATIVE_INDICATORS)

    @staticmethod
    def is_annual_period(period_str: str) -> bool:
        """Check if a period represents an annual period (12 months/year)"""
        if not period_str:
            return False
        
        period_lower = period_str.lower()
        return any(indicator in period_lower for indicator in PeriodConfig.ANNUAL_INDICATORS)

    @staticmethod
    def get_cumulative_penalty(period_str: str) -> int:
        """Get penalty score for cumulative periods based on duration"""
        if not period_str:
            return 50
        
        period_lower = period_str.lower()
        
        if 'six months' in period_lower or '6 months' in period_lower:
            return 75  # 6 months is moderately bad for quarterly
        elif 'nine months' in period_lower or '9 months' in period_lower:
            return 100  # 9 months is worse
        elif 'twelve months' in period_lower or '12 months' in period_lower or 'year' in period_lower:
            return 150  # 12 months is worst for quarterly
        
        return 50  # Default penalty for other cumulative periods

    @staticmethod
    def get_period_type_description(period_name: str) -> str:
        """Get a human-readable description of what period type a column represents"""
        if not period_name:
            return "Unknown period type"
        
        if PeriodClassifier.is_quarterly_period(period_name):
            return "3-month quarterly period ✅"
        elif 'six months' in period_name.lower() or '6 months' in period_name.lower():
            return "6-month cumulative period ⚠️"
        elif 'nine months' in period_name.lower() or '9 months' in period_name.lower():
            return "9-month cumulative period ⚠️"
        elif PeriodClassifier.is_annual_period(period_name):
            return "12-month annual period"
        
        # Try to extract date for period identification
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', period_name)
        if date_match:
            return f"Period ending {date_match.group(1)}"
        
        return "Unknown period type"


class PeriodMatcher:
    """Utilities for matching periods against criteria"""
    
    @staticmethod
    def get_target_duration_for_form(form_type: Optional[str]) -> int:
        """Get the expected duration in months for a filing type"""
        if not form_type:
            return 3  # Default to quarterly
        return PeriodConfig.FILING_TYPE_DURATIONS.get(form_type, 3)

    @staticmethod
    def get_duration_tolerance_for_form(form_type: Optional[str]) -> float:
        """Get the duration tolerance for a filing type - STRICT MODE"""
        if form_type == '10-K':
            return PeriodConfig.ANNUAL_DURATION_TOLERANCE
        elif form_type == '10-Q':
            # Ultra strict for quarterly filings - must be exactly 3 months
            return PeriodConfig.QUARTERLY_STRICT_TOLERANCE
        return PeriodConfig.QUARTERLY_STRICT_TOLERANCE  # Default to strict quarterly

    @staticmethod
    def is_period_match(period_start: datetime, period_end: datetime, target_end: datetime, 
                       actual_duration_months: float, target_duration_months: int, 
                       form_type: Optional[str] = None) -> bool:
        """Check if a period matches target criteria - STRICT MODE"""
        
        # Must end on or very close to the target date
        end_date_diff = abs((period_end - target_end).days)
        if end_date_diff > PeriodConfig.DATE_TOLERANCE_DAYS:
            return False
        
        # STRICT ENFORCEMENT: For quarterly filings, absolutely no non-quarterly periods
        if form_type == '10-Q' or target_duration_months == 3:
            # Ultra strict tolerance for quarterly periods
            duration_diff = abs(actual_duration_months - 3.0)
            if duration_diff > PeriodConfig.QUARTERLY_STRICT_TOLERANCE:
                return False
        else:
            # Duration must be close to target for other types
            duration_diff = abs(actual_duration_months - target_duration_months)
            tolerance = PeriodMatcher.get_duration_tolerance_for_form(form_type)
            if duration_diff > tolerance:
                return False
        
        return True

    @staticmethod
    def is_strictly_quarterly_period(duration_months: float) -> bool:
        """Check if a period is strictly a quarterly period (exactly ~3 months)"""
        return abs(duration_months - 3.0) <= PeriodConfig.QUARTERLY_STRICT_TOLERANCE

    @staticmethod
    def reject_non_quarterly_period(duration_months: float, form_type: Optional[str] = None) -> bool:
        """Determine if a period should be rejected for not being quarterly"""
        if form_type == '10-Q':
            # Absolutely reject anything that's not exactly quarterly
            return not PeriodMatcher.is_strictly_quarterly_period(duration_months)
        return False  # Don't reject for other form types

    @staticmethod
    def log_strict_rejection(duration_months: float, period_str: str, form_type: str, reason: str = ""):
        """Log detailed information about strict period rejections for debugging"""
        tolerance = PeriodConfig.QUARTERLY_STRICT_TOLERANCE
        print(f"🚫 STRICT REJECTION DETAILS:")
        print(f"   Period: {period_str}")
        print(f"   Duration: {duration_months:.3f} months")
        print(f"   Filing type: {form_type}")
        print(f"   Required: 3.000 ± {tolerance:.3f} months")
        print(f"   Deviation: {abs(duration_months - 3.0):.3f} months")
        if reason:
            print(f"   Reason: {reason}")
        print(f"   Status: {'✅ Would accept' if abs(duration_months - 3.0) <= tolerance else '🚫 Rejected'}")

    @staticmethod
    def validate_period_selection(selected_period: str, form_type: str) -> Tuple[bool, str]:
        """Validate that the selected period is appropriate for the filing type - STRICT MODE"""
        
        if not selected_period:
            return False, "No period selected"
        
        if form_type == '10-Q':
            # STRICT VALIDATION: Only accept exactly quarterly periods
            if (PeriodClassifier.is_cumulative_period(selected_period) and 
                not PeriodClassifier.is_quarterly_period(selected_period)):
                period_desc = PeriodClassifier.get_period_type_description(selected_period)
                return False, f"STRICT REJECTION: {period_desc} for quarterly filing - MUST use exactly 3-month period"
            elif not PeriodClassifier.is_quarterly_period(selected_period):
                return False, f"STRICT REJECTION: Non-quarterly period for 10-Q filing - MUST use exactly 3-month period"
        
        elif form_type == '10-K':
            if (PeriodClassifier.is_quarterly_period(selected_period) and 
                not PeriodClassifier.is_annual_period(selected_period)):
                return False, f"WARNING: Selected quarterly period for annual filing - should use 12-month period"
        
        return True, "Period selection is appropriate"


class FiscalYearCalculator:
    """Centralized fiscal year calculation utilities"""

    _WEEKDAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    @staticmethod
    def _parse_fiscal_year_end_code(fiscal_year_end_code: str) -> Optional[Tuple[int, int]]:
        """Parse 'MMDD' fiscal year end code into (month, day)."""
        if not fiscal_year_end_code:
            return None
        code = str(fiscal_year_end_code).strip()
        if len(code) == 4 and code.isdigit():
            return int(code[:2]), int(code[2:])
        return None

    @staticmethod
    def _nearest_weekday(anchor: datetime, weekday) -> datetime:
        """Return the given weekday on or nearest to the anchor date (52/53-week filers).

        Example: anchor=Jan 31, weekday=Sunday -> the Sunday closest to Jan 31
        (Chewy's fiscal year end).
        """
        if isinstance(weekday, str):
            weekday_num = FiscalYearCalculator._WEEKDAYS.get(weekday.strip().lower())
        else:
            weekday_num = int(weekday) if weekday is not None else None
        if weekday_num is None:
            return anchor
        delta = (weekday_num - anchor.weekday()) % 7
        if delta > 3:
            delta -= 7
        return anchor + timedelta(days=delta)

    @staticmethod
    def _fiscal_year_end_date(
        fiscal_year: Optional[int],
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
        fiscal_year_convention: str = "end",
    ) -> Optional[datetime]:
        """Return the fiscal-year-end date for a given fiscal year.

        Args:
            fiscal_year: The fiscal year label (e.g. 2024).
            fiscal_year_end_code: 'MMDD' (e.g. '0630' for June 30, '0201' for Feb 1).
            weekday: Optional weekday (name or int, Monday=0) for 52/53-week filers
                whose fiscal year ends on that weekday nearest to the anchor date
                (e.g. "sunday" for Chewy). Defaults to a fixed calendar date.
            fiscal_year_convention:
                'end' (default) -> fiscal year N ends on the year-end that falls in
                    calendar year N (e.g. Walmart's FY2025 ends Jan 2025).
                'start' -> fiscal year N ends early in calendar year N+1 for early-year
                    (Jan/Feb) filers (e.g. Chewy's FY2025 ends Feb 2026).
        """
        parsed = FiscalYearCalculator._parse_fiscal_year_end_code(fiscal_year_end_code)
        if parsed is None or not fiscal_year:
            return None
        fy_month, fy_day = parsed
        end_calendar_year = fiscal_year
        if fiscal_year_convention == "start" and fy_month in (1, 2):
            end_calendar_year = fiscal_year + 1
        anchor = datetime(end_calendar_year, fy_month, fy_day)
        if weekday is None:
            return anchor
        return FiscalYearCalculator._nearest_weekday(anchor, weekday)

    @staticmethod
    def calculate_fiscal_quarter_boundaries(
        fiscal_year: int,
        quarter: int,
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
        fiscal_year_convention: str = "end",
    ) -> Optional[Tuple[datetime, datetime]]:
        """
        Calculate the start and end dates for a specific fiscal quarter.
        
        This is the DRY centralized function for fiscal quarter boundary calculation.
        Use this instead of duplicating the logic everywhere.
        
        Args:
            fiscal_year: The fiscal year (e.g., 2024)
            quarter: The fiscal quarter (1, 2, 3, or 4)
            fiscal_year_end_code: Company fiscal year end code (e.g., '0630' for June 30)
            weekday: Optional weekday for 52/53-week filers (see _fiscal_year_end_date).
            fiscal_year_convention: 'end' (default) or 'start' (see _fiscal_year_end_date).
            
        Returns:
            Tuple of (quarter_start_date, quarter_end_date) or None if calculation fails
            
        Example:
            For Microsoft FY2024 Q2 (FYE: 0630):
            calculate_fiscal_quarter_boundaries(2024, 2, '0630')
            Returns: (datetime(2023, 10, 1), datetime(2023, 12, 31))
        """
        if not fiscal_year or not quarter or not fiscal_year_end_code:
            return None
        
        if quarter not in [1, 2, 3, 4]:
            return None
        
        try:
            # Fiscal year end date (handles early-year filers + 52/53-week weekday rule)
            fiscal_year_end = FiscalYearCalculator._fiscal_year_end_date(
                fiscal_year, fiscal_year_end_code, weekday, fiscal_year_convention
            )
            prev_fiscal_year_end = FiscalYearCalculator._fiscal_year_end_date(
                fiscal_year - 1, fiscal_year_end_code, weekday, fiscal_year_convention
            )
            if fiscal_year_end is None or prev_fiscal_year_end is None:
                return None
            
            # Calculate fiscal year start (day after previous fiscal year end)
            fiscal_year_start = prev_fiscal_year_end + timedelta(days=1)
            
            # Calculate quarter boundaries
            quarter_start = fiscal_year_start + relativedelta(months=3 * (quarter - 1))
            quarter_end = quarter_start + relativedelta(months=3) - timedelta(days=1)
            
            return (quarter_start, quarter_end)
            
        except (ValueError, AttributeError):
            return None
    
    @staticmethod
    def determine_fiscal_year_from_date(
        end_date: datetime,
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
        fiscal_year_convention: str = "end",
    ) -> Optional[int]:
        """
        Determine which fiscal year a given date belongs to.
        
        Args:
            end_date: The date to check
            fiscal_year_end_code: Company fiscal year end code (e.g., '0630' for June 30)
            weekday: Optional weekday for 52/53-week filers (see _fiscal_year_end_date).
            fiscal_year_convention: 'end' (default) or 'start' (see _fiscal_year_end_date).
            
        Returns:
            The fiscal year that this date belongs to, or None if calculation fails
            
        Example:
            For date 2023-12-31 with FYE 0630 (June 30):
            This falls in FY2024 (which ends June 30, 2024)
        """
        if not end_date or not fiscal_year_end_code:
            return None
        
        try:
            if FiscalYearCalculator._parse_fiscal_year_end_code(fiscal_year_end_code) is None:
                return None
            
            tolerance = timedelta(days=PeriodConfig.FISCAL_YEAR_TOLERANCE_DAYS)
            
            # A period end date can only belong to a fiscal year ending in its own
            # calendar year, the previous, or the next. Determine which fiscal-year
            # span (s, e] contains the end_date (with tolerance at the boundaries to
            # absorb 52/53-week drift).
            for candidate in [end_date.year - 1, end_date.year, end_date.year + 1]:
                e = FiscalYearCalculator._fiscal_year_end_date(
                    candidate, fiscal_year_end_code, weekday, fiscal_year_convention
                )
                prev_e = FiscalYearCalculator._fiscal_year_end_date(
                    candidate - 1, fiscal_year_end_code, weekday, fiscal_year_convention
                )
                if e is None or prev_e is None:
                    continue
                s = prev_e + timedelta(days=1)
                if (s - tolerance) <= end_date <= (e + tolerance):
                    return candidate
            
            # Shouldn't normally happen; fall back to nearest fiscal year end
            best, best_days = None, None
            for candidate in [end_date.year - 1, end_date.year, end_date.year + 1]:
                e = FiscalYearCalculator._fiscal_year_end_date(
                    candidate, fiscal_year_end_code, weekday, fiscal_year_convention
                )
                if e is None:
                    continue
                days = abs((e - end_date).days)
                if best_days is None or days < best_days:
                    best, best_days = candidate, days
            return best
                
        except (ValueError, AttributeError):
            return None
    
    @staticmethod
    def determine_quarter_from_date(
        end_date: datetime,
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
        fiscal_year_convention: str = "end",
    ) -> Optional[int]:
        """
        Determine which fiscal quarter a given date belongs to.
        
        Args:
            end_date: The date to check
            fiscal_year_end_code: Company fiscal year end code (e.g., '0630' for June 30)
            weekday: Optional weekday for 52/53-week filers (see _fiscal_year_end_date).
            fiscal_year_convention: 'end' (default) or 'start' (see _fiscal_year_end_date).
            
        Returns:
            The fiscal quarter (1, 2, 3, or 4) that this date belongs to, or None if calculation fails

        Notes:
            Quarter boundaries are calculated on a strict 3-month grid.

            Retailers that operate a 52/53-week calendar may use an uneven
            quarter layout (e.g. Kroger's first quarter is 16 weeks while the
            rest are 12 weeks).  A pure "which interval contains this date"
            lookup mislabels such periods because the real quarter end can be
            several weeks past the grid boundary (Kroger's Q1 ends in late May
            instead of late April).  We instead pick the quarter whose grid end
            is *nearest* to the period end date, which correctly resolves both
            even (13-week) and uneven (16/12/12/12) fiscal calendars.
        """
        if not end_date or not fiscal_year_end_code:
            return None
        
        try:
            # First determine the fiscal year
            fiscal_year = FiscalYearCalculator.determine_fiscal_year_from_date(
                end_date, fiscal_year_end_code, weekday, fiscal_year_convention
            )
            if not fiscal_year:
                return None
            
            # Pick the quarter whose (grid) end date is closest to the period end.
            best_quarter: Optional[int] = None
            best_delta: Optional[int] = None
            for q in [1, 2, 3, 4]:
                boundaries = FiscalYearCalculator.calculate_fiscal_quarter_boundaries(
                    fiscal_year, q, fiscal_year_end_code, weekday, fiscal_year_convention
                )
                if not boundaries:
                    continue
                _, q_end = boundaries
                delta = abs((end_date - q_end).days)
                if best_delta is None or delta < best_delta:
                    best_quarter, best_delta = q, delta

            if best_quarter is not None:
                return best_quarter

            # Default to Q4 if we can't determine (shouldn't happen)
            return 4
            
        except (ValueError, AttributeError):
            return None
    
    @staticmethod
    def extract_quarter_from_xbrl_fiscal_period(fiscal_period: str) -> Optional[int]:
        """Extract quarter number from XBRL fiscal_period field"""
        if not fiscal_period:
            return None
        
        fiscal_period = str(fiscal_period).strip().upper()
        
        quarter_map = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4}
        return quarter_map.get(fiscal_period)

    @staticmethod
    def calculate_fiscal_year_and_quarter(
        end_date: datetime,
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
        fiscal_year_convention: str = "end",
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        Calculate fiscal year and quarter based on end date and fiscal year end code.
        
        This function now uses the centralized DRY logic for fiscal calculations.

        Args:
            end_date: The period end date.
            fiscal_year_end_code: 'MMDD' fiscal year end code (e.g. '0630').
            weekday: Optional weekday for 52/53-week filers (see _fiscal_year_end_date).
            fiscal_year_convention: 'end' (default) or 'start' (see _fiscal_year_end_date).
        """
        if not end_date or not fiscal_year_end_code:
            return None, None
        
        # Use centralized functions - DRY principle
        fiscal_year = FiscalYearCalculator.determine_fiscal_year_from_date(
            end_date, fiscal_year_end_code, weekday, fiscal_year_convention
        )
        quarter = FiscalYearCalculator.determine_quarter_from_date(
            end_date, fiscal_year_end_code, weekday, fiscal_year_convention
        )
        
        return fiscal_year, quarter

    @staticmethod
    def determine_fiscal_year_convention(
        fiscal_anchors,
        fiscal_year_end_code: str,
        weekday: Optional[object] = None,
    ) -> Optional[str]:
        """Infer whether a company labels its fiscal years by START year or END year.

        Companies with an early (Jan/Feb) fiscal year end differ in how they name
        their fiscal years:
          * END-year naming (Walmart, majority): FY2025 runs Feb 2024 - Jan 2025.
          * START-year naming (Chewy):            FY2025 runs Feb 2025 - Feb 2026.

        Given a company's known (end_date, fiscal_year) anchors (e.g. from its
        existing reporting periods in the DB), this returns 'start', 'end', or None
        (when ambiguous, e.g. calendar-year filers where the two conventions agree).

        Args:
            fiscal_anchors: Iterable of (end_date, fiscal_year) tuples.
            fiscal_year_end_code: 'MMDD' fiscal year end code.
            weekday: Optional weekday for 52/53-week filers.
        """
        if not fiscal_anchors or not fiscal_year_end_code:
            return None
        try:
            start_matches = end_matches = total = 0
            for end_date, fy in fiscal_anchors:
                if not end_date or fy is None:
                    continue
                if isinstance(end_date, str):
                    try:
                        end_date = datetime.strptime(end_date[:10], "%Y-%m-%d")
                    except (ValueError, TypeError):
                        continue
                total += 1
                fys = FiscalYearCalculator.determine_fiscal_year_from_date(
                    end_date, fiscal_year_end_code, weekday, "start"
                )
                fye = FiscalYearCalculator.determine_fiscal_year_from_date(
                    end_date, fiscal_year_end_code, weekday, "end"
                )
                if fys == fy:
                    start_matches += 1
                if fye == fy:
                    end_matches += 1
            if total == 0:
                return None
            if start_matches > end_matches:
                return "start"
            if end_matches > start_matches:
                return "end"
            return None
        except (ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def determine_quarter_from_form_and_date(form_type: str, end_date: datetime, fiscal_year_end_code: str) -> Optional[int]:
        """
        Determine fiscal quarter using form type and end date.
        
        This function now uses the centralized DRY logic for fiscal calculations.
        """
        if not end_date or not fiscal_year_end_code:
            return None
        
        # For 10-K filings, it's always Q4 (annual/full year)
        if form_type == '10-K':
            return 4
        
        # For 10-Q filings, use centralized quarter determination
        if form_type == '10-Q':
            return FiscalYearCalculator.determine_quarter_from_date(end_date, fiscal_year_end_code)
        
        # For other form types, return None
        return None


def create_period_query_filter(cik: str, statement_type: str, period_type: Optional[str] = None) -> Dict[str, Any]:
    """Create MongoDB query filter for financial statements"""
    query_filter = {
        'cik': str(cik),
        'statement_type': statement_type
    }
    
    if period_type:
        normalized_period = normalize_period_type(period_type)
        query_filter['reporting_period.period_type'] = normalized_period
    
    return query_filter
