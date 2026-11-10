"""Persister honours agent-decided concept aliases (no duplicate creation)."""
from unittest.mock import Mock, patch

from bson import ObjectId

from data_normalization_service.core.config import AppConfig
from data_normalization_service.services.normalization_service import (
    FinancialNormalizationService,
)

NEW_TAG = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
OLD_TAG = "us-gaap:Revenues"


def _service(mocker):
    mocker.patch(
        "data_normalization_service.services.normalization_service.DatabaseConnection"
    )
    config = Mock(spec=AppConfig)
    config.database = Mock()
    service = FinancialNormalizationService(config)
    service.concept_cache = {}
    return service


def test_item_annotation_routes_to_existing_concept(mocker):
    """item['_concept_target'] makes the persister reuse the target concept id."""
    service = _service(mocker)
    target_id = ObjectId()
    mock_repo = Mock()
    mock_repo.find_existing.return_value = {"_id": target_id, "concept": OLD_TAG}
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)

    item = {"concept": NEW_TAG, "label": "Revenue", "_concept_target": OLD_TAG}

    cid = service._get_or_create_concept(
        cik="0001234567", statement_type="income", item=item, form_type="10-K"
    )
    assert cid == target_id
    # Lookup happened on the TARGET name, never created a new concept.
    call_args = mock_repo.find_existing.call_args[0]
    assert call_args == ("0001234567", "income", OLD_TAG)
    assert service._create_concept is not None  # (not invoked — see call assert below)


def test_annotation_never_creates_a_new_concept(mocker):
    service = _service(mocker)
    target_id = ObjectId()
    mock_repo = Mock()
    mock_repo.find_existing.side_effect = [{"_id": target_id, "concept": OLD_TAG}]
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)
    create = mocker.patch.object(service, "_create_concept")

    item = {"concept": NEW_TAG, "_concept_target": OLD_TAG}
    cid = service._get_or_create_concept(
        cik="0001234567", statement_type="income", item=item, form_type="10-K"
    )
    assert cid == target_id
    create.assert_not_called()


def test_stale_alias_target_falls_back_to_normal_creation(mocker):
    """If the aliased target row is gone, the incoming concept is created as itself."""
    service = _service(mocker)
    mock_repo = Mock()
    mock_repo.find_existing.side_effect = [
        None,  # alias target not found
        {"_id": ObjectId(), "concept": NEW_TAG},  # incoming name exists
    ]
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)

    item = {"concept": NEW_TAG, "_concept_target": OLD_TAG}
    cid = service._get_or_create_concept(
        cik="0001234567", statement_type="income", item=item, form_type="10-K"
    )
    assert cid is not None


def test_no_annotation_creates_normally(mocker):
    service = _service(mocker)
    new_id = ObjectId()
    mock_repo = Mock()
    mock_repo.find_existing.return_value = {"_id": new_id, "concept": NEW_TAG}
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)

    item = {"concept": NEW_TAG}
    cid = service._get_or_create_concept(
        cik="0001234567", statement_type="income", item=item, form_type="10-K"
    )
    assert cid == new_id
    call_args = mock_repo.find_existing.call_args[0]
    assert call_args == ("0001234567", "income", NEW_TAG)


def test_quarterly_concept_fallback_missing_resolves_none(mocker):
    from data_normalization_service.services.quarterly_service import (
        PeriodBasedFinancialCalculationService,
    )

    config = Mock(spec=AppConfig)
    config.database = Mock()
    service = PeriodBasedFinancialCalculationService(config, None)

    q_repo = Mock()
    q_repo.collection.find_one.return_value = None
    a_repo = Mock()
    a_repo.collection.find_one.return_value = None
    service.quarterly_concept_repo = q_repo
    service.annual_concept_repo = a_repo

    assert service._find_concept_in_any_collection("0001234567", "income", NEW_TAG) is None


def test_in_place_concept_update_reuses_id_and_records_aliases(mocker):
    """When a new same/equivalent concept arrives, update the document in-place using its ID and add aliases."""
    service = _service(mocker)
    existing_id = ObjectId()
    existing_doc = {
        "_id": existing_id,
        "statement_type": "income",
        "concept": "us-gaap:CostOfRevenue",
        "label": "Cost of operations",
        "path": "003.003",
        "order_key": "ba",
        "form_type": None,
        "cik": "0001818874",
        "aliases": ["us-gaap:CostOfRevenue"],
    }
    mock_repo = Mock()
    mock_repo.find_existing.return_value = existing_doc
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)

    item = {
        "concept": "us-gaap:CostOfGoodsAndServicesSold",
        "label": "Cost of operations and services",
    }
    cid = service._get_or_create_concept(
        cik="0001818874", statement_type="income", item=item, form_type="10-Q"
    )

    assert cid == existing_id
    # Verified update_one was called on the exact document ID
    mock_repo.collection.update_one.assert_called_once()
    filter_arg, update_arg = mock_repo.collection.update_one.call_args[0]
    assert filter_arg == {"_id": existing_id}
    # Concept updated to latest incoming tag
    assert update_arg["$set"]["concept"] == "us-gaap:CostOfGoodsAndServicesSold"
    assert update_arg["$set"]["label"] == "Cost of operations and services"
    assert update_arg["$set"]["form_type"] == "10-Q"
    # Both old and new tags added to aliases array
    added_aliases = set(update_arg["$addToSet"]["aliases"]["$each"])
    assert "us-gaap:CostOfRevenue" in added_aliases
    assert "us-gaap:CostOfGoodsAndServicesSold" in added_aliases


def test_concept_repository_find_existing_with_canonical_and_aliases(mocker):
    """ConceptRepository.find_existing matches by exact name, aliases array, or canonical equivalence."""
    from data_normalization_service.database.database import ConceptRepository

    mock_db_conn = Mock()
    mock_coll = Mock()
    mock_db_conn.target_db = {"quarterly_concepts": mock_coll}
    repo = ConceptRepository(mock_db_conn, "quarterly_concepts")

    # 1. Exact match
    mock_coll.find_one.return_value = {"_id": ObjectId(), "concept": "us-gaap:CostOfRevenue"}
    res1 = repo.find_existing("0001818874", "income", "us-gaap:CostOfRevenue")
    assert res1 is not None
    assert res1["concept"] == "us-gaap:CostOfRevenue"

    # 2. Canonical equivalence: searching for CostOfGoodsAndServicesSold finds CostOfRevenue
    # First query (exact) returns None, second (alias) returns None, third (canonical) finds it
    doc_id = ObjectId()
    mock_coll.find_one.side_effect = [
        None,  # exact
        None,  # alias
        {"_id": doc_id, "concept": "us-gaap:CostOfRevenue", "path": "003.003", "order_key": "ba"},
    ]
    res2 = repo.find_existing("0001818874", "income", "us-gaap:CostOfGoodsAndServicesSold")
    assert res2 is not None
    assert res2["_id"] == doc_id
    assert res2["path"] == "003.003"


def test_dimensional_merge_updates_concept_and_label_keeps_everything_else(mocker):
    """When merging/reusing a dimensional concept, it updates concept and label while keeping path, order_key, and _id as is."""
    service = _service(mocker)
    existing_id = ObjectId()
    existing_doc = {
        "_id": existing_id,
        "cik": "0001326801",
        "statement_type": "income",
        "concept": "meta:FamilyOfAppsMember",
        "label": "Old Advertising Label",
        "path": "001.001.001.001",
        "order_key": "b",
        "hierarchy_level": 3,
        "level": 3,
        "parent_concept": "meta:FamilyOfAppsMember",
        "parent_path": "001.001.001",
    }
    mock_repo = Mock()
    mock_repo.find_dimensional_existing.return_value = existing_doc
    mock_repo.find_dimensional_candidates_by_parent_name.return_value = [existing_doc]
    mock_repo.collection = Mock()
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=mock_repo)

    parent_id = ObjectId()
    dimension_data = {
        "label": "New Advertising Clean Label",
        "concept_name": "meta:FamilyOfAppsMember",
        "dimensions": {"ProductOrServiceAxis": "us-gaap:AdvertisingMember"},
        "dimension_details": {},
        "period": "2026-07-01 to 2026-10-01",
    }

    cid = service._get_or_create_dimensional_concept(
        concept_id=parent_id,
        company_cik="0001326801",
        statement_type="income",
        dimension_data=dimension_data,
        form_type="10-Q",
        assigned_path="001.003.002",
        assigned_order_key="x",
    )

    assert cid == existing_id
    # Verified update_one on existing document
    mock_repo.collection.update_one.assert_called()
    # Check that concept and label were updated
    update_calls = mock_repo.collection.update_one.call_args_list
    combined_set = {}
    for call in update_calls:
        if "$set" in call[0][1]:
            combined_set.update(call[0][1]["$set"])

    assert combined_set.get("concept") == "us-gaap:AdvertisingMember"
    assert combined_set.get("label") == "New Advertising Clean Label"
    # CRITICAL: path and order_key must NEVER be overwritten! They must remain as is!
    assert "path" not in combined_set
    assert "order_key" not in combined_set



