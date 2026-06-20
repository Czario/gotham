"""Helper utilities for SEC data scraper"""

from .error_handling import (
    DatabaseOperationError,
    ProcessingError,
    safe_database_operation,
    safe_processing_operation,
    validate_required_fields,
    log_operation_result,
    format_filing_log_message
)

from .logger_config import (
    LoggerConfig,
    setup_production_logging,
    setup_development_logging,
    get_module_logger,
    setup_basic_logging,
    get_performance_logger,
    get_error_logger
)

from .period_normalizer import (
    normalize_period_type,
    normalize_query_period_type
)

from .period_selectors import (
    select_primary_period_for_filing,
    get_period_type_description,
    validate_period_selection
)

from .period_utils import (
    PeriodConfig,
    PeriodParser,
    PeriodClassifier,
    PeriodMatcher,
    FiscalYearCalculator,
    create_period_query_filter
)

__all__ = [
    'DatabaseOperationError',
    'ProcessingError', 
    'safe_database_operation',
    'safe_processing_operation',
    'validate_required_fields',
    'log_operation_result',
    'format_filing_log_message',
    'LoggerConfig',
    'setup_production_logging',
    'setup_development_logging',
    'get_module_logger',
    'setup_basic_logging',
    'get_performance_logger',
    'get_error_logger',
    'normalize_period_type',
    'normalize_query_period_type',
    'select_primary_period_for_filing',
    'get_period_type_description',
    'validate_period_selection',
    'PeriodConfig',
    'PeriodParser',
    'PeriodClassifier',
    'PeriodMatcher',
    'FiscalYearCalculator',
    'create_period_query_filter'
]