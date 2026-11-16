"""Unit tests for Google ADK orchestration, built-in tools, SEC presentation diff,
comparative historical period backfill, and complete-stock audit.
"""
from __future__ import annotations

import json
from bson import ObjectId
from langchain_core.messages import AIMessage

from core.extractors.sec_presentation_extractor import (
    build_sec_presentation_snapshot,
    format_sec_presentation_tree,
)
from filings_agent.adk import (
    ADKAgentTool,
    ADKGraphRunner,
    ADKLlmAgent,
    ADKLoopAgent,
    ADKSequentialAgent,
    ADKStepAgent,
    InMemoryArtifactService,
    InMemoryMemoryService,
    build_adk_exit_loop_tool,
    build_adk_load_artifacts_tool,
    build_adk_load_memory_tool,
    build_adk_transfer_to_agent_tool,
    wrap_custom_function_tool,
)
from filings_agent.company_graph import build_company_graph
from filings_agent.graph import build_filing_graph
from filings_agent.nodes.historical_backfill import (
    make_historical_backfill_node,
    persist_historical_backfill,
)
from filings_agent.nodes.stock_audit import run_complete_stock_audit
from filings_agent.state import new_company_state, new_state
from filings_agent.validation.sec_presentation_diff import check_sec_presentation_diff


def test_adk_builtin_tools_priority_and_loop_exit():
    """Verify ADK built-in tools (load_artifacts, load_memory, exit_loop, transfer_to_agent)."""
    artifact_svc = InMemoryArtifactService()
    artifact_svc.save_artifact("sec_presentation", {"role": "IncomeStatement"}, session_id="s1")

    memory_svc = InMemoryMemoryService()
    memory_svc.add_memory(
        cik="0000320193",
        category="alias",
        content="us-gaap:SalesRevenueNet promoted to us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    )

    actions_sink: dict = {}
    load_art_tool = build_adk_load_artifacts_tool(artifact_svc, session_id="s1")
    load_mem_tool = build_adk_load_memory_tool(memory_svc, cik="0000320193")
    exit_tool = build_adk_exit_loop_tool(actions_sink)
    transfer_tool = build_adk_transfer_to_agent_tool(actions_sink)

    assert "sec_presentation" in load_art_tool.invoke({"artifact_names": ""})
    assert "IncomeStatement" in load_art_tool.invoke({"artifact_names": "sec_presentation"})
    assert "SalesRevenueNet" in load_mem_tool.invoke({"query": "SalesRevenueNet"})

    exit_res = json.loads(exit_tool.invoke({"reason": "0 lint errors"}))
    assert exit_res["escalate"] is True
    assert actions_sink["escalate"] is True

    tr_res = json.loads(transfer_tool.invoke({"agent_name": "hierarchy_agent", "reason": "needs tree"}))
    assert tr_res["transferred_to"] == "hierarchy_agent"


def test_sec_presentation_snapshot_and_diff_validator():
    """Verify SEC presentation linkbase snapshot captures tree, dims, and comparative periods."""
    statement_docs = [
        {
            "statement_type": "income",
            "role_uri": "http://apple.com/role/StatementsOfOperations",
            "description": "CONSOLIDATED STATEMENTS OF OPERATIONS",
            "data": [
                {
                    "concept": "us-gaap:Revenues",
                    "label": "Total net sales",
                    "value": 391035000000.0,
                    "period": "2023-10-01 to 2024-09-28",
                    "level": 0,
                    "order": 1.0,
                    "abstract": False,
                    "dimensional_facts": [
                        {"member": "aapl:IPhoneMember", "label": "iPhone", "value": 201183000000.0}
                    ],
                    "comparative_facts": [
                        {
                            "concept": "us-gaap:Revenues",
                            "label": "Total net sales",
                            "value": 383285000000.0,
                            "period": "2022-09-25 to 2023-09-30",
                            "unit": "USD",
                        }
                    ],
                },
                {
                    "concept": "us-gaap:NetIncomeLoss",
                    "label": "Net income",
                    "value": 93736000000.0,
                    "period": "2023-10-01 to 2024-09-28",
                    "level": 0,
                    "order": 2.0,
                    "abstract": False,
                },
            ],
        }
    ]
    snapshot = build_sec_presentation_snapshot(statement_docs=statement_docs)
    assert "income" in snapshot["statements"]
    tree_text = format_sec_presentation_tree(snapshot, "income")
    assert "us-gaap:Revenues" in tree_text
    assert "aapl:IPhoneMember" in tree_text
    assert "2022-09-25 to 2023-09-30" in tree_text

    # Bundle missing NetIncomeLoss and missing iPhone dimensional breakdown
    incomplete_bundle = type(
        "Bundle",
        (),
        {
            "statement_type": "income",
            "concepts": [{"concept": "us-gaap:Revenues", "value": 391035000000.0}],
            "dimensional_concepts": [],
        },
    )()
    findings = check_sec_presentation_diff(incomplete_bundle, snapshot)
    finding_types = {f.type for f in findings}
    assert "missing_sec_presentation_concept" in finding_types
    assert "missing_sec_dimensional_breakdown" in finding_types


def test_historical_comparative_period_backfill():
    """Verify comparative prior-period facts missing in DB are discovered and backfilled."""
    statement_docs = [
        {
            "statement_type": "income",
            "data": [
                {
                    "concept": "us-gaap:Revenues",
                    "label": "Total net sales",
                    "value": 400000.0,
                    "period": "2024-01-01 to 2024-12-31",
                    "comparative_facts": [
                        {
                            "concept": "us-gaap:Revenues",
                            "label": "Total net sales",
                            "value": 350000.0,
                            "period": "2023-01-01 to 2023-12-31",
                            "unit": "USD",
                        },
                        {
                            "concept": "us-gaap:RestructuringCharges",
                            "label": "Restructuring charges",
                            "value": 12000.0,
                            "period": "2023-01-01 to 2023-12-31",
                            "unit": "USD",
                        },
                    ],
                    "comparative_dimensional_facts": [
                        {
                            "concept": "us-gaap:Revenues",
                            "label": "Cloud Segment",
                            "value": 150000.0,
                            "period": "2023-01-01 to 2023-12-31",
                            "dimension_details": {"us-gaap:StatementBusinessSegmentsAxis": "co:CloudMember"},
                        }
                    ],
                }
            ],
        }
    ]
    state = new_state(
        cik="0000320193",
        ticker="AAPL",
        form_type="10-K",
        accession_number="0000320193-25-000001",
        statement_docs=statement_docs,
    )
    state["reporting_period"] = {"end_date": "2024-12-31", "fiscal_year": 2024, "period_type": "annual"}
    state["sec_presentation"] = build_sec_presentation_snapshot(statement_docs=statement_docs)

    class _FakeNorm:
        db_connection = None

    node = make_historical_backfill_node(_FakeNorm())
    out = node(state)
    bf = out["historical_backfill"]
    assert bf["periods_checked"] == 1
    assert bf["missing_facts_found"] == 2
    assert bf["missing_dim_facts_found"] == 1
    assert bf["entries"][0]["end_date"] == "2023-12-31"
    assert bf["entries"][0]["cadence"] == "annual"

    summary = persist_historical_backfill(_FakeNorm(), out)
    assert summary["periods_backfilled"] == 1
    assert summary["values_inserted"] == 2
    assert summary["dims_inserted"] == 1


def test_complete_stock_audit_detects_hierarchy_continuity_and_scale_issues():
    """Verify complete-stock audit detects 404 paths, orphan parents, continuity gaps, and 1000x scale jumps."""
    cid_rev = ObjectId()
    cid_bad = ObjectId()
    cid_orphan = ObjectId()

    concepts_annual = [
        {
            "_id": cid_rev,
            "cik": "0000320193",
            "statement_type": "income",
            "concept": "us-gaap:Revenues",
            "path": "001",
            "order_key": "a",
        },
        {
            "_id": cid_bad,
            "cik": "0000320193",
            "statement_type": "income",
            "concept": "us-gaap:BadConcept",
            "path": "404",
            "order_key": "z",
        },
        {
            "_id": cid_orphan,
            "cik": "0000320193",
            "statement_type": "income",
            "concept": "us-gaap:OrphanChild",
            "path": "001.001",
            "order_key": "a",
            "parent_concept": "us-gaap:NonExistentParent",
        },
    ]

    # Values: Revenues present in 2022, 2023 (with 1000x scale jump!), and 2024; OrphanChild present in 2022 & 2024 (missing 2023!)
    values_annual = [
        {
            "cik": "0000320193",
            "statement_type": "income",
            "concept_id": cid_rev,
            "concept": "us-gaap:Revenues",
            "value": 100_000_000.0,
            "reporting_period": {"end_date": "2022-12-31"},
        },
        {
            "cik": "0000320193",
            "statement_type": "income",
            "concept_id": cid_rev,
            "concept": "us-gaap:Revenues",
            "value": 100_000.0,  # 1,000x scale error
            "reporting_period": {"end_date": "2023-12-31"},
        },
        {
            "cik": "0000320193",
            "statement_type": "income",
            "concept_id": cid_rev,
            "concept": "us-gaap:Revenues",
            "value": 120_000_000.0,
            "reporting_period": {"end_date": "2024-12-31"},
        },
        {
            "cik": "0000320193",
            "statement_type": "income",
            "concept_id": cid_orphan,
            "concept": "us-gaap:OrphanChild",
            "value": 5_000_000.0,
            "reporting_period": {"end_date": "2022-12-31"},
        },
        {
            "cik": "0000320193",
            "statement_type": "income",
            "concept_id": cid_orphan,
            "concept": "us-gaap:OrphanChild",
            "value": 6_000_000.0,
            "reporting_period": {"end_date": "2024-12-31"},
        },
    ]

    class _FakeCol:
        def __init__(self, docs):
            self._docs = docs

        def find(self, query=None):
            return list(self._docs)

    fake_db = {
        "concepts_annual": _FakeCol(concepts_annual),
        "concept_values_annual": _FakeCol(values_annual),
        "concepts_quarterly": _FakeCol([]),
        "concept_values_quarterly": _FakeCol([]),
    }

    audit = run_complete_stock_audit("0000320193", target_db=fake_db)
    assert audit["status"] == "findings_detected"
    h_types = {i["type"] for i in audit["hierarchy_issues"]}
    assert "polluted_or_missing_path" in h_types
    assert "orphan_parent_concept" in h_types
    assert len(audit["continuity_gaps"]) == 1
    assert audit["continuity_gaps"][0]["missing_periods"] == ["2023-12-31"]
    assert len(audit["scale_anomalies"]) >= 1


def test_human_visible_html_signs_and_hierarchy_match():
    """Verify rendered HTML statement table signs ((12,500) -> -12500) and section hierarchy match exactly."""
    from core.extractors.sec_presentation_extractor import parse_rendered_sec_html_tables

    html_sample = """
    <html><body>
      <table>
        <tr><td>Operating expenses:</td><td></td><td></td></tr>
        <tr><td style="padding-left:16px">Research and development</td><td>15,000</td><td>13,000</td></tr>
        <tr><td style="padding-left:16px">Selling, general and administrative</td><td>10,000</td><td>9,000</td></tr>
        <tr><td>Total operating expenses</td><td>25,000</td><td>22,000</td></tr>
        <tr><td>Net income</td><td>40,000</td><td>35,000</td></tr>
      </table>
      <table>
        <tr><td>Net cash provided by operating activities</td><td>55,000</td><td>50,000</td></tr>
        <tr><td><ix:nonFraction name="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment" scale="0">12,500</ix:nonFraction> (12,500)</td><td>(12,500)</td><td>(11,000)</td></tr>
        <tr><td>Net cash used in investing activities</td><td>(12,500)</td><td>(11,000)</td></tr>
        <tr><td>Net cash used in financing activities</td><td>(8,000)</td><td>(7,500)</td></tr>
      </table>
    </body></html>
    """
    tables = parse_rendered_sec_html_tables(html_sample)
    assert "income" in tables
    assert "cashflow" in tables

    inc_by_c = {r["concept"]: r for r in tables["income"]}
    # R&D and SG&A under 'Operating expenses:' automatically attach to 'us-gaap:OperatingExpenses' ('Total operating expenses')
    assert inc_by_c["us-gaap:ResearchAndDevelopmentExpense"]["parent_concept"] == "us-gaap:OperatingExpenses"
    assert inc_by_c["us-gaap:SellingGeneralAndAdministrativeExpense"]["parent_concept"] == "us-gaap:OperatingExpenses"

    cf_by_c = {r["concept"]: r for r in tables["cashflow"]}
    # Cash outflows in parentheses `(12,500)` have negative value and display_sign == -1
    assert cf_by_c["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"]["value"] == -12500.0
    assert cf_by_c["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"]["display_sign"] == -1
    assert cf_by_c["us-gaap:NetCashProvidedByUsedInInvestingActivities"]["value"] == -12500.0
    assert cf_by_c["us-gaap:NetCashProvidedByUsedInInvestingActivities"]["display_sign"] == -1

