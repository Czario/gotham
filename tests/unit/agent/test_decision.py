"""P8 — the agent's explicit final write decision.

The decision is a hard rail (validation ceiling) plus an explicit choice
(policy by default, an LLM judge when enabled).  ``persist`` executes it
literally; nothing reaches the database without a decision.
"""
from datetime import datetime, timezone

from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.decision import (
    ACTION_SKIP,
    ACTION_WRITE,
    ACTION_WRITE_PARTIAL,
    apply_agent_plan,
    compute_ceiling,
    needs_agent_decision,
    policy_decision,
)
from filings_agent.graph import build_filing_graph
from filings_agent.nodes.decide import make_decide_node
from filings_agent.state import new_state


def item(concept, value, **overrides):
    data = {
        "concept": concept,
        "label": concept,
        "value": value,
        "period": "2024-01-01 00:00:00 to 2024-12-31 00:00:00",
        "level": 1,
        "order": 1,
        "abstract": False,
        "path": "001",
        "order_key": "a",
    }
    data.update(overrides)
    return data


def bundle(statement_type="income", items=None):
    return StatementBundle(
        company_cik="0000320193",
        statement_type=statement_type,
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=list(items if items is not None else [
            item("us-gaap:Revenues", 1_000_000.0, path="001", order_key="a"),
            item("us-gaap:CostOfRevenue", 400_000.0, path="002", order_key="b"),
            item("us-gaap:GrossProfit", 600_000.0, path="003", order_key="c"),
        ]),
        values=[],
        source_items=[],
    )


def math_finding(statement_type="income", concept="us-gaap:GrossProfit"):
    return {
        "type": "math_mismatch",
        "severity": "high",
        "message": "does not reconcile",
        "statement_type": statement_type,
        "concept": concept,
    }


def structural_finding(statement_type="balancesheet"):
    return {
        "type": "duplicate_path_order",
        "severity": "high",
        "message": "duplicate path",
        "statement_type": statement_type,
    }


# ── ceiling ─────────────────────────────────────────────────────────────────


def test_ceiling_blocks_statement_level_and_excludes_concept_level():
    income = bundle("income")
    balance = bundle("balancesheet")
    permitted, blocked, drops = compute_ceiling(
        [income, balance], [math_finding(), structural_finding()]
    )
    assert permitted == ["income"]          # concept-scoped finding keeps income
    assert blocked == ["balancesheet"]      # statement-scoped finding blocks it
    assert drops == {"income": ["us-gaap:GrossProfit"]}


def test_ceiling_filing_level_finding_blocks_everything():
    permitted, blocked, _ = compute_ceiling(
        [bundle("income"), bundle("balancesheet")],
        [{"type": "math_mismatch", "severity": "high", "message": "no statement"}],
    )
    assert permitted == []
    assert set(blocked) == {"income", "balancesheet"}


def test_ceiling_ignores_non_blocking_findings():
    permitted, blocked, drops = compute_ceiling(
        [bundle("income")],
        [{"type": "duplicate_concept", "severity": "medium", "message": "m",
          "statement_type": "income"}],
    )
    assert permitted == ["income"] and blocked == [] and drops == {}


# ── policy ──────────────────────────────────────────────────────────────────


def test_policy_writes_everything_when_validation_passes():
    decision = policy_decision([bundle("income"), bundle("balancesheet")], [])
    assert decision.action == ACTION_WRITE
    assert decision.statements == ["income", "balancesheet"]
    assert decision.decided_by == "policy"


def test_policy_skips_on_blocking_findings_by_default():
    decision = policy_decision([bundle("income")], [math_finding()], strict_accuracy=True)
    assert decision.action == ACTION_SKIP
    assert decision.statements == []
    # The ceiling is recorded so the agent can see what is still writable.
    assert decision.permitted == ["income"]
    assert decision.drop_concepts == {"income": ["us-gaap:GrossProfit"]}


def test_policy_writes_permitted_in_advisory_mode():
    decision = policy_decision(
        [bundle("income"), bundle("balancesheet")],
        [structural_finding()],
        strict_accuracy=False,
    )
    assert decision.action == ACTION_WRITE
    assert decision.statements == ["income"]  # balance sheet blocked by ceiling
    assert decision.blocked == ["balancesheet"]


def test_policy_skips_when_nothing_is_writable():
    decision = policy_decision([], [])
    assert decision.action == ACTION_SKIP


# ── agent plan ──────────────────────────────────────────────────────────────


def test_needs_agent_decision_only_for_the_ambiguous_middle():
    blocked_all = policy_decision([bundle("income")], [structural_finding()])
    ambiguous = policy_decision([bundle("income")], [math_finding()])
    clean = policy_decision([bundle("income")], [])
    assert needs_agent_decision(ambiguous, enabled=True) is True
    assert needs_agent_decision(clean, enabled=True) is False
    assert needs_agent_decision(blocked_all, enabled=True) is True
    assert needs_agent_decision(ambiguous, enabled=False) is False


def test_apply_agent_plan_can_open_a_partial_write_within_the_ceiling():
    policy = policy_decision([bundle("income"), bundle("balancesheet")], [structural_finding()])
    assert policy.action == ACTION_SKIP and policy.permitted == ["income"]

    decision = apply_agent_plan(policy, {
        "action": "write_partial",
        "statements": ["income"],
        "reason": "balance sheet unreliable; income independently sound",
        "confidence": 0.8,
    })
    assert decision.action == ACTION_WRITE_PARTIAL
    assert decision.statements == ["income"]
    assert decision.decided_by == "agent"
    assert decision.confidence == 0.8


def test_apply_agent_plan_cannot_widen_the_ceiling():
    policy = policy_decision([bundle("income"), bundle("balancesheet")], [structural_finding()])
    decision = apply_agent_plan(policy, {
        "action": "write",
        "statements": ["income", "balancesheet"],  # balance sheet is blocked
        "reason": "trust me",
    })
    assert decision.statements == ["income"]  # ceiling wins
    assert "balancesheet" not in decision.statements


def test_apply_agent_plan_cannot_remove_ceiling_concept_exclusions():
    policy = policy_decision([bundle("income")], [math_finding()])
    decision = apply_agent_plan(policy, {
        "action": "write",
        "statements": ["income"],
        "drop_concepts": {},  # tries to clear the proven-bad concept
        "reason": "looks fine to me",
    })
    assert decision.drop_concepts["income"] == ["us-gaap:GrossProfit"]


def test_apply_agent_plan_may_add_exclusions_and_skip():
    policy = policy_decision([bundle("income")], [math_finding()])
    narrowed = apply_agent_plan(policy, {
        "action": "write_partial",
        "statements": ["income"],
        "drop_concepts": {"income": ["us-gaap:Revenues"]},
        "reason": "revenue looks stale too",
    })
    assert set(narrowed.drop_concepts["income"]) == {"us-gaap:GrossProfit", "us-gaap:Revenues"}

    skipped = apply_agent_plan(policy, {"action": "skip", "reason": "extraction suspect"})
    assert skipped.action == ACTION_SKIP
    assert skipped.reason == "extraction suspect"


def test_apply_agent_plan_ignores_garbage():
    policy = policy_decision([bundle("income")], [math_finding()])
    assert apply_agent_plan(policy, None) is policy
    assert apply_agent_plan(policy, "nonsense") is policy


# ── decide node ─────────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self):
        self.decisions = []

    def update_decision(self, cik, accession_number, form_type, decision):
        self.decisions.append(decision)


def _state(bundles, findings):
    return {
        "cik": "0000320193",
        "ticker": "AAPL",
        "form_type": "10-K",
        "accession_number": "acc-1",
        "bundles": bundles,
        "findings": findings,
    }


def test_decide_node_records_the_policy_decision():
    store = FakeStore()
    events = []
    node = make_decide_node(report_store=store, on_event=lambda e, **f: events.append((e, f)))
    out = node(_state([bundle("income")], [math_finding()]))

    assert out["status"] == "decided"
    assert out["write_plan"]["action"] == ACTION_SKIP
    assert out["decision"] == out["write_plan"]
    assert store.decisions and store.decisions[0]["action"] == "skip"
    assert events and events[0][0] == "write_decision"


class StubDecisionChat:
    def __init__(self, payload):
        self.payload = payload

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "finalize_decision",
                "args": {"result_json": self.payload},
                "id": "d1",
            }],
        )


def test_decide_node_uses_the_agent_judge_when_enabled(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_DECISION_ENABLED", True)
    node = make_decide_node(
        chat_llm=StubDecisionChat(
            '{"action":"write_partial","statements":["income"],'
            '"reason":"balance sheet untrustworthy","confidence":0.7}'
        )
    )
    out = node(_state([bundle("income"), bundle("balancesheet")], [structural_finding()]))

    assert out["decision"]["action"] == ACTION_WRITE_PARTIAL
    assert out["decision"]["statements"] == ["income"]
    assert out["decision"]["decided_by"] == "agent"


def test_decide_node_falls_back_to_policy_when_judge_fails(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_DECISION_ENABLED", True)

    class Boom:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("provider down")

    out = make_decide_node(chat_llm=Boom())(
        _state([bundle("income")], [math_finding()])
    )
    assert out["decision"]["decided_by"] == "policy"
    assert out["decision"]["action"] == ACTION_SKIP


def test_decide_node_skips_the_judge_when_no_decision_is_needed(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_DECISION_ENABLED", True)

    class Exploding:
        def bind_tools(self, tools):
            raise AssertionError("judge must not be consulted for a clean filing")

    out = make_decide_node(chat_llm=Exploding())(_state([bundle("income")], []))
    assert out["decision"]["action"] == ACTION_WRITE
    assert out["decision"]["decided_by"] == "policy"


# ── graph integration ───────────────────────────────────────────────────────


class FakeNormService:
    """Hands out one pre-built bundle per statement doc, in order."""

    def __init__(self, bundles, persist_ok=True):
        self._queue = list(bundles)
        self._ok = persist_ok
        self.persisted = []

    def normalize_statement_to_bundle(self, *args, **kwargs):
        return self._queue.pop(0) if self._queue else None

    def persist_statement_bundle(self, bundle, **kwargs):
        self.persisted.append(bundle)
        return self._ok


def _graph_state(bundles):
    state = new_state(
        cik="0000320193", ticker="AAPL", company_name="Apple Inc.",
        form_type="10-K", accession_number="acc-1",
        statement_docs=[{"statement_type": b.statement_type} for b in bundles],
        filing_doc={"form_type": "10-K"},
        company_doc={"cik": "0000320193"},
    )
    state["reporting_period"] = {"end_date": "2024-12-31", "fiscal_year": 2024}
    return state


def test_graph_skips_the_write_when_the_agent_decides_so(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "STRICT_ACCURACY", True)
    svc = FakeNormService([bundle("income")])
    graph = build_filing_graph(svc)

    final = graph.invoke(_graph_state([bundle("income")]))

    # The bundle here is clean, so the policy writes it; assert the decision is
    # explicit and persist executed it.
    assert final["decision"]["action"] == ACTION_WRITE
    assert final["status"] == "saved"
    assert len(svc.persisted) == 1


def test_graph_persists_partial_write_minus_excluded_concept(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "STRICT_ACCURACY", False)
    income = bundle("income", [
        item("us-gaap:Revenues", 1_000_000.0, path="001", order_key="a"),
        item("us-gaap:CostOfRevenue", 400_000.0, path="002", order_key="b"),
        item("us-gaap:GrossProfit", 999_999.0, path="003", order_key="c"),
    ])
    income.values = [{"concept": "us-gaap:GrossProfit", "value": 999_999.0}]
    svc = FakeNormService([income])
    graph = build_filing_graph(svc)

    final = graph.invoke(_graph_state([income]))

    assert final["status"] == "saved"
    assert final["decision"]["action"] == ACTION_WRITE
    assert final["decision"]["drop_concepts"] == {"income": ["us-gaap:GrossProfit"]}
    # The proven-wrong concept never reaches the writer.
    written = svc.persisted[0]
    assert [c["concept"] for c in written.concepts] == [
        "us-gaap:Revenues", "us-gaap:CostOfRevenue",
    ]
    assert [v["concept"] for v in written.values] == []


# ── deferred pre-write hook (reload safety) ─────────────────────────────────


def test_pre_write_hook_runs_only_when_the_agent_writes(monkeypatch):
    """A deferred --reload delete must never run for a skipped filing."""
    import filings_agent.config as config

    monkeypatch.setattr(config, "STRICT_ACCURACY", True)

    # Clean filing -> write -> hook runs.
    called = []
    svc = FakeNormService([bundle("income")])
    state = _graph_state([bundle("income")])
    state["pre_write_hook"] = lambda: called.append("delete")
    final = build_filing_graph(svc).invoke(state)
    assert final["status"] == "saved"
    assert called == ["delete"]

    # Blocked filing -> policy skips -> the hook must NOT run.
    monkeypatch.setattr(config, "STRICT_ACCURACY", True)
    income = bundle("income", [
        item("us-gaap:Revenues", 1_000_000.0, path="001", order_key="a"),
        item("us-gaap:CostOfRevenue", 400_000.0, path="002", order_key="b"),
        item("us-gaap:GrossProfit", 999_999.0, path="003", order_key="c"),
    ])
    called2 = []
    svc2 = FakeNormService([income])
    state2 = _graph_state([income])
    state2["pre_write_hook"] = lambda: called2.append("delete")
    final2 = build_filing_graph(svc2).invoke(state2)

    assert final2["decision"]["action"] == "skip"
    assert final2["status"] == "skipped"
    assert called2 == []           # existing data survives the skip
    assert svc2.persisted == []


def test_pre_write_hook_failure_blocks_the_write():
    svc = FakeNormService([bundle("income")])

    def boom():
        raise RuntimeError("delete failed")

    state = _graph_state([bundle("income")])
    state["pre_write_hook"] = boom
    final = build_filing_graph(svc).invoke(state)

    assert final["status"] == "failed"
    assert "pre-write step failed" in (final["error"] or "")
    assert svc.persisted == []     # nothing written after a failed delete
