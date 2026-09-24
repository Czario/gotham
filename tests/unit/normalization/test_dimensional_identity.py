"""Dimensional concept identity must not include the filing-specific context.

Regression: `context_id` was part of the lookup, so the same member under the
same parent was re-created on every filing.  Those duplicate rows then shared a
materialised path (path collisions) and bloated the tree.
"""
from bson import ObjectId

from data_normalization_service.database.database import ConceptRepository


class _RecordingCollection:
    def __init__(self):
        self.queries = []

    def find_one(self, query):
        self.queries.append(query)
        return None


def _repo():
    repo = object.__new__(ConceptRepository)
    repo._collection = _RecordingCollection()
    return repo


def test_context_id_is_not_part_of_the_dim_identity():
    repo = _repo()
    parent = ObjectId()
    repo.find_dimensional_existing(
        "0000320193", "income", "product_service", "aapl:IPhoneMember",
        parent, context_id="filing-specific-ctx", dimension_signature="axis=member",
    )
    query = repo._collection.queries[0]
    assert "context_id" not in query
    assert query["dimension_signature"] == "axis=member"
    assert query["concept_id"] == parent


def test_legacy_rows_without_signature_are_still_reused():
    repo = _repo()
    parent = ObjectId()
    repo.find_dimensional_existing(
        "0000320193", "income", "product_service", "aapl:IPhoneMember",
        parent, dimension_signature="axis=member",
    )
    # first attempt by signature, then a legacy fallback that requires the field
    # to be absent so we reuse (rather than duplicate) pre-signature rows
    assert len(repo._collection.queries) == 2
    assert repo._collection.queries[1]["dimension_signature"] == {"$exists": False}
