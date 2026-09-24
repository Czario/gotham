"""
Pytest configuration and fixtures for SEC Data Scraper v9 tests (merged project).

Covers both the main extraction service and the normalization sub-service.
"""

import pytest
import sys
import os
from unittest.mock import MagicMock, patch
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------
# The agent-owned hierarchy review calls an LLM.  Tests must never do that, so
# it is OFF for the whole suite; tests that exercise the agent path enable it
# explicitly (monkeypatch) and inject a stub model.
os.environ["HIERARCHY_AGENT_ENABLED"] = "0"

# ---------------------------------------------------------------------------
# Python path setup
# ---------------------------------------------------------------------------
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Normalization src path (editable install handles this via uv, but kept as
# fallback for bare pytest runs)
norm_src = os.path.join(project_root, "normalization", "src")
if norm_src not in sys.path:
    sys.path.insert(0, norm_src)

# ---------------------------------------------------------------------------
# Inline shared test data (previously in tests/fixtures/test_data.py)
# ---------------------------------------------------------------------------
_sample_company_data = {
    "cik": "0000320193",
    "name": "Apple Inc.",
    "ticker": "AAPL",
    "sic": "3674",
    "sic_description": "Semiconductors and Related Devices",
    "state_of_inc": "CA",
    "fiscal_year_end": "0930",
}

_sample_filing_data = {
    "cik": "0000320193",
    "accession_number": "0000320193-23-000077",
    "form_type": "10-K",
    "filing_date": "2023-11-03",
    "period_of_report": "2023-09-30",
    "fiscal_year": 2023,
    "fiscal_period": "FY",
}

_sample_financial_data = [
    {
        "concept": "us-gaap_Revenue",
        "label": "Revenue",
        "abstract": False,
        "path": "001",
        "order_key": "a",
        "periods": {"2023-09-30": {"value": 383285000000, "unit": "USD"}},
    },
    {
        "concept": "us-gaap_NetIncomeLoss",
        "label": "Net Income",
        "abstract": False,
        "path": "002",
        "order_key": "b",
        "periods": {"2023-09-30": {"value": 96995000000, "unit": "USD"}},
    },
]

_sample_dimensional_data = [
    {
        "concept": "us-gaap_Revenue",
        "label": "Revenue by Segment",
        "dimension": "us-gaap_StatementBusinessSegmentsAxis",
        "member": "aapl_MacMember",
        "value": 29357000000,
        "period": "2023-09-30",
        "unit": "USD",
    }
]

_sample_sec_api_response = {
    "cik": "320193",
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenue": {
                "label": "Revenue",
                "description": "Amount of revenue recognized",
                "units": {"USD": []},
            }
        }
    },
}

_sample_xbrl_contexts = {
    "ctx_2023": {
        "entity": "0000320193",
        "period": {"startDate": "2022-10-01", "endDate": "2023-09-30"},
        "dimensions": {},
    }
}

_sample_error_data = {
    "invalid_cik": "INVALID",
    "missing_fields": {},
    "malformed_date": "not-a-date",
}

_sample_period_data = {
    "annual": {"fiscal_year": 2023, "period_type": "FY", "end_date": "2023-09-30"},
    "quarterly": {"fiscal_year": 2023, "fiscal_quarter": "Q4", "end_date": "2023-09-30"},
}


class MockDatabase:
    """Simple in-memory mock database for tests."""

    def __init__(self):
        self._collections: dict = {}

    def __getitem__(self, name):
        if name not in self._collections:
            self._collections[name] = []
        return self._collections[name]

    def get_collection(self, name):
        return self[name]


# ---------------------------------------------------------------------------
# SEC extraction service fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_company_data():
    return _sample_company_data.copy()

@pytest.fixture
def sample_company_list():
    return [_sample_company_data.copy()]

@pytest.fixture
def sample_filing_data():
    return _sample_filing_data.copy()

@pytest.fixture
def sample_filing_list():
    return [_sample_filing_data.copy()]

@pytest.fixture
def sample_financial_data():
    return [item.copy() for item in _sample_financial_data]

@pytest.fixture
def sample_financial_dataframe():
    return pd.DataFrame(_sample_financial_data)

@pytest.fixture
def sample_dimensional_data():
    return [item.copy() for item in _sample_dimensional_data]

@pytest.fixture
def sample_dimensional_dataframe():
    return pd.DataFrame(_sample_dimensional_data)

@pytest.fixture
def mock_database():
    return MockDatabase()

@pytest.fixture
def mock_mongodb_client():
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_db.__getitem__.return_value = mock_collection
    return mock_client

@pytest.fixture
def mock_mongodb_collection():
    mock_collection = MagicMock()
    mock_collection.insert_one.return_value.inserted_id = "mock_id"
    mock_collection.find_one.return_value = None
    mock_collection.find.return_value = []
    mock_collection.update_one.return_value.modified_count = 1
    mock_collection.delete_one.return_value.deleted_count = 1
    return mock_collection

@pytest.fixture
def sample_sec_api_response():
    return _sample_sec_api_response.copy()

@pytest.fixture
def mock_sec_api_client():
    mock_client = MagicMock()
    mock_client.get_company_filings.return_value = [_sample_filing_data.copy()]
    mock_client.get_company_facts.return_value = _sample_sec_api_response.copy()
    mock_client.download_xbrl.return_value = "/path/to/xbrl.zip"
    return mock_client

@pytest.fixture
def sample_xbrl_contexts():
    return _sample_xbrl_contexts.copy()

@pytest.fixture
def mock_xbrl_extractor():
    mock_extractor = MagicMock()
    mock_extractor.extract_financial_data.return_value = [i.copy() for i in _sample_financial_data]
    mock_extractor.extract_dimensional_data.return_value = [i.copy() for i in _sample_dimensional_data]
    return mock_extractor

@pytest.fixture
def sample_error_data():
    return _sample_error_data.copy()

@pytest.fixture
def sample_period_data():
    return _sample_period_data.copy()

# ---------------------------------------------------------------------------
# Normalization service fixtures (merged from normalization/tests/conftest.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def normalization_test_config():
    """AppConfig wired to test databases."""
    from data_normalization_service.core.config import AppConfig, DatabaseConfig
    return AppConfig(
        database=DatabaseConfig(
            mongodb_uri="mongodb://localhost:27017",
            source_db_name="test_source_db",
            database_name="test_target_db",
        ),
        log_level="DEBUG",
    )

# ---------------------------------------------------------------------------
# Shared environment / infrastructure fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_config():
    """Generic test configuration settings."""
    return {
        "mongodb": {
            "connection_string": "mongodb://localhost:27017/",
            "database_name": "test_sec_scraper",
            "timeout": 5000,
        },
        "sec_api": {
            "base_url": "https://data.sec.gov",
            "user_agent": "Test SEC Scraper",
            "rate_limit": 0.1,
        },
    }

@pytest.fixture
def temp_directory(tmp_path):
    """Temporary directory for test files"""
    return tmp_path

@pytest.fixture
def sample_json_file(tmp_path):
    """Create a temporary JSON file with sample data"""
    import json
    
    json_file = tmp_path / "test_data.json"
    json_file.write_text(json.dumps(_sample_company_data, indent=2))
    
    return str(json_file)

# Mock external dependencies
@pytest.fixture
def mock_arelle():
    """Mock Arelle XBRL processor"""
    with patch('arelle.ModelManager.ModelManager') as mock_model_manager:
        mock_model_manager.return_value.load.return_value = MagicMock()
        yield mock_model_manager

@pytest.fixture
def mock_requests():
    """Mock requests library"""
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _sample_sec_api_response.copy()
        mock_response.content = b"mock file content"
        mock_get.return_value = mock_response
        yield mock_get

# Database setup/teardown fixtures
@pytest.fixture(scope="function")
def clean_database():
    """Ensure clean database state for each test"""
    # Setup: Clean state
    yield
    # Teardown: Clean up after test
    pass

@pytest.fixture(scope="session")
def mongodb_test_instance():
    """Test MongoDB instance (if available)"""
    try:
        from pymongo import MongoClient
        client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=1000)
        # Test connection
        client.server_info()
        yield client
        client.close()
    except Exception:
        # Skip tests requiring MongoDB if not available
        pytest.skip("MongoDB not available for testing")

# Performance testing fixtures
@pytest.fixture
def benchmark_data():
    """Large dataset for performance testing"""
    return {
        "companies": [_sample_company_data.copy() for _ in range(100)],
        "filings": [_sample_filing_data.copy() for _ in range(500)],
        "financial_data": _sample_financial_data * 1000
    }

# Pytest configuration
def pytest_configure(config):
    """Pytest configuration"""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as end-to-end test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "requires_network: mark test as requiring network access"
    )
    config.addinivalue_line(
        "markers", "requires_database: mark test as requiring database connection"
    )

def pytest_collection_modifyitems(config, items):
    """Modify test collection to handle markers"""
    for item in items:
        # Add 'unit' marker to tests in unit/ directory
        if "unit" in str(item.fspath):
            item.add_marker(pytest.mark.unit)
        # Add 'integration' marker to tests in integration/ directory  
        elif "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        # Add 'e2e' marker to tests in e2e/ directory
        elif "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)