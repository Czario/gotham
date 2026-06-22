"""Integration tests for the normalization service."""

import pytest
import os
from unittest.mock import Mock, patch
from data_normalization_service.services.normalization_service import FinancialNormalizationService
from data_normalization_service.core.config import AppConfig, DatabaseConfig


class TestNormalizationServiceIntegration:
    """Integration tests for the normalization service."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return AppConfig(
            database=DatabaseConfig(
                mongodb_uri="mongodb://localhost:27017",
                source_db_name="test_source",
                database_name="test_target"
            ),
            log_level="DEBUG"
        )
    
    @patch('data_normalization_service.services.normalization_service.DatabaseConnection')
    @patch('data_normalization_service.services.normalization_service.DatabaseTracker')
    def test_service_initialization(self, mock_tracker, mock_db_connection, mock_config):
        """Test service initialization with mocked dependencies."""
        # Mock the database connection and tracker
        mock_db_connection.return_value = Mock()
        mock_tracker.return_value = Mock()
        
        service = FinancialNormalizationService(mock_config)
        
        assert service.config == mock_config
        assert not service.taxonomy_manager  # Should be None when disabled
        mock_db_connection.assert_called_once()
        mock_tracker.assert_called_once()
    
    @patch('data_normalization_service.services.normalization_service.DatabaseConnection')
    @patch('data_normalization_service.services.normalization_service.DatabaseTracker')
    @patch('data_normalization_service.services.normalization_service.get_taxonomy_manager')
    def test_service_initialization_with_taxonomy(self, mock_get_taxonomy, mock_tracker, mock_db_connection, mock_config):
        """Test service initialization with taxonomy enabled."""
        # Mock the dependencies
        mock_db_connection.return_value = Mock()
        mock_tracker.return_value = Mock()
        mock_taxonomy = Mock()
        mock_taxonomy.get_label_stats.return_value = {"total_concepts": 1000}
        mock_get_taxonomy.return_value = mock_taxonomy
        
        service = FinancialNormalizationService(mock_config)
        
        assert service.config == mock_config
        assert service.taxonomy_manager == mock_taxonomy
        mock_get_taxonomy.assert_called_once()
    
    @patch('data_normalization_service.services.normalization_service.DatabaseConnection')
    @patch('data_normalization_service.services.normalization_service.DatabaseTracker')
    def test_get_repo_by_form_type_annual(self, mock_tracker, mock_db_connection, mock_config):
        """Test repository selection for annual forms."""
        mock_db_connection.return_value = Mock()
        mock_tracker.return_value = Mock()
        
        service = FinancialNormalizationService(mock_config)
        
        # Test annual form type
        annual_concept_repo = service._get_concept_repo_by_form_type('10-K')
        annual_value_repo = service._get_value_repo_by_form_type('10-K')
        
        assert annual_concept_repo == service.annual_concept_repo
        assert annual_value_repo == service.annual_value_repo
    
    @patch('data_normalization_service.services.normalization_service.DatabaseConnection')
    @patch('data_normalization_service.services.normalization_service.DatabaseTracker')
    def test_get_repo_by_form_type_quarterly(self, mock_tracker, mock_db_connection, mock_config):
        """Test repository selection for quarterly forms."""
        mock_db_connection.return_value = Mock()
        mock_tracker.return_value = Mock()
        
        service = FinancialNormalizationService(mock_config)
        
        # Test quarterly form type
        quarterly_concept_repo = service._get_concept_repo_by_form_type('10-Q')
        quarterly_value_repo = service._get_value_repo_by_form_type('10-Q')
        
        assert quarterly_concept_repo == service.quarterly_concept_repo
        assert quarterly_value_repo == service.quarterly_value_repo
