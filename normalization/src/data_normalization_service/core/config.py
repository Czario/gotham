"""
Configuration management for the normalization service.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False


def load_env_file(env_path: Optional[Path] = None) -> None:
    """Load environment variables from .env file."""
    if env_path is None:
        env_path = Path(__file__).parent / '.env'
    
    if DOTENV_AVAILABLE:
        # Use python-dotenv if available
        load_dotenv(env_path)
    elif env_path.exists():
        # Fallback to manual parsing
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()


@dataclass
class DatabaseConfig:
    """Database configuration."""
    mongodb_uri: str
    database_name: str
    # Kept for backward-compat; unused in merged pipeline
    source_db_name: str = ""


@dataclass
class AppConfig:
    """Application configuration."""
    database: DatabaseConfig
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> 'AppConfig':
        """Create configuration from environment variables."""
        # Load .env file first
        load_env_file()
        
        # Get configuration from environment variables
        mongodb_uri = os.getenv('MONGODB_URI')
        if not mongodb_uri:
            raise RuntimeError("MONGODB_URI environment variable is required. Set it in your .env file.")
        # SOURCE_DB_NAME retained only for legacy runs; merged pipeline ignores it
        source_db_name = os.getenv('SOURCE_DB_NAME', '')
        database_name = os.getenv('DATABASE_NAME')
        if not database_name:
            raise RuntimeError("DATABASE_NAME environment variable is required. Set it in your .env file.")
        
        return cls(
            database=DatabaseConfig(
                mongodb_uri=mongodb_uri,
                database_name=database_name,
                source_db_name=source_db_name
            )
        )
