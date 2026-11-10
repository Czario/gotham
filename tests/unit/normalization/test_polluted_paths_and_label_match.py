"""Tests for excluding 404/555 polluted paths, ID-based concept updates by label, and value protection."""
import pytest
from bson import ObjectId
from unittest.mock import MagicMock

from data_normalization_service.database.database import is_polluted_path, _POLLUTED_PATH_RX, ConceptRepository
from data_normalization_service.services.normalization_service import FinancialNormalizationService
from data_normalization_service.core.models import Filing, FinancialStatement


def test_is_polluted_path():
    assert is_polluted_path("404") is True
    assert is_polluted_path("555") is True
    assert is_polluted_path("001.404.002") is True
    assert is_polluted_path("001.555") is True
    assert is_polluted_path("001.002.003") is False
    assert is_polluted_path("001") is False
    assert is_polluted_path(None) is False
    assert is_polluted_path("") is False


def test_value_record_skips_polluted_path():
    service = FinancialNormalizationService.__new__(FinancialNormalizationService)
    filing_id = ObjectId()
    filing = Filing(id=filing_id, form_type="10-Q", accession_number="0001-26-0001")
    statement = FinancialStatement(
        id=ObjectId(),
        company_cik="0001326801",
        filing_id=filing_id,
        statement_type="income",
        reporting_period={"fiscal_year": 2026, "quarter": 3},
        created_at=None,
        financial_data=[],
    )
    item_404 = {"concept": "custom:Item404", "path": "404", "value": 100}
    # Should skip writing value for 404 concept
    res = service._create_value_record(ObjectId(), statement, filing, item_404, 100.0)
    assert res is None


def test_dimensional_existing_finds_by_clean_label_and_preserves_path():
    repo = ConceptRepository.__new__(ConceptRepository)
    repo._collection = MagicMock()

    stored_advertising = {
        "_id": ObjectId(),
        "cik": "0001326801",
        "statement_type": "income",
        "concept": "meta:FamilyOfAppsMember",
        "label": "Advertising ",
        "path": "001.001.001.001",
        "order_key": "b",
        "dimension_concept": True,
    }

    # First find_one (by exact concept tag) returns None
    # find (by label query) returns stored_advertising
    repo._collection.find_one.return_value = None
    repo._collection.find.return_value = [stored_advertising]

    found = repo.find_dimensional_existing(
        company_cik="0001326801",
        statement_type="income",
        segment_type="product_service",
        concept="us-gaap:AdvertisingMember",
        parent_concept_id=ObjectId(),
        dimension_signature="product=advertising",
        label="Advertising",
    )
    assert found is not None
    assert found["_id"] == stored_advertising["_id"]
    assert found["path"] == "001.001.001.001"
