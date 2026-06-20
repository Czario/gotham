"""
Comprehensive logging configuration for the data normalization service.
"""
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime


class TqdmLoggingHandler(logging.Handler):
    """Custom logging handler that works with tqdm progress bars."""
    
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        
    def emit(self, record):
        try:
            from tqdm import tqdm
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except ImportError:
            # Fallback to stderr if tqdm is not available
            sys.stderr.write(self.format(record) + '\n')
        except Exception:
            self.handleError(record)


class StatusLoggingHandler(logging.Handler):
    """Special handler for status messages that always appear in console."""
    
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        
    def emit(self, record):
        try:
            from tqdm import tqdm
            msg = self.format(record)
            # Use print to ensure it appears even with progress bars
            tqdm.write(msg, file=sys.stdout)
        except ImportError:
            # Fallback to stdout if tqdm is not available
            sys.stdout.write(self.format(record) + '\n')
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


class LoggingConfig:
    """Centralized logging configuration."""
    
    def __init__(self, 
                 log_level: str = "INFO",
                 log_dir: Optional[Path] = None,
                 console_errors_only: bool = True,
                 enable_file_logging: bool = True):
        """
        Initialize logging configuration.
        
        Args:
            log_level: Base logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_dir: Directory for log files (defaults to project_root/logs)
            console_errors_only: If True, only show errors/warnings in console
            enable_file_logging: Whether to enable file logging
        """
        self.log_level = getattr(logging, log_level.upper(), logging.INFO)
        self.console_errors_only = console_errors_only
        self.enable_file_logging = enable_file_logging
        
        # Set up log directory
        if log_dir is None:
            project_root = Path(__file__).parent.parent.parent.parent
            self.log_dir = project_root / "logs"
        else:
            self.log_dir = log_dir
            
        if self.enable_file_logging:
            self.log_dir.mkdir(exist_ok=True)
        
        # Setup logging
        self._setup_logging()
    
    def _setup_logging(self):
        """Configure logging with multiple handlers."""
        # Remove existing handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Set root logger level
        root_logger.setLevel(self.log_level)
        
        # Create formatters
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        simple_formatter = logging.Formatter(
            '%(levelname)s: %(message)s'
        )
        
        console_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # Console handler (errors and warnings only if console_errors_only is True)
        console_handler = TqdmLoggingHandler()
        if self.console_errors_only:
            console_handler.setLevel(logging.WARNING)
            console_handler.setFormatter(simple_formatter)
        else:
            console_handler.setLevel(self.log_level)
            console_handler.setFormatter(console_formatter)
        
        root_logger.addHandler(console_handler)
        
        if self.enable_file_logging:
            # File handlers
            timestamp = datetime.now().strftime("%Y%m%d")
            
            # Main log file (all levels)
            main_log_file = self.log_dir / f"normalization_{timestamp}.log"
            file_handler = logging.handlers.RotatingFileHandler(
                main_log_file,
                maxBytes=50 * 1024 * 1024,  # 50MB
                backupCount=5,
                encoding='utf-8'
            )
            file_handler.setLevel(self.log_level)
            file_handler.setFormatter(detailed_formatter)
            root_logger.addHandler(file_handler)
            
            # Error log file (errors only)
            error_log_file = self.log_dir / f"errors_{timestamp}.log"
            error_handler = logging.handlers.RotatingFileHandler(
                error_log_file,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=3,
                encoding='utf-8'
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(detailed_formatter)
            root_logger.addHandler(error_handler)
            
            # Debug log file (debug and above) - only if debug level is set
            if self.log_level <= logging.DEBUG:
                debug_log_file = self.log_dir / f"debug_{timestamp}.log"
                debug_handler = logging.handlers.RotatingFileHandler(
                    debug_log_file,
                    maxBytes=100 * 1024 * 1024,  # 100MB
                    backupCount=2,
                    encoding='utf-8'
                )
                debug_handler.setLevel(logging.DEBUG)
                debug_handler.setFormatter(detailed_formatter)
                root_logger.addHandler(debug_handler)
        
        # Configure specific loggers
        self._configure_module_loggers()
        
        # Set up status loggers
        self._setup_status_loggers()
        
        # Log the configuration
        logger = logging.getLogger(__name__)
        logger.info("="*50)
        logger.info("LOGGING CONFIGURATION INITIALIZED")
        logger.info(f"Log Level: {logging.getLevelName(self.log_level)}")
        logger.info(f"Console Errors Only: {self.console_errors_only}")
        logger.info(f"File Logging: {self.enable_file_logging}")
        if self.enable_file_logging:
            logger.info(f"Log Directory: {self.log_dir}")
        logger.info("="*50)
    
    def _configure_module_loggers(self):
        """Configure specific loggers for different modules."""
        # MongoDB logger (reduce verbosity)
        pymongo_logger = logging.getLogger('pymongo')
        pymongo_logger.setLevel(logging.WARNING)
        
        # urllib3 logger (reduce verbosity)
        urllib3_logger = logging.getLogger('urllib3')
        urllib3_logger.setLevel(logging.WARNING)
        
        # requests logger (reduce verbosity)
        requests_logger = logging.getLogger('requests')
        requests_logger.setLevel(logging.WARNING)
    
    def _setup_status_loggers(self):
        """Set up status loggers that always show in console."""
        # This method can be used to pre-configure status loggers if needed
        pass
    
    def get_logger(self, name: str) -> logging.Logger:
        """Get a logger with the specified name."""
        return logging.getLogger(name)
    
    def create_progress_logger(self, name: str) -> logging.Logger:
        """Create a logger specifically for progress reporting."""
        logger = logging.getLogger(f"{name}.progress")
        
        # Add a console handler that always shows progress messages
        progress_handler = TqdmLoggingHandler()
        progress_handler.setLevel(logging.INFO)
        progress_handler.setFormatter(
            logging.Formatter('%(message)s')
        )
        
        logger.addHandler(progress_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # Don't propagate to root logger
        
        return logger
    
    def create_status_logger(self, name: str) -> logging.Logger:
        """Create a logger specifically for status messages that always appear."""
        logger = logging.getLogger(f"{name}.status")
        
        # Add a status handler that always shows messages
        status_handler = StatusLoggingHandler()
        status_handler.setLevel(logging.INFO)
        status_handler.setFormatter(
            logging.Formatter('📊 %(message)s')
        )
        
        logger.addHandler(status_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # Don't propagate to root logger
        
        return logger


def setup_logging(log_level: str = "INFO", 
                  console_errors_only: bool = True,
                  enable_file_logging: bool = True,
                  log_dir: Optional[Path] = None) -> LoggingConfig:
    """
    Setup comprehensive logging for the application.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        console_errors_only: Only show errors/warnings in console
        enable_file_logging: Enable file logging
        log_dir: Custom log directory
    
    Returns:
        LoggingConfig: The logging configuration instance
    """
    return LoggingConfig(
        log_level=log_level,
        log_dir=log_dir,
        console_errors_only=console_errors_only,
        enable_file_logging=enable_file_logging
    )


def get_logger(name: str) -> logging.Logger:
    """Convenience function to get a logger."""
    return logging.getLogger(name)


def get_status_logger(name: str) -> logging.Logger:
    """Get a status logger that always shows messages in console."""
    logger_name = f"{name}.status"
    logger = logging.getLogger(logger_name)
    
    # Check if this logger already has status handlers
    has_status_handler = any(isinstance(handler, StatusLoggingHandler) for handler in logger.handlers)
    
    if not has_status_handler:
        # Add a status handler that always shows messages
        status_handler = StatusLoggingHandler()
        status_handler.setLevel(logging.INFO)
        status_handler.setFormatter(
            logging.Formatter('📊 %(message)s')
        )
        
        logger.addHandler(status_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # Don't propagate to root logger
    
    return logger
