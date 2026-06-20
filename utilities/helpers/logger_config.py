#!/usr/bin/env python3
"""
Centralized logging configuration for SEC Data Scraper v9
Provides consistent logging setup across all modules
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

class LoggerConfig:
    """Centralized logger configuration class"""
    
    # Log levels mapping
    LOG_LEVELS = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    
    # Default log format
    DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    DETAILED_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s() - %(message)s'
    
    @classmethod
    def setup_logging(
        cls,
        level: str = 'INFO',
        log_file: Optional[str] = None,
        log_dir: str = 'logs',
        detailed: bool = False,
        console_output: bool = True,
        file_output: bool = False,
        max_bytes: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5
    ) -> logging.Logger:
        """
        Set up centralized logging configuration
        
        Args:
            level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_file: Optional specific log file name
            log_dir: Directory for log files
            detailed: Whether to use detailed format with file/line info
            console_output: Whether to output to console
            file_output: Whether to write logs to file (default: False for clean output)
            max_bytes: Maximum size of log file before rotation
            backup_count: Number of backup files to keep
            
        Returns:
            logging.Logger: Configured root logger
        """
        # Create logs directory if it doesn't exist
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(cls.LOG_LEVELS.get(level.upper(), logging.INFO))
        
        # Clear any existing handlers
        root_logger.handlers.clear()
        
        # Choose format
        log_format = cls.DETAILED_FORMAT if detailed else cls.DEFAULT_FORMAT
        formatter = logging.Formatter(
            log_format,
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        if console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(cls.LOG_LEVELS.get(level.upper(), logging.INFO))
            console_handler.setFormatter(formatter)
            root_logger.addHandler(console_handler)
        
        # File handler with rotation (optional)
        if file_output:
            if log_file:
                file_path = log_path / log_file
            else:
                # Auto-generate log file name with timestamp
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                file_path = log_path / f'sec_scraper_{timestamp}.log'
            
            file_handler = logging.handlers.RotatingFileHandler(
                file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8'
            )
            file_handler.setLevel(cls.LOG_LEVELS.get(level.upper(), logging.INFO))
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            
            # Log the configuration
            root_logger.info(f"Logging configured - Level: {level}, File: {file_path}")
        else:
            # Log to console when no file output
            if console_output:
                root_logger.info(f"Logging configured - Level: {level}, Console only")
        
        return root_logger
    
    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """
        Get a logger instance for a specific module
        
        Args:
            name: Logger name (typically __name__)
            
        Returns:
            logging.Logger: Logger instance
        """
        return logging.getLogger(name)
    
    @classmethod
    def setup_performance_logger(cls, log_dir: str = 'logs') -> logging.Logger:
        """
        Set up a separate logger for performance metrics
        
        Args:
            log_dir: Directory for log files
            
        Returns:
            logging.Logger: Performance logger
        """
        perf_logger = logging.getLogger('performance')
        perf_logger.setLevel(logging.INFO)
        
        # Prevent propagation to root logger
        perf_logger.propagate = False
        
        # Create performance log file
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d')
        perf_file = log_path / f'performance_{timestamp}.log'
        
        # Performance-specific format
        perf_formatter = logging.Formatter(
            '%(asctime)s - PERF - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        perf_handler = logging.handlers.RotatingFileHandler(
            perf_file,
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=10
        )
        perf_handler.setFormatter(perf_formatter)
        perf_logger.addHandler(perf_handler)
        
        return perf_logger
    
    @classmethod
    def setup_error_logger(cls, log_dir: str = 'logs') -> logging.Logger:
        """
        Set up a separate logger for errors only
        
        Args:
            log_dir: Directory for log files
            
        Returns:
            logging.Logger: Error logger
        """
        error_logger = logging.getLogger('errors')
        error_logger.setLevel(logging.ERROR)
        
        # Prevent propagation to root logger
        error_logger.propagate = False
        
        # Create error log file
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d')
        error_file = log_path / f'errors_{timestamp}.log'
        
        # Error-specific format with full details
        error_formatter = logging.Formatter(
            '%(asctime)s - ERROR - %(name)s - %(filename)s:%(lineno)d - %(funcName)s() - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        error_handler = logging.handlers.RotatingFileHandler(
            error_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=10
        )
        error_handler.setFormatter(error_formatter)
        error_logger.addHandler(error_handler)
        
        return error_logger

# Convenience functions for quick setup
def setup_basic_logging(level: str = 'INFO') -> logging.Logger:
    """Quick setup for basic logging"""
    return LoggerConfig.setup_logging(level=level)

def setup_production_logging(log_dir: str = 'logs') -> tuple[logging.Logger, logging.Logger, logging.Logger]:
    """
    Setup comprehensive logging for production environment
    
    Returns:
        tuple: (main_logger, performance_logger, error_logger)
    """
    main_logger = LoggerConfig.setup_logging(
        level='INFO',
        log_dir=log_dir,
        detailed=True,
        console_output=True
    )
    
    perf_logger = LoggerConfig.setup_performance_logger(log_dir)
    error_logger = LoggerConfig.setup_error_logger(log_dir)
    
    return main_logger, perf_logger, error_logger

def setup_development_logging() -> logging.Logger:
    """Setup logging optimized for development"""
    return LoggerConfig.setup_logging(
        level='DEBUG',
        detailed=True,
        console_output=True,
        log_file='development.log'
    )

# Module-level loggers for common use cases
def get_module_logger(module_name: str) -> logging.Logger:
    """Get a logger for a specific module"""
    return LoggerConfig.get_logger(module_name)

def get_performance_logger() -> logging.Logger:
    """Get the performance logger"""
    return logging.getLogger('performance')

def get_error_logger() -> logging.Logger:
    """Get the error logger"""
    return logging.getLogger('errors')
