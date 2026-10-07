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

    def test_custom_header_adopts_legacy_unparented_row(self, mocker):
        """A parentless legacy header should be repaired, not duplicated."""
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        service.concept_cache = {}

        legacy_id = ObjectId()
        legacy = {
            '_id': legacy_id,
            'concept': 'custom:GeographicalRev',
            'label': 'Geographical Revenue',
            'path': '001.001',
            'dimension_concept': False,
            # Parent is absent: this is the legacy shape that missed the
            # parent-scoped find_existing() lookup and caused a new insert.
        }
        repo = Mock()
        repo.find_existing.return_value = None
        repo.collection.find.return_value = [legacy]
        mocker.patch.object(service, '_get_concept_repo_by_form_type', return_value=repo)
        create = mocker.patch.object(service, '_create_concept')

        item = {
            'concept': 'custom:GeographicalRev',
            'label': 'Geographical Revenue',
            'parent_concept': 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
            'path': '001.001',
            'order_key': 'a',
            'abstract': True,
        }
        result = service._get_or_create_concept(
            '0001773751', 'income', item, '10-Q'
        )

        assert result == legacy_id
        create.assert_not_called()
        repo.collection.update_one.assert_called_once_with(
            {'_id': legacy_id},
            {'$set': {
                'parent_concept': item['parent_concept'],
                'path': '001.001',
                'order_key': 'a',
            }},
        )

    def test_ambiguous_legacy_custom_headers_do_not_create_another_row(self, mocker):
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        service.concept_cache = {}

        repo = Mock()
        repo.find_existing.return_value = None
        repo.collection.find.return_value = [
            {'_id': ObjectId(), 'path': '001.001'},
            {'_id': ObjectId(), 'path': '002.001'},
        ]
        mocker.patch.object(service, '_get_concept_repo_by_form_type', return_value=repo)
        create = mocker.patch.object(service, '_create_concept')

        with pytest.raises(ValueError, match='refusing to insert another duplicate'):
            service._get_or_create_concept(
                '0001773751', 'income',
                {'concept': 'custom:GeographicalRev',
                 'parent_concept': 'us-gaap:Revenue', 'path': '003.001'},
                '10-Q',
            )
        create.assert_not_called()

    def test_dimensional_member_reuses_row_when_parent_id_drifted(self, mocker):
        """A stale parent ObjectId must not duplicate the same member row."""
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)
        service.concept_cache = {}

        parent_id, old_parent_id, member_id = ObjectId(), ObjectId(), ObjectId()
        parent_name = 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'
        legacy = {
            '_id': member_id,
            'concept': 'country:US',
            'concept_name': parent_name,
            'concept_id': old_parent_id,
            'dimension_concept': True,
            'dimensions': {'StatementGeographicalAxis': 'US'},
            'dimension_details': {
                'StatementGeographicalAxis': {
                    'member_qname': 'country:US',
                    'member_label': 'UNITED STATES',
                },
            },
            'path': '001.001.001',
            'order_key': 'b',
        }
        repo = Mock()
        repo.find_dimensional_existing.return_value = None
        repo.find_dimensional_candidates_by_parent_name.return_value = [legacy]
        repo.collection.find_one.return_value = {
            '_id': parent_id, 'concept': parent_name, 'path': '001'
        }
        mocker.patch.object(service, '_get_concept_repo_by_form_type', return_value=repo)
        create = mocker.patch.object(service, '_create_dimensional_concept')
        mocker.patch.object(
            service, '_determine_segment_info',
            return_value=('geographic_segment', 'country:US'),
        )

        result = service._get_or_create_dimensional_concept(
            concept_id=parent_id,
            company_cik='0001773751',
            statement_type='income',
            dimension_data={
                'concept_name': parent_name,
                'dimensions': {'StatementGeographicalAxis': 'US'},
                'dimension_details': {
                    'StatementGeographicalAxis': {
                        'member_qname': 'country:US',
                        'member_label': 'UNITED STATES',
                    },
                },
            },
            form_type='10-Q',
            assigned_path='001.001.001',
            assigned_order_key='b',
        )

        assert result == member_id
        create.assert_not_called()
        updates = repo.collection.update_one.call_args.args[1]['$set']
        assert updates['concept_id'] == parent_id
        assert updates['parent_concept'] == parent_name
        assert updates['parent_path'] == '001'
        assert updates['dimension_signature'] == 'statementgeographical=country:us'

    def test_dimensional_duplicates_are_coalesced_with_value_history(self, mocker):
        mocker.patch('data_normalization_service.services.normalization_service.DatabaseConnection')
        config = Mock(spec=AppConfig)
        config.database = Mock()
        service = FinancialNormalizationService(config)

        winner_id, loser_id = ObjectId(), ObjectId()
        old_unique, old_collision = ObjectId(), ObjectId()
        value_collection = Mock()
        value_collection.find.return_value = [
            {
                '_id': old_unique, 'cik': '0001773751', 'dimension_value': True,
                'dimensional_concept_id': loser_id, 'concept_id': loser_id,
                'reporting_period': {'fiscal_year': 2025, 'quarter': 2},
            },
            {
                '_id': old_collision, 'cik': '0001773751', 'dimension_value': True,
                'dimensional_concept_id': loser_id, 'concept_id': loser_id,
                'reporting_period': {'fiscal_year': 2026, 'quarter': 2},
            },
        ]
        value_collection.find_one.side_effect = [
            None,
            {'_id': ObjectId()},  # winner already owns 2026 Q2
        ]
        concept_collection = Mock()
        concept_collection.delete_one.return_value = Mock(deleted_count=1)

        service._coalesce_dimensional_concept_rows(
            Mock(collection=concept_collection),
            Mock(collection=value_collection),
            winner_id,
            [{'_id': loser_id}],
            '0001773751',
        )

        value_collection.update_one.assert_called_once_with(
            {'_id': old_unique},
            {'$set': {
                'concept_id': winner_id,
                'dimensional_concept_id': winner_id,
            }},
        )
        value_collection.delete_one.assert_called_once_with({'_id': old_collision})
        concept_collection.delete_one.assert_called_once_with({'_id': loser_id})

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
        # Dedup now uses the repository finder (the DB unique index key):
        # (cik, concept_id, fiscal_year[, quarter]).
        mock_value_repo.find_existing_value.return_value = {
            '_id': ObjectId(),
            'concept_id': ObjectId(),
            'value': 1000000,
            'accession_number': 'old-acc',
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
        
        # Verify: dedup runs through the repository finder (the DB unique-index
        # key) and nothing is inserted because the period already exists.
        mock_value_repo.find_existing_value.assert_called_once()
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
        
        # Mock the value repository: the index-aligned finder reports no row
        # for this period, so the value is inserted.
        mock_value_repo = Mock()
        mock_value_repo.find_existing_value.return_value = None
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
        
        # Verify: the finder reports no row for this period, so we insert.
        mock_value_repo.find_existing_value.assert_called_once()
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
        '..', '..', '..',
        'normalization', 'docs',
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
