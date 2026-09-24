"""Unified, agent-owned hierarchy — encoder + graph wiring.

The model decides; ``_materialize`` only encodes its parent/position choices
into path strings.  No validation, no fallback, no auto-override.
"""
from __future__ import annotations

from filings_agent.graph import build_filing_graph
from filings_agent.nodes.hierarchy_agent import _materialize


def test_materialize_encodes_parent_position():
    rows, _ = _materialize([
        {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
        {"concept": "custom:ProductSegmentation", "parent": "us-gaap:Revenues",
         "position": 0, "abstract": True},
        {"concept": "aapl:IPhoneMember", "parent": "custom:ProductSegmentation", "position": 0},
        {"concept": "us-gaap:GrossProfit", "parent": None, "position": 1},
    ], [], {},)

    by = {r["concept"]: r for r in rows}
    assert by["us-gaap:Revenues"]["path"] == "001"
    assert by["custom:ProductSegmentation"]["path"] == "001.001"
    assert by["aapl:IPhoneMember"]["path"] == "001.001.001"
    assert by["us-gaap:GrossProfit"]["path"] == "002"


def test_materialize_dims_under_grouping_header():
    rows, dims = _materialize([
        {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
        {"concept": "custom:ProductSegmentation", "parent": "us-gaap:Revenues",
         "position": 0, "abstract": True},
    ], [
        {"concept": "aapl:IPhoneMember", "parent_header": "custom:ProductSegmentation",
         "parent_concept": "us-gaap:Revenues", "position": 0},
    ], {})
    assert dims[0]["path"].startswith("001.001.")


def test_materialize_respects_explicit_path():
    rows, _ = _materialize([
        {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
        {"concept": "us-gaap:InterestPaidNet", "path": "555", "order_key": "a"},
    ], [], {})
    by = {r["concept"]: r for r in rows}
    assert by["us-gaap:Revenues"]["path"] == "001"
    assert by["us-gaap:InterestPaidNet"]["path"] == "555"


def test_graph_has_only_the_unified_hierarchy_path():
    graph = build_filing_graph(_DummyService())
    nodes = set(graph.get_graph().nodes)
    assert "hierarchy_agent" in nodes
    # legacy nodes are gone
    assert "resolve_hierarchy" not in nodes
    assert "hierarchy_review" not in nodes
    assert "concept_resolve" not in nodes


class _DummyService:
    def normalize_statement_to_bundle(self, *a, **k):
        return None

    def persist_statement_bundle(self, *a, **k):
        return False

    def apply_hierarchy_updates(self, *a, **k):
        return None

    def update_hierarchy_seed_metadata(self, *a, **k):
        return None


import json
from datetime import datetime, timezone

from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.nodes.hierarchy_agent import make_hierarchy_agent_node


class _StubChat:
    """Emits decide_mapping + propose_hierarchy, then finalize."""

    def __init__(self, decide=None, rows=None, dims=None):
        self.decide = decide
        self.rows = rows or []
        self.dims = dims or []
        self.step = 0

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            calls = []
            if self.decide is not None:
                calls.append({"name": "decide_mapping",
                              "args": {"concept_json": json.dumps(self.decide)}, "id": "m1"})
            calls.append({"name": "propose_hierarchy",
                          "args": {"rows_json": json.dumps(self.rows),
                                   "dims_json": json.dumps(self.dims)}, "id": "p1"})
            return AIMessage(content="", tool_calls=calls)
        return AIMessage(content="", tool_calls=[
            {"name": "finalize_hierarchy",
             "args": {"result_json": json.dumps({"confirmed": True, "notes": "ok"})}, "id": "f1"},
        ])


class _Repo:
    def __init__(self, docs):
        self.collection = type("C", (), {"find": lambda self, q=None, *a, **k: list(docs)})()


class _Service:
    def __init__(self, stored):
        self._stored = stored

    def _get_concept_repo_by_form_type(self, form_type):
        # only non-dim stored rows matter for the node's read
        return _Repo([s for s in self._stored if not s.get("dimension_concept")])


def _bundle(concepts, dims=None):
    return StatementBundle(
        company_cik="0000320193", statement_type="income", form_type="10-Q",
        reporting_period={"fiscal_year": 2026, "quarter": 3},
        created_at=datetime.now(timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=concepts, abstract_concepts=[], dimensional_concepts=dims or [],
    )


def test_unified_node_materializes_and_attaches_merge():
    stored = [{"_id": ObjectId(), "concept": "us-gaap:Revenues", "path": "001",
               "order_key": "a", "dimension_concept": False}]
    bundle = _bundle([
        {"concept": "us-gaap:SalesRevenueNet", "label": "Net sales", "value": 100.0},
        {"concept": "us-gaap:CostOfRevenue", "label": "Cost", "value": 40.0},
    ])
    node = make_hierarchy_agent_node(
        _Service(stored),
        chat_llm=_StubChat(
            decide={"concept": "us-gaap:SalesRevenueNet",
                    "same_as": "us-gaap:Revenues", "keep_tag": "stored"},
            rows=[
                {"concept": "us-gaap:SalesRevenueNet", "parent": None, "position": 0},
                {"concept": "us-gaap:CostOfRevenue", "parent": None, "position": 1},
            ],
        ),
    )
    out = node({"cik": "0000320193", "ticker": "AAPL", "status": "validated",
                "bundles": [bundle]})

    assert out["status"] == "hierarchy_agent_done"
    by = {c["concept"]: c for c in bundle.concepts}
    assert by["us-gaap:SalesRevenueNet"]["path"] == "001"
    assert by["us-gaap:CostOfRevenue"]["path"] == "002"
    assert by["us-gaap:SalesRevenueNet"].get("_concept_target") == "us-gaap:Revenues"


def test_unified_node_records_newest_tag_promotion():
    stored = [{"_id": ObjectId(), "concept": "us-gaap:Revenues", "path": "001",
               "order_key": "a", "dimension_concept": False}]
    bundle = _bundle([
        {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
         "label": "Revenue", "value": 100.0},
    ])
    node = make_hierarchy_agent_node(
        _Service(stored),
        chat_llm=_StubChat(
            decide={"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                    "same_as": "us-gaap:Revenues", "keep_tag": "incoming"},
            rows=[{"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                   "parent": None, "position": 0}],
        ),
    )
    out = node({"cik": "0000320193", "ticker": "AAPL", "status": "validated",
                "bundles": [bundle]})

    promos = out.get("concept_promotions") or []
    assert len(promos) == 1
    assert promos[0]["from_concept"] == "us-gaap:Revenues"
    assert promos[0]["to_concept"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
