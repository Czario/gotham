#!/usr/bin/env python3
"""Utility functions for normalizing period types and dates"""

from typing import Dict, Any, Optional

def normalize_period_type(period_type: str) -> str:
    """
    Normalize period type to standard values
    
    Args:
        period_type: Input period type (annual, annually, quarterly, etc.)
        
    Returns:
        Normalized period type
    """
    if not period_type:
        return "unknown"
    
    period_type_lower = period_type.lower().strip()
    
    # Normalize annual variations
    if period_type_lower in ['annual', 'annually', 'yearly', 'year']:
        return 'annual'
    
    # Normalize quarterly variations
    if period_type_lower in ['quarterly', 'quarter', 'q1', 'q2', 'q3', 'q4']:
        return 'quarterly'
    
    # Normalize interim variations
    if period_type_lower in ['interim', 'semi-annual', 'semiannual', 'half-year']:
        return 'interim'
    
    # Return as-is for other values
    return period_type_lower

def normalize_query_period_type(query_period_type: str) -> str:
    """
    Normalize query period type to match database storage format
    
    Args:
        query_period_type: Period type from API query (e.g., "annually")
        
    Returns:
        Database period type (e.g., "annual")
    """
    return normalize_period_type(query_period_type)

def create_period_query_filter(cik: str, statement_type: str, period_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Create MongoDB query filter for financial statements
    
    Args:
        cik: Company CIK
        statement_type: Type of statement
        period_type: Period type filter (optional)
        
    Returns:
        MongoDB query filter
    """
    query_filter = {
        'cik': str(cik),
        'statement_type': statement_type
    }
    
    if period_type:
        normalized_period = normalize_query_period_type(period_type)
        query_filter['reporting_period.period_type'] = normalized_period
    
    return query_filter
