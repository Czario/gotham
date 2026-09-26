"""Sign-convention subagent: policy families, tools, node, graph wiring."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from filings_agent.signs import policy
from filings_agent.tools.sign_tools import build_sign_tools


# ── policy: families match the LOCAL name (namespace-agnostic) ──────────────


@pytest.mark.parametrize("statement,concept,expected", [
    # income: interest expense (standard AND company-specific tags)
    ("income", "us-gaap:InterestExpense", policy.NEGATIVE),
    ("income", "us-gaap:InterestExpenseDebt", policy.NEGATIVE),
    ("income", "aapl:InterestExpenseNonoperating", policy.NEGATIVE),
    ("income", "us-gaap:InterestAndDebtExpense", policy.NEGATIVE),
    # balance sheet: receivables
    ("balancesheet", "us-gaap:AccountsReceivableNetCurrent", policy.POSITIVE),
    ("balancesheet", "us-gaap:AccountsReceivableGrossCurrent", policy.POSITIVE),
    ("balancesheet", "us-gaap:AccountsNotesAndLoansReceivableNetCurrent", policy.POSITIVE),
    ("balancesheet", "us-gaap:NontradeReceivablesCurrent", policy.POSITIVE),
    # cash flow: share-based compensation add-back
    ("cashflow", "us-gaap:ShareBasedCompensation", policy.POSITIVE),
    ("cashflow", "us-gaap:AllocatedShareBasedCompensationExpense", policy.POSITIVE),
])
def test_families_are_covered(statement, concept, expected):
    assert policy.required_sign(statement, concept) == expected


@pytest.mark.parametrize("statement,concept", [
    # near-misses that must NEVER be touched
    ("income", "us-gaap:InterestIncomeExpenseNet"),       # net interest
    ("income", "us-gaap:InterestIncome"),                 # interest income
    ("balancesheet", "us-gaap:AllowanceForDoubtfulAccountsReceivableCurrent"),
    ("cashflow", "us-gaap:IncreaseDecreaseInAccountsReceivable"),
    ("cashflow", "us-gaap:PaymentsRelatedToTaxWithholdingForShareBasedCompensation"),
    ("cashflow", "us-gaap:ExcessTaxBenefitFromShareBasedCompensationFinancingActivities"),
    # right family, wrong statement
    ("cashflow", "us-gaap:InterestExpense"),
    ("income", "us-gaap:ShareBasedCompensation"),
])
def test_near_misses_are_excluded(statement, concept):
    assert policy.required_sign(statement, concept) is None


def test_classify_reports_only_violations():
    class _Bundle:
        statement_type = "income"
        concepts = [
            {"concept": "us-gaap:InterestExpense", "label": "Interest expense", "value": 1000.0},
            {"concept": "us-gaap:InterestIncome", "label": "Interest income", "value": -5.0},
            {"concept": "us-gaap:Revenues", "label": "Revenue", "value": -1.0},
            {"concept": "us-gaap:InterestExpenseDebt", "value": 0.0},
        ]

    rows = policy.classify(_Bundle())
    assert [r["concept"] for r in rows] == ["us-gaap:InterestExpense", "us-gaap:InterestExpenseDebt"]
    assert [r["concept"] for r in rows if not r["compliant"]] == ["us-gaap:InterestExpense"]
    assert policy.violations([_Bundle()])[0]["required_sign"] == policy.NEGATIVE


# ── set_sign tool: idempotent SET, magnitude preserved ─────────────────────


class _Bundle:
    def __init__(self, statement_type, concepts):
        self.statement_type = statement_type
        self.concepts = concepts
        self.values = [{"concept": c["concept"], "value": c["value"]} for c in concepts]


def _tools(bundle, proposal=None):
    proposal = proposal if proposal is not None else {"fixes": []}
    tools = {t.name: t for t in build_sign_tools([bundle], proposal)}
    return tools, proposal


def test_set_sign_fixes_and_is_idempotent():
    bundle = _Bundle("income", [{"concept": "us-gaap:InterestExpense", "value": 1000.0}])
    tools, proposal = _tools(bundle)

    out = tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "income", "concept": "us-gaap:InterestExpense"}
    )})
    assert "→ -1000" in out or "-1000" in out
    assert bundle.concepts[0]["value"] == -1000.0
    assert bundle.values[0]["value"] == -1000.0                      # flattened view synced
    assert bundle.concepts[0]["sign_fix_source"] == "sign_agent"
    assert proposal["fixes"][0]["old_value"] == 1000.0

    # running again must NOT flip it back
    tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "income", "concept": "us-gaap:InterestExpense"}
    )})
    assert bundle.concepts[0]["value"] == -1000.0
    assert len(proposal["fixes"]) == 1
    assert "already" in tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "income", "concept": "us-gaap:InterestExpense"}
    )})


def test_set_sign_rejects_uncovered_concept_without_explicit_sign():
    bundle = _Bundle("income", [{"concept": "us-gaap:Revenues", "value": 5.0}])
    tools, proposal = _tools(bundle)
    out = tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "income", "concept": "us-gaap:Revenues"}
    )})
    assert out.startswith("Error")
    assert bundle.concepts[0]["value"] == 5.0
    assert proposal["fixes"] == []


def test_set_sign_leaves_zero_alone():
    bundle = _Bundle("cashflow", [{"concept": "us-gaap:ShareBasedCompensation", "value": 0.0}])
    tools, _ = _tools(bundle)
    out = tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "cashflow", "concept": "us-gaap:ShareBasedCompensation"}
    )})
    assert "no sign to set" in out


def test_check_signs_is_a_critic():
    bundle = _Bundle("cashflow", [{"concept": "us-gaap:ShareBasedCompensation", "value": -50.0}])
    tools, _ = _tools(bundle)
    assert "1 sign violation" in tools["check_signs"].invoke({})
    tools["set_sign"].invoke({"row_json": json.dumps(
        {"statement_type": "cashflow", "concept": "us-gaap:ShareBasedCompensation"}
    )})
    assert tools["check_signs"].invoke({}).startswith("OK")


# ── node ────────────────────────────────────────────────────────────────────


def _state(bundles):
    return {"cik": "0000320193", "ticker": "AAPL", "bundles": bundles, "status": "normalized"}


def test_node_fast_paths_when_everything_is_compliant(monkeypatch):
    from filings_agent.nodes import sign_agent

    called = []
    monkeypatch.setattr(sign_agent, "run_agent_loop", lambda *a, **k: called.append(1))
    bundle = _Bundle("cashflow", [{"concept": "us-gaap:ShareBasedCompensation", "value": 100.0}])

    out = sign_agent.make_sign_agent_node()(_state([bundle]))

    assert called == []                                   # no LLM needed
    assert out["status"] == "sign_checked"
    assert out["sign_fixes"] == []
    assert out["sign_fix_unresolved"] == []


def test_node_backstop_enforces_declared_convention_when_agent_unavailable(monkeypatch):
    from filings_agent.nodes import sign_agent

    def _boom(*_a, **_k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(sign_agent, "run_agent_loop", _boom)
    bundle = _Bundle("income", [
        {"concept": "us-gaap:InterestExpense", "value": 1200.0},
        {"concept": "us-gaap:InterestIncome", "value": -3.0},        # excluded
    ])

    out = sign_agent.make_sign_agent_node()(_state([bundle]))

    assert bundle.concepts[0]["value"] == -1200.0                  # fixed
    assert bundle.concepts[1]["value"] == -3.0                     # untouched
    assert out["sign_fix_unresolved"] == []
    assert out["sign_fixes"][0]["source"] == "sign_policy"
    assert out["sign_fixes"][0]["required_sign"] == policy.NEGATIVE


def test_node_uses_the_agent_tools_then_verifies(monkeypatch):
    from filings_agent.nodes import sign_agent

    bundle = _Bundle("cashflow", [{"concept": "us-gaap:ShareBasedCompensation", "value": -7.0}])

    class _StubChat:
        def __init__(self):
            self.step = 0

        def bind_tools(self, tools):
            self.tools = tools
            return self

        def invoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[
                    {"name": "list_sign_candidates", "args": {}, "id": "l1"},
                ])
            if self.step == 2:
                return AIMessage(content="", tool_calls=[{
                    "name": "set_sign",
                    "args": {"row_json": json.dumps({
                        "statement_type": "cashflow",
                        "concept": "us-gaap:ShareBasedCompensation",
                        "reason": "non-cash add-back",
                    })},
                    "id": "s1",
                }])
            if self.step == 3:
                return AIMessage(content="", tool_calls=[{"name": "check_signs", "args": {}, "id": "c1"}])
            return AIMessage(content="", tool_calls=[{
                "name": "finalize_signs",
                "args": {"result_json": json.dumps({"status": "done", "fixes": 1})},
                "id": "f1",
            }])

    out = sign_agent.make_sign_agent_node(chat_llm=_StubChat())(_state([bundle]))

    assert bundle.concepts[0]["value"] == 7.0
    assert out["sign_fixes"][0]["source"] == "sign_agent"
    assert out["sign_fix_unresolved"] == []


def test_node_never_raises_even_on_broken_input(monkeypatch):
    from filings_agent.nodes import sign_agent

    class _Weird:
        statement_type = "income"
        concepts = "not-a-list"          # must not explode the filing

    out = sign_agent.make_sign_agent_node()(_state([_Weird()]))
    assert out["status"] == "sign_checked"


# ── graph wiring: runs on every filing, between normalize and validate ──────


def test_sign_fix_is_wired_into_the_graph():
    from filings_agent.graph import build_filing_graph

    svc = Mock()
    svc.normalize_statement_to_bundle = lambda *a, **k: None
    compiled = build_filing_graph(svc)
    nodes = set(compiled.get_graph().nodes)
    assert "sign_fix" in nodes
