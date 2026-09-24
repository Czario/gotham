"""Newest-period direction for concept resolution.

The agent decides only EQUIVALENCE.  Which tag name survives is a deterministic
period comparison: the tag belonging to the newest reporting period wins.

  * incoming period NEWER than the stored concept's newest value -> PROMOTE
    (move the stored values onto the incoming tag; delete the stored row);
  * incoming OLDER or equal, or direction unknowable -> ATTACH (historical
    behaviour: incoming values attach to the stored concept);
  * the decision is scoped to one form type — 10-K and 10-Q never cross.
"""
from datetime import datetime, timezone

from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.nodes.concept_resolve import (
    _period_key,
    _quarter_number,
    concept_promotion_enabled,
    make_concept_resolve_node,
)

OLD_TAG = "us-gaap:Revenues"
NEW_TAG = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


# ── fakes ───────────────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class _ConceptCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, *a, **k):
        return list(self._docs)


class _ConceptRepo:
    def __init__(self, docs):
        self.collection = _ConceptCollection(docs)


class _ValueCollection:
    def __init__(self, by_concept):
        self._by = by_concept or {}

    def find(self, query, projection=None):
        return _Cursor(self._by.get(query.get("concept_id"), []))


class _ValueRepo:
    def __init__(self, by_concept):
        self.collection = _ValueCollection(by_concept)


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = docs or []
        self.upserts = []
        self.deletes = []

    def find(self, *a, **k):
        return list(self._docs)

    def find_one(self, *a, **k):
        return None

    def create_index(self, *a, **k):
        pass

    def update_one(self, query, update, **k):
        self.upserts.append((query, update))
        return type("R", (), {"modified_count": 1, "matched_count": 1})()

    def update_many(self, query, update, **k):
        return type("R", (), {"modified_count": 0})()

    def delete_many(self, query, **k):
        self.deletes.append(query)
        return type("R", (), {"deleted_count": 0})()


class _FakeTargetDB:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = _FakeCollection()
        return self.collections[name]


class _FakeDbConnection:
    def __init__(self):
        self.target_db = _FakeTargetDB()


class _PromoService:
    def __init__(self, stored, values_by_concept=None):
        self.db_connection = _FakeDbConnection()
        self._stored = stored
        self._values = values_by_concept or {}

    def _get_concept_repo_by_form_type(self, form_type):
        return _ConceptRepo(self._stored)

    def _get_value_repo_by_form_type(self, form_type):
        return _ValueRepo(self._values)


class _StubChat:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content="", tool_calls=[{
            "name": "finalize_concepts",
            "args": {"result_json": '{"confirmed": true}'},
            "id": "c1",
        }])


def _bundle(period, form_type="10-Q", concepts=None):
    return StatementBundle(
        company_cik="0000789019",
        statement_type="income",
        form_type=form_type,
        reporting_period=period,
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=concepts or [
            {"concept": NEW_TAG, "label": "Revenue from contract", "value": 1000.0},
        ],
        abstract_concepts=[],
    )


def _stored_revenue(concept=OLD_TAG):
    return [{"_id": ObjectId(), "concept": concept, "label": "Revenues",
             "path": "001", "order_key": "a"}]


def _install_decision(monkeypatch, target):
    from filings_agent.agent import tools as agent_tools

    real_build = agent_tools.build_concept_tools

    def build(stored, row, decisions_box):
        tools = real_build(stored, row, decisions_box)
        decisions_box[NEW_TAG] = {"same_as": target, "reason": "same revenue line"}
        return tools

    monkeypatch.setattr(agent_tools, "build_concept_tools", build)


def _tagged(bundle, name):
    return next((c for c in bundle.concepts if c.get("concept") == name), None)


# ── period helpers ──────────────────────────────────────────────────────────


def test_period_key_ordering():
    assert _quarter_number("Q2") == 2
    assert _quarter_number(3) == 3
    assert _quarter_number(None) == 0
    assert _period_key({"fiscal_year": 2025}) == 20250
    assert _period_key({"fiscal_year": 2025, "quarter": 1}) < _period_key(
        {"fiscal_year": 2025, "quarter": 2}
    )
    assert _period_key({"fiscal_year": 2024, "quarter": 4}) < _period_key(
        {"fiscal_year": 2025, "quarter": 1}
    )
    assert _period_key({}) is None


# ── node behaviour ──────────────────────────────────────────────────────────


def test_newer_incoming_period_promotes_the_new_tag(monkeypatch):
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    stored = _stored_revenue()
    values = {stored[0]["_id"]: [{"reporting_period": {"fiscal_year": 2025, "quarter": 1}}]}
    svc = _PromoService(stored, values)
    _install_decision(monkeypatch, OLD_TAG)

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    out = make_concept_resolve_node(svc, chat_llm=_StubChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    promos = out["concept_promotions"]
    assert len(promos) == 1
    assert promos[0]["from_concept"] == OLD_TAG
    assert promos[0]["to_concept"] == NEW_TAG
    # A promoted name is NOT annotated onto the loser — it will be written
    # under its own (winning) tag and the loser is deleted at persist time.
    assert _tagged(bundle, NEW_TAG).get("_concept_target") is None
    assert out["concept_resolution"]["promotions"] == 1
    assert out["concept_resolution"]["aliases"] == 0
    # The promoted name must not be recorded as an alias onto the loser.
    assert svc.db_connection.target_db["concept_aliases"].upserts == []


def test_older_incoming_period_attaches_to_stored_tag(monkeypatch):
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    stored = _stored_revenue()
    values = {stored[0]["_id"]: [{"reporting_period": {"fiscal_year": 2025, "quarter": 2}}]}
    svc = _PromoService(stored, values)
    _install_decision(monkeypatch, OLD_TAG)

    bundle = _bundle({"fiscal_year": 2025, "quarter": 1})
    out = make_concept_resolve_node(svc, chat_llm=_StubChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert out["concept_promotions"] == []
    assert _tagged(bundle, NEW_TAG)["_concept_target"] == OLD_TAG
    assert out["concept_resolution"]["aliases"] == 1


def test_unknown_stored_period_attaches_safely(monkeypatch):
    """No value period available -> keep the historical attach behaviour."""
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    stored = _stored_revenue()
    svc = _PromoService(stored, values_by_concept={})  # no stored values
    _install_decision(monkeypatch, OLD_TAG)

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    out = make_concept_resolve_node(svc, chat_llm=_StubChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert out["concept_promotions"] == []
    assert _tagged(bundle, NEW_TAG)["_concept_target"] == OLD_TAG


def test_cached_alias_with_newer_period_promotes_without_llm(monkeypatch):
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    stored = _stored_revenue()
    values = {stored[0]["_id"]: [{"reporting_period": {"fiscal_year": 2025, "quarter": 1}}]}
    svc = _PromoService(stored, values)
    svc.db_connection.target_db["concept_aliases"]._docs = [{
        "cik": "0000789019", "statement_type": "income", "form_type": "10-Q",
        "concept": NEW_TAG, "target_concept": OLD_TAG,
    }]
    chat = _StubChat()

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    out = make_concept_resolve_node(svc, chat_llm=chat)({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert chat.calls == 0  # cache hit — zero LLM calls
    assert len(out["concept_promotions"]) == 1
    assert _tagged(bundle, NEW_TAG).get("_concept_target") is None


def test_promotion_disabled_attaches_even_when_newer(monkeypatch):
    """The promotion kill switch keeps the historical attach behaviour."""
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CONCEPT_PROMOTION_ENABLED", "0")
    stored = _stored_revenue()
    values = {stored[0]["_id"]: [{"reporting_period": {"fiscal_year": 2025, "quarter": 1}}]}
    svc = _PromoService(stored, values)
    _install_decision(monkeypatch, OLD_TAG)

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    out = make_concept_resolve_node(svc, chat_llm=_StubChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert out["concept_promotions"] == []
    assert _tagged(bundle, NEW_TAG)["_concept_target"] == OLD_TAG


def test_promotion_flag_default_on():
    import os

    os.environ.pop("CONCEPT_PROMOTION_ENABLED", None)
    assert concept_promotion_enabled() is True
    os.environ["CONCEPT_PROMOTION_ENABLED"] = "0"
    assert concept_promotion_enabled() is False
    os.environ.pop("CONCEPT_PROMOTION_ENABLED", None)


def test_same_period_does_not_promote(monkeypatch):
    """Equal periods: the stored tag stays (strictly newer is required)."""
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")
    stored = _stored_revenue()
    values = {stored[0]["_id"]: [{"reporting_period": {"fiscal_year": 2025, "quarter": 2}}]}
    svc = _PromoService(stored, values)
    _install_decision(monkeypatch, OLD_TAG)

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    out = make_concept_resolve_node(svc, chat_llm=_StubChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert out["concept_promotions"] == []
    assert _tagged(bundle, NEW_TAG)["_concept_target"] == OLD_TAG


# ── persist wiring ──────────────────────────────────────────────────────────


def test_persist_applies_promotion_before_writing(monkeypatch):
    """The persister merges the older concept BEFORE writing the bundle, so the
    write lands under the promoted (winning) tag.  Scoped to the write plan."""
    from filings_agent.nodes.persist import make_persist_node

    events = []

    class _Svc:
        def promote_concept(self, cik, statement_type, form_type, from_concept, to_concept):
            events.append(("promote", from_concept, to_concept))
            return {"values_moved": 4, "deleted": True}

        def persist_statement_bundle(self, bundle, **kwargs):
            events.append(("persist", bundle.statement_type))
            return True

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2})
    state = {
        "cik": "0000789019",
        "accession_number": "acc-1",
        "bundles": [bundle],
        "findings": [],
        "write_plan": {"action": "write", "statements": ["income"],
                       "decided_by": "policy", "reason": "ok"},
        "concept_promotions": [{
            "cik": "0000789019", "statement_type": "income", "form_type": "10-Q",
            "from_concept": OLD_TAG, "to_concept": NEW_TAG,
        }],
    }

    out = make_persist_node(_Svc())(state)

    assert out["status"] == "saved"
    assert events == [("promote", OLD_TAG, NEW_TAG), ("persist", "income")]
    assert out["persist_receipt"]["promotions"][0]["values_moved"] == 4


def test_persist_skips_promotion_outside_the_write_plan():
    from filings_agent.nodes.persist import make_persist_node

    called = []

    class _Svc:
        def promote_concept(self, *a, **k):
            called.append(a)
            return {}

        def persist_statement_bundle(self, bundle, **kwargs):
            return True

    bundle = _bundle({"fiscal_year": 2025, "quarter": 2}, form_type="10-Q")
    state = {
        "cik": "0000789019", "bundles": [bundle], "findings": [],
        "write_plan": {"action": "write", "statements": ["income"],
                       "decided_by": "policy", "reason": "ok"},
        "concept_promotions": [{
            "cik": "0000789019", "statement_type": "balancesheet",
            "form_type": "10-Q", "from_concept": OLD_TAG, "to_concept": NEW_TAG,
        }],
    }
    make_persist_node(_Svc())(state)
    assert called == []
