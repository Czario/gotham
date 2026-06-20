"""Unit tests for data models."""

import pytest
from bson import ObjectId
from datetime import datetime
from data_normalization_service.core.models import (
    ConceptKey,
    ConceptDocument,
    ValueDocument,
    Company,
    FinancialStatement
)


class TestConceptKey:
    """Test ConceptKey model."""
    
    def test_concept_key_creation(self):
        """Test creating a concept key."""
        key = ConceptKey(
            cik="0000320193",
            statement_type="income_statement",
            concept="us-gaap_Revenue"
        )
        
        assert key.cik == "0000320193"
        assert key.statement_type == "income_statement"
        assert key.concept == "us-gaap_Revenue"
    
    def test_concept_key_hash(self):
        """Test concept key hashing."""
        key1 = ConceptKey("123", "income", "revenue")
        key2 = ConceptKey("123", "income", "revenue")
        key3 = ConceptKey("456", "income", "revenue")
        
        assert hash(key1) == hash(key2)
        assert hash(key1) != hash(key3)


class TestConceptDocument:
    """Test ConceptDocument model."""
    
    def test_concept_document_creation(self):
        """Test creating a concept document."""
        doc = ConceptDocument(
            company_cik="0000320193",
            statement_type="income_statement",
            concept="us-gaap_Revenue",
            label="Revenue",
            path="001",
            order_key="a",
            abstract=False,
            dimension=False
        )
        
        assert doc.company_cik == "0000320193"
        assert doc.statement_type == "income_statement"
        assert doc.concept == "us-gaap_Revenue"
        assert doc.label == "Revenue"
        assert doc.path == "001"
        assert doc.order_key == "a"
        assert doc.abstract is False
        assert doc.dimension is False
    
    def test_concept_document_to_dict(self):
        """Test converting concept document to dictionary."""
        doc = ConceptDocument(
            company_cik="0000320193",
            statement_type="income_statement",
            concept="us-gaap_Revenue",
            label="Revenue",
            path="001",
            order_key="a"
        )
        
        result = doc.to_dict()
        
        assert result["company_cik"] == "0000320193"
        assert result["statement_type"] == "income_statement"
        assert result["concept"] == "us-gaap_Revenue"
        assert result["label"] == "Revenue"
        assert result["path"] == "001"
        assert result["order_key"] == "a"
    
    def test_dimensional_concept_document(self):
        """Test dimensional concept document."""
        doc = ConceptDocument(
            company_cik="0000320193",
            statement_type="income_statement",
            concept="us-gaap_Revenue",
            dimension_concept=True,
            concept_id=ObjectId(),
            segment_type="business_segment"
        )
        
        result = doc.to_dict()
        
        assert result["dimension_concept"] is True
        assert "concept_id" in result
        assert result["segment_type"] == "business_segment"


class TestValueDocument:
    """Test ValueDocument model."""
    
    def test_value_document_creation(self):
        """Test creating a value document."""
        concept_id = ObjectId()
        filing_id = ObjectId()
        doc = ValueDocument(
            concept_id=concept_id,
            company_cik="0000320193",
            statement_type="income_statement",
            form_type="10-K",
            filing_id=filing_id,
            reporting_period={"end_date": "2023-12-31"},
            value=1000000.0
        )
        
        assert doc.concept_id == concept_id
        assert doc.company_cik == "0000320193"
        assert doc.statement_type == "income_statement"
        assert doc.form_type == "10-K"
        assert doc.filing_id == filing_id
        assert doc.reporting_period == {"end_date": "2023-12-31"}
        assert doc.value == 1000000.0
    
    def test_value_document_to_dict(self):
        """Test converting value document to dictionary."""
        concept_id = ObjectId()
        filing_id = ObjectId()
        doc = ValueDocument(
            concept_id=concept_id,
            company_cik="0000320193",
            statement_type="income_statement",
            form_type="10-K",
            filing_id=filing_id,
            reporting_period={"end_date": "2023-12-31"},
            value=1000000.0
        )
        
        result = doc.to_dict()
        
        assert result["concept_id"] == concept_id
        assert result["company_cik"] == "0000320193"
        assert result["value"] == 1000000.0
        assert result["filing_id"] == filing_id
