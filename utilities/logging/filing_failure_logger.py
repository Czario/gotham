#!/usr/bin/env python3
"""
Filing Failure Logger - Per-stock logging for failed filings
Creates individual log files for each stock to track extraction failures
"""

import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any
from threading import Lock

class FilingFailureLogger:
    """Logger for tracking filing extraction failures on a per-stock basis"""
    
    # Class-level cache for loggers to avoid creating duplicates
    _loggers_cache: Dict[str, logging.Logger] = {}
    _lock = Lock()
    
    def __init__(self, log_dir: str = 'logs/filing_failures'):
        """
        Initialize the filing failure logger
        
        Args:
            log_dir: Directory where stock-specific log files will be created
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def get_stock_logger(self, ticker: str, cik: str) -> logging.Logger:
        """
        Get or create a logger for a specific stock
        
        Args:
            ticker: Stock ticker symbol (e.g., 'AAPL')
            cik: Company CIK identifier
            
        Returns:
            logging.Logger: Stock-specific logger
        """
        # Create a unique key for this stock
        stock_key = f"{ticker}_{cik}"
        
        # Check cache first (thread-safe)
        with self._lock:
            if stock_key in self._loggers_cache:
                return self._loggers_cache[stock_key]
            
            # Create new logger for this stock
            logger_name = f"filing_failure.{stock_key}"
            stock_logger = logging.getLogger(logger_name)
            
            # Only configure if not already configured
            if not stock_logger.handlers:
                stock_logger.setLevel(logging.INFO)
                stock_logger.propagate = False  # Don't propagate to root logger
                
                # Create log file path
                log_file = self.log_dir / f"{ticker}_{cik}_failures.log"
                
                # Create file handler
                file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
                file_handler.setLevel(logging.INFO)
                
                # Custom format for filing failures
                formatter = logging.Formatter(
                    '%(asctime)s | %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S'
                )
                file_handler.setFormatter(formatter)
                stock_logger.addHandler(file_handler)
                
                # Write header if this is a new file
                if not log_file.exists() or log_file.stat().st_size == 0:
                    stock_logger.info("="*100)
                    stock_logger.info(f"FILING FAILURE LOG FOR {ticker} (CIK: {cik})")
                    stock_logger.info("="*100)
                    stock_logger.info("")
            
            # Cache the logger
            self._loggers_cache[stock_key] = stock_logger
            return stock_logger
    
    def log_filing_failure(
        self,
        ticker: str,
        cik: str,
        accession_number: str,
        form_type: str,
        failure_reason: str,
        filing_date: Optional[str] = None,
        fiscal_year: Optional[int] = None,
        fiscal_quarter: Optional[str] = None,
        report_period: Optional[str] = None,
        additional_info: Optional[Dict[str, Any]] = None,
        log_session_header: bool = True
    ):
        """
        Log a filing extraction failure with all relevant details
        
        Args:
            ticker: Stock ticker symbol
            cik: Company CIK identifier
            accession_number: SEC accession number
            form_type: Filing form type (e.g., '10-K', '10-Q')
            failure_reason: Description of why the filing failed
            filing_date: Date the filing was submitted to SEC
            fiscal_year: Fiscal year of the filing
            fiscal_quarter: Fiscal quarter (Q1, Q2, Q3, Q4) if applicable
            report_period: Report period end date
            additional_info: Any additional context information
            log_session_header: Whether to log session header (for first failure in session)
        """
        logger = self.get_stock_logger(ticker, cik)
        
        # Log session header if this is the first failure
        if log_session_header:
            stock_key = f"{ticker}_{cik}"
            if not hasattr(self, '_session_headers_logged'):
                self._session_headers_logged = set()
            
            if stock_key not in self._session_headers_logged:
                logger.info("")
                logger.info("-" * 100)
                logger.info(f"  Processing Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info("-" * 100)
                self._session_headers_logged.add(stock_key)
        
        # Build the failure message
        message_parts = [
            f"FAILED FILING",
            f"Accession: {accession_number}",
            f"Form: {form_type}"
        ]
        
        if filing_date:
            message_parts.append(f"Filing Date: {filing_date}")
        
        if report_period:
            message_parts.append(f"Report Period: {report_period}")
        
        if fiscal_year:
            fiscal_info = f"FY{fiscal_year}"
            if fiscal_quarter:
                fiscal_info += f" {fiscal_quarter}"
            message_parts.append(f"Fiscal Period: {fiscal_info}")
        
        message_parts.append(f"Reason: {failure_reason}")
        
        # Add additional info if provided
        if additional_info:
            for key, value in additional_info.items():
                message_parts.append(f"{key}: {value}")
        
        # Join all parts with separator
        full_message = " | ".join(message_parts)
        
        # Log the failure
        logger.info(full_message)
    
    def log_filing_success(
        self,
        ticker: str,
        cik: str,
        accession_number: str,
        form_type: str,
        statements_extracted: int,
        filing_date: Optional[str] = None,
        fiscal_year: Optional[int] = None,
        fiscal_quarter: Optional[str] = None,
        report_period: Optional[str] = None
    ):
        """
        Log a successful filing extraction (optional, for comparison)
        
        Args:
            ticker: Stock ticker symbol
            cik: Company CIK identifier
            accession_number: SEC accession number
            form_type: Filing form type
            statements_extracted: Number of statements successfully extracted
            filing_date: Date the filing was submitted to SEC
            fiscal_year: Fiscal year of the filing
            fiscal_quarter: Fiscal quarter if applicable
            report_period: Report period end date
        """
        logger = self.get_stock_logger(ticker, cik)
        
        message_parts = [
            f"SUCCESS",
            f"Accession: {accession_number}",
            f"Form: {form_type}",
            f"Statements: {statements_extracted}"
        ]
        
        if filing_date:
            message_parts.append(f"Filing Date: {filing_date}")
        
        if report_period:
            message_parts.append(f"Report Period: {report_period}")
        
        if fiscal_year:
            fiscal_info = f"FY{fiscal_year}"
            if fiscal_quarter:
                fiscal_info += f" {fiscal_quarter}"
            message_parts.append(f"Fiscal Period: {fiscal_info}")
        
        full_message = " | ".join(message_parts)
        logger.info(full_message)
    
    def log_section_header(self, ticker: str, cik: str, section_title: str):
        """
        Log a section header in the stock log file
        
        Args:
            ticker: Stock ticker symbol
            cik: Company CIK identifier
            section_title: Title of the section
        """
        logger = self.get_stock_logger(ticker, cik)
        logger.info("")
        logger.info("-" * 100)
        logger.info(f"  {section_title}")
        logger.info("-" * 100)
    
    def log_processing_summary(
        self,
        ticker: str,
        cik: str,
        total_filings: int,
        successful: int,
        failed: int,
        skipped: int = 0
    ):
        """
        Log a summary of processing results for a stock
        
        Args:
            ticker: Stock ticker symbol
            cik: Company CIK identifier
            total_filings: Total number of filings processed
            successful: Number of successful extractions
            failed: Number of failed extractions
            skipped: Number of skipped filings
        """
        logger = self.get_stock_logger(ticker, cik)
        
        logger.info("")
        logger.info("="*100)
        logger.info(f"PROCESSING SUMMARY - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*100)
        logger.info(f"Total Filings: {total_filings}")
        logger.info(f"Successful: {successful} ({successful/total_filings*100:.1f}%)" if total_filings > 0 else "Successful: 0")
        logger.info(f"Failed: {failed} ({failed/total_filings*100:.1f}%)" if total_filings > 0 else "Failed: 0")
        if skipped > 0:
            logger.info(f"Skipped: {skipped} ({skipped/total_filings*100:.1f}%)" if total_filings > 0 else "Skipped: 0")
        logger.info("="*100)
        logger.info("")


# Global instance for easy access
_global_failure_logger: Optional[FilingFailureLogger] = None

def get_filing_failure_logger(log_dir: str = 'logs/filing_failures') -> FilingFailureLogger:
    """
    Get the global filing failure logger instance
    
    Args:
        log_dir: Directory for log files
        
    Returns:
        FilingFailureLogger: The global instance
    """
    global _global_failure_logger
    if _global_failure_logger is None:
        _global_failure_logger = FilingFailureLogger(log_dir)
    return _global_failure_logger
