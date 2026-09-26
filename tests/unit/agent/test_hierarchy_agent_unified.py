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
        if self.step == 2:
            # The agent must CHECK the materialized result before finalizing.
            return AIMessage(content="", tool_calls=[
                {"name": "preview_hierarchy", "args": {}, "id": "pv1"},
            ])
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


def test_materialize_preserves_order_field_over_alphabetical():
    # Z-concept comes first in reading order (order 1); A-concept comes second (order 2)
    # Alphabetical sorting would put A at 001 and Z at 002.
    rows, _ = _materialize([
        {"concept": "us-gaap:ZRevenues", "parent": None, "order": 1},
        {"concept": "us-gaap:ACostOfRevenue", "parent": None, "order": 2},
    ], [], {})
    by = {r["concept"]: r for r in rows}
    assert by["us-gaap:ZRevenues"]["path"] == "001"
    assert by["us-gaap:ACostOfRevenue"]["path"] == "002"


def test_materialize_promotes_custom_headers_from_dims():
    rows, dims = _materialize(
        [{"concept": "us-gaap:Revenues", "parent": None, "order": 1}],
        [
            {"member": "custom:ProductSegmentation", "parent": "us-gaap:Revenues", "abstract": True, "order": 1},
            {"member": "aapl:IPhoneMember", "parent": "custom:ProductSegmentation", "order": 1},
        ],
        {},
    )
    row_by = {r["concept"]: r for r in rows}
    assert "custom:ProductSegmentation" in row_by
    assert row_by["custom:ProductSegmentation"]["path"] == "001.001"
    assert len(dims) == 1
    assert dims[0]["concept"] == "aapl:IPhoneMember"
    assert dims[0]["path"] == "001.001.001"



def test_materialize_keeps_same_header_under_each_parent():
    """Regression: a grouping header (custom:ProductSegmentation) must exist
    once PER PARENT.  Name-only identity used to collapse the two into one row
    and orphan every parent but the last writer."""
    rows, dims = _materialize(
        [
            {"concept": "us-gaap:Revenues", "parent": None, "order": 1},
            {"concept": "us-gaap:CostOfGoodsAndServicesSold", "parent": None, "order": 2},
        ],
        [
            {"concept": "custom:ProductSegmentation", "parent": "us-gaap:Revenues",
             "abstract": True, "order": 1},
            {"concept": "aapl:IPhoneMember", "parent_concept": "us-gaap:Revenues",
             "parent_header": "custom:ProductSegmentation", "order": 1},
            {"concept": "custom:ProductSegmentation", "parent": "us-gaap:CostOfGoodsAndServicesSold",
             "abstract": True, "order": 1},
            {"concept": "us-gaap:ProductMember", "parent_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "parent_header": "custom:ProductSegmentation", "order": 1},
        ],
        {},
    )
    headers = [r for r in rows if r["concept"] == "custom:ProductSegmentation"]
    assert len(headers) == 2, "same header under two parents must not collapse"
    assert {h["path"] for h in headers} == {"001.001", "002.001"}
    by_concept = {d["concept"]: d for d in dims}
    assert by_concept["aapl:IPhoneMember"]["path"].startswith("001.001.")
    assert by_concept["us-gaap:ProductMember"]["path"].startswith("002.001.")


def test_materialize_never_reuses_a_stored_path_held_by_an_absent_row():
    """Regression: a newly-added row must not steal the slot of an existing
    row the agent did not re-propose (that is how duplicate paths appear)."""
    rows, _ = _materialize(
        [{"concept": "us-gaap:Revenues", "parent": None, "order": 1},
         {"concept": "us-gaap:GrossProfit", "parent": None, "order": 2}],
        [],
        {"us-gaap:Revenues": "001"},
        occupied_paths={"001", "002"},
    )
    by = {r["concept"]: r for r in rows}
    assert by["us-gaap:Revenues"]["path"] == "001"   # keeps its stored slot
    assert by["us-gaap:GrossProfit"]["path"] == "003"  # cannot take 002


def test_query_hierarchy_diff_and_header_consistency_lint():
    from filings_agent.tools.hierarchy_tools import build_unified_hierarchy_tools

    stored = [
        {"concept": "us-gaap:Revenues", "path": "001", "parent_concept": None, "dimension_concept": False},
        {"concept": "custom:ProductSegmentation", "path": "001.001", "parent_concept": "us-gaap:Revenues", "abstract": True, "dimension_concept": False},
        {"concept": "aapl:IPhoneMember", "path": "001.001.001", "parent_concept": "us-gaap:Revenues", "dimension_concept": True},
    ]
    filing = [
        {"concept": "us-gaap:Revenues", "dimension_concept": False},
        {"concept": "aapl:IPhoneMember", "dimension_concept": True},
        {"concept": "us-gaap:CostOfRevenue", "dimension_concept": False},  # new line item
    ]
    proposal = {}
    tools = build_unified_hierarchy_tools(stored, filing, proposal)
    tools_by_name = {t.name: t for t in tools}

    assert "query_hierarchy_diff" in tools_by_name
    diff_output = tools_by_name["query_hierarchy_diff"].invoke({})
    assert "MATCHED" in diff_output
    assert "us-gaap:Revenues" in diff_output
    assert "NEW INCOMING CONCEPTS" in diff_output
    assert "us-gaap:CostOfRevenue" in diff_output
    assert "EXISTING STORED GROUPING HEADERS" in diff_output
    assert "custom:ProductSegmentation" in diff_output

    # Test linter catches duplicate header spelling
    tools_by_name["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:Revenues", "parent": None, "order": 1},
            {"concept": "custom:ProductSegment", "parent": "us-gaap:Revenues", "order": 1}, # typo/inconsistent header
        ]),
        "dims_json": "[]",
    })
    lint_report = tools_by_name["lint_hierarchy"].invoke({})
    assert "Reuse existing header" in lint_report


def test_propose_hierarchy_incremental_additions_merge():
    from filings_agent.tools.hierarchy_tools import build_unified_hierarchy_tools
    stored = [
        {"concept": "us-gaap:Revenues", "path": "001", "parent_concept": None, "dimension_concept": False},
        {"concept": "us-gaap:CostOfRevenue", "path": "002", "parent_concept": None, "dimension_concept": False},
        {"concept": "aapl:IPhoneMember", "path": "001.001", "parent_concept": "us-gaap:Revenues", "dimension_concept": True},
    ]
    filing = [
        {"concept": "us-gaap:Revenues", "dimension_concept": False},
        {"concept": "us-gaap:CostOfRevenue", "dimension_concept": False},
        {"concept": "us-gaap:GrossProfit", "dimension_concept": False},  # new line item
        {"concept": "aapl:IPhoneMember", "dimension_concept": True},
        {"concept": "aapl:MacMember", "dimension_concept": True},        # new dim
    ]
    proposal = {}
    tools = build_unified_hierarchy_tools(stored, filing, proposal)
    tools_by_name = {t.name: t for t in tools}

    # query_stored_hierarchy is now exposed on incremental so the agent sees the
    # full stored tree before re-proposing the complete ordered tree.
    assert "query_stored_hierarchy" in tools_by_name
    assert "query_filing_hierarchy" not in tools_by_name
    assert "query_hierarchy_diff" in tools_by_name
    assert "preview_hierarchy" in tools_by_name

    # Agent passes ONLY the NEW items in propose_hierarchy (partial merge);
    # stored rows are merged back with explicit positional ranks.
    res = tools_by_name["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:GrossProfit", "parent": None, "order": 3},
        ]),
        "dims_json": json.dumps([
            {"concept": "aapl:MacMember", "parent_concept": "us-gaap:Revenues", "order": 2},
        ]),
    })

    assert "OK" in res
    # propose accumulates only what the agent passed (stored merge happens at
    # preview time), and later revisions never erase earlier rows.
    row_concepts = [r["concept"] for r in proposal["rows"]]
    assert row_concepts == ["us-gaap:GrossProfit"]
    dim_concepts = [d["concept"] for d in proposal["dims"]]
    assert dim_concepts == ["aapl:MacMember"]

    # A second (revision) call must NOT erase the first call's rows.
    tools_by_name["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:CostOfRevenue", "parent": None, "order": 2},
        ]),
        "dims_json": "[]",
    })
    row_concepts = [r["concept"] for r in proposal["rows"]]
    assert "us-gaap:GrossProfit" in row_concepts
    assert "us-gaap:CostOfRevenue" in row_concepts
    assert len(proposal["rows"]) == 2

    dim_concepts = [d["concept"] for d in proposal["dims"]]
    assert "aapl:IPhoneMember" not in dim_concepts
    assert "aapl:MacMember" in dim_concepts
    assert len(proposal["dims"]) == 1




def _stored_context(docs):
    """Mirror _stored_rows_for's sorted read + identity maps for a unit test."""
    main = [d for d in docs if not d.get("dimension_concept")]
    main.sort(key=lambda s: (
        str(s.get("parent_concept") or ""),
        str(s.get("order_key") or ""),
        str(s.get("path") or ""),
    ))
    path_by_concept = {}
    for s in main:
        if s.get("concept") and s.get("path"):
            path_by_concept.setdefault(s["concept"], str(s["path"]))
    occupied = {str(s["path"]) for s in main if s.get("path") and str(s["path"]) != "555"}
    identity_paths, identity_order_keys = {}, {}
    for s in main:
        if not s.get("concept") or not s.get("path"):
            continue
        key = (s["concept"], s.get("parent_concept"), False, s.get("parent_header"))
        identity_paths.setdefault(key, str(s["path"]))
        if s.get("order_key") is not None:
            identity_order_keys.setdefault(key, str(s["order_key"]))
    return main, path_by_concept, occupied, identity_paths, identity_order_keys


def test_incremental_merge_preserves_stored_order_keys_no_drift():
    """Regression: a new concept must never reshuffle existing siblings' order_keys.

    This is the corruption the agent-owned hierarchy used to produce — existing
    rows were re-keyed from Mongo's arbitrary find() order on the next filing.
    """
    from filings_agent.tools.hierarchy_tools import build_unified_hierarchy_tools

    # Filing 1: seed three siblings under Revenues.
    seed = [
        {"concept": "us-gaap:Revenues", "parent": None, "order": 1},
        {"concept": "us-gaap:ProductMember", "parent": "us-gaap:Revenues", "order": 1},
        {"concept": "us-gaap:ServiceMember", "parent": "us-gaap:Revenues", "order": 2},
        {"concept": "us-gaap:HardwareMember", "parent": "us-gaap:Revenues", "order": 3},
    ]
    rows1, _ = _materialize(seed, [], {})
    docs = [
        {"concept": r["concept"], "path": r["path"], "order_key": r["order_key"],
         "parent_concept": r.get("parent"), "dimension_concept": False}
        for r in rows1
    ]

    # Filing 2: partial merge adding a new sibling (stored rows have no explicit
    # position, so they must keep their stored order_keys).
    stored, sp, occ, idp, idok = _stored_context(docs)
    filing = [{"concept": d["concept"]} for d in docs] + [{"concept": "us-gaap:CloudMember"}]
    proposal = {"merges": {}, "rows": [], "dims": []}
    tools = build_unified_hierarchy_tools(
        stored, filing, proposal,
        stored_paths=sp, occupied_paths=occ,
        identity_paths=idp, identity_order_keys=idok,
        materialize=_materialize,
    )
    tb = {t.name: t for t in tools}
    assert "preview_hierarchy" in tb
    assert "query_stored_hierarchy" in tb

    tb["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:CloudMember", "parent": "us-gaap:Revenues", "order": 4},
        ]),
        "dims_json": "[]",
    })
    preview = tb["preview_hierarchy"].invoke({})
    assert "no duplicate paths" in preview

    rows = proposal["_preview"]["rows"]
    by = {r["concept"]: r for r in rows}
    # Existing siblings keep their stored order_keys (a, b, c) — no drift.
    for d in docs:
        if d["concept"] == "us-gaap:Revenues":
            continue
        assert by[d["concept"]]["order_key"] == d["order_key"], d["concept"]
        assert by[d["concept"]]["path"] == d["path"], d["concept"]
    # New sibling is appended (next free path slot) after the existing three.
    assert by["us-gaap:CloudMember"]["path"] == "001.004"


class _NoPreviewChat:
    """Finalizes without ever calling preview_hierarchy (wrong)."""

    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            return AIMessage(content="", tool_calls=[
                {"name": "propose_hierarchy",
                 "args": {"rows_json": json.dumps([
                     {"concept": "us-gaap:Revenues", "parent": None, "position": 0},
                 ]), "dims_json": "[]"}, "id": "p1"},
            ])
        return AIMessage(content="", tool_calls=[
            {"name": "finalize_hierarchy",
             "args": {"result_json": json.dumps({"status": "done"})}, "id": "f1"},
        ])


def test_node_skips_bundle_when_agent_never_previews():
    """The check step is mandatory: no preview_hierarchy() → nothing is written."""
    bundle = _bundle([
        {"concept": "us-gaap:Revenues", "label": "Revenue", "value": 100.0},
    ])
    node = make_hierarchy_agent_node(_Service([]), chat_llm=_NoPreviewChat())
    out = node({"cik": "0000320193", "ticker": "AAPL", "status": "validated",
                "bundles": [bundle]})

    assert out["status"] == "hierarchy_agent_done"
    # The bundle must remain untouched — no path/order_key was ever assigned.
    assert not bundle.concepts[0].get("_hierarchy_resolved")
    assert not bundle.concepts[0].get("path")


def test_agentic_critic_and_move_remove_tools():
    """The agent gets a critic (check_hierarchy) + actions (move_row/remove_row):
    it detects a spurious wrapper root and a missing parent, then fixes both."""
    from filings_agent.tools.hierarchy_tools import build_unified_hierarchy_tools

    proposal = {"merges": {}, "rows": [], "dims": []}
    filing = [{"concept": c} for c in [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetIncomeLoss",
        "us-gaap:DepreciationDepletionAndAmortization",
    ]]
    tools = build_unified_hierarchy_tools(
        [], filing, proposal, statement_type="cashflow",
        stored_paths={}, occupied_paths=set(), identity_paths={}, identity_order_keys={},
        materialize=_materialize,
    )
    tb = {t.name: t for t in tools}
    assert {"check_hierarchy", "move_row", "remove_row"} <= set(tb)

    # Agent proposes a spurious whole-statement wrapper ROOT and a missing parent.
    tb["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "custom:OperatingActivitiesSection", "parent": None, "order": 1, "abstract": True},
            {"concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
             "parent": "custom:OperatingActivitiesSection", "order": 1},
            {"concept": "us-gaap:DepreciationDepletionAndAmortization",
             "parent": "us-gaap:GhostParent", "order": 2},
        ]),
        "dims_json": "[]",
    })
    report = tb["check_hierarchy"].invoke({})
    assert "whole-statement wrapper" in report
    assert "GhostParent" in report

    # Agent fixes: remove the wrapper, move the rows to real placements.
    tb["remove_row"].invoke({"row_json": json.dumps({"concept": "custom:OperatingActivitiesSection"})})
    tb["move_row"].invoke({"row_json": json.dumps({
        "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities", "parent": None, "position": 1,
    })})
    tb["move_row"].invoke({"row_json": json.dumps({
        "concept": "us-gaap:DepreciationDepletionAndAmortization",
        "parent": "us-gaap:NetCashProvidedByUsedInOperatingActivities", "position": 2,
    })})

    report2 = tb["check_hierarchy"].invoke({})
    assert "whole-statement wrapper" not in report2
    assert "GhostParent" not in report2


def test_reproposal_does_not_resurrect_a_removed_row():
    """Once the agent removes a row, a later propose_hierarchy() that still
    lists it (e.g. copied from the bundle) must NOT add it back — otherwise the
    agent thrashes remove→propose→remove. Re-adding is done via move_row."""
    from filings_agent.tools.hierarchy_tools import build_unified_hierarchy_tools

    proposal = {"merges": {}, "rows": [], "dims": []}
    filing = [{"concept": c} for c in [
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "custom:GeographicSegmentation",
    ]]
    tools = build_unified_hierarchy_tools(
        [], filing, proposal, statement_type="income",
        stored_paths={}, occupied_paths=set(), identity_paths={}, identity_order_keys={},
        materialize=_materialize,
    )
    tb = {t.name: t for t in tools}

    tb["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "parent": None, "order": 1, "abstract": True},
            {"concept": "custom:GeographicSegmentation",
             "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "order": 1},
        ]),
        "dims_json": "[]",
    })
    tb["remove_row"].invoke({"row_json": json.dumps({"concept": "custom:GeographicSegmentation"})})

    # A revision that still carries the removed row (the bundle's full list).
    tb["propose_hierarchy"].invoke({
        "rows_json": json.dumps([
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "parent": None, "order": 1, "abstract": True},
            {"concept": "custom:GeographicSegmentation",
             "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "order": 1},
        ]),
        "dims_json": "[]",
    })
    assert "custom:GeographicSegmentation" not in {
        r.get("concept") for r in proposal["rows"]
    }

    # …but an explicit move_row() re-adds it deliberately.
    tb["move_row"].invoke({"row_json": json.dumps({
        "concept": "custom:GeographicSegmentation",
        "parent": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "position": 1,
    })})
    assert "custom:GeographicSegmentation" in {
        r.get("concept") for r in proposal["rows"]
    }
