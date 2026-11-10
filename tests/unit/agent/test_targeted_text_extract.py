"""Unit tests for the targeted text extraction node."""
from unittest.mock import MagicMock
from bson import ObjectId

from filings_agent.nodes.targeted_extract import make_targeted_extract_node


def test_targeted_extract_skips_when_no_missing_concepts():
    mock_norm = MagicMock()
    mock_repo = MagicMock()
    mock_norm._get_concept_repo_by_form_type.return_value = mock_repo
    # Return stored targets
    mock_repo.collection.find.return_value = [
        {"_id": ObjectId(), "concept": "us-gaap:Revenues", "path": "001", "label": "Revenue"}
    ]

    bundle = MagicMock()
    bundle.statement_type = "income"
    # Extracted concepts has Revenues with value
    bundle.concepts = [{"concept": "us-gaap:Revenues", "value": 100000.0}]
    bundle.dimensional_concepts = []

    state = {
        "status": "normalized",
        "cik": "0001326801",
        "ticker": "META",
        "form_type": "10-Q",
        "bundles": [bundle],
    }

    node = make_targeted_extract_node(mock_norm)
    out = node(state)
    assert out.get("status") == "normalized"  # Passthrough without error
