"""Concept-resolution agent — LLM decides whether two concept names are the same.

Guardrails under test: the model may only alias onto STORED concept names (or
declare new), decisions are cached in ``concept_aliases`` so repeats cost no LLM
call, and a provider failure leaves the historical behaviour intact by default.
"""
import json
import os
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.nodes.concept_resolve import (
    _shortlist,
    _similarity,
    _tokenize,
    concept_agent_enabled,
    make_concept_resolve_node,
)


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = docs or []
        self.upserts = []

    def find(self, *a, **k):
        return list(self._docs)

    def find_one(self, *a, **k):
        return None

    def create_index(self, *a, **k):
        pass

    def update_one(self, query, update, **k):
        self.upserts.append((query, update))
        return type("R", (), {"modified_count": 1, "matched_count": 1})()


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


class _FakeRepo:
    def __init__(self, docs):
        self.collection = _FakeCollection(docs)


class _FakeService:
    """Stored income concepts for a company that NEVER used the ASC 606 tag."""

    def __init__(self, stored=None):
        self.db_connection = _FakeDbConnection()
        self._docs = stored or [
            {"concept": "us-gaap:Revenues", "label": "Revenues", "path": "001",
             "order_key": "a"},
            {"concept": "us-gaap:CostOfRevenue", "label": "Cost of revenue",
             "path": "002", "order_key": "b"},
            {"concept": "us-gaap:GrossProfit", "label": "Gross profit", "path": "003",
             "order_key": "c"},
        ]

    def _get_concept_repo_by_form_type(self, form_type):
        return _FakeRepo(self._docs)


def _bundle():
    return StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "label": "Revenue from contract with customer",
             "value": 1000.0},
            {"concept": "us-gaap:GrossProfit", "label": "Gross profit", "value": 600.0},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense", "label": "R&D",
             "value": 80.0},
        ],
        abstract_concepts=[],
    )


class StubConceptChat:
    """Finalizes immediately; the decisions are pre-filled into the tools."""

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return AIMessage(content="", tool_calls=[{
            "name": "finalize_concepts",
            "args": {"result_json": '{"confirmed": true}'},
            "id": "c1",
        }])


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("CONCEPT_AGENT_ENABLED", "1")


def install_decisions(monkeypatch, decisions: dict[str, dict]):
    """Pre-fill the decisions box the NODE passes to the tools (mirrors the
    hierarchy tests, which pre-fill the proposal the same way)."""
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_concept_tools

    def build(stored, row, decisions_box):
        tools = real_build(stored, row, decisions_box)
        decisions_box.update(decisions)
        return tools

    monkeypatch.setattr(agent_tools, "build_concept_tools", build)


def _tagged(bundle, name):
    return next((c for c in bundle.concepts if c.get("concept") == name), None)


# ── pure helpers ────────────────────────────────────────────────────────────


def test_tokenize_and_similarity():
    assert _tokenize("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax") == {
        "revenue", "from", "contract", "with", "customer", "excluding", "assessed", "tax",
    }
    assert _similarity(
        "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    ) > _similarity("us-gaap:Revenues", "us-gaap:GrossProfit")
    assert _similarity("us-gaap:A", "us-gaap:B") == 0.0


def test_shortlist_bounded_and_ranked():
    rows = [{"concept": f"us-gaap:Concept{i}"} for i in range(200)]
    rows[0]["concept"] = "us-gaap:Revenues"
    out = _shortlist(rows, ["us-gaap:Revenues"])
    assert out[0]["concept"] == "us-gaap:Revenues"
    assert len(out) == 120  # bounded for the prompt budget


# ── node behaviour ──────────────────────────────────────────────────────────


def test_agent_merges_new_tag_onto_stored_concept(enabled, monkeypatch):
    """LLM decides the ASC 606 revenue tag == stored Revenues: annotate + persist."""
    install_decisions(monkeypatch, {
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": {
            "same_as": "us-gaap:Revenues", "reason": "ASC 606 renamed total revenue"},
        "us-gaap:ResearchAndDevelopmentExpense": {
            "same_as": None, "reason": "new R&D line"},
    })
    bundle = _bundle()
    svc = _FakeService()

    node = make_concept_resolve_node(svc, chat_llm=StubConceptChat())
    out = node({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })

    assert out["concept_resolution"]["aliases"] == 1

    # The bundle item is annotated so the persister reuses the stored concept.
    tagged = _tagged(bundle, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
    assert tagged["_concept_target"] == "us-gaap:Revenues"
    # R&D decided NEW → no annotation.  GrossProfit was already stored → untouched.
    assert "_concept_target" not in _tagged(bundle, "us-gaap:ResearchAndDevelopmentExpense")
    assert "_concept_target" not in _tagged(bundle, "us-gaap:GrossProfit")

    # Decision persisted so the next filing resolves with zero LLM calls.
    store = svc.db_connection.target_db["concept_aliases"]
    assert len(store.upserts) == 1
    assert store.upserts[0][1]["$set"]["target_concept"] == "us-gaap:Revenues"


def test_invented_target_is_dropped(enabled, monkeypatch):
    """A same_as name outside the stored set is never honoured (tool + node)."""
    from filings_agent.agent.tools import build_concept_tools

    # The tool itself rejects an invented target…
    decisions: dict = {}
    tools = build_concept_tools([], [], decisions)
    reply = dict(tools)["decide_mapping"] if isinstance(tools, dict) else next(
        t for t in tools if t.name == "decide_mapping"
    )
    result = reply.invoke(json.dumps({
        "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "same_as": "us-gaap:MadeUpConcept",
    }))
    assert "NOT a stored concept" in result
    assert decisions == {}

    # …and a hostile model that bypasses the tool is neutralised by the node.
    install_decisions(monkeypatch, {
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": {
            "same_as": "us-gaap:MadeUpConcept"},
    })
    bundle = _bundle()
    out = make_concept_resolve_node(_FakeService(), chat_llm=StubConceptChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })
    assert out["status"] == "hierarchy_reviewed"
    tagged = _tagged(bundle, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
    assert "_concept_target" not in tagged  # ignored → created as its own concept


def test_repeat_filing_costs_no_llm_call(enabled):
    """A previously decided alias resolves from the cache — no agent loop.

    The bundle here contains ONLY the already-decided concept plus stored rows,
    so no NEW concept needs an LLM decision at all.
    """
    svc = _FakeService()
    svc.db_connection.target_db["concept_aliases"]._docs = [{
        "cik": "0000789019", "statement_type": "income", "form_type": "10-K",
        "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "target_concept": "us-gaap:Revenues",
    }]
    called = {"n": 0}

    class CountingChat(StubConceptChat):
        def invoke(self, messages):
            called["n"] += 1
            return super().invoke(messages)

    bundle = StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "label": "Revenue from contract with customer", "value": 1000.0},
            {"concept": "us-gaap:GrossProfit", "label": "Gross profit", "value": 600.0},
        ],
        abstract_concepts=[],
    )
    out = make_concept_resolve_node(svc, chat_llm=CountingChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })
    assert called["n"] == 0
    assert out["concept_resolution"]["from_cache"] == 1
    tagged = _tagged(bundle, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
    assert tagged["_concept_target"] == "us-gaap:Revenues"


def test_provider_failure_falls_back_by_default(enabled):
    """Provider down → incoming concepts are created as new (historical path)."""

    class Boom:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("provider down")

    bundle = _bundle()
    out = make_concept_resolve_node(_FakeService(), chat_llm=Boom())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })
    assert out["status"] == "hierarchy_reviewed"  # run continues
    assert all("_concept_target" not in c for c in bundle.concepts)


def test_provider_failure_blocks_when_policy_strict(enabled, monkeypatch):
    monkeypatch.setenv("CONCEPT_AGENT_FAILURE_POLICY", "block")

    class Boom:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("provider down")

    bundle = _bundle()
    out = make_concept_resolve_node(_FakeService(), chat_llm=Boom())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
        "bundles": [bundle],
    })
    assert out["status"] == "failed"
    assert "concept resolution not decided by the agent" in out["error"]


def test_enabled_flag_off_skips_agent():
    bundle = _bundle()

    class ExplodingChat:
        def invoke(self, messages):
            pytest.fail("agent must not run when disabled")

    os.environ["CONCEPT_AGENT_ENABLED"] = "0"
    try:
        out = make_concept_resolve_node(_FakeService(), chat_llm=ExplodingChat())({
            "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_reviewed",
            "bundles": [bundle],
        })
    finally:
        os.environ.pop("CONCEPT_AGENT_ENABLED", None)
    assert out["concept_resolution"]["skipped"] is True
    assert all("_concept_target" not in c for c in bundle.concepts)


def test_concept_agent_enabled_flag():
    os.environ.pop("CONCEPT_AGENT_ENABLED", None)
    assert concept_agent_enabled() is True
    os.environ["CONCEPT_AGENT_ENABLED"] = "0"
    assert concept_agent_enabled() is False
    os.environ.pop("CONCEPT_AGENT_ENABLED", None)