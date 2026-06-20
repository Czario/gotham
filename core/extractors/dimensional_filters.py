#!/usr/bin/env python3
"""
Enhanced dimensional data filtering to ensure consistency with selected period
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from utilities.helpers.period_utils import PeriodParser, PeriodMatcher, PeriodConfig


def filter_dimensional_data_by_period_length(dimensional_df: pd.DataFrame, target_period_col: str, target_duration_months: int = 3) -> pd.DataFrame:
    """
    Filter dimensional data to match the target period duration.
    
    For quarterly filings, we want 3-month periods, not 6-month or 9-month cumulative periods.
    
    Args:
        dimensional_df: DataFrame with XBRL dimensional data
        target_period_col: The target period column name (e.g., "2025-03-29 (Q1)")
        target_duration_months: Expected duration in months (3 for quarterly, 12 for annual)
        
    Returns:
        Filtered DataFrame containing only periods that match the target duration
    """
    
    if dimensional_df.empty:
        return dimensional_df
    
    # Extract the target end date from the period column
    target_end_date = PeriodParser.extract_date_from_period_name(target_period_col)
    if not target_end_date:
        return dimensional_df  # Can't parse date, return as-is
    
    # Get form type for strict validation
    form_type = None
    if target_duration_months == 3:
        form_type = '10-Q'  # Assume quarterly is from 10-Q
    elif target_duration_months == 12:
        form_type = '10-K'  # Assume annual is from 10-K
    
    # Filter dimensional data to periods that match the target duration - STRICT MODE
    filtered_rows = []
    rejected_count = 0
    
    for idx, row in dimensional_df.iterrows():
        period_start = row.get('period_start')
        period_end = row.get('period_end')
        
        if pd.notna(period_start) and pd.notna(period_end):
            try:
                start_date = pd.to_datetime(period_start)
                end_date = pd.to_datetime(period_end)
                
                # Calculate period duration in months
                duration_months = PeriodParser.calculate_duration_months(start_date, end_date)
                
                # STRICT ENFORCEMENT: Reject non-quarterly periods for quarterly targets
                if PeriodMatcher.reject_non_quarterly_period(duration_months, form_type):
                    rejected_count += 1
                    continue  # Skip this row entirely
                
                # Check if this period matches our strict target criteria
                period_matches = PeriodMatcher.is_period_match(
                    start_date, end_date, target_end_date, 
                    duration_months, target_duration_months, form_type
                )
                
                if period_matches:
                    filtered_rows.append(row)
                    
            except Exception as e:
                # If we can't parse dates, only include for non-strict mode
                if target_duration_months != 3:  # Only be conservative for non-quarterly
                    filtered_rows.append(row)
        else:
            # If no period info, only include for non-strict mode
            if target_duration_months != 3:  # Only be conservative for non-quarterly
                filtered_rows.append(row)
    
    if rejected_count > 0:
        print(f"🚫 DIMENSIONAL STRICT FILTERING: Rejected {rejected_count} non-quarterly dimensional facts")
    
    # Convert back to DataFrame
    if filtered_rows:
        return pd.DataFrame(filtered_rows)
    else:
        return pd.DataFrame()  # No matching periods found


def get_target_duration_for_form(form_type: str) -> int:
    """Get the expected duration in months for a filing type"""
    return PeriodMatcher.get_target_duration_for_form(form_type)


def debug_period_filtering(dimensional_df: pd.DataFrame, target_period: str) -> None:
    """Debug function to show what periods are available and which would be filtered"""
    
    print("\n=== PERIOD FILTERING DEBUG ===")
    print(f"Target period: {target_period}")
    
    target_end = PeriodParser.extract_date_from_period_name(target_period)
    print(f"Target end date: {target_end}")
    
    if dimensional_df.empty:
        print("No dimensional data to analyze")
        return
    
    print("\nAvailable periods in dimensional data:")
    
    period_summary = {}
    for idx, row in dimensional_df.iterrows():
        period_start = row.get('period_start')
        period_end = row.get('period_end')
        value = row.get('value') or row.get('numeric_value')
        
        if pd.notna(period_start) and pd.notna(period_end):
            try:
                start_date = pd.to_datetime(period_start)
                end_date = pd.to_datetime(period_end)
                duration_months = PeriodParser.calculate_duration_months(start_date, end_date)
                
                period_key = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
                
                if period_key not in period_summary:
                    period_summary[period_key] = {
                        'duration_months': duration_months,
                        'end_date': end_date,
                        'values': []
                    }
                
                if pd.notna(value):
                    period_summary[period_key]['values'].append(float(value))
                    
            except Exception as e:
                continue
    
    # Show period summary
    for period_desc, info in period_summary.items():
        duration = info['duration_months']
        end_date = info['end_date']
        values = info['values']
        
        print(f"\n  {period_desc}")
        print(f"    Duration: {duration:.1f} months")
        
        if values:
            avg_value = sum(values) / len(values)
            print(f"    Sample value: ${avg_value/1000000:,.0f} million")
        
        # Check if it would match our target
        if target_end:
            would_match = PeriodMatcher.is_period_match(
                pd.to_datetime(period_desc.split(' to ')[0]),
                end_date, target_end, duration, 3  # Assuming quarterly
            )
            
            if would_match:
                print("    ✅ WOULD MATCH target period")
            else:
                print("    ❌ Would be filtered out")
                
                # Explain why
                end_diff = abs((end_date - target_end).days)
                duration_diff = abs(duration - 3)
                
                if end_diff > PeriodConfig.DATE_TOLERANCE_DAYS:
                    print(f"       Reason: End date differs by {end_diff} days")
                if duration_diff > PeriodConfig.DEFAULT_DURATION_TOLERANCE:
                    print(f"       Reason: Duration differs by {duration_diff:.1f} months")
    
    print("\n=== END DEBUG ===\n")
