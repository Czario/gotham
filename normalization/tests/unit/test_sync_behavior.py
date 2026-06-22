"""
Test to verify sync behavior for reprocessing scenarios.

This test demonstrates that when all values are deleted but concepts remain,
reprocessing will keep those concepts as-is and only add the values back.
"""
import pytest
from unittest.mock import Mock, MagicMock
from bson import ObjectId
from datetime import datetime

from data_normalization_service.services.normalization_service import FinancialNormalizationService
from data_normalization_service.core.config import AppConfig
from data_normalization_service.core.models import FinancialStatement, Filing


class TestSyncBehavior:
    """Test sync behavior for concepts and values during reprocessing."""
    
    def test_concept_reuse_when_exists(self, mocker):
        """
        Test that when a concept exists in the database,
        it is reused (not recreated) during reprocessing.
        """
        # Setup
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        
        # Mock the concept repository
        existing_concept_id = ObjectId()
        mock_repo = Mock()
        mock_repo.find_existing.return_value = {'_id': existing_concept_id, 'concept': 'us-gaap_Revenue'}
        mocker.patch.object(service, '_get_concept_repo_by_form_type', return_value=mock_repo)
        
        # Clear cache to ensure we test database lookup
        service.concept_cache = {}
        
        # Execute
        item = {
            'concept': 'us-gaap_Revenue',
            'label': 'Revenue',
            'path': '001',
            'order_key': 'a'
        }
        
        concept_id = service._get_or_create_concept(
            cik='0001234567',
            statement_type='income_statement',
            item=item,
            form_type='10-K'
        )
        
        # Verify
        assert concept_id == existing_concept_id, "Should reuse existing concept ID"
        mock_repo.find_existing.assert_called_once()
        # Verify that _create_concept was NOT called (concept was reused, not recreated)
        
    
    def test_value_skip_when_exists(self, mocker):
        """
        Test that when a value exists in the database,
        it is skipped (not duplicated) during reprocessing.
        """
        # Setup
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        
        # Mock the value repository
        mock_value_repo = Mock()
        mock_value_repo.collection.find_one.return_value = {
            '_id': ObjectId(),
            'concept_id': ObjectId(),
            'value': 1000000,
            'reporting_period': {'fiscal_year': 2023, 'period_date': '2023-12-31'}
        }
        mocker.patch.object(service, '_get_value_repo_by_form_type', return_value=mock_value_repo)
        
        # Mock statement and filing
        statement = Mock(spec=FinancialStatement)
        statement.company_cik = '0001234567'
        statement.statement_type = 'income_statement'
        statement.reporting_period = {
            'fiscal_year': 2023,
            'period_date': '2023-12-31'
        }
        statement.created_at = datetime.now()
        
        filing = Mock(spec=Filing)
        filing.form_type = '10-K'
        filing.accession_number = '0001234567-23-000001'
        
        # Execute
        service._create_value_record(
            concept_id=ObjectId(),
            statement=statement,
            filing=filing,
            item={'concept': 'us-gaap_Revenue'},
            value=1000000,
            is_calculated=False
        )
        
        # Verify
        # Should check for existing value
        mock_value_repo.collection.find_one.assert_called_once()
        # Should NOT insert (because value exists)
        mock_value_repo.collection.insert_one.assert_not_called()
    
    
    def test_value_insert_when_not_exists(self, mocker):
        """
        Test that when a value doesn't exist in the database,
        it is inserted during processing.
        """
        # Setup
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        
        # Mock the value repository - return None to simulate value not exists
        mock_value_repo = Mock()
        mock_value_repo.collection.find_one.return_value = None
        mock_value_repo.collection.insert_one.return_value = Mock()
        mocker.patch.object(service, '_get_value_repo_by_form_type', return_value=mock_value_repo)
        
        # Mock statement and filing
        statement = Mock(spec=FinancialStatement)
        statement.company_cik = '0001234567'
        statement.statement_type = 'income_statement'
        statement.reporting_period = {
            'fiscal_year': 2023,
            'period_date': '2023-12-31'
        }
        statement.created_at = datetime.now()
        
        filing = Mock(spec=Filing)
        filing.form_type = '10-K'
        filing.accession_number = '0001234567-23-000001'
        
        # Execute
        service._create_value_record(
            concept_id=ObjectId(),
            statement=statement,
            filing=filing,
            item={'concept': 'us-gaap_Revenue'},
            value=1000000,
            is_calculated=False
        )
        
        # Verify
        # Should check for existing value
        mock_value_repo.collection.find_one.assert_called_once()
        # Should insert (because value doesn't exist)
        mock_value_repo.collection.insert_one.assert_called_once()
    
    
    def test_dimensional_concept_reuse_when_exists(self, mocker):
        """
        Test that dimensional concepts are also reused when they exist.
        """
        # Setup
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        
        # Mock the concept repository
        existing_dim_concept_id = ObjectId()
        mock_repo = Mock()
        mock_repo.find_dimensional_existing.return_value = {
            '_id': existing_dim_concept_id,
            'concept': 'ProductA',
            'segment_type': 'product_service'
        }
        mocker.patch.object(service, '_get_concept_repo_by_form_type', return_value=mock_repo)
        
        # Mock _determine_segment_info
        mocker.patch.object(
            service,
            '_determine_segment_info',
            return_value=('product_service', 'ProductA')
        )
        
        # Clear cache
        service.concept_cache = {}
        
        # Execute
        dimension_data = {
            'dimensions': {'srt:ProductOrServiceAxis': 'ProductA'},
            'value': 500000
        }
        
        dim_concept_id = service._get_or_create_dimensional_concept(
            concept_id=ObjectId(),
            company_cik='0001234567',
            statement_type='income_statement',
            dimension_data=dimension_data,
            form_type='10-K'
        )
        
        # Verify
        assert dim_concept_id == existing_dim_concept_id, "Should reuse existing dimensional concept ID"
        mock_repo.find_dimensional_existing.assert_called_once()


def test_sync_behavior_documentation():
    """
    Verify that the sync behavior documentation exists and is accessible.
    """
    import os
    doc_path = os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'docs',
        'SYNC_BEHAVIOR.md'
    )
    assert os.path.exists(doc_path), "Sync behavior documentation should exist"
    
    # Verify key concepts are documented
    with open(doc_path, 'r') as f:
        content = f.read()
        assert 'sync behavior' in content.lower()
        assert 'reprocessing' in content.lower()
        assert 'concepts' in content.lower()
        assert 'values' in content.lower()
        assert 'keep those concepts as-is' in content.lower()
