"""Unit tests for configuration module."""

import pytest
import os
from unittest.mock import patch
from data_normalization_service.core.config import AppConfig, DatabaseConfig


class TestDatabaseConfig:
    """Test database configuration."""
    
    def test_database_config_creation(self):
        """Test creating database config."""
        config = DatabaseConfig(
            mongodb_uri="mongodb://localhost:27017",
            source_db_name="source_db",
            database_name="target_db"
        )
        
        assert config.mongodb_uri == "mongodb://localhost:27017"
        assert config.source_db_name == "source_db"
        assert config.database_name == "target_db"


class TestAppConfig:
    """Test application configuration."""
    
    def test_app_config_creation(self):
        """Test creating app config."""
        db_config = DatabaseConfig(
            mongodb_uri="mongodb://localhost:27017",
            source_db_name="source_db",
            database_name="target_db"
        )
        
        app_config = AppConfig(
            database=db_config,
            log_level="INFO"
        )
        
        assert app_config.database == db_config
        assert app_config.log_level == "INFO"
    
    @patch.dict(os.environ, {
        'MONGODB_URI': 'mongodb://test:27017',
        'SOURCE_DB_NAME': 'test_source',
        'DATABASE_NAME': 'test_target',
    })
    def test_from_env(self):
        """Test creating config from environment variables."""
        config = AppConfig.from_env()
        
        assert config.database.mongodb_uri == 'mongodb://test:27017'
        assert config.database.source_db_name == 'test_source'
        assert config.database.database_name == 'test_target'
    
    @patch.dict(os.environ, {}, clear=True)
    def test_from_env_missing_mongodb_uri(self):
        """Require MongoDB configuration instead of silently using defaults."""
        with pytest.raises(RuntimeError, match="MONGODB_URI"):
            AppConfig.from_env()
