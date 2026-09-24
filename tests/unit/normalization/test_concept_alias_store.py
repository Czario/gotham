"""Durable alias store — promotion re-keying.

When the newest-period concept wins, the store must stop resolving members to
the loser and start resolving them to the winner, the winner must no longer
alias to anything, and the old tag must keep pointing at the winner so a future
filing reporting it costs zero LLM calls.
"""
from data_normalization_service.database.concept_aliases import ConceptAliasStore

OLD_TAG = "us-gaap:Revenues"
NEW_TAG = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


class _Coll:
    def __init__(self):
        self.updates_many = []
        self.deletes = []
        self.upserts = []

    def update_many(self, query, update):
        self.updates_many.append((query, update))
        return type("R", (), {"modified_count": 2})()

    def delete_many(self, query):
        self.deletes.append(query)
        return type("R", (), {"deleted_count": 1})()

    def update_one(self, query, update, **kwargs):
        self.upserts.append((query, update))
        return type("R", (), {"modified_count": 1, "matched_count": 1})()

    def find_one(self, *a, **k):
        return None

    def find(self, *a, **k):
        return []

    def create_index(self, *a, **k):
        pass


class _TargetDB:
    def __init__(self):
        self._c = _Coll()

    def __getitem__(self, name):
        return self._c


class _Conn:
    def __init__(self):
        self.target_db = _TargetDB()


def _store():
    return ConceptAliasStore(_Conn())


def test_promote_rekeys_members_and_records_old_tag():
    store = _store()

    repointed = store.promote("0000789019", "income", "10-Q", OLD_TAG, NEW_TAG)

    assert repointed == 2
    # members that pointed at the loser now point at the winner
    query, update = store.collection.updates_many[0]
    assert query == {
        "cik": "0000789019", "statement_type": "income", "form_type": "10-Q",
        "target_concept": OLD_TAG,
    }
    assert update["$set"]["target_concept"] == NEW_TAG
    # the winner is canonical now — its own alias entry is removed
    assert store.collection.deletes[0]["concept"] == NEW_TAG
    # the old tag resolves to the winner for future filings
    query, update = store.collection.upserts[0]
    assert query["concept"] == OLD_TAG
    assert update["$set"]["target_concept"] == NEW_TAG
    assert update["$set"]["decided_by"] == "concept_promotion"


def test_promote_rejects_same_name():
    store = _store()
    assert store.promote("c", "income", "10-Q", OLD_TAG, OLD_TAG) == 0
    assert store.collection.updates_many == []
