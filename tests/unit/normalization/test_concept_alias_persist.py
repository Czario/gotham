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

