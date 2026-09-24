"""Command-line entry point for the normalization service (``normalize-data``).

Moved here from the repository-root ``main.py`` so the console script shipped by
the wheel can import it (``main:main`` was not part of the installed package and
raised ``ModuleNotFoundError``).  The old path is kept as a thin shim.
"""
import sys
import argparse
from pathlib import Path
from typing import List

from data_normalization_service.core.config import AppConfig
from data_normalization_service.core.logging_config import setup_logging as setup_comprehensive_logging, get_logger, get_status_logger
from data_normalization_service.services.normalization_service import FinancialNormalizationService


def _load_company_identifiers_from_file(raw_path: str) -> List[str]:
    """Load ticker/CIK identifiers from a file path."""
    file_path = Path(raw_path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"Path does not exist or is not a file: {raw_path}")

    identifiers: List[str] = []
    with file_path.open('r', encoding='utf-8') as file_handle:
        for line in file_handle:
            normalized_line = line.strip()
            if not normalized_line or normalized_line.startswith('#'):
                continue
            identifiers.append(normalized_line)

    return identifiers


def _resolve_to_ciks(service: FinancialNormalizationService, identifiers: List[str]) -> List[str]:
    """Resolve a mixed list of CIKs and ticker symbols to CIKs."""
    resolved_ciks: List[str] = []

    for identifier in identifiers:
        if identifier.isdigit():
            resolved_ciks.append(identifier)
            continue

        ticker = identifier.upper()
        company = service.company_repo.find_by_ticker_source(ticker)
        if company is None:
            company = service.company_repo.find_by_ticker_target(ticker)

        if company is None:
            raise ValueError(
                f"Unable to resolve ticker '{identifier}' to a CIK. "
                "Provide a valid CIK or ticker present in the companies collection."
            )

        resolved_ciks.append(company.cik)

    # Preserve order while removing duplicates
    unique_ciks: List[str] = []
    seen = set()
    for cik in resolved_ciks:
        if cik not in seen:
            seen.add(cik)
            unique_ciks.append(cik)

    return unique_ciks


def main() -> None:
    """Main function to run the normalization service."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Financial Data Normalization Service")
    parser.add_argument('--path', type=str,
                       help='Path to file containing one CIK/ticker per line')
    
    args = parser.parse_args()
    
    try:
        # Parse configuration from environment variables
        config = AppConfig.from_env()
        
        # Setup comprehensive logging with default settings
        logging_config = setup_comprehensive_logging(
            log_level='INFO',
            console_errors_only=False,
            enable_file_logging=True
        )
        
        # Get loggers
        logger = get_logger(__name__)
        status_logger = logging_config.create_status_logger(__name__)
        
        # Display startup information
        status_logger.info("="*60)
        status_logger.info("FINANCIAL DATA NORMALIZATION SERVICE")
        status_logger.info("="*60)
        
        if not args.path:
            status_logger.info("📈 Running full normalization with sync behavior (only inserts missing data)")
        
        status_logger.info("📁 Log files will be saved to: logs/")
        
        logger.info(f"Arguments: {args}")
        
        # Create service
        service = FinancialNormalizationService(config)

        requested_identifiers = _load_company_identifiers_from_file(args.path) if args.path else []
        resolved_cik_list = _resolve_to_ciks(service, requested_identifiers) if requested_identifiers else []

        if resolved_cik_list:
            if len(resolved_cik_list) > 1:
                status_logger.info(f"🎯 Processing specific companies: {', '.join(resolved_cik_list)}")
            else:
                status_logger.info(f"🎯 Processing specific company: {resolved_cik_list[0]}")
        
        # Run based on command-line options
        if resolved_cik_list:
            # Process specific company/companies (uses sync behavior internally)
            for cik in resolved_cik_list:
                status_logger.info(f"📊 Processing company: {cik}")
                service.process_specific_company(cik)
        else:
            # Default normalization (uses sync behavior by default)
            service.normalize_data()
        
        # Display completion status
        status_logger.info("="*60)
        status_logger.info("✅ NORMALIZATION COMPLETED SUCCESSFULLY")
        status_logger.info("="*60)
        status_logger.info("📁 Check logs/ directory for detailed information")
        
        logger.info("Service completed successfully")
        
    except KeyboardInterrupt:
        logger = get_logger(__name__)
        status_logger = get_status_logger(__name__)
        status_logger.info("⚠️  Service interrupted by user")
        logger.warning("Service interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger = get_logger(__name__)
        status_logger = get_status_logger(__name__)
        status_logger.info(f"❌ Service failed: {e}")
        logger.error(f"Service failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
