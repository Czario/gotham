#!/usr/bin/env python3
"""
Improved period selection utilities to ensure primary periods are selected
"""

import re
import pandas as pd
from typing import List, Optional


def select_primary_period_for_filing(date_columns: List[str], form_type: str, df: Optional[pd.DataFrame] = None) -> Optional[str]:
    """
    Select the primary period column based on filing type.
    
    For quarterly filings (10-Q): Select "Three Months Ended" over cumulative periods
    For annual filings (10-K): Select "Twelve Months Ended" or annual periods
    
    Args:
        date_columns: List of available date columns from the financial statement
        form_type: Type of SEC form ('10-Q', '10-K', etc.)
        df: Optional dataframe for additional analysis
        
    Returns:
        Best date column for the filing type, or None if no columns available
    """
    
    if not date_columns:
        return None
    
    # Score each period column based on filing type and period characteristics
    period_scores = {}
    
    for col in date_columns:
        score = 0
        col_lower = col.lower()
        
        # Base score based on position (Edgar tools generally orders by relevance)
        position_score = len(date_columns) - date_columns.index(col)
        score += position_score
        
        if form_type == '10-Q':
            # STRICT MODE: For quarterly filings (10-Q), ONLY accept quarterly periods
            if _is_quarterly_period(col_lower):
                score += 1000  # Massive bonus for quarterly periods
            elif _is_cumulative_period(col_lower):
                # STRICT: Completely reject cumulative periods for quarterly filings
                score = -10000  # Massive penalty to ensure rejection
            else:
                # Any other period type gets heavily penalized for quarterly filings
                score -= 5000
            
        elif form_type == '10-K':
            # For annual filings (10-K), prefer annual periods
            if _is_annual_period(col_lower):
                score += 100
            elif _is_quarterly_period(col_lower):
                # For 10-K, quarterly periods are less desirable
                score -= 50
                
        # Additional data quality checks if dataframe is provided
        if df is not None:
            data_quality_score = _assess_data_quality(col, df)
            score += data_quality_score
        
        period_scores[col] = score
    
    # Return the highest scoring period
    if period_scores:
        best_period = max(period_scores.items(), key=lambda x: x[1])
        return best_period[0]
    
    # Fallback to first column
    return date_columns[0]


def _is_quarterly_period(col_lower: str) -> bool:
    """Check if a column represents a quarterly period (3 months)"""
    quarterly_indicators = [
        'three months', '3 months', 'three-months',
        'quarter', 'quarterly',
        'q1', 'q2', 'q3', 'q4'
    ]
    return any(indicator in col_lower for indicator in quarterly_indicators)


def _is_cumulative_period(col_lower: str) -> bool:
    """Check if a column represents a cumulative period (6, 9, or 12 months)"""
    cumulative_indicators = [
        'six months', '6 months', 'six-months',
        'nine months', '9 months', 'nine-months', 
        'twelve months', '12 months', 'twelve-months',
        'year to date', 'ytd', 'cumulative'
    ]
    return any(indicator in col_lower for indicator in cumulative_indicators)


def _is_annual_period(col_lower: str) -> bool:
    """Check if a column represents an annual period (12 months/year)"""
    annual_indicators = [
        'twelve months', '12 months', 'twelve-months',
        'year ended', 'annual', 'yearly'
    ]
    return any(indicator in col_lower for indicator in annual_indicators)


def _get_cumulative_penalty(col_lower: str) -> int:
    """Get penalty score for cumulative periods based on duration"""
    if 'six months' in col_lower or '6 months' in col_lower:
        return 75  # 6 months is moderately bad for quarterly
    elif 'nine months' in col_lower or '9 months' in col_lower:
        return 100  # 9 months is worse
    elif 'twelve months' in col_lower or '12 months' in col_lower or 'year' in col_lower:
        return 150  # 12 months is worst for quarterly
    return 50  # Default penalty for other cumulative periods


def _assess_data_quality(col: str, df: pd.DataFrame) -> int:
    """Assess data quality of a period column"""
    score = 0
    
    # Check if column has data
    non_null_count = df[col].notna().sum()
    if non_null_count > 0:
        score += 10
        
        # Check if data looks reasonable (not all zeros)
        numeric_data = pd.to_numeric(df[col], errors='coerce').dropna()
        if len(numeric_data) > 0:
            non_zero_count = (numeric_data != 0).sum()
            if non_zero_count > 0:
                score += 5
    
    return score


def get_period_type_description(col_name: str) -> str:
    """Get a human-readable description of what period type a column represents"""
    col_lower = col_name.lower()
    
    if _is_quarterly_period(col_lower):
        return "3-month quarterly period ✅"
    elif 'six months' in col_lower or '6 months' in col_lower:
        return "6-month cumulative period ⚠️"
    elif 'nine months' in col_lower or '9 months' in col_lower:
        return "9-month cumulative period ⚠️"
    elif _is_annual_period(col_lower):
        return "12-month annual period"
    
    # Try to extract date for period identification
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', col_name)
    if date_match:
        return f"Period ending {date_match.group(1)}"
    
    return "Unknown period type"


def validate_period_selection(selected_period: str, form_type: str) -> tuple[bool, str]:
    """
    STRICT VALIDATION: Ensure the selected period is appropriate for the filing type
    
    Returns:
        (is_valid, warning_message)
    """
    
    if not selected_period:
        return False, "No period selected"
    
    col_lower = selected_period.lower()
    
    if form_type == '10-Q':
        # STRICT MODE: Absolutely reject any non-quarterly periods for 10-Q
        if _is_cumulative_period(col_lower):
            period_desc = get_period_type_description(selected_period)
            return False, f"STRICT REJECTION: {period_desc} for quarterly filing - MUST use exactly 3-month period"
        elif not _is_quarterly_period(col_lower):
            return False, f"STRICT REJECTION: Non-quarterly period for 10-Q filing - MUST use exactly 3-month period"
    
    elif form_type == '10-K':
        if _is_quarterly_period(col_lower) and not _is_annual_period(col_lower):
            return False, f"WARNING: Selected quarterly period for annual filing - should use 12-month period"
    
    return True, "Period selection is appropriate"
