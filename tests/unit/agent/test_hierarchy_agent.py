"""P16 — agent-owned hierarchy.

The agent decides the shape of a statement tree (grouping headers, parents,
sibling order, which legacy rows to hide).  Paths and order keys are computed by
the deterministic planner, so a model can never emit a malformed tree, and an
invalid or unavailable proposal leaves the deterministic resolver's plan intact.
"""
import json
import os
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.hierarchy.planner import order_key, plan_hierarchy
from filings_agent.nodes.hierarchy_review import make_hierarchy_review_node, should_review


# ── planner: deterministic shape, model only picks structure ────────────────


def test_planner_computes_paths_and_order_keys():
    plan = plan_hierarchy([
        {"concept": "Revenues", "parent": None, "position": 0},
        {"concept": "CostOfRevenue", "parent": None, "position": 1},
        {"concept": "GrossProfit", "parent": None, "position": 2},
        {"concept": "OperatingExpenses", "parent": None, "position": 3, "abstract": True},
        {"concept": "R&D", "parent": "OperatingExpenses", "position": 0},
        {"concept": "SG&A", "parent": "OperatingExpenses", "position": 1},
    ])
    assert plan.valid, plan.errors
    placed = plan.by_concept()
    assert placed["Revenues"].path == "001"
    assert placed["OperatingExpenses"].path == "004"
    assert placed["R&D"].path == "004.001"
    assert placed["SG&A"].path == "004.002"
    assert placed["R&D"].order_key == "a" and placed["SG&A"].order_key == "b"
    assert plan.max_depth == 1


def test_planner_rejects_missing_parent():
    plan = plan_hierarchy([{"concept": "R&D", "parent": "DoesNotExist", "position": 0}])
    assert not plan.valid
    assert "neither proposed nor already stored" in " ".join(plan.errors)


def test_planner_accepts_parent_that_already_exists():
    plan = plan_hierarchy(
        [{"concept": "R&D", "parent": "OperatingExpenses", "position": 0}],
        known_concepts=["OperatingExpenses"],
    )
    assert plan.valid, plan.errors


def test_planner_rejects_duplicate_sibling_position():
    plan = plan_hierarchy([
        {"concept": "A", "parent": None, "position": 0},
        {"concept": "B", "parent": None, "position": 0},
    ])
    assert not plan.valid
    assert any("already used" in e for e in plan.errors)


def test_planner_rejects_cycles():
    plan = plan_hierarchy([
        {"concept": "A", "parent": "B", "position": 0},
        {"concept": "B", "parent": "A", "position": 0},
    ])
    assert not plan.valid
    assert any("cycle" in e for e in plan.errors)


def test_planner_rejects_duplicate_concept_and_empty():
    assert not plan_hierarchy([]).valid
    dup = plan_hierarchy([
        {"concept": "A", "parent": None, "position": 0},
        {"concept": "A", "parent": None, "position": 1},
    ])
    assert any("duplicate concept" in e for e in dup.errors)


def test_order_key_encoding_scales():
    assert order_key(0) == "a"
    assert order_key(25) == "z"
    assert order_key(26) == "aa"
    assert order_key(27) == "ab"


# ── review trigger: cost control ────────────────────────────────────────────


def test_review_triggered_for_seed_or_anomalies_only():
    assert should_review({"seeded_statement_types": ["income"]})[0] is True
    assert should_review({"integrity": {"duplicate_paths": 2}})[0] is True
    assert should_review({"integrity": {"orphans": 1}})[0] is True
    assert should_review({"conflicts": [{"concept": "x"}]})[0] is True
    # A clean reuse costs no LLM call.
    assert should_review({"integrity": {"duplicate_paths": 0, "orphans": 0}})[0] is False


# ── node: agent plan applied, guardrails enforced ───────────────────────────


def _bundle():
    return StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:Revenues", "label": "Revenue", "value": 1000.0},
            {"concept": "us-gaap:GrossProfit", "label": "Gross profit", "value": 600.0},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense", "label": "R&D", "value": 80.0},
        ],
        abstract_concepts=[],
    )


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, *a, **k):
        return list(self._docs)


class _FakeRepo:
    def __init__(self, docs):
        self.collection = _FakeCollection(docs)


class _FakeService:
    """Stored hierarchy: R&D wrongly nested under Gross Profit, plus a legacy row."""

    def __init__(self):
        self.updated = []
        self._docs = [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:GrossProfit", "path": "002", "order_key": "b"},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense", "path": "002.001",
             "order_key": "a"},
            {"concept": "custom:Segmentationold1rev", "path": "001.001", "order_key": "a"},
        ]

    def _get_concept_repo_by_form_type(self, form_type):
        return _FakeRepo(self._docs)


class StubHierarchyChat:
    """Emits the corrected tree: headers + hide the legacy row."""

    def __init__(self):
        self.payload = json.dumps([
            {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
            {"concept": "us-gaap:GrossProfit", "parent": None, "position": 1},
            {"concept": "us-gaap:OperatingExpensesAbstract", "parent": None,
             "position": 2, "abstract": True},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense",
             "parent": "us-gaap:OperatingExpensesAbstract", "position": 0},
            {"concept": "custom:Segmentationold1rev", "parent": None, "position": 9,
             "hide": True},
        ])

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return AIMessage(content="", tool_calls=[{
            "name": "finalize_hierarchy",
            "args": {"result_json": '{"confirmed": true}'},
            "id": "h1",
        }])


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HIERARCHY_AGENT_ENABLED", "1")


def test_agent_plan_is_applied_and_junk_hidden(enabled, monkeypatch):
    bundle = _bundle()
    svc = _FakeService()

    # The agent's proposal is captured through the propose_hierarchy tool.
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        stub = StubHierarchyChat()
        # Drive propose_hierarchy the way the model would.
        proposal["rows"] = json.loads(stub.payload)
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    node = make_hierarchy_review_node(svc, chat_llm=StubHierarchyChat())
    out = node({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"],
                           "integrity": {"duplicate_paths": 0, "orphans": 0}},
    })

    plan = out["hierarchy_plan"]
    assert plan["decided_by"] == "agent"
    assert plan["agent_plans"][0]["hidden"] == 1
    assert plan["agent_plans"][0]["grouping_headers"] == 1

    # R&D now hangs off the grouping header, not off Gross Profit.
    rd = next(c for c in bundle.concepts if c["concept"].endswith("ResearchAndDevelopmentExpense"))
    gross = next(c for c in bundle.concepts if c["concept"].endswith("GrossProfit"))
    assert rd["path"].startswith("003.")
    assert not rd["path"].startswith(gross["path"] + ".")

    # The legacy row is hidden (not deleted) for the stored document.
    hides = [u for u in plan["existing_updates"] if u.get("hide")]
    assert any(u["concept"] == "custom:Segmentationold1rev" for u in hides)


def test_invalid_agent_plan_blocks_the_write_by_default(enabled, monkeypatch):
    """A malformed proposal must never reach the database — and by default the
    filing is blocked rather than silently downgraded to a deterministic tree."""
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        # Broken: parent does not exist.
        proposal["rows"] = [{"concept": "us-gaap:Revenues", "parent": "Nope", "position": 0}]
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    bundle = _bundle()
    original = [c.get("path") for c in bundle.concepts]
    out = make_hierarchy_review_node(_FakeService(), chat_llm=StubHierarchyChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"],
                           "integrity": {"duplicate_paths": 0, "orphans": 0}},
    })

    assert out["status"] == "failed"
    assert "hierarchy not decided by the agent" in out["error"]
    assert out["hierarchy_review_failures"][0]["statement_type"] == "income"
    assert [c.get("path") for c in bundle.concepts] == original  # untouched


def test_invalid_agent_plan_falls_back_when_policy_says_so(enabled, monkeypatch):
    """HIERARCHY_AGENT_FAILURE_POLICY=fallback restores the old behaviour."""
    monkeypatch.setenv("HIERARCHY_AGENT_FAILURE_POLICY", "fallback")
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        proposal["rows"] = [{"concept": "us-gaap:Revenues", "parent": "Nope", "position": 0}]
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    bundle = _bundle()
    original = [c.get("path") for c in bundle.concepts]
    out = make_hierarchy_review_node(_FakeService(), chat_llm=StubHierarchyChat())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"],
                           "integrity": {"duplicate_paths": 0, "orphans": 0}},
    })

    assert out["status"] == "hierarchy_reviewed"
    assert out["hierarchy_plan"]["decided_by"] == "policy_fallback"
    assert out["hierarchy_plan"]["hierarchy_review_failures"]
    assert [c.get("path") for c in bundle.concepts] == original


def test_provider_failure_blocks_by_default(enabled):
    class Boom:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("provider down")

    bundle = _bundle()
    original = [c.get("path") for c in bundle.concepts]
    out = make_hierarchy_review_node(_FakeService(), chat_llm=Boom())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"],
                           "integrity": {"duplicate_paths": 0, "orphans": 0}},
    })
    assert out["status"] == "failed"
    assert "provider down" in out["error"]
    assert [c.get("path") for c in bundle.concepts] == original


def test_provider_failure_falls_back_when_policy_says_so(enabled, monkeypatch):
    monkeypatch.setenv("HIERARCHY_AGENT_FAILURE_POLICY", "fallback")

    class Boom:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("provider down")

    bundle = _bundle()
    original = [c.get("path") for c in bundle.concepts]
    out = make_hierarchy_review_node(_FakeService(), chat_llm=Boom())({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"],
                           "integrity": {"duplicate_paths": 0, "orphans": 0}},
    })
    assert out["status"] == "hierarchy_reviewed"
    assert out["hierarchy_plan"]["decided_by"] == "policy_fallback"
    assert [c.get("path") for c in bundle.concepts] == original


# ── graph wiring: a blocked hierarchy must never be written ─────────────────


def test_blocked_hierarchy_short_circuits_before_persist():
    """The agent owns the hierarchy, so a blocked statement must not be written.

    ``hierarchy_review -> validate_final`` has to be a CONDITIONAL edge: a plain
    edge would let validate_final reset the status to "validated" and the write
    would proceed with a hierarchy the agent never approved.
    """
    from unittest.mock import Mock

    from filings_agent.graph import _route_after, build_filing_graph

    edges = {(e.source, e.target) for e in build_filing_graph(Mock()).get_graph().edges}
    assert ("hierarchy_review", "concept_resolve") in edges
    assert ("concept_resolve", "validate_final") in edges
    assert ("hierarchy_review", "__end__") in edges, (
        "hierarchy_review must be able to short-circuit to END"
    )

    route = _route_after("validate_final")
    assert route({"status": "failed"}) == "__end__", "failed must not reach persist"
    assert route({"status": "hierarchy_reviewed"}) == "validate_final"
    assert route({"status": "validated"}) == "validate_final"


def test_disabled_agent_skips_the_llm(monkeypatch):
    monkeypatch.setenv("HIERARCHY_AGENT_ENABLED", "0")

    class Exploding:
        def bind_tools(self, tools):
            raise AssertionError("LLM must not be consulted when disabled")

    bundle = _bundle()
    out = make_hierarchy_review_node(_FakeService(), chat_llm=Exploding())({
        "cik": "1", "status": "hierarchy_resolved", "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"]},
    })
    assert out["hierarchy_plan"]["decided_by"] == "policy"


def test_clean_reuse_still_calls_the_agent_by_default(enabled, monkeypatch):
    """The agent owns the hierarchy: no statement is skipped as "clean"."""
    called = {"n": 0}
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools
    stub = StubHierarchyChat()

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        proposal["rows"] = json.loads(stub.payload)  # what the model would submit
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    class Counting(StubHierarchyChat):
        def bind_tools(self, tools):
            called["n"] += 1
            return super().bind_tools(tools)

    out = make_hierarchy_review_node(_FakeService(), chat_llm=Counting())({
        "cik": "1", "status": "hierarchy_resolved", "bundles": [_bundle()],
        "hierarchy_plan": {"integrity": {"duplicate_paths": 0, "orphans": 0}},
    })
    assert called["n"] == 1, "a clean reuse must still be decided by the agent"
    assert out["status"] == "hierarchy_reviewed"
    assert out["hierarchy_plan"]["decided_by"] == "agent"


def test_agent_that_finalizes_without_proposing_blocks(enabled):
    """Finalizing with no proposal means the hierarchy was never decided."""
    out = make_hierarchy_review_node(_FakeService(), chat_llm=StubHierarchyChat())({
        "cik": "1", "status": "hierarchy_resolved", "bundles": [_bundle()],
        "hierarchy_plan": {"integrity": {"duplicate_paths": 0, "orphans": 0}},
    })
    assert out["status"] == "failed"
    assert "agent proposed no rows" in out["error"]


def test_clean_reuse_skips_the_llm_when_always_is_off(enabled, monkeypatch):
    monkeypatch.setenv("HIERARCHY_AGENT_ALWAYS", "0")

    class Exploding:
        def bind_tools(self, tools):
            raise AssertionError("clean reuse must not call the LLM")

    out = make_hierarchy_review_node(_FakeService(), chat_llm=Exploding())({
        "cik": "1", "status": "hierarchy_resolved", "bundles": [_bundle()],
        "hierarchy_plan": {"integrity": {"duplicate_paths": 0, "orphans": 0}},
    })
    assert out["hierarchy_plan"]["decided_by"] == "policy"


# ── regression: the two bugs the demo exposed ──────────────────────────────


def _svc_with_ids():
    """Stored rows WITH _id — apply_hierarchy_updates targets _id."""
    docs = [
        {"_id": ObjectId(), "concept": "us-gaap:GrossProfit", "path": "003",
         "order_key": "c"},
        {"_id": ObjectId(), "concept": "us-gaap:ResearchAndDevelopmentExpense",
         "path": "003.001", "order_key": "a"},
        {"_id": ObjectId(), "concept": "custom:ServerAndToolsSegold", "path": "001.003",
         "order_key": "c"},
    ]

    class _Col:
        def __init__(self, d):
            self._d = d

        def find(self, *a, **k):
            return list(self._d)

    class _Svc:
        def _get_concept_repo_by_form_type(self, ft):
            return type("R", (), {"collection": _Col(docs)})()

    return _Svc(), docs


PLAN_WITH_HIDE = [
    {"concept": "us-gaap:GrossProfit", "parent": None, "position": 0},
    {"concept": "us-gaap:OperatingExpensesAbstract", "parent": None, "position": 1,
     "abstract": True},
    {"concept": "us-gaap:ResearchAndDevelopmentExpense",
     "parent": "us-gaap:OperatingExpensesAbstract", "position": 0},
    {"concept": "custom:ServerAndToolsSegold", "parent": None, "position": 9,
     "hide": True},
]


def _run_with_plan(monkeypatch, plan):
    from filings_agent.agent import tools as agent_tools

    monkeypatch.setattr(
        agent_tools, "build_hierarchy_tools",
        lambda stored, filing, proposal: (proposal.update({"rows": plan}), [])[1],
    )
    svc, docs = _svc_with_ids()
    bundle = _bundle()
    out = make_hierarchy_review_node(svc, chat_llm=StubHierarchyChat())({
        "cik": "1", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"], "integrity": {}},
    })
    return out["hierarchy_plan"], bundle, docs


def test_stored_row_updates_carry_the_id(enabled, monkeypatch):
    """Dropping _id silently skipped every stored-row change (re-path + hide)."""
    plan, _bundle_out, docs = _run_with_plan(monkeypatch, PLAN_WITH_HIDE)
    ids = {str(d["_id"]) for d in docs}
    assert plan["existing_updates"], "expected stored-row updates"
    for update in plan["existing_updates"]:
        assert update.get("_id") is not None, f"missing _id for {update['concept']}"
        assert str(update["_id"]) in ids
    hidden = [u for u in plan["existing_updates"] if u.get("hide")]
    assert [u["concept"] for u in hidden] == ["custom:ServerAndToolsSegold"]


def test_dim_sync_updates_do_not_crash_the_node(enabled, monkeypatch):
    """Regression: the dim-sync branch emits no ``hide``/``abstract``, and the
    caller indexed them — ``KeyError: 'hide'`` failed the entire node (seen three
    times in the live log, each one blocking a filing).

    These updates only get produced when the agent reviews a statement whose
    stored dimensional rows are already out of date, which always-review now
    exercises on reused statements too.
    """
    from filings_agent.agent import tools as agent_tools

    monkeypatch.setattr(
        agent_tools, "build_hierarchy_tools",
        lambda stored, filing, proposal: (proposal.update({"rows": PLAN_WITH_HIDE}), [])[1],
    )
    svc, _docs = _svc_with_ids()

    bundle = _bundle()
    # Stored dimensional row whose path no longer matches the resolved placement.
    bundle.dimensional_concepts = [{
        "concept": "us-gaap:GrossProfit",
        "parent_concept": "us-gaap:Revenues",
        "path": "007.001",
        "order_key": "a",
        "dimension_data": {"dimensions": {}, "dimension_details": {}},
    }]

    out = make_hierarchy_review_node(svc, chat_llm=StubHierarchyChat())({
        "cik": "1", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {"seeded_statement_types": ["income"], "integrity": {}},
    })

    assert out["status"] == "hierarchy_reviewed", out.get("error")
    updates = out["hierarchy_plan"]["existing_updates"]
    dim_sync = [u for u in updates if str(u.get("reason", "")).endswith("dim_sync")]
    assert dim_sync, "expected a dim-sync update to be emitted"
    # Missing hide/abstract is tolerated and must not hide the row.
    assert all(u.get("hide") is None for u in dim_sync)
    assert all(u.get("path") for u in dim_sync)


def test_new_grouping_header_is_added_to_the_bundle(enabled, monkeypatch):
    """`or []` on an empty list appended to a throwaway copy, so the header was
    never created by the persister."""
    plan, bundle, _docs = _run_with_plan(monkeypatch, PLAN_WITH_HIDE)
    assert plan["agent_plans"][0]["created_headers"] == [
        "us-gaap:OperatingExpensesAbstract"
    ]
    headers = {a["concept"]: a for a in bundle.abstract_concepts}
    assert "us-gaap:OperatingExpensesAbstract" in headers
    header = headers["us-gaap:OperatingExpensesAbstract"]
    assert header["abstract"] is True
    assert header["_hierarchy_resolved"] is True

    # …and the re-parented child now hangs off that header.
    rd = next(c for c in bundle.concepts
              if c["concept"].endswith("ResearchAndDevelopmentExpense"))
    assert rd["path"].startswith(header["path"] + ".")


# ── new concepts in later filings (LLM placement vs deterministic) ──────────


def test_review_triggered_when_new_concepts_introduced():
    plan = {
        "seeded_statement_types": [],
        "new_concepts": [{"concept": "us-gaap:BrandNewMetric", "statement_type": "income"}],
        "integrity": {"duplicate_paths": 0, "orphans": 0},
    }
    should, reason = should_review(plan)
    assert should is True
    assert "1 new concept(s) introduced" in reason


def test_resolver_detects_new_concepts_on_later_filings():
    from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles

    existing = [
        {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a", "dimension_concept": False},
    ]

    class _FakeRepo:
        collection = type("Col", (), {"find": lambda self, *a, **k: list(existing)})()

        def find_concept_reference_for_hierarchy(self, *a, **k):
            return None

    class _FakeService:
        def _get_concept_repo_by_form_type(self, ft):
            return _FakeRepo()

    bundle = StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:Revenues", "label": "Revenue", "value": 1000.0},
            {"concept": "us-gaap:BrandNewMetric", "label": "Brand New", "value": 200.0},
        ],
        abstract_concepts=[],
    )

    plan = resolve_hierarchy_bundles([bundle], _FakeService(), cik="0000789019")
    assert len(plan["new_concepts"]) == 1
    assert plan["new_concepts"][0]["concept"] == "us-gaap:BrandNewMetric"
    assert bundle.concepts[1]["is_new_concept"] is True
    assert bundle.concepts[0]["is_new_concept"] is False


def test_agent_places_new_concept_under_existing_parent(enabled, monkeypatch):
    """When a new filing introduces a new concept, the agent places it based on existing hierarchy."""
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools

    existing_docs = [
        {"_id": ObjectId(), "concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
        {"_id": ObjectId(), "concept": "us-gaap:GrossProfit", "path": "002", "order_key": "b"},
        {"_id": ObjectId(), "concept": "us-gaap:OperatingExpensesAbstract", "path": "003", "order_key": "c", "abstract": True},
        {"_id": ObjectId(), "concept": "us-gaap:ResearchAndDevelopmentExpense", "path": "003.001", "order_key": "a"},
    ]

    class _CustomService:
        def _get_concept_repo_by_form_type(self, ft):
            return _FakeRepo(existing_docs)

    bundle = StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:GrossProfit", "path": "002", "order_key": "b"},
            {"concept": "us-gaap:SellingAndMarketingExpense", "label": "S&M"},
        ],
        abstract_concepts=[],
    )

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        place_tool = next(t for t in tools if t.name == "place_new_concepts")
        place_tool.invoke({
            "placements_json": json.dumps([{
                "concept": "us-gaap:SellingAndMarketingExpense",
                "parent": "us-gaap:OperatingExpensesAbstract",
                "position": 1,
            }])
        })
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    node = make_hierarchy_review_node(_CustomService(), chat_llm=StubHierarchyChat())
    out = node({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {
            "seeded_statement_types": [],
            "new_concepts": [{"concept": "us-gaap:SellingAndMarketingExpense"}],
            "integrity": {},
        },
    })

    plan = out["hierarchy_plan"]
    assert plan["decided_by"] == "agent"
    sm = next(c for c in bundle.concepts if c["concept"] == "us-gaap:SellingAndMarketingExpense")
    assert sm["path"] == "003.002"
    assert sm["order_key"] == "b"
    assert sm["hierarchy_source"] == "agent"


def test_agent_places_new_concept_with_explicit_path_and_order(enabled, monkeypatch):
    """The agent can decide explicit path and order_key based on existing hierarchy."""
    from filings_agent.agent import tools as agent_tools
    real_build = agent_tools.build_hierarchy_tools

    existing_docs = [
        {"_id": ObjectId(), "concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
        {"_id": ObjectId(), "concept": "us-gaap:OperatingExpensesAbstract", "path": "002", "order_key": "b", "abstract": True},
    ]

    class _CustomService:
        def _get_concept_repo_by_form_type(self, ft):
            return _FakeRepo(existing_docs)

    bundle = StatementBundle(
        company_cik="0000789019", statement_type="income", form_type="10-K",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:NewCost", "label": "New Cost"},
        ],
        abstract_concepts=[],
    )

    def build(stored, filing, proposal):
        tools = real_build(stored, filing, proposal)
        place_tool = next(t for t in tools if t.name == "place_new_concepts")
        place_tool.invoke({
            "placements_json": json.dumps([{
                "concept": "us-gaap:NewCost",
                "path": "002.005",
                "order_key": "e",
            }])
        })
        return tools

    monkeypatch.setattr(agent_tools, "build_hierarchy_tools", build)

    node = make_hierarchy_review_node(_CustomService(), chat_llm=StubHierarchyChat())
    out = node({
        "cik": "0000789019", "ticker": "MSFT", "status": "hierarchy_resolved",
        "bundles": [bundle],
        "hierarchy_plan": {
            "seeded_statement_types": [],
            "new_concepts": [{"concept": "us-gaap:NewCost"}],
            "integrity": {},
        },
    })

    sm = next(c for c in bundle.concepts if c["concept"] == "us-gaap:NewCost")
    assert sm["path"] == "002.005"
    assert sm["order_key"] == "e"
    assert sm["hierarchy_source"] == "agent"


def test_place_new_concepts_rejects_collision():
    stored = [
        {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
    ]
    # Attempting to assign 001/a to a new concept must be rejected
    plan = plan_hierarchy(
        [{"concept": "us-gaap:NewRevenue", "path": "001", "order_key": "a"}],
        stored_rows=stored,
    )
    assert not plan.valid
    assert any("already occupied by 'us-gaap:Revenues'" in e for e in plan.errors)


def test_non_relevant_concepts_placed_in_path_555():
    """Non-relevant concepts are placed in path 555 without collisions."""
    plan = plan_hierarchy(
        [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:InterestPaid", "path": "555"},
            {"concept": "us-gaap:NotesIssued1", "path": "555"},
            {"concept": "custom:LegacyBreakdown", "hide": True},
        ]
    )
    assert plan.valid, f"Expected valid plan, got errors: {plan.errors}"
    by_concept = plan.by_concept()
    assert by_concept["us-gaap:Revenues"].path == "001"

    # All non-relevant items routed to path 555 with distinct order keys
    for concept in ("us-gaap:InterestPaid", "us-gaap:NotesIssued1", "custom:LegacyBreakdown"):
        row = by_concept[concept]
        assert row.path == "555"
        assert row.order_key in ("a", "b", "c")

    order_keys = {by_concept[c].order_key for c in ("us-gaap:InterestPaid", "us-gaap:NotesIssued1", "custom:LegacyBreakdown")}
    assert len(order_keys) == 3, f"Expected 3 distinct order keys, got: {order_keys}"


def test_persist_node_applies_hierarchy_updates():
    """persist_node must call apply_hierarchy_updates and update_hierarchy_seed_metadata."""
    from filings_agent.nodes.persist import make_persist_node
    from bson import ObjectId

    applied_updates = []
    seed_updates = []

    class MockService:
        def persist_statement_bundle(self, bundle, **kwargs):
            return True

        def apply_hierarchy_updates(self, updates):
            applied_updates.extend(updates)

        def update_hierarchy_seed_metadata(self, cik, plan):
            seed_updates.append((cik, plan))

    mock_svc = MockService()
    persist_fn = make_persist_node(mock_svc)

    doc_id = ObjectId()
    sample_update = {
        "_id": doc_id,
        "concept": "us-gaap:CostOfGoods",
        "path": "002",
        "order_key": "b",
        "form_type": "10-Q",
    }

    dummy_bundle = type("Bundle", (), {"statement_type": "income", "concepts": [{"concept": "us-gaap:Revenues"}]})()
    state = {
        "bundles": [dummy_bundle],
        "cik": "0000320193",
        "accession_number": "0000320193-26-000020",
        "write_plan": {"action": "write", "statements": ["income"], "decided_by": "agent"},
        "hierarchy_plan": {
            "existing_updates": [sample_update],
            "seeded_statement_types": ["income"],
        },
    }

    result = persist_fn(state)
    assert result["status"] == "saved"
    assert len(applied_updates) == 1
    assert applied_updates[0]["_id"] == doc_id
    assert len(seed_updates) == 1
    assert seed_updates[0][0] == "0000320193"


def test_resolver_re_roots_dropped_wrapper_prefix_zero_orphans():
    """When a dropped root wrapper leaves 001. prefix on line items, resolver re-roots them to zero orphans."""
    from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles

    bundle = type("Bundle", (), {
        "company_cik": "0000320193",
        "statement_type": "income",
        "form_type": "10-Q",
        "concepts": [
            {"concept": "us-gaap:Revenues", "path": "001.001", "order_key": "a", "label": "Revenue"},
            {"concept": "us-gaap:CostOfRevenue", "path": "001.002", "order_key": "b", "label": "Cost"},
            {"concept": "us-gaap:GrossProfit", "path": "001.003", "order_key": "c", "label": "Gross Profit"},
            {"concept": "us-gaap:OperatingExpenses", "path": "001.004", "order_key": "d", "label": "Operating Expenses"},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense", "path": "001.004.001", "order_key": "a", "label": "R&D"},
        ],
        "abstract_concepts": [],
    })()

    class MockNorm:
        def _get_concept_repo_by_form_type(self, form_type):
            return type("R", (), {"collection": type("C", (), {"find": lambda *a, **k: []})()})()

    plan = resolve_hierarchy_bundles([bundle], MockNorm(), cik="0000320193")
    assert plan["integrity"]["orphans"] == 0, f"Expected 0 orphans, got {plan['integrity']['orphans']}"
    assert bundle.concepts[0]["path"] == "001"
    assert bundle.concepts[1]["path"] == "002"
    assert bundle.concepts[2]["path"] == "003"
    assert bundle.concepts[3]["path"] == "004"
    assert bundle.concepts[4]["path"] == "004.001"


def test_no_abstract_wrapper_persisted_and_abstract_flag_is_false():
    """Verify that concepts ending in Abstract are never persisted, and grouping concepts have abstract=False."""
    from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles

    bundle = type("Bundle", (), {
        "company_cik": "0000320193",
        "statement_type": "income",
        "form_type": "10-Q",
        "concepts": [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a", "label": "Revenue"},
            {"concept": "us-gaap:CostOfRevenue", "path": "002", "order_key": "b", "label": "Cost"},
            {"concept": "us-gaap:ResearchAndDevelopmentExpense", "path": "004.001", "order_key": "a", "label": "R&D"},
        ],
        "abstract_concepts": [
            {"concept": "us-gaap:IncomeStatementAbstract", "abstract": True, "path": "000", "order_key": "a"},
            {"concept": "us-gaap:OperatingExpenses", "abstract": True, "path": "004", "order_key": "d"},
            {"concept": "us-gaap:EarningsPerShareAbstract", "abstract": True, "path": "010", "order_key": "j"},
        ],
    })()

    class MockNorm:
        def _get_concept_repo_by_form_type(self, form_type):
            return type("R", (), {"collection": type("C", (), {"find": lambda *a, **k: []})()})()

    plan = resolve_hierarchy_bundles([bundle], MockNorm(), cik="0000320193")
    assert plan["integrity"]["orphans"] == 0

    # Ensure OperatingExpenses was resolved and given a path
    op_exp = next(c for c in bundle.abstract_concepts if c["concept"] == "us-gaap:OperatingExpenses")
    assert op_exp["path"] == "004"

    # IncomeStatementAbstract and EarningsPerShareAbstract were skipped from items_to_resolve
    inc_abs = next(c for c in bundle.abstract_concepts if c["concept"] == "us-gaap:IncomeStatementAbstract")
    assert "_hierarchy_resolved" not in inc_abs


def test_eps_abstract_skipped_and_rejected_from_database():
    """Verify that EarningsPerShareAbstract is never passed to hierarchy review and rejected by DB inserts."""
    from filings_agent.nodes.hierarchy_review import _filing_concepts
    from data_normalization_service.core.models import ConceptDocument
    from data_normalization_service.utils.duplicate_prevention import DuplicatePreventionManager
    from unittest.mock import MagicMock

    # 1. _filing_concepts filters out concepts ending with Abstract
    test_bundle = type("B", (), {
        "concepts": [
            {"concept": "us-gaap:Revenues", "value": 100.0, "label": "Revenues"},
            {"concept": "us-gaap:EarningsPerShareBasic", "value": 1.5, "label": "EPS Basic"},
        ],
        "abstract_concepts": [
            {"concept": "us-gaap:EarningsPerShareAbstract", "abstract": True, "label": "EPS Abstract"},
            {"concept": "us-gaap:OperatingExpensesAbstract", "abstract": True, "label": "OpEx Abstract"},
        ],
    })()
    fc = _filing_concepts(test_bundle)
    fc_concepts = [c["concept"] for c in fc]
    assert "us-gaap:Revenues" in fc_concepts
    assert "us-gaap:EarningsPerShareBasic" in fc_concepts
    assert "us-gaap:EarningsPerShareAbstract" not in fc_concepts
    assert "us-gaap:OperatingExpensesAbstract" not in fc_concepts
    assert all(c["abstract"] is False for c in fc)

    # 2. DuplicatePreventionManager.safe_insert_concept refuses abstract concepts
    mock_repo = MagicMock()
    dpm = DuplicatePreventionManager(MagicMock(), mock_repo)
    abs_doc = ConceptDocument(
        company_cik="0000320193",
        statement_type="income",
        concept="us-gaap:EarningsPerShareAbstract",
        label="Earnings Per Share [Abstract]",
        path="010",
        order_key="a",
        abstract=True,
    )
    result = dpm.safe_insert_concept(abs_doc)
    assert result is None
    mock_repo.insert.assert_not_called()


def test_filing_concepts_includes_custom_abstract_grouping_headers():
    from filings_agent.nodes.hierarchy_review import _filing_concepts

    test_bundle = type("Bundle", (), {
        "concepts": [
            {"concept": "us-gaap:Revenues", "abstract": False, "label": "Revenue"},
        ],
        "abstract_concepts": [
            {"concept": "custom:ProductSegmentation", "abstract": True, "label": "Product Segmentation"},
            {"concept": "custom:GeographicSegmentation", "abstract": True, "label": "Geographic Segmentation"},
            {"concept": "us-gaap:IncomeStatementAbstract", "abstract": True, "label": "Income Statement"},
        ],
    })()

    fc = _filing_concepts(test_bundle)
    fc_by_concept = {c["concept"]: c for c in fc}

    assert "us-gaap:Revenues" in fc_by_concept
    assert "custom:ProductSegmentation" in fc_by_concept
    assert "custom:GeographicSegmentation" in fc_by_concept
    assert "us-gaap:IncomeStatementAbstract" not in fc_by_concept

    assert fc_by_concept["custom:ProductSegmentation"]["abstract"] is True
    assert fc_by_concept["custom:GeographicSegmentation"]["abstract"] is True
    assert fc_by_concept["us-gaap:Revenues"]["abstract"] is False


def test_apply_plan_synchronizes_dimensional_concepts():
    from filings_agent.hierarchy.planner import plan_hierarchy
    from filings_agent.nodes.hierarchy_review import _apply_plan

    b = type("Bundle", (), {
        "concepts": [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:CostOfRevenue", "path": "002", "order_key": "b"},
        ],
        "abstract_concepts": [
            {"concept": "custom:ProductSegmentation", "abstract": True, "path": "001.001", "order_key": "a"},
            {"concept": "custom:GeographicSegmentation", "abstract": True, "path": "001.002", "order_key": "b"},
        ],
        "dimensional_concepts": [
            {"concept": "aapl:IPhoneMember", "parent_header": "custom:ProductSegmentation"},
            {"concept": "aapl:AmericasSegmentMember", "parent_header": "custom:GeographicSegmentation"},
        ],
    })()

    # Agent reorders custom:GeographicSegmentation to 001.001 and custom:ProductSegmentation to 001.002
    plan = plan_hierarchy([
        {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
        {"concept": "custom:GeographicSegmentation", "parent": "us-gaap:Revenues", "position": 0, "abstract": True},
        {"concept": "custom:ProductSegmentation", "parent": "us-gaap:Revenues", "position": 1, "abstract": True},
        {"concept": "us-gaap:CostOfRevenue", "parent": None, "position": 1},
    ])
    assert plan.valid, plan.errors

    summary = _apply_plan(b, plan, stored=[], decided_by="agent")
    assert summary["placed"] == 4

    # Verify custom headers placement
    by_concept = {a["concept"]: a for a in b.abstract_concepts}
    assert by_concept["custom:GeographicSegmentation"]["path"] == "001.001"
    assert by_concept["custom:ProductSegmentation"]["path"] == "001.002"

    # Verify dimensional concepts were synchronized under new header paths
    dim_by_concept = {d["concept"]: d for d in b.dimensional_concepts}
    assert dim_by_concept["aapl:AmericasSegmentMember"]["path"] == "001.001.001"
    assert dim_by_concept["aapl:IPhoneMember"]["path"] == "001.002.001"


def test_apply_plan_synchronizes_non_header_dimensional_concepts_and_stored_dims():
    from unittest.mock import MagicMock
    from filings_agent.nodes.hierarchy_review import _apply_plan

    dim_id = ObjectId()
    stored_dims = [
        {
            "_id": dim_id,
            "concept": "meta:FamilyOfAppsMember",
            "path": "002.001",
            "order_key": "a",
            "dimension_concept": True,
        }
    ]
    mock_repo = MagicMock()
    mock_repo.collection.find.return_value = stored_dims
    mock_norm = MagicMock()
    mock_norm._get_concept_repo_by_form_type.return_value = mock_repo

    b = type("StatementBundleDouble", (), {
        "statement_type": "income",
        "form_type": "10-Q",
        "company_cik": "0001326801",
        "concepts": [
            {"concept": "us-gaap:Revenue", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:OperatingIncomeLoss", "path": "002", "order_key": "b"},
        ],
        "abstract_concepts": [],
        "dimensional_concepts": [
            {
                "concept": "meta:FamilyOfAppsMember",
                "parent_concept": "us-gaap:OperatingIncomeLoss",
                "parent_header": None,
                "path": "002.001",
                "order_key": "a",
            },
            {
                "concept": "meta:RealityLabsMember",
                "parent_concept": "us-gaap:OperatingIncomeLoss",
                "parent_header": None,
                "path": "002.002",
                "order_key": "a",
            },
        ],
    })()

    # Agent moves OperatingIncomeLoss to 004
    plan = plan_hierarchy([
        {"concept": "us-gaap:Revenue", "parent": None, "position": 0},
        {"concept": "us-gaap:CostOfRevenue", "parent": None, "position": 1},
        {"concept": "us-gaap:CostsAndExpenses", "parent": None, "position": 2},
        {"concept": "us-gaap:OperatingIncomeLoss", "parent": None, "position": 3},
    ])
    assert plan.valid, plan.errors

    summary = _apply_plan(b, plan, stored=[], decided_by="agent", norm_service=mock_norm)
    dim_by_concept = {d["concept"]: d for d in b.dimensional_concepts}

    # Verify both dimensional concepts under OperatingIncomeLoss were repathed to 004.001 and 004.002
    assert dim_by_concept["meta:FamilyOfAppsMember"]["path"] == "004.001"
    assert dim_by_concept["meta:FamilyOfAppsMember"]["order_key"] == "a"
    assert dim_by_concept["meta:RealityLabsMember"]["path"] == "004.002"
    assert dim_by_concept["meta:RealityLabsMember"]["order_key"] == "b"

    # Verify stored_updates queued update for stored dimensional concept
    matching_updates = [u for u in summary["stored_updates"] if u["_id"] == dim_id]
    assert len(matching_updates) == 1
    assert matching_updates[0]["path"] == "004.001"
    assert matching_updates[0]["order_key"] == "a"


def test_should_review_scoped_to_statement_type():
    """should_review must evaluate defects and seeding per statement_type."""
    plan = {
        "seeded_statement_types": ["balancesheet"],
        "new_concepts": [{"statement_type": "balancesheet", "concept": "us-gaap:NewItem"}],
        "conflicts": [],
        "statement_integrity": {
            "income": {"duplicate_paths": 0, "orphans": 0},
            "balancesheet": {"duplicate_paths": 0, "orphans": 1},
        },
        "integrity": {"duplicate_paths": 0, "orphans": 1},
    }
    # Income is clean and not seeded -> False
    should, reason = should_review(plan, statement_type="income")
    assert should is False
    assert reason == ""

    # Balancesheet has orphans and is seeded -> True
    should, reason = should_review(plan, statement_type="balancesheet")
    assert should is True
    assert "balancesheet" in reason


def test_custom_grouping_headers_protected_from_555_and_preserved():
    """Custom grouping headers must never be sent to 555 and must be preserved if omitted."""
    stored = [
        {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
        {"concept": "custom:ProductSegmentation", "path": "001.001", "order_key": "a", "abstract": True},
    ]

    # 1. Proposal erroneously attempts to put custom:ProductSegmentation at 555
    plan_with_555 = plan_hierarchy(
        [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "custom:ProductSegmentation", "path": "555", "order_key": "a", "abstract": True},
        ],
        stored_rows=stored,
    )
    assert plan_with_555.valid
    row = plan_with_555.by_concept()["custom:ProductSegmentation"]
    assert row.path == "001.001"  # Restored to stored path, not 555

    # 2. Proposal omits custom:ProductSegmentation entirely
    plan_omitted = plan_hierarchy(
        [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:CostOfRevenue", "parent": None, "position": 1},
        ],
        stored_rows=stored,
    )
    assert plan_omitted.valid
    assert "custom:ProductSegmentation" in plan_omitted.by_concept()
    assert plan_omitted.by_concept()["custom:ProductSegmentation"].path == "001.001"


def test_free_placement_rejects_missing_parent():
    """_free_placement must not accept a candidate path if its parent does not exist."""
    from filings_agent.hierarchy.resolver import _free_placement

    occupied = {("001", "a"): "us-gaap:Assets"}
    existing = {"001"}
    valid = {"001"}

    # Candidate 011.001 has parent 011 which does NOT exist in existing or valid parents
    path, order, source = _free_placement(
        candidate_path="011.001",
        candidate_order="a",
        concept="us-gaap:AccountsPayableTradeCurrent",
        occupied=occupied,
        existing_paths=existing,
        valid_parents=valid,
    )
    assert path != "011.001"
    assert source == "conflict_safe_sibling"
    assert not path.startswith("011")








# ── parent-scoped dimensional rows (Revenue vs CostOfRevenue collisions) ─────


def test_dim_sync_is_parent_scoped_not_concept_scoped():
    """Same member under two line items must not collapse onto one path.

    Regression: the dim-sync matched stored dimensional rows to bundle rows by
    concept name only, so ``us-gaap:ServiceMember`` under Revenues and under
    CostOfGoodsAndServicesSold received the SAME path — nesting cost-of-revenue
    children under Revenue (and vice-versa).
    """
    from unittest.mock import MagicMock

    from filings_agent.nodes.hierarchy_review import _apply_plan

    rev_dim_id = ObjectId()
    cogs_dim_id = ObjectId()
    stored_dims = [
        {
            "_id": rev_dim_id,
            "concept": "us-gaap:ServiceMember",
            "concept_id": ObjectId(),
            "parent_concept": "us-gaap:Revenues",
            "path": "001.001.002",
            "order_key": "b",
            "dimension_concept": True,
        },
        {
            "_id": cogs_dim_id,
            "concept": "us-gaap:ServiceMember",
            "concept_id": ObjectId(),
            "parent_concept": "us-gaap:CostOfGoodsAndServicesSold",
            "path": "001.001.002",
            "order_key": "b",
            "dimension_concept": True,
        },
    ]
    mock_repo = MagicMock()
    mock_repo.collection.find.return_value = stored_dims
    mock_norm = MagicMock()
    mock_norm._get_concept_repo_by_form_type.return_value = mock_repo

    bundle = type("Bundle", (), {
        "statement_type": "income",
        "form_type": "10-Q",
        "company_cik": "0000320193",
        "concepts": [
            {"concept": "us-gaap:Revenues", "path": "001", "order_key": "a"},
            {"concept": "us-gaap:CostOfGoodsAndServicesSold", "path": "002", "order_key": "b"},
        ],
        "abstract_concepts": [
            {"concept": "custom:ProductSegmentation", "abstract": True,
             "path": "001.001", "order_key": "a"},
        ],
        "dimensional_concepts": [
            {"concept": "us-gaap:ServiceMember", "parent_concept": "us-gaap:Revenues",
             "parent_header": "custom:ProductSegmentation"},
            {"concept": "us-gaap:ServiceMember",
             "parent_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "parent_header": None},
        ],
    })()

    plan = plan_hierarchy([
        {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
        {"concept": "custom:ProductSegmentation", "parent": "us-gaap:Revenues",
         "position": 0, "abstract": True},
        {"concept": "us-gaap:CostOfGoodsAndServicesSold", "parent": None, "position": 1},
    ])
    assert plan.valid, plan.errors

    summary = _apply_plan(bundle, plan, stored=[], decided_by="agent", norm_service=mock_norm)

    rev_dim = next(d for d in bundle.dimensional_concepts
                   if d["parent_concept"] == "us-gaap:Revenues")
    cogs_dim = next(d for d in bundle.dimensional_concepts
                    if d["parent_concept"] == "us-gaap:CostOfGoodsAndServicesSold")
    assert rev_dim["path"] == "001.001.001"
    assert cogs_dim["path"] == "002.001"

    rev_update = next(u for u in summary["stored_updates"] if u["_id"] == rev_dim_id)
    cogs_update = next(u for u in summary["stored_updates"] if u["_id"] == cogs_dim_id)
    assert rev_update["path"] == "001.001.001"
    assert cogs_update["path"] == "002.001"
    assert rev_update["path"] != cogs_update["path"]


def test_resolver_does_not_treat_cost_of_revenue_as_revenue():
    """Cost lines must not share the revenue line's custom segmentation header.

    ``us-gaap:CostOfRevenue`` contains the substring "revenue" and was grouped
    under ``custom:ProductSegmentation``; since only one row per concept name can
    be stored, its children ended up on a path that never existed (orphans).
    """
    from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles

    class _EmptyRepo:
        collection = type("Col", (), {"find": lambda self, *a, **k: []})()

        def find_concept_reference_for_hierarchy(self, *a, **k):
            return None

    class _EmptyService:
        def _get_concept_repo_by_form_type(self, ft):
            return _EmptyRepo()

    product_axis = {"ProductOrServiceAxis": "us-gaap:ProductMember"}
    bundle = StatementBundle(
        company_cik="0000320193", statement_type="income", form_type="10-Q",
        reporting_period={"end_date": "2025-06-30", "fiscal_year": 2025},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(), filing_id=ObjectId(),
        concepts=[
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "label": "Revenue", "value": 1000.0},
            {"concept": "us-gaap:CostOfRevenue", "label": "Cost of Revenue", "value": 400.0},
        ],
        abstract_concepts=[],
        dimensional_concepts=[
            {"concept": "us-gaap:ProductMember",
             "parent_concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "segment_type": "product_service",
             "dimension_data": {"dimensions": product_axis}},
            {"concept": "us-gaap:ProductMember",
             "parent_concept": "us-gaap:CostOfRevenue",
             "segment_type": "product_service",
             "dimension_data": {"dimensions": product_axis}},
        ],
    )

    resolve_hierarchy_bundles([bundle], _EmptyService(), cik="0000320193")

    rev_path = bundle.concepts[0]["path"]
    cost_path = bundle.concepts[1]["path"]
    assert rev_path != cost_path

    rev_dim = next(d for d in bundle.dimensional_concepts
                   if d["parent_concept"].endswith("AssessedTax"))
    cost_dim = next(d for d in bundle.dimensional_concepts
                    if d["parent_concept"] == "us-gaap:CostOfRevenue")

    # Revenue child hangs under the ProductSegmentation header; cost child hangs
    # directly under CostOfRevenue — never under the revenue header.
    assert rev_dim["path"].startswith(f"{rev_path}.")
    assert cost_dim["path"].startswith(f"{cost_path}.")
    assert not cost_dim["path"].startswith(rev_path + ".")

    headers = [a["concept"] for a in bundle.abstract_concepts]
    assert headers.count("custom:ProductSegmentation") == 1
