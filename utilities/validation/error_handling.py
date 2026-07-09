#!/usr/bin/env python3
"""Common utilities for error handling and logging to reduce code duplication"""

import logging
from typing import Any, Callable, Dict, Optional, TypeVar, Union
from functools import wraps

logger = logging.getLogger(__name__)

T = TypeVar('T')

class DatabaseOperationError(Exception):
    """Custom exception for database operation errors"""
    pass

class ProcessingError(Exception):
    """Custom exception for processing errors"""
    pass

# Sentinel object to indicate that an exception should be raised
class _RaiseSentinel:
    pass

_RAISE = _RaiseSentinel()

def safe_database_operation(operation_name: str, default_return: Union[Any, _RaiseSentinel] = _RAISE):
    """
    Decorator for database operations with unified error handling
    
    Args:
        operation_name: Name of the operation for logging
        default_return: Default return value on error (_RAISE sentinel to raise exception)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., Union[T, Any]]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Union[T, Any]:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {operation_name}: {e}")
                if default_return is not _RAISE:
                    return default_return
                raise DatabaseOperationError(f"{operation_name} failed: {e}")
        return wrapper
    return decorator

def safe_processing_operation(operation_name: str, default_return: Any = False):
    """
    Decorator for processing operations with unified error handling
    
    Args:
        operation_name: Name of the operation for logging
        default_return: Default return value on error
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {operation_name}: {e}")
                return default_return
        return wrapper
    return decorator

def validate_required_fields(data: Dict, required_fields: list, operation_name: str) -> bool:
    """
    Validate that required fields are present in data
    
    Args:
        data: Dictionary to validate
        required_fields: List of required field names
        operation_name: Name of operation for error logging
        
    Returns:
        bool: True if all required fields present, False otherwise
    """
    missing_fields = [field for field in required_fields if not data.get(field)]
    
    if missing_fields:
        logger.error(f"{operation_name}: Missing required fields: {missing_fields}")
        return False
    
    return True

def log_operation_result(operation_name: str, success: bool, details: Optional[str] = None):
    """
    Log operation results in a consistent format
    
    Args:
        operation_name: Name of the operation
        success: Whether operation was successful
        details: Additional details to log
    """
    status = "✅" if success else "❌"
    message = f"{status} {operation_name}"
    
    if details:
        message += f": {details}"
    
    if success:
        logger.info(message)
    else:
        logger.error(message)

def create_period_description(reporting_period: Dict) -> str:
    """
    Create a standardized period description from reporting period data
    
    Args:
        reporting_period: Dictionary containing period information
        
    Returns:
        str: Formatted period description
    """
    end_date = reporting_period.get("end_date")
    period_date = reporting_period.get("period_date")
    fiscal_year = reporting_period.get("fiscal_year")
    quarter = reporting_period.get("quarter")
    
    if end_date:
        end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)[:10]
        return f"ending {end_date_str}"
    elif period_date:
        return f"ending {period_date}"
    elif fiscal_year:
        quarter_str = f" Q{quarter}" if quarter else ""
        return f"FY{fiscal_year}{quarter_str}"
    else:
        return "Unknown period"

def format_filing_log_message(statement_type: str, reporting_period: Dict) -> str:
    """
    Format standardized filing log message

    Args:
        statement_type: Type of financial statement
        reporting_period: Period information

    Returns:
        str: Formatted log message
    """
    period_desc = create_period_description(reporting_period)
    form_type = reporting_period.get("form_type", "")
    fiscal_year_end_code = reporting_period.get("fiscal_year_end_code", "")
    data_source = reporting_period.get('data_source', 'sec_api')
    return f"📅 {statement_type} for period {period_desc} ({form_type}) - FYE: {fiscal_year_end_code} - Source: {data_source}"
