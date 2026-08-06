#!/usr/bin/env python3
"""
SEC data extractor that extracts period information from SEC API with fiscal year calculations.
This module extracts period information from SEC API and calculates fiscal year and quarter.
"""

from datetime import datetime
from typing import Dict, Optional
from utilities.helpers.period_utils import FiscalYearCalculator
from utilities.helpers.logger_config import get_module_logger

logger = get_module_logger(__name__)


def extract_period_info_from_sec_api(filing_info: Dict, company_info: Dict) -> Dict:
    """
    Extract period information from SEC API data and calculate fiscal year and quarter.
    
    This function uses SEC API data and calculates fiscal year and quarter:
    - reportDate: Period end date from SEC API
    - form: Form type (10-K, 10-Q, etc.) from SEC API  
    - fiscalYearEnd: Company fiscal year end code from SEC API
    - Calculates fiscal_year and quarter based on company fiscal year end
    
    Args:
        filing_info: Filing information from SEC API (includes reportDate, form, etc.)
        company_info: Company information from SEC API (includes fiscalYearEnd)
        
    Returns:
        Period information dictionary with SEC API data and calculated fiscal year/quarter
    """
    
    # Extract raw SEC API data
    report_date = filing_info.get('reportDate')
    form_type = filing_info.get('form', '')
    filing_date = filing_info.get('filingDate')
    fiscal_year_end_code = company_info.get('fiscalYearEnd')
    
    # Convert report date string to datetime if available
    end_date = None
    if report_date:
        try:
            end_date = datetime.strptime(report_date, '%Y-%m-%d')
        except ValueError:
            pass
    
    # Determine period type based on form (no calculation, just mapping)
    if form_type == '10-K':
        period_type = "annual"
    elif form_type == '10-Q':
        period_type = "quarterly"
    else:
        # Default to quarterly for unknown forms to satisfy database schema
        period_type = "quarterly"
    
    # Calculate fiscal year and quarter if we have the necessary data
    fiscal_year = None
    quarter = None
    
    if end_date and fiscal_year_end_code:
        try:
            fiscal_year, calculated_quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(end_date, fiscal_year_end_code)
            
            # For 10-K (annual reports), only include fiscal_year, not quarter
            # For 10-Q (quarterly reports), include both fiscal_year and quarter
            if form_type == '10-K':
                logger.debug(f"Calculated fiscal year {fiscal_year} for annual report (10-K)")
                # quarter remains None for 10-K forms
            elif form_type == '10-Q':
                quarter = calculated_quarter
                logger.debug(f"Calculated fiscal year {fiscal_year} Q{quarter} for quarterly report (10-Q)")
            else:
                # For other forms, include quarter if available
                quarter = calculated_quarter
                logger.debug(f"Calculated fiscal year {fiscal_year} Q{quarter} for form {form_type}")
        except Exception as e:
            print(f"⚠️ Failed to calculate fiscal year/quarter: {e}")
    
    # Build the result dictionary
    result = {
        # Raw SEC API period data
        "end_date": end_date,
        "period_date": report_date,
        "period_type": period_type,
        "form_type": form_type,

        # Authoritative filing period from SEC Submissions API.
        # Stored separately so downstream alignment logic always has
        # an unambiguous anchor, even when period_date is later
        # overridden by XBRL-derived calculations (e.g. local files).
        "filing_report_date": report_date,

        # Raw SEC API company data
        "fiscal_year_end_code": fiscal_year_end_code,
        
        # Calculated fiscal period data
        "fiscal_year": fiscal_year,
        
        # Metadata
        "data_source": "sec_api_with_fiscal_calculations",
        "cik": company_info.get('cik'),
        "company_name": company_info.get('name'),
    }
    
    # Only add quarter for quarterly reports (10-Q) and other non-annual forms
    if quarter is not None:
        result["quarter"] = quarter
    
    return result
