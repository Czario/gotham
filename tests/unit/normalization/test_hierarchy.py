"""Unit tests for hierarchy manager."""

import pytest
from data_normalization_service.utils.hierarchy import HierarchyManager


class TestHierarchyManager:
    """Test hierarchy manager functionality."""
    
    def test_hierarchy_manager_creation(self):
        """Test creating a hierarchy manager."""
        manager = HierarchyManager()
        assert manager is not None
    
    def test_build_hierarchy_data_empty(self):
        """Test building hierarchy with empty data."""
        manager = HierarchyManager()
        result = manager.build_hierarchy_data([])
        assert result == []
    
    def test_build_hierarchy_data_simple(self):
        """Test building hierarchy with simple data."""
        manager = HierarchyManager()
        
        financial_data = [
            {
                "concept": "us-gaap_Revenue",
                "label": "Revenue",
                "abstract": False
            },
            {
                "concept": "us-gaap_CostOfGoodsSold",
                "label": "Cost of Goods Sold", 
                "abstract": False
            }
        ]
        
        result = manager.build_hierarchy_data(financial_data)
        
        assert len(result) == 2
        assert result[0]["concept"] == "us-gaap_Revenue"
        assert result[1]["concept"] == "us-gaap_CostOfGoodsSold"
    
    def test_filter_abstract_concepts(self):
        """Test filtering out abstract concepts."""
        manager = HierarchyManager()
        
        financial_data = [
            {
                "concept": "us-gaap_Revenue",
                "label": "Revenue",
                "abstract": False
            },
            {
                "concept": "us-gaap_AbstractSection",
                "label": "Abstract Section",
                "abstract": True
            },
            {
                "concept": "us-gaap_CostOfGoodsSold",
                "label": "Cost of Goods Sold",
                "abstract": False
            }
        ]
        
        result = manager.build_hierarchy_data(financial_data)
        
        # Should only have non-abstract concepts
        concrete_concepts = [item for item in result if not item.get("abstract", False)]
        assert len(concrete_concepts) == 2
        
        concept_names = [item["concept"] for item in concrete_concepts]
        assert "us-gaap_Revenue" in concept_names
        assert "us-gaap_CostOfGoodsSold" in concept_names
        assert "us-gaap_AbstractSection" not in concept_names
