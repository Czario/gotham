"""Newest-period concept promotion (service executor).

Rules under test:
  * values are MOVED to the winning concept, never deleted;
  * the losing concept ROW is hard-deleted last;
  * dimensional children are re-parented onto the winner;
  * the durable alias store is re-keyed (old tag -> new tag);
  * the whole operation is scoped to ONE form type (10-K never touches 10-Q);
  * it is idempotent / retry-safe.
"""
from unittest.mock import Mock

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


def _wire(service, mocker, *, loser, winner, values_moved=5, children_moved=0,
          deleted=1):
    concept_repo = Mock()
    concept_repo.find_existing.side_effect = [loser, winner]
    concept_repo.collection.update_many.return_value = Mock(modified_count=children_moved)
    concept_repo.collection.delete_one.return_value = Mock(deleted_count=deleted)

    value_repo = Mock()
    value_repo.collection.update_many.return_value = Mock(modified_count=values_moved)

    mocker.patch.object(
        service, "_get_concept_repo_by_form_type", return_value=concept_repo
    )
    mocker.patch.object(
        service, "_get_value_repo_by_form_type", return_value=value_repo
    )
    return concept_repo, value_repo


def test_promotion_moves_values_and_deletes_only_the_concept_row(mocker):
    service = _service(mocker)
    loser = {"_id": ObjectId(), "concept": OLD_TAG, "label": "Revenues",
             "path": "001", "order_key": "a"}
    winner = {"_id": ObjectId(), "concept": NEW_TAG, "path": "001"}
    concept_repo, value_repo = _wire(
        service, mocker, loser=loser, winner=winner, values_moved=7, children_moved=2
    )

    store = Mock()
    store.promote.return_value = 3
    service._concept_alias_store = store

    receipt = service.promote_concept(
        "0000789019", "income", "10-Q", OLD_TAG, NEW_TAG
    )

    assert receipt["values_moved"] == 7
    assert receipt["dimensional_children_moved"] == 2
    assert receipt["alias_rows_repointed"] == 3
    assert receipt["deleted"] is True

    # 1) values move onto the winner's id …
    move_call = value_repo.collection.update_many.call_args
    assert move_call[0][0] == {"concept_id": loser["_id"]}
    assert move_call[0][1] == {"$set": {"concept_id": winner["_id"]}}

    # 2) the loser concept row is deleted — and NOTHING else destructive happens
    concept_repo.collection.delete_one.assert_called_once_with({"_id": loser["_id"]})
    assert not value_repo.collection.delete_many.called
    assert not value_repo.collection.delete_one.called

    # 3) alias store re-keyed and scoped to this form type
    store.promote.assert_called_once_with(
        "0000789019", "income", "10-Q", OLD_TAG, NEW_TAG,
        reason="newest-period promotion",
    )


def test_promotion_creates_the_winner_when_absent(mocker):
    service = _service(mocker)
    loser = {"_id": ObjectId(), "concept": OLD_TAG, "label": "Revenues",
             "path": "001", "order_key": "a"}
    winner_id = ObjectId()
    winner = {"_id": winner_id, "concept": NEW_TAG, "path": "001"}

    concept_repo = Mock()
    # loser found, winner missing, winner found after creation
    concept_repo.find_existing.side_effect = [loser, None, winner]
    concept_repo.collection.update_many.return_value = Mock(modified_count=0)
    concept_repo.collection.delete_one.return_value = Mock(deleted_count=1)
    value_repo = Mock()
    value_repo.collection.update_many.return_value = Mock(modified_count=0)

    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=concept_repo)
    mocker.patch.object(service, "_get_value_repo_by_form_type", return_value=value_repo)
    create = mocker.patch.object(service, "_create_concept", return_value=winner_id)

    receipt = service.promote_concept("0000789019", "income", "10-Q", OLD_TAG, NEW_TAG)

    create.assert_called_once()
    assert create.call_args[0][0] == "0000789019"
    assert create.call_args[0][2]["concept"] == NEW_TAG
    # inherits the loser's hierarchy placement
    assert create.call_args[0][2]["path"] == "001"
    assert receipt["deleted"] is True


def test_promotion_is_idempotent_when_loser_is_gone(mocker):
    service = _service(mocker)
    concept_repo = Mock()
    concept_repo.find_existing.return_value = None  # loser already deleted
    mocker.patch.object(service, "_get_concept_repo_by_form_type", return_value=concept_repo)
    mocker.patch.object(service, "_get_value_repo_by_form_type", return_value=Mock())

    store = Mock()
    store.promote.return_value = 1
    service._concept_alias_store = store

    receipt = service.promote_concept("0000789019", "income", "10-Q", OLD_TAG, NEW_TAG)

    assert receipt["skipped"] == "from_concept_absent"
    assert receipt["deleted"] is False
    assert not concept_repo.collection.delete_one.called
    # the old tag still resolves to the winner for a future filing
    store.promote.assert_called_once()


def test_promotion_rejects_same_or_empty_names(mocker):
    service = _service(mocker)
    assert service.promote_concept("c", "income", "10-Q", OLD_TAG, OLD_TAG)["skipped"] == "invalid_names"
    assert service.promote_concept("c", "income", "10-Q", "", NEW_TAG)["skipped"] == "invalid_names"


def test_promotion_handles_unique_key_collision_without_losing_the_period(mocker):
    """When the winner already holds a period, the bulk move trips the unique
    key; the fallback keeps the winner's row and removes only the genuine
    same-period duplicate of the loser."""
    service = _service(mocker)
    loser = {"_id": ObjectId(), "concept": OLD_TAG, "path": "p"}
    winner = {"_id": ObjectId(), "concept": NEW_TAG, "path": "p"}
    concept_repo, value_repo = _wire(service, mocker, loser=loser, winner=winner)

    # bulk repoint trips the unique index …
    value_repo.collection.update_many.side_effect = RuntimeError("E11000 duplicate key")
    collide_doc = {"_id": ObjectId(), "reporting_period": {"fiscal_year": 2025, "quarter": 1}}
    fresh_doc = {"_id": ObjectId(), "reporting_period": {"fiscal_year": 2024, "quarter": 4}}
    value_repo.collection.find.return_value = [collide_doc, fresh_doc]
    # … the winner already has 2025-Q1 (collision), but not 2024-Q4
    value_repo.collection.find_one.side_effect = [{"_id": ObjectId()}, None]

    service._concept_alias_store = Mock()
    service._concept_alias_store.promote.return_value = 0

    receipt = service.promote_concept("0000789019", "income", "10-Q", OLD_TAG, NEW_TAG)

    assert receipt["values_moved"] == 1
    assert receipt["duplicate_values_dropped"] == 1
    # the period that collided keeps the winner's row; only the duplicate goes
    value_repo.collection.delete_one.assert_called_once_with({"_id": collide_doc["_id"]})
    # the non-colliding period is repointed to the winner
    value_repo.collection.update_one.assert_called_once_with(
        {"_id": fresh_doc["_id"]}, {"$set": {"concept_id": winner["_id"]}}
    )
    # the loser concept row is still deleted
    concept_repo.collection.delete_one.assert_called_once_with({"_id": loser["_id"]})


def test_promotion_is_scoped_to_the_given_form_type(mocker):
    """A 10-Q promotion asks only the quarterly repositories for data."""
    service = _service(mocker)
    _wire(
        service, mocker,
        loser={"_id": ObjectId(), "concept": OLD_TAG, "path": "p"},
        winner={"_id": ObjectId(), "concept": NEW_TAG, "path": "p"},
    )
    service._concept_alias_store = Mock()
    service._concept_alias_store.promote.return_value = 0

    service.promote_concept("0000789019", "income", "10-Q", OLD_TAG, NEW_TAG)

    service._get_concept_repo_by_form_type.assert_called_with("10-Q")
    service._get_value_repo_by_form_type.assert_called_with("10-Q")
